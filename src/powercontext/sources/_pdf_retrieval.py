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

"""Offline ranking of persisted PDF pages with complementary evidence coverage."""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from powercontext.builtin.runtime import MemoryEntryRecord

# Financial reports use several names for the same statement or accounting concept.
# These are vocabulary mappings, independent of any document or evaluation question.
_PDF_QUERY_EQUIVALENTS = (
    (
        r"\b(?:geograph\w*|countr\w*|region\w*)\b|\bwhere\b.*\b(?:operat\w*|business)",
        "geographic international countries regions markets divisions segments",
    ),
    (r"\b(?:lawsuit\w*|litigat\w*|legal|battles)\b", "litigation legal proceedings contingencies lawsuits"),
    (
        r"\b(?:capex|capital expenditures?|capital spending)\b",
        "capital expenditures additions property plant equipment purchases",
    ),
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
    (r"legal|litigat\w*|lawsuits?|battles?", r"legal\s+proceedings|litigation|contingencies"),
    (r"geograph\w*|countr\w*|regions?", r"geographic|our\s+(?:business|divisions)|reportable\s+segments"),
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


_GENERIC_DOCUMENT_NAMES = _PDF_STOP_WORDS | {
    "cash",
    "income",
    "report",
    "annual",
    "quarterly",
    "filing",
    "document",
    "financial",
    "statement",
    "statements",
    "revenue",
    "sales",
    "balance",
    "assets",
    "liabilities",
    "control",
    "test",
    "sample",
    "notes",
    "earnings",
    "operating",
    "flows",
    "flow",
    "net",
    "activities",
    "consolidated",
}
_SECTION_TITLES = {
    "business": r"business(?:\s+overview)?|general\s+business\s+(?:information|description)",
    "geographic": r"geographic(?:al)?(?:\s+(?:information|areas|segments))?|our\s+divisions|reportable\s+segments",
    "income": r"(?:consolidated\s+)?statements?\s+of\s+(?:consolidated\s+)?(?:income|operations|earnings|profit(?:\s+and\s+loss)?)",
    "cashflow": r"(?:consolidated\s+)?statements?\s+of\s+(?:consolidated\s+)?cash\s+flows?",
    "balance": r"(?:consolidated\s+)?balance\s+sheets?|(?:consolidated\s+)?statements?\s+of\s+financial\s+position",
    "equity": r"(?:consolidated\s+)?statements?\s+of\s+(?:changes\s+in\s+)?(?:(?:shareholders|stockholders)['\u2019]?\s+)?equity",
}


@dataclass(frozen=True)
class _Page:
    record: MemoryEntryRecord
    document: str
    body: str
    words: tuple[str, ...]
    headings: frozenset[str]


def _words(text: str) -> tuple[str, ...]:
    text = re.sub(r"([a-z])-\s*\n\s*([a-z])", r"\1\2", text.lower())
    return tuple(word for word in re.findall(r"[a-z]+|\d+", text) if word not in _PDF_STOP_WORDS)


def _stem_map(vocabulary: set[str]) -> dict[str, str]:
    if not vocabulary:
        return {}
    words = sorted(vocabulary)
    # One input word per row makes the mapping independent of token offsets and
    # SQLite's maximum token length. This private index holds no citations.
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE VIRTUAL TABLE words USING fts5(body, tokenize='porter unicode61')")
        database.executemany("INSERT INTO words(body) VALUES (?)", ((word,) for word in words))
        database.execute("CREATE VIRTUAL TABLE stems USING fts5vocab(words, 'instance')")
        return {words[row - 1]: term for term, row in database.execute("SELECT term, doc FROM stems")}


def _heading_match(body: str, pattern: str) -> bool:
    # Exact short heading lines exclude prose references and TOC page numbers.
    lines = "\n".join(line.strip() for line in body.splitlines() if len(line) < 160)
    return (
        re.search(
            rf"^(?:item\s+\d+[a-z]?[.:]?\s+)?(?:{pattern})"
            r"(?:\s+(?:and\s+)?comprehensive\s+(?:income|loss))?(?:\s*\(loss\))?"
            r"(?:\s*\(continued\))?[.:]?\s*$",
            lines,
            re.IGNORECASE | re.MULTILINE,
        )
        is not None
    )


def _pages(records: Sequence[MemoryEntryRecord]) -> tuple[_Page, ...]:
    pages = []
    seen = set()
    for record in records:
        if not any(source.source_type == "pdf-page" for source in record.entry.sources):
            continue
        document, _, body = record.entry.text.partition("\n")
        if not body.strip():
            continue
        identity = tuple(
            sorted(
                (source.source_type, source.source_id)
                for source in record.entry.sources
                if source.source_type == "pdf-page"
            )
        )
        if identity in seen:
            continue
        seen.add(identity)
        pages.append(
            _Page(
                record,
                document,
                body,
                _words(body),
                frozenset(kind for kind, pattern in _SECTION_TITLES.items() if _heading_match(body, pattern)),
            )
        )
    return tuple(pages)


def _query_weights(question: str) -> dict[str, float]:
    weights = dict.fromkeys(_words(question), 1.0)
    for pattern, expansion in _PDF_QUERY_EQUIVALENTS:
        if re.search(pattern, question, re.IGNORECASE):
            for word in _words(expansion):
                weights.setdefault(word, 0.45)
    return {word: weight * (0.2 if word.isdigit() else 1.0) for word, weight in weights.items()}


def _issuer_match(document: str, question: str) -> tuple[bool, bool]:
    year = re.search(r"(?:19|20)\d{2}", document)
    issuer = document[: year.start()] if year else document
    pieces = re.findall(r"[a-z0-9]+", issuer.lower())
    issuer = "".join(pieces)
    if not issuer or all(piece in _GENERIC_DOCUMENT_NAMES for piece in pieces):
        return False, False
    positions = tuple(re.finditer(r"[a-z0-9]", question, re.IGNORECASE))
    compact = "".join(position.group().lower() for position in positions)
    mentions = tuple(re.finditer(re.escape(issuer), compact))
    named = any(
        (positions[mention.start()].start() == 0 or not question[positions[mention.start()].start() - 1].isalnum())
        and (
            positions[mention.end() - 1].end() == len(question)
            or not question[positions[mention.end() - 1].end()].isalnum()
        )
        for mention in mentions
    )
    years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", question))
    return named, bool(named and year and year.group() in years)


def _scores(pages: tuple[_Page, ...], question: str, stems: dict[str, str], query: dict[str, float]):
    counts = [Counter(stems[word] for word in page.words) for page in pages]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    frequencies = {term: sum(term in count for count in counts) for term in query}
    idfs = {term: math.log(1 + (len(pages) - n + 0.5) / (n + 0.5)) for term, n in frequencies.items()}
    scores = []
    for index, count in enumerate(counts):
        norm = 1.2 * (0.25 + 0.75 * lengths[index] / average)
        score = sum(weight * idfs[term] * count[term] * 2.2 / (count[term] + norm) for term, weight in query.items())
        for query_pattern, title_pattern in _PDF_SECTION_QUERIES:
            if re.search(rf"\b(?:{query_pattern})\b", question, re.IGNORECASE) and _heading_match(
                pages[index].body, title_pattern
            ):
                score += 12.0
        scores.append(score)
    priors = [_issuer_match(page.document, question) for page in pages]
    if any(named for named, _ in priors):
        maximum = max(scores) or 1
        exact_year = any(year for _, year in priors)
        for index, (named, year) in enumerate(priors):
            if named and scores[index] > 0:
                scores[index] = (
                    scores[index] * 0.4 + maximum * 0.2
                    if exact_year and not year
                    else scores[index] + maximum * (1.2 if year else 0.8)
                )
            elif not named:
                scores[index] *= 0.4
    return scores, counts


def _requested_sections(question: str) -> tuple[str, ...]:
    rules = (
        ("business", r"geograph\w*|countries|regions?|where.*(?:operat\w*|business)"),
        ("geographic", r"geograph\w*|countries|regions?|where.*(?:operat\w*|business)"),
        ("income", r"income|profits?|earnings?|ebitda|p\s*&\s*l"),
        (
            "cashflow",
            r"cash(?:flows?)?|capex|capital\s+(?:spending|expenditures?)|ebitda|depreciation|amortization|d\s*&\s*a",
        ),
        ("balance", r"balance|assets?|liabilities|working\s+capital|quick\s+ratio|current\s+ratio"),
        ("equity", r"equity|shareholders?|stockholders?|dividends?|repurchases?|buybacks?"),
    )
    return tuple(kind for kind, pattern in rules if re.search(rf"\b(?:{pattern})\b", question, re.IGNORECASE))


def _section_candidates(pages, question):
    priors = [_issuer_match(page.document, question) for page in pages]
    named = {i for i, (issuer, _) in enumerate(priors) if issuer}
    exact_year = {i for i in named if priors[i][1]}
    return exact_year or named or set(range(len(pages)))


def _cover_evidence(pages, question, scores, counts, query, limit):
    order = sorted(range(len(pages)), key=lambda i: (-scores[i], pages[i].record.entry.entry_id))
    selected = [order[0]]
    eligible = _section_candidates(pages, question)
    # Reserve at most one best page for each requested complementary statement.
    # Remaining capacity is filled with relevant, less redundant page evidence.
    for kind in _requested_sections(question):
        if any(kind in pages[i].headings for i in selected):
            continue
        candidate = next((i for i in order if i in eligible and kind in pages[i].headings and i not in selected), None)
        if candidate is not None and len(selected) < limit:
            selected.append(candidate)
    terms = [frozenset(term for term in count if not term.isdigit()) for count in counts]
    similarity = [0.0] * len(pages)
    covered: set[str] = set()
    for index in selected:
        covered.update(counts[index].keys() & query.keys())
        for i, tokens in enumerate(terms):
            similarity[i] = max(similarity[i], len(tokens & terms[index]) / (len(tokens | terms[index]) or 1))
    remaining = set(order) - set(selected)
    maximum = max(scores) or 1
    total = sum(query.values()) or 1
    while remaining and len(selected) < limit:

        def utility(i):
            gain = sum(query[term] for term in counts[i].keys() & query.keys() - covered) / total
            return scores[i] * (1 - 0.85 * similarity[i]) + maximum * 0.25 * gain

        index = min(remaining, key=lambda i: (-utility(i), pages[i].record.entry.entry_id))
        selected.append(index)
        remaining.remove(index)
        covered.update(counts[index].keys() & query.keys())
        for i in remaining:
            similarity[i] = max(similarity[i], len(terms[i] & terms[index]) / (len(terms[i] | terms[index]) or 1))
    return tuple(pages[i].record for i in selected)


def rank_pdf_records(records: Sequence[MemoryEntryRecord], question: str, limit: int) -> tuple[MemoryEntryRecord, ...]:
    pages = _pages(records)
    if not pages or limit <= 0 or not question.strip():
        return ()
    weights = _query_weights(question)
    if not weights:
        return ()
    vocabulary = set(weights).union(*(page.words for page in pages))
    stems = _stem_map(vocabulary)
    query: dict[str, float] = {}
    for word, weight in weights.items():
        term = stems[word]
        query[term] = max(query.get(term, 0.0), weight)
    scores, counts = _scores(pages, question, stems, query)
    return _cover_evidence(pages, question, scores, counts, query, limit)
