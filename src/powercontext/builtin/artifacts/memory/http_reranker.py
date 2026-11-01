# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP score-service adapter for globally reranking Memory candidates."""

from __future__ import annotations

import math
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Score(_StrictResponseModel):
    rank: int
    score: float

    @field_validator("rank", mode="before")
    @classmethod
    def require_integer_rank(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("score rank must be an integer")  # noqa: TRY003
        return value

    @field_validator("score", mode="before")
    @classmethod
    def require_finite_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError("score must be a finite number")  # noqa: TRY003
        return value


class _ScoreResponse(_StrictResponseModel):
    scores: tuple[_Score, ...]


class HTTPMemoryReranker:
    """Score bounded request batches and make one stable global selection."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self.policy_id = policy_id
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Return the highest service-scored global ranks across every batch."""
        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        request_count = 0

        ranked_candidates = tuple(enumerate(candidates, start=1))
        for offset in range(0, len(ranked_candidates), self._batch_size):
            batch = ranked_candidates[offset : offset + self._batch_size]
            payload: dict[str, Any] = {
                "query": query,
                "candidates": [{"rank": rank, "text": candidate.text} for rank, candidate in batch],
            }
            request_count += 1
            response = await self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
            parsed = _ScoreResponse.model_validate(response.json())

            expected = {rank for rank, _candidate in batch}
            observed = [item.rank for item in parsed.scores]
            if len(observed) != len(expected) or set(observed) != expected:
                raise ValueError("score response must cover every batch rank exactly once")  # noqa: TRY003
            scores.update((item.rank, item.score) for item in parsed.scores)

        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(selected),
            usage=InferenceUsage(requests=request_count),
        )


__all__ = ["HTTPMemoryReranker"]
