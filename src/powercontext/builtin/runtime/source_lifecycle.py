"""Persistent evidence dependency graph and native availability propagation."""
from __future__ import annotations

import asyncio
import json
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation
from powercontext.builtin.persistence.artifact_governance import ArtifactGovernanceRepository, ArtifactLifecycleState
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE as TABLE
from powercontext.sources import SourceRef


class SourceLifecycleError(ValueError):
    pass


class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    reason: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SourceLifecycleApplication:
    def __init__(self, runtime):
        self.runtime = runtime
        self.locks = {}

    def for_scope(self, scope_id):
        return SourceLifecycleService(self.runtime, scope_id, self.locks.setdefault(scope_id, asyncio.Lock()))


class SourceLifecycleService:
    def __init__(self, runtime, scope, lock):
        self.runtime, self.scope, self.lock = runtime, scope, lock
        self.database = runtime._provider.database

    async def _load(self):
        async with self.database.connection() as connection:
            rows = (await connection.execute(select(TABLE.c.node_id, TABLE.c.payload).where(TABLE.c.scope_id == self.scope))).all()
        return {key: json.loads(payload) for key, payload in rows}

    async def _save(self, key, value, *, new=False):
        async with self.database.transaction() as connection:
            payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if new:
                await connection.execute(insert(TABLE).values(scope_id=self.scope, node_id=key, payload=payload))
            else:
                await connection.execute(update(TABLE).where(TABLE.c.scope_id == self.scope, TABLE.c.node_id == key).values(payload=payload))

    async def register_source(self, node_id, source_ref, revision=1):
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("invalid revision")
        await self.runtime.records.for_scope(self.scope).get_source(source_ref.source_type, source_ref.source_id)
        await self._register(node_id, "source", source_ref, (), "all", revision)

    async def register_memory(self, node_id, citation, *, parents, policy="all"):
        from powercontext.builtin.runtime.models import GetMemoryEntryRequest
        await self.runtime.memory.for_scope(self.scope).get(GetMemoryEntryRequest(citation=citation))
        await self._register(node_id, "memory", citation, parents, policy, None)

    async def register_topic(self, node_id, artifact_ref, *, parents, policy="all"):
        if artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("not a topic")
        await self.runtime.records.for_scope(self.scope).get_artifact_revision(artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision)
        await self._register(node_id, "topic", artifact_ref, parents, policy, None)

    async def _register(self, key, kind, reference, parents, policy, revision):
        if not isinstance(key, str) or not key.strip() or len(key) > 256 or policy not in {"all", "any"}:
            raise SourceLifecycleError("invalid node")
        async with self.lock:
            nodes = await self._load()
            if kind != "source" and (not parents or len(set(parents)) != len(parents) or any(parent not in nodes or parent == key for parent in parents)):
                raise SourceLifecycleError("invalid parents")
            binding = dict(kind=kind, reference=reference.model_dump(mode="json"), parents=list(parents), policy=policy, revision=revision)
            if key in nodes:
                if any(nodes[key][field] != value for field, value in binding.items()):
                    raise SourceLifecycleError("conflicting node binding")
            else:
                nodes[key] = {**binding, "available": True, "event": None}
                await self._save(key, nodes[key], new=True)
            await self._propagate(nodes)

    async def apply(self, event):
        async with self.lock:
            nodes = await self._load()
            node = nodes.get(event.node_id)
            if node is None or node["kind"] != "source" or node["revision"] != event.revision:
                raise SourceLifecycleError("unknown source or revision")
            previous = node["event"]
            data = event.model_dump(mode="json")
            if previous is not None and (previous["sequence"] > event.sequence or previous["sequence"] == event.sequence and previous != data):
                raise SourceLifecycleError("event sequence conflict")
            node.update(event=data, available=event.available)
            # Persist intent first: retries reconcile any interrupted native writes.
            await self._save(event.node_id, node)
            return await self._propagate(nodes)

    async def _propagate(self, nodes):
        active = set()
        pending = dict(nodes)
        while pending:
            ready = [key for key, node in pending.items() if all(parent not in pending for parent in node["parents"])]
            if not ready:
                raise SourceLifecycleError("cyclic graph")
            for key in ready:
                node = pending.pop(key)
                if node["kind"] == "source":
                    available = node["available"]
                else:
                    supports = [parent in active for parent in node["parents"]]
                    available = all(supports) if node["policy"] == "all" else any(supports)
                    await self._native(node, available)
                    if node["available"] != available:
                        node["available"] = available
                        await self._save(key, node)
                if available:
                    active.add(key)
        return sorted(active)

    async def _native(self, node, available):
        if node["kind"] == "memory":
            reference = MemoryCitation.model_validate(node["reference"])
            async with self.runtime._context(self.scope) as context:
                service = context.artifacts.memory
                current = await service.latest(await service.revision(reference.memory_ref))
                entries = await service.entries(current)
                entry = next(entry for entry in entries if entry.entry_id == reference.entry_id)
                record = next(row for row in (await self.runtime.memory.for_scope(self.scope).list(include_inactive=True)).entries if row.entry.entry_id == reference.entry_id)
                if (record.state == "active") != available:
                    operation = service.reactivate if available else service.forget
                    await operation(current, entries=(entry,), reason="source lifecycle")
        else:
            reference = ArtifactRef.model_validate(node["reference"])
            async with self.database.transaction() as connection:
                repository = ArtifactGovernanceRepository()
                current = await repository.get(connection, self.scope, reference.family, reference.artifact_id)
                state = ArtifactLifecycleState.ACTIVE if available else ArtifactLifecycleState.DEPRECATED
                if current.lifecycle_state != state:
                    await repository.transition(connection, self.scope, reference.family, reference.artifact_id, current.governance_generation, state, None)

    async def resolve(self, node_id, *, purpose):
        nodes = await self._load()
        if node_id not in nodes or purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown node or purpose")
        node = nodes[node_id]
        if purpose == "generation" and not node["available"]:
            raise SourceLifecycleError("evidence is unavailable")
        cls = {"memory": MemoryCitation, "topic": ArtifactRef, "source": SourceRef}[node["kind"]]
        return cls.model_validate(node["reference"])
