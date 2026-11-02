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

"""Lossless Pi v3 branch evidence and durable incremental acquisition."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict


def _text(content):
    if isinstance(content, str):
        return content
    parts = []
    for block in content or ():
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "toolCall":
            arguments = json.dumps(
                block.get("arguments", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            parts.append(f"[tool_call {block.get('name', '')} {block.get('id', '')}] {arguments}")
    return "\n".join(parts)


def _records(value):
    try:
        rows = [json.loads(line) for line in value.content.decode("utf-8-sig").splitlines() if line.strip()]
        header = rows[0]
        if (
            header.get("type") != "session"
            or header.get("version") != 3
            or not isinstance(header.get("id"), str)
            or not header["id"].strip()
        ):
            raise ValueError("invalid session header")  # noqa: TRY003
        nodes = {}
        for row in rows[1:]:
            key = row.get("id")
            if not isinstance(key, str) or not key or key in nodes or "parentId" not in row:
                raise ValueError("invalid event identity")  # noqa: TRY003
            if row["parentId"] is not None and row["parentId"] not in nodes:
                raise ValueError("parent must precede child")  # noqa: TRY003
            nodes[key] = row
        return header["id"], nodes
    except (IndexError, AttributeError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid Pi archive") from error  # noqa: TRY003


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    session_id, nodes = _records(value)
    leaf = value.leaf_id if value.leaf_id is not None else next(reversed(nodes), None)
    if leaf is not None and leaf not in nodes:
        raise ValueError("unknown leaf")  # noqa: TRY003
    chain = []
    while leaf is not None:
        row = nodes[leaf]
        chain.append(row)
        leaf = row["parentId"]
    events = []
    for row in reversed(chain):
        kind = row.get("type")
        if kind == "message":
            message = row.get("message", {})
            role = message.get("role", "")
            body = _text(message.get("content"))
            if role == "toolResult":
                body = (
                    f"[tool_result {message.get('toolName', '')} {message.get('toolCallId', '')}] {body}"
                    if body
                    else ""
                )
            elif role == "bashExecution":
                body = f"$ {message.get('command', '')}\n{message.get('output', '')}"
        elif kind in {"compaction", "branch_summary"}:
            role, body = kind, row.get("summary", "")
        elif kind == "custom_message":
            role, body = "custom", _text(row.get("content"))
        else:
            continue
        if body:
            events.append(
                PiEventInput(
                    session_id=session_id,
                    event_id=row["id"],
                    parent_id=row["parentId"],
                    event_type=kind,
                    role=role,
                    content=f"{role}: {body}",
                    record=row,
                )
            )
    return tuple(events)


class PiEventSource(Source):
    event: PiEventInput


class PiEventAdapter:
    input_class = PiEventInput
    source_class = PiEventSource
    name = "pi-event"

    async def resolve(self, value, /):
        identity = hashlib.sha256(
            json.dumps([value.session_id, value.event_id], ensure_ascii=False).encode()
        ).hexdigest()
        return PiEventSource(name=f"pi:{identity}", materialization=SourceMaterialization.CAPTURED, event=value)

    async def read(self, source, /):
        return source.event


class PiEventProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source, /):
        event = source.event
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=event.content,
            metadata=event.model_dump(include={"session_id", "event_id", "parent_id", "event_type", "role"}),
        )


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(PiEventAdapter(), projections=(PiEventProjection(),))


class PiSessionConnector:
    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path, leaf_id=None):
        self.path, self.leaf_id = Path(path), leaf_id

    async def run(self, session, /):
        value = PiSessionInput(content=self.path.read_bytes(), leaf_id=self.leaf_id)
        events = parse_pi_session(value)
        session_id, nodes = _records(value)
        previous = dict(session.checkpoint or {})
        fingerprints = {
            json.dumps([session_id, key]): hashlib.sha256(
                json.dumps(row, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            for key, row in nodes.items()
        }
        for key, digest in previous.items():
            if json.loads(key)[0] == session_id and fingerprints.get(key) != digest:
                raise ValueError("imported event was modified or removed")  # noqa: TRY003
        for event in events:
            key = json.dumps([event.session_id, event.event_id])
            if key not in previous:
                await session.submit(event.event_id, "pi-event", event)
                previous[key] = fingerprints[key]
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=previous)
