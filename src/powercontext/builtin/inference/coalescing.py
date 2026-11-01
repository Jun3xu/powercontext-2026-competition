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

"""Share in-flight embedding work and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage


class CoalescingEmbeddingModel:
    def __init__(self, model, *, max_batch_size=64, coalesce_ms=5.0, cache_size=1024):
        if (
            type(max_batch_size) is not int
            or max_batch_size < 1
            or type(cache_size) is not int
            or cache_size < 0
            or coalesce_ms < 0
        ):
            raise ValueError("invalid embedding coalescing configuration")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._batch_size = max_batch_size
        self._delay = coalesce_ms / 1000
        self._capacity = cache_size
        self._cache = OrderedDict()
        self._pending = {}
        self._queue = []
        self._scheduled = False
        self._tasks = set()

    async def embed(self, texts):
        loop = asyncio.get_running_loop()
        vectors = {}
        waiting = {}
        for text in texts:
            if text in self._cache:
                vectors[text] = self._cache[text]
                self._cache.move_to_end(text)
            elif text not in waiting:
                if text not in self._pending:
                    future = loop.create_future()
                    # A cancelled sole consumer must not produce an unhandled exception.
                    future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                    self._pending[text] = future
                    self._queue.append(text)
                waiting[text] = self._pending[text]
        if self._queue and not self._scheduled:
            self._scheduled = True
            self._spawn(self._dispatch())
        results = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        usage = {}
        for text, (vector, batch_id, batch_usage) in zip(waiting, results, strict=True):
            vectors[text] = vector
            usage[batch_id] = batch_usage

        def total(field):
            values = [getattr(item, field) for item in usage.values()]
            return (
                sum(value for value in values if value is not None)
                if any(value is not None for value in values)
                else None
            )

        return EmbeddingResult(
            vectors=tuple(vectors[text] for text in texts),
            usage=InferenceUsage(
                requests=sum(item.requests for item in usage.values()),
                input_tokens=total("input_tokens"),
                output_tokens=total("output_tokens"),
            ),
        )

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self):
        await asyncio.sleep(self._delay)
        queue, self._queue = self._queue, []
        self._scheduled = False
        for start in range(0, len(queue), self._batch_size):
            self._spawn(self._execute(tuple(queue[start : start + self._batch_size])))

    async def _execute(self, texts):
        batch_id = object()
        try:
            result = await self._model.embed(texts)
            if len(result.vectors) != len(texts):
                raise ValueError("embedding vector count does not match input")  # noqa: TRY301, TRY003
            for text, vector in zip(texts, result.vectors, strict=True):
                if self._capacity:
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                    while len(self._cache) > self._capacity:
                        self._cache.popitem(last=False)
                self._pending.pop(text).set_result((vector, batch_id, result.usage))
        except BaseException as error:
            for text in texts:
                future = self._pending.pop(text, None)
                if future is not None:
                    future.set_exception(error)
