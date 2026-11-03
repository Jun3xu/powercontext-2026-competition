"""Whole captured PDF pages survive Memory persistence without weakening ordinary limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    ReviseMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import PDF_PAGE_SOURCE_DEFINITION, PDFPageInput
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY


class _PagePipeline:
    def __init__(self, *, alter: bool = False, kind: str = "source-evidence"):
        self.alter = alter
        self.kind = kind

    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind=self.kind,
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"] + (" altered" if self.alter else ""),
                sources=(source,),
            )
            for source in request.sources
        )


async def _seed(path: Path, text: str = "Long complete page with financial figures 12345. " * 400):
    page = PDFPageInput(document_id="control", document_sha256="a" * 64, page_number=1, text=text)
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = await registry.resolve(page)
    observation = project_source_for_transport(registry, source)
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{path / 'memory.db'}"))
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Pages", summary="Complete pages", idempotency_key="pages")
        )
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
    return config, scope.scope_id, observation


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source-evidence", "fact"])
async def test_complete_long_pdf_page_survives_restart_and_rebuild(tmp_path, kind):
    config, scope_id, observation = await _seed(tmp_path)
    expected = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"].strip()
    assert len(expected.encode()) > 8192
    async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline(kind=kind)) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        await memory.flush()
        entries = (await memory.list()).entries
        assert len(entries) == 1
        citation = entries[0].citation
        assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == expected
    async with open_builtin_contexts(config) as contexts:
        context = await contexts.get(scope_id)
        await context.artifacts.memory.rebuild_projections()
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        assert (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text == expected
        assert (await memory.list()).entries[0].entry.text == expected
        with pytest.raises(ValueError, match="captured page projection"):
            await memory.revise(
                ReviseMemoryEntryRequest(citation=citation, kind="source-evidence", text=expected + " altered")
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "fact"])
async def test_ordinary_long_memory_still_rejected(tmp_path, kind):
    config, scope_id, _ = await _seed(tmp_path)
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        with pytest.raises(ValueError, match="8192 UTF-8 bytes"):
            await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind=kind, text="x" * 8193),)))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source-evidence", "fact"])
async def test_captured_reference_does_not_allow_altered_long_text(tmp_path, kind):
    config, scope_id, _ = await _seed(tmp_path)
    async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline(alter=True, kind=kind)) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        with pytest.raises(ValueError, match="captured page projection"):
            await memory.flush()
        assert not (await memory.list()).entries


@pytest.mark.asyncio
async def test_complete_captured_page_still_has_a_bounded_size(tmp_path):
    config, scope_id, _ = await _seed(tmp_path, text="x" * 1_048_576)
    async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
        memory = runtime.memory.for_scope(scope_id)
        with pytest.raises(ValueError, match="1048576 UTF-8 bytes"):
            await memory.flush()
        assert not (await memory.list()).entries
