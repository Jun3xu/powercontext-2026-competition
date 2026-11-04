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

"""Complete physical PDF pages with exact revision references."""

from __future__ import annotations

import hashlib
import io

from pydantic import BaseModel

from powercontext.builtin.runtime import SearchMemoryRequest
from powercontext.sources._pdf_retrieval import pdf_retrieval_queries, rank_pdf_records
from powercontext.sources._pdf_text import extract_pdf_page_text
from powercontext.sources._pdf_validation import validate_pdf_text_streams
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PDFDocumentInput(BaseModel):
    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    from pypdf import PdfReader

    if not value.document_id.strip():
        raise ValueError("blank document identity")  # noqa: TRY003
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("password required")  # noqa: TRY301, TRY003
        validate_pdf_text_streams(reader)
        digest = hashlib.sha256(value.content).hexdigest()
        return tuple(
            PDFPageInput(
                document_id=value.document_id, document_sha256=digest, page_number=i, text=extract_pdf_page_text(page)
            )
            for i, page in enumerate(reader.pages, 1)
        )
    except Exception as error:
        raise ValueError("cannot completely parse PDF") from error  # noqa: TRY003


class PDFPageSource(Source):
    page: PDFPageInput


class PDFPageAdapter:
    input_class = PDFPageInput
    source_class = PDFPageSource
    name = "pdf-page"

    async def resolve(self, value, /):
        digest = hashlib.sha256(value.document_id.encode()).hexdigest()
        return PDFPageSource(
            name=f"pdf:{digest}:{value.document_sha256}:{value.page_number}",
            materialization=SourceMaterialization.CAPTURED,
            page=value,
        )

    async def read(self, source, /):
        return source.page


class PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source, /):
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata=page.model_dump(exclude={"text"}),
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(PDFPageAdapter(), projections=(PDFPageProjection(),))


async def search_pdf_pages(memory, question: str, limit: int = 10):
    """Retrieve Runtime-indexed PDF candidates and return their exact citations."""
    if limit <= 0:
        return ()
    records = (await memory.list()).entries
    if not records or not question.strip():
        return ()
    candidate_limit = max(80, len(records), limit)
    candidate_ids = set()
    for query in pdf_retrieval_queries(records, question):
        result = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=candidate_limit))
        candidate_ids.update(hit.entry_id for hit in result.hits)
    candidates = tuple(record for record in records if record.entry.entry_id in candidate_ids)
    ranked = rank_pdf_records(candidates, question, limit)
    seen, citations = set(), []
    for record in ranked:
        identity = tuple((source.source_type, source.source_id) for source in record.entry.sources)
        if identity not in seen:
            seen.add(identity)
            citations.append(record.citation)
    return tuple(citations)
