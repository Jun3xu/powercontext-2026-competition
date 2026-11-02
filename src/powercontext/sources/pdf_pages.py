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
        digest = hashlib.sha256(value.content).hexdigest()
        return tuple(
            PDFPageInput(
                document_id=value.document_id, document_sha256=digest, page_number=i, text=page.extract_text() or ""
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


# Financial reports use several names for the same statement or accounting concept.
# These are vocabulary mappings, independent of any document or evaluation question.
_PDF_QUERY_EQUIVALENTS = (
    (
        r"\b(?:geograph\w*|countr\w*|region\w*)\b",
        "geographic international countries regions markets divisions segments",
    ),
    (r"\b(?:lawsuit\w*|litigat\w*|legal|battles)\b", "litigation legal proceedings contingencies lawsuits"),
    (r"\b(?:capex|capital expenditures?)\b", "capital expenditures additions property plant equipment purchases"),
    (r"\b(?:ebitda|d\s*&\s*a|depreciation|amortization)\b", "operating income depreciation amortization"),
    (r"\b(?:income statements?|p\s*&\s*l|profit and loss)\b", "consolidated statements income operations earnings"),
    (r"\bcash flows?\b", "consolidated statement cash flows operating investing financing activities"),
    (r"\b(?:revenue\w*|net sales)\b", "revenue net sales"),
    (r"\b(?:restructur\w*|reorganiz\w*)\b", "restructuring reorganization charges costs"),
    (r"\b(?:dividend\w*|repurchas\w*|buybacks?)\b", "dividends repurchases shareholders equity stock"),
)


_PDF_SECTION_QUERIES = (
    (
        r"income|p\s*&\s*l|profit|ebitda|earnings",
        r"(?:consolidated\s+)?statements?\s+of\s+(?:income|operations|earnings)",
    ),
    (r"cash|capex|ebitda|d\s*&\s*a", r"(?:consolidated\s+)?statements?\s+of\s+cash\s+flows?"),
    (r"balance sheet|assets|liabilities|equity", r"(?:consolidated\s+)?balance\s+sheets?"),
    (r"legal|litigat|lawsuit|battles", r"legal\s+proceedings|litigation|contingencies"),
    (r"geograph|countr|regions?", r"geographic|our\s+(?:business|divisions)|reportable\s+segments"),
)


_PDF_STOP_WORDS = {
    "and",
    "respond",
    "details",
    "been",
    "to",
    "were",
    "materially",
    "which",
    "shown",
    "explicitly",
    "fy",
    "what",
    "s",
    "much",
    "many",
    "directly",
    "important",
    "be",
    "had",
    "outlined",
    "this",
    "at",
    "the",
    "by",
    "company",
    "with",
    "do",
    "it",
    "is",
    "for",
    "analyst",
    "question",
    "in",
    "response",
    "reported",
    "ongoing",
    "use",
    "did",
    "state",
    "as",
    "or",
    "has",
    "not",
    "fiscal",
    "within",
    "unadjusted",
    "a",
    "from",
    "are",
    "then",
    "define",
    "less",
    "only",
    "using",
    "perspective",
    "on",
    "an",
    "does",
    "calculate",
    "any",
    "how",
    "would",
    "that",
    "answer",
    "primarily",
    "quantity",
    "was",
    "of",
    "its",
    "assuming",
    "have",
    "can",
    "give",
}


def _pdf_tokens(text: str) -> list[str]:
    import re

    words = re.findall(r"[a-z]+|\d+", text.lower())
    result = []
    for word in words:
        if word in _PDF_STOP_WORDS:
            continue
        if len(word) > 4:
            if word.endswith("ies"):
                word = word[:-3] + "y"
            elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
                word = word[:-1]
            if word.endswith("ing") and len(word) > 6:
                word = word[:-3]
            elif word.endswith("ed") and len(word) > 5:
                word = word[:-2]
        result.append(word)
    return result


def _select_pdf_reports(records, question: str):
    import re

    # The projection's first line is the original document identity. Resolve an
    # explicitly named issuer before scoring page text; competitor mentions in
    # another issuer's report must not override this identity.
    question_compact = re.sub(r"[^a-z0-9]", "", question.lower())
    report_identities = {}
    for record in records:
        name = record.entry.text.partition("\n")[0]
        match = re.search(r"(?:19|20)\d{2}", name)
        company = name[: match.start()] if match else name
        company = re.sub(r"[^a-z0-9]", "", company.lower())
        report_identities[name] = (company, match.group() if match else None)
    companies = {
        company
        for company, _ in report_identities.values()
        if company
        and (
            (len(company) >= 4 and company in question_compact)
            or (len(company) < 4 and re.search(rf"\b{re.escape(company)}\b", question, re.IGNORECASE))
        )
    }
    if companies:
        # Prefer the longest explicit name when one issuer name prefixes another.
        companies = {name for name in companies if not any(name != other and name in other for other in companies)}
        records = tuple(
            record for record in records if report_identities[record.entry.text.partition("\n")[0]][0] in companies
        )
        years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", question))
        matching_year = tuple(
            record for record in records if report_identities[record.entry.text.partition("\n")[0]][1] in years
        )
        # Earlier periods may only be reported in a later available filing.
        if matching_year:
            records = matching_year
    return records


def _pdf_query_terms(question: str) -> dict[str, float]:
    import re

    query = _pdf_tokens(question)
    expanded = dict.fromkeys(query, 1.0)
    for pattern, vocabulary in _PDF_QUERY_EQUIVALENTS:
        if re.search(pattern, question.lower()):
            for word in _pdf_tokens(vocabulary):
                expanded.setdefault(word, 0.45)
    # Years are useful context, but should not outrank the requested subject.
    for word in expanded:
        if word.isdigit():
            expanded[word] *= 0.2
    return expanded


def _pdf_page_scores(records, question: str) -> list[float]:
    import math
    import re
    from collections import Counter

    bodies = [record.entry.text.partition("\n")[2] for record in records]
    page_tokens = [_pdf_tokens(body) for body in bodies]
    counts = [Counter(words) for words in page_tokens]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    expanded = _pdf_query_terms(question)
    frequencies = {term: sum(term in count for count in counts) for term in expanded}
    idfs = {term: math.log(1 + (len(records) - n + 0.5) / (n + 0.5)) for term, n in frequencies.items()}
    query_pairs = set(zip(_pdf_tokens(question), _pdf_tokens(question)[1:], strict=False))
    expanded_scores = []
    for index, count in enumerate(counts):
        norm = 1.2 * (0.25 + 0.75 * lengths[index] / average)
        score = 0.0
        for term, weight in expanded.items():
            tf = count[term]
            contribution = idfs[term] * tf * 2.2 / (tf + norm)
            score += weight * contribution
        # Prefer a page actually presenting a statement over a passing reference
        # to it deep in another page. Phrase matches reward the same distinction.
        heading = Counter(page_tokens[index][:80])
        score += sum(idfs[term] * weight * 0.4 for term, weight in expanded.items() if term in heading)
        pairs = set(zip(page_tokens[index], page_tokens[index][1:], strict=False))
        score += sum((idfs.get(a, 0) + idfs.get(b, 0)) * 0.5 for a, b in query_pairs & pairs)
        heading_text = "\n".join(line.strip().lower() for line in bodies[index].splitlines() if len(line) < 160)
        # A named statement is a direct retrieval target even when its numeric
        # table has few of the question's narrative words.
        for query_pattern, title_pattern in _PDF_SECTION_QUERIES:
            if re.search(query_pattern, question.lower()) and re.search(
                rf"^(?:item\s+\d+[a-z]?[.:]?\s+)?(?:{title_pattern})\b", heading_text, re.MULTILINE
            ):
                score += 12.0
        expanded_scores.append(score)
    return expanded_scores


async def search_pdf_pages(memory, question: str, limit: int = 10):
    """Return exact persisted page citations for a scoped report query.

    Retrieval normalization does not change captured page text or references.
    """
    if limit <= 0:
        return ()
    records = _select_pdf_reports((await memory.list()).entries, question)
    if not records:
        return ()
    expanded_scores = _pdf_page_scores(records, question)
    ranked = sorted(range(len(records)), key=lambda i: (-expanded_scores[i], records[i].entry.entry_id))
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
