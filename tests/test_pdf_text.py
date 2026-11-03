"""Individually positioned glyphs must remain searchable as complete words."""

from __future__ import annotations

import io
from typing import cast

import pytest
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.sources._pdf_text import extract_pdf_page_text


def _page(content: bytes) -> PageObject:
    writer = PdfWriter()
    page = writer.add_blank_page(600, 300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
    })
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return PdfReader(output).pages[0]


def test_positioned_glyphs_recover_complete_words():
    text = "Revenue Income Total Cash Flow"
    glyphs = " ".join(f"({character}) -{500 if character == ' ' else 135}" for character in text)
    page = _page(f"BT /F1 12 Tf 1 0 0 1 20 250 Tm [{glyphs}] TJ ET".encode())
    assert "Revenue" not in page.extract_text()
    assert extract_pdf_page_text(page) == text


def test_ordinary_table_text_keeps_default_extraction():
    page = _page(b"BT /F1 12 Tf 20 250 Td (Revenue 2024 2023) Tj 0 -20 Td (North 123 98) Tj ET")
    assert extract_pdf_page_text(page) == page.extract_text()


def test_blank_page_has_empty_text():
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    output = io.BytesIO()
    writer.write(output)
    assert extract_pdf_page_text(PdfReader(output).pages[0]) == ""


class _ExtractionResult:
    def __init__(self, plain: str, layout: str | Exception):
        self.plain = plain
        self.layout = layout

    def extract_text(self, **kwargs) -> str:
        if not kwargs:
            return self.plain
        if isinstance(self.layout, Exception):
            raise self.layout
        return self.layout


@pytest.mark.parametrize(
    "layout",
    [
        "Revenue Income",  # Missing a table figure.
        "Revenue Income 123 123",  # Duplicated a figure.
        "R e v e n u e I n c o m e 123",  # No actual word recovery.
        ValueError("cannot lay out this page"),
    ],
)
def test_unsafe_or_unavailable_layout_keeps_readable_plain_text(layout):
    plain = "R e v e n u e I n c o m e 123"
    page = cast("PageObject", _ExtractionResult(plain, layout))
    assert extract_pdf_page_text(page) == plain


def test_canonical_unicode_matches_memory_normalization():
    page = cast("PageObject", _ExtractionResult("Cafe\u0301 revenue 123", "unused"))
    assert extract_pdf_page_text(page) == "Café revenue 123"


@pytest.mark.parametrize(("plain_numbers", "layout_numbers"), [("100 200", "100200"), ("100200", "100 200")])
def test_layout_cannot_merge_or_split_numeric_values(plain_numbers, layout_numbers):
    plain = f"R e v e n u e I n c o m e {plain_numbers}"
    layout = f"Revenue Income {layout_numbers}"
    page = cast("PageObject", _ExtractionResult(plain, layout))
    assert extract_pdf_page_text(page) == plain


def test_invalid_font_unicode_mapping_fails_before_persistence():
    from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document

    writer = PdfWriter()
    writer.add_page(_page(b"BT /F1 12 Tf 20 250 Td (A) Tj ET"))
    mapping = DecodedStreamObject()
    mapping.set_data(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def "
        b"/CMapName /Test def /CMapType 2 def "
        b"1 begincodespacerange <00> <FF> endcodespacerange "
        b"1 beginbfchar <41> <D800> endbfchar "
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
    )
    resources = cast("DictionaryObject", writer.pages[0]["/Resources"])
    fonts = cast("DictionaryObject", resources["/Font"])
    font = cast("DictionaryObject", fonts["/F1"])
    font[NameObject("/ToUnicode")] = writer._add_object(mapping)
    output = io.BytesIO()
    writer.write(output)
    assert PdfReader(output).pages[0].extract_text() == "\ud800"
    with pytest.raises(ValueError, match="cannot completely parse PDF"):
        parse_pdf_document(PDFDocumentInput(document_id="font-map", content=output.getvalue()))


def test_short_positioned_title_is_recovered():
    glyphs = " ".join(f"({char}) -135" for char in "Revenue")
    page = _page(f"BT /F1 12 Tf 1 0 0 1 20 250 Tm [{glyphs}] TJ 0 -20 Td (Current financial year) Tj ET".encode())
    assert "Revenue" not in page.extract_text()
    assert extract_pdf_page_text(page) == "Revenue\nCurrent financial year"


@pytest.mark.parametrize("labels", ["A B C D E F G H", "A B C D"])
def test_intentional_letter_lists_and_column_labels_are_not_collapsed(labels):
    page = _page(f"BT /F1 12 Tf 20 250 Td ({labels}) Tj 0 -20 Td (10 20 30 40) Tj ET".encode())
    assert extract_pdf_page_text(page) == page.extract_text()


def test_positioned_letters_and_digits_recover_words_and_values():
    text = "Revenue 1234 Income 5678"
    glyphs = " ".join(f"({char}) -{500 if char == ' ' else 135}" for char in text)
    page = _page(f"BT /F1 12 Tf 1 0 0 1 20 250 Tm [{glyphs}] TJ ET".encode())
    assert "1234" not in page.extract_text()
    assert extract_pdf_page_text(page) == text


@pytest.mark.parametrize("tracked", [False, True])
def test_wide_numeric_columns_remain_separate_during_layout(tracked):
    def draw(value):
        return "[" + " ".join(f"({char}) -135" for char in value) + "] TJ" if tracked else f"({value}) Tj"

    content = (
        "BT /F1 12 Tf 1 0 0 1 20 250 Tm (Revenue) Tj "
        f"1 0 0 1 150 250 Tm {draw('1234')} 1 0 0 1 250 250 Tm {draw('5678')} ET"
    )
    page = _page(content.encode())
    result = extract_pdf_page_text(page)
    assert result.split() == ["Revenue", "1234", "5678"]
