"""Damaged compressed page streams must not turn into successful blank pages."""

from __future__ import annotations

import base64
import io
import zlib
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, EncodedStreamObject, NameObject, NullObject

from powercontext.sources._pdf_validation import validate_pdf_text_streams
from powercontext.sources.pdf_pages import PDFDocumentInput, parse_pdf_document


def _report(contents: tuple[tuple[bytes, bool], ...]) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for data, ascii85 in contents:
        page = writer.add_blank_page(200, 200)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        stream = EncodedStreamObject()
        stream._data = base64.a85encode(data) + b"~>" if ascii85 else data
        stream[NameObject("/Filter")] = (
            ArrayObject([NameObject("/ASCII85Decode"), NameObject("/FlateDecode")])
            if ascii85
            else NameObject("/FlateDecode")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


_TEXT = b"BT /F1 12 Tf 10 100 Td (Complete text) Tj ET"


@pytest.mark.parametrize("ascii85", [False, True])
def test_valid_compressed_content_and_filter_chain(ascii85):
    raw = _report(((zlib.compress(_TEXT), ascii85),))
    validate_pdf_text_streams(PdfReader(io.BytesIO(raw), strict=True))
    assert parse_pdf_document(PDFDocumentInput(document_id="control", content=raw))[0].text == "Complete text"


@pytest.mark.parametrize("data", [b"corrupt deflate bytes", zlib.compress(_TEXT)[:-4]])
def test_invalid_or_incomplete_deflate_cannot_be_silently_recovered(data):
    raw = _report(((data, False),))
    with pytest.raises(ValueError, match="compressed PDF text stream"):
        validate_pdf_text_streams(PdfReader(io.BytesIO(raw), strict=True))


def test_damaged_later_page_fails_parser_atomically():
    raw = _report(((zlib.compress(_TEXT), False), (b"corrupt deflate bytes", False)))
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="two-pages", content=raw))


def test_blank_page_without_contents_remains_valid():
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    output = io.BytesIO()
    writer.write(output)
    reader = PdfReader(io.BytesIO(output.getvalue()), strict=True)
    validate_pdf_text_streams(reader)
    assert reader.pages[0].extract_text() == ""


def test_public_complete_report_has_valid_text_streams():
    path = (
        Path(__file__).parents[1]
        / "competition/challenges/source-03-pdf-retrieval/workload/public/pdfs/PEPSICO_2022_10K.pdf"
    )
    reader = PdfReader(path, strict=True)
    validate_pdf_text_streams(reader)
    assert len(reader.pages) == 503


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_empty_password_encrypted_content_is_valid(algorithm):
    if algorithm.startswith("AES"):
        pytest.importorskip("cryptography")
    original = _report(((zlib.compress(_TEXT), False),))
    writer = PdfWriter(clone_from=io.BytesIO(original))
    writer.encrypt(user_password="", owner_password="owner", algorithm=algorithm)  # noqa: S106
    output = io.BytesIO()
    writer.write(output)
    reader = PdfReader(io.BytesIO(output.getvalue()), strict=True)
    assert reader.decrypt("")
    validate_pdf_text_streams(reader)
    assert reader.pages[0].extract_text() == "Complete text"


def test_blank_page_with_null_optional_xobjects_is_valid():
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): NullObject()})
    output = io.BytesIO()
    writer.write(output)
    reader = PdfReader(io.BytesIO(output.getvalue()), strict=True)
    validate_pdf_text_streams(reader)
    assert reader.pages[0].extract_text() == ""
