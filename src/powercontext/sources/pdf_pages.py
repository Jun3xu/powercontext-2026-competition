"""Complete physical PDF pages with exact revision references."""
from __future__ import annotations

import hashlib
import io

from pydantic import BaseModel
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
        raise ValueError("blank document identity")
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("password required")
        digest = hashlib.sha256(value.content).hexdigest()
        return tuple(PDFPageInput(document_id=value.document_id, document_sha256=digest, page_number=i, text=page.extract_text() or "") for i, page in enumerate(reader.pages, 1))
    except Exception as error:
        raise ValueError("cannot completely parse PDF") from error


class PDFPageSource(Source):
    page: PDFPageInput


class PDFPageAdapter:
    input_class = PDFPageInput
    source_class = PDFPageSource
    name = "pdf-page"

    async def resolve(self, value, /):
        digest = hashlib.sha256(value.document_id.encode()).hexdigest()
        return PDFPageSource(name=f"pdf:{digest}:{value.document_sha256}:{value.page_number}", materialization=SourceMaterialization.CAPTURED, page=value)

    async def read(self, source, /):
        return source.page


class PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source, /):
        page = source.page
        return TextEvidence(source_type="pdf-page", source_id=source.name, content=f"{page.document_id}\n{page.text}", metadata=page.model_dump(exclude={"text"}))


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(PDFPageAdapter(), projections=(PDFPageProjection(),))


async def search_pdf_pages(memory, question: str, limit: int = 10):
    import math
    import re
    from collections import Counter

    if limit <= 0:
        return ()
    # Finance questions often contain boilerplate absent from the evidence page.
    # Score the public scoped corpus with BM25 instead of requiring every word.
    records = (await memory.list()).entries
    if not records:
        return ()
    stop = set("a an the is are was were be been of in on at to for from by with and or as what how much many does did do would have has had which this that its it company's company".split())
    def tokens(text):
        return [word for word in re.findall(r"[a-z]+|\d+", text.lower()) if word not in stop]
    counts = [Counter(tokens(record.entry.text)) for record in records]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(1, len(lengths)) or 1
    query = Counter(tokens(question))
    frequencies = {term: sum(term in count for count in counts) for term in query}
    scores = []
    for index, count in enumerate(counts):
        score = 0.0
        for term, weight in query.items():
            frequency = count[term]
            idf = math.log(1 + (len(records) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += weight * idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * lengths[index] / average))
        scores.append(score)
    ranked = sorted(range(len(records)), key=lambda i: (-scores[i], records[i].entry.entry_id))
    seen, citations = set(), []
    for index in ranked:
        record = records[index]
        identity = tuple((source.source_type, source.source_id) for source in record.entry.sources)
        if identity in seen or not identity:
            continue
        seen.add(identity)
        citations.append(record.citation)
        if len(citations) == limit:
            break
    return tuple(citations)
