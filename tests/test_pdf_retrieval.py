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
    assert result[0] == 25
    assert len(result) == 20


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
    assert result[0] == 25
    assert len(result) == 20


@pytest.mark.asyncio
async def test_missing_requested_year_keeps_available_report():
    memory = ReportMemory(["ACME_2024_10K\nPrior year net sales 2023 90."])
    assert await search_pdf_pages(memory, "What was Acme revenue in FY2023?", limit=20) == (0,)


@pytest.mark.asyncio
async def test_arbitrary_document_identifier_cannot_remove_a_relevant_report():
    memory = ReportMemory([
        "CASH_2024_10K\nThere are no material cash flow details on this page.",
        "ACME_2024_10K\nStatement of Cash Flows\nNet cash provided by operating activities 100.",
    ])
    result = await search_pdf_pages(memory, "What is net cash provided by operating activities in 2024?", limit=1)
    assert result == (1,)


@pytest.mark.asyncio
async def test_comparison_preserves_both_explicit_issuer_names():
    memory = ReportMemory([
        "ACME_2024_10K\nNet sales 100.",
        "ACMEHOLDINGS_2024_10K\nNet sales 200.",
    ])
    result = await search_pdf_pages(memory, "Compare Acme and Acme Holdings revenues in FY2024.", limit=2)
    assert set(result) == {0, 1}


@pytest.mark.asyncio
async def test_inflection_differences_do_not_hide_international_operations():
    memory = ReportMemory(
        [f"ACME_2024_10K\nThe company operates a distribution outlet {n}." for n in range(25)]
        + ["ACME_2024_10K\nInternational operations are spread among European and Asian market areas."]
    )
    result = await search_pdf_pages(memory, "Where does Acme operate?", limit=20)
    assert 25 in result


@pytest.mark.asyncio
async def test_actual_statement_beats_table_of_contents_references():
    memory = ReportMemory(
        [f"ACME_2024_10K\nConsolidated Statement of Income {n}" for n in range(25)]
        + ["ACME_2024_10K\nConsolidated Statement of Income\nSales 100."]
    )
    assert await search_pdf_pages(memory, "Acme income statement", limit=1) == (25,)


@pytest.mark.asyncio
async def test_multiple_requested_statements_are_not_crowded_by_one_type():
    memory = ReportMemory(
        [
            f"ACME_2024_10K\nConsolidated Statement of Income\n"
            f"Income operating profit EBITDA sales depreciation cash flows financing {n}."
            for n in range(25)
        ]
        + ["ACME_2024_10K\nConsolidated Statement of Cash Flows\nDepreciation 10."]
    )
    result = await search_pdf_pages(memory, "Acme EBITDA from the income statement and cash flow statement", limit=3)
    assert 25 in result


@pytest.mark.asyncio
@pytest.mark.parametrize("issuer", ["Acme", "acme", "3M"])
async def test_issuer_prior_is_case_insensitive_and_supports_digits(issuer):
    document = "3M" if issuer == "3M" else "ACME"
    memory = ReportMemory(
        [f"OTHER_{n}_2024_10K\nRevenue sales profit earnings. A competitor is mentioned." for n in range(25)]
        + [f"{document}_2024_10K\nNet sales 100."]
    )
    assert await search_pdf_pages(memory, f"What revenue did {issuer} earn in FY2024?", limit=1) == (25,)


@pytest.mark.asyncio
async def test_compound_generic_document_name_cannot_override_relevance():
    memory = ReportMemory(
        [f"CASH_FLOWS_2024_10K\nCash flow details not material. Administrative schedule {n}." for n in range(25)]
        + ["ACME_2024_10K\nStatement of Cash Flows\nNet cash provided by operating activities 100."]
    )
    question = "What is Net Cash Provided by Operating Activities in the Cash Flows statement for 2024?"
    assert await search_pdf_pages(memory, question, limit=1) == (25,)


@pytest.mark.asyncio
@pytest.mark.parametrize("other_document", ["OTHER_2024_10K", "ACME_2023_10K"])
async def test_section_coverage_respects_stronger_issuer_and_year_relevance(other_document):
    memory = ReportMemory([
        f"{other_document}\nConsolidated Statement of Income\nOperating income 50.",
        "ACME_2024_10K\nOperating income 200.",
    ])
    assert await search_pdf_pages(memory, "Acme operating income in FY2024", limit=1) == (1,)


@pytest.mark.asyncio
async def test_company_name_is_not_a_statement_keyword():
    memory = ReportMemory([
        "OTHER_2024_10K\nConsolidated Statement of Cash Flows\nCash flow 100.",
        "CASHMORE_2024_10K\nEmployees\nThe company employs 700 people.",
    ])
    assert await search_pdf_pages(memory, "Cashmore employee headcount", limit=1) == (1,)


@pytest.mark.asyncio
async def test_duplicate_page_sources_do_not_consume_the_result_limit():
    memory = ReportMemory([
        "ACME_2024_10K\nRevenue sales 100.",
        "ACME_2024_10K\nRevenue sales 100.",
        "ACME_2024_10K\nRevenue sales 200.",
    ])
    memory.records[1].entry.sources = memory.records[0].entry.sources
    result = await search_pdf_pages(memory, "Acme revenues", limit=2)
    assert len(result) == 2 and 2 in result
    assert not ({0, 1} <= set(result))


_COVERAGE_CASES = []
_COVERAGE_CASES.append((
    "earnings plus noncash costs",
    [
        f"ACME_2024_10K\nConsolidated Statements of Cash Flows (continued)\nOperating income earnings cash flows depreciation amortization revenue net sales operating activities. Schedule of subsidiary {i}. "
        * 2
        for i in range(25)
    ]
    + [
        "ACME_2024_10K\nConsolidated Statements of Income\nNet revenue 10000. Operating earnings 1400. Net earnings 900.",
        "ACME_2024_10K\nConsolidated Statements of Cash Flows\nDepreciation and amortization 350. Investing activities capital expenditures 150.",
    ],
    "For Acme in 2024, estimate the operating cash earning margin using operating earnings plus depreciation and amortization, divided by revenue. Use both income and cash flow statements.",
    {25, 26},
))
_COVERAGE_CASES.append((
    "liquidity inventory exclusion",
    [
        f"ACME_2024_10K\nNotes to Consolidated Financial Statements\nInventory current assets current liabilities quick ratio cash inventories financial instruments accounting estimates. Business division {i}."
        for i in range(25)
    ]
    + [
        "ACME_2024_10K\nConsolidated Balance Sheets\nCurrent assets 100. Current liabilities 50. Total assets 200.",
        "ACME_2024_10K\nNote 4 Inventories\nRaw materials 10. Finished goods 15. Supplies 5. Total 30.",
    ],
    "Compute the Acme 2024 quick ratio by excluding inventories from current assets before dividing by current liabilities. Retrieve the balance sheet and inventory breakdown.",
    {25, 26},
))
_COVERAGE_CASES.append((
    "two independent named risk subjects",
    [
        f"ACME_2024_10K\nLegal Proceedings\nLegal litigation contingencies pending claims lawsuits reserves matters obligations uncertainty environmental remediation damages. Risk subsidiary {i}."
        for i in range(25)
    ]
    + [
        "ACME_2024_10K\nNote 9 Litigation\nA court awarded damages of 12 and we reserved the full amount.",
        "ACME_2024_10K\nNote 10 Environmental obligations\nSite cleanup costs of 23 were accrued.",
    ],
    "What amounts did Acme accrue for litigation and environmental remediation during 2024? Find both the legal reserve and site cleanup accrual.",
    {25, 26},
))


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name,texts,question,expected", _COVERAGE_CASES)
async def test_complementary_notes_survive_near_duplicate_discussion(case_name, texts, question, expected):
    result = await search_pdf_pages(ReportMemory(texts), question, limit=20)
    assert expected <= set(result), case_name


@pytest.mark.asyncio
async def test_geographic_question_includes_business_description_not_only_segment_notes():
    memory = ReportMemory(
        [
            f"ACME_2024_10K\nGeographic information\nInternational countries regions markets divisions segments {n}."
            for n in range(25)
        ]
        + ["ACME_2024_10K\nItem 1. Business.\nWe manufacture tools in North America and distribute them worldwide."]
    )
    result = await search_pdf_pages(memory, "In which geographies does Acme operate?", limit=3)
    assert 25 in result
