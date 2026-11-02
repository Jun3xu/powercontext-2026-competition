"""Retrieval regressions with independent synthetic report pages."""

from types import SimpleNamespace

import pytest

from powercontext.sources.pdf_pages import search_pdf_pages


class ReportMemory:
    def __init__(self, texts):
        self.records = tuple(
            SimpleNamespace(
                entry=SimpleNamespace(
                    text=text,
                    entry_id=str(index),
                    sources=(SimpleNamespace(source_type="pdf-page", source_id=str(index)),),
                ),
                citation=index,
            )
            for index, text in enumerate(texts)
        )

    async def list(self):
        return SimpleNamespace(entries=self.records)


@pytest.mark.asyncio
async def test_legal_synonyms_find_proceedings_page():
    memory = ReportMemory(
        ["ACME_2024_10K\nImportant ongoing business developments. " * 5]
        + [f"ACME_2024_10K\nOngoing important operating activity {n}." for n in range(25)]
        + ["ACME_2024_10K\nLegal Proceedings\nPending litigation and contingent liabilities."]
    )
    result = await search_pdf_pages(memory, "Does Acme have ongoing legal battles in FY2024?", limit=3)
    assert 26 in result
    assert len(result) == len(set(result)) == 3


@pytest.mark.asyncio
async def test_abbreviations_find_original_financial_statement():
    memory = ReportMemory(
        [f"ACME_2024_10K\nFinancial statement discussion and analysis EBITDA margin {n}." for n in range(25)]
        + [
            "ACME_2024_10K\nConsolidated Statements of Operations\nNet sales 100. Operating income 20.",
            "ACME_2024_10K\nConsolidated Statements of Cash Flows\nDepreciation and amortization 10.",
        ]
    )
    result = await search_pdf_pages(memory, "Calculate Acme FY2024 EBITDA from P&L and D&A.", limit=3)
    assert {25, 26} <= set(result)
    assert await search_pdf_pages(memory, "EBITDA", limit=0) == ()


@pytest.mark.asyncio
async def test_named_company_disambiguates_identical_statements():
    memory = ReportMemory(
        [f"OTHER_{n}_10K\nConsolidated Statements of Income\nNet sales 100. Operating income 20." for n in range(25)]
        + ["ACME_2024_10K\nConsolidated Statements of Income\nNet sales 100. Operating income 20."]
    )
    assert await search_pdf_pages(memory, "Acme operating income in FY2024", limit=1) == (25,)


@pytest.mark.asyncio
async def test_explicit_company_cannot_be_displaced_by_other_reports():
    memory = ReportMemory(
        [
            f"OTHER_{n}_2024_10K\nConsolidated Statements of Income\n"
            "Acme is a competitor. Revenue net sales operating income earnings profit. " * 3
            for n in range(25)
        ]
        + ["ACME_2024_10K\nNet sales 100."]
    )
    result = await search_pdf_pages(memory, "What revenue did Acme earn in FY2024?", limit=20)
    assert 25 in result
    assert result == (25,)


@pytest.mark.asyncio
async def test_requested_fiscal_year_selects_matching_report():
    memory = ReportMemory(
        [
            f"ACME_{year}_10K\nConsolidated Statement of Income\nRevenue net sales profit earnings."
            for year in range(1995, 2020)
        ]
        + ["ACME_2024_10K\nNet sales 100."]
    )
    result = await search_pdf_pages(memory, "What was Acme revenue in FY2024?", limit=20)
    assert result == (25,)


@pytest.mark.asyncio
async def test_missing_requested_year_keeps_available_report():
    memory = ReportMemory(["ACME_2024_10K\nPrior year net sales 2023 90."])
    assert await search_pdf_pages(memory, "What was Acme revenue in FY2023?", limit=20) == (0,)
