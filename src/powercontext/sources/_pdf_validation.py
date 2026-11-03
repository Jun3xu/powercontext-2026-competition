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

"""Check compressed text streams before pypdf's permissive recovery can hide loss."""

from __future__ import annotations

import zlib

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, EncodedStreamObject, NameObject, NullObject, StreamObject


def _require_complete_deflate(data: bytes) -> None:
    decoder = zlib.decompressobj()
    pending = data
    try:
        while pending and not decoder.eof:
            # Discard decoded chunks; validation need not retain a second full copy
            # of a potentially large content stream.
            decoder.decompress(pending, 65536)
            pending = decoder.unconsumed_tail
    except zlib.error as error:
        raise ValueError("corrupt compressed PDF text stream") from error  # noqa: TRY003
    if not decoder.eof:
        raise ValueError("incomplete compressed PDF text stream")  # noqa: TRY003


def _validate_stream(stream: StreamObject) -> None:
    filters = stream.get("/Filter", ())
    filters = filters.get_object() if hasattr(filters, "get_object") else filters
    if not isinstance(filters, (ArrayObject, tuple)):
        filters = (filters,)
    parameters = stream.get("/DecodeParms", ())
    parameters = parameters.get_object() if hasattr(parameters, "get_object") else parameters
    if not isinstance(parameters, (ArrayObject, tuple)):
        parameters = (parameters,)
    data = stream._data
    for index, name in enumerate(filters):
        if name in {"/FlateDecode", "/Fl"}:
            _require_complete_deflate(data)
        # Decode one stage to reach a later Flate filter, preserving predictor
        # parameters. The fixed pypdf dependency owns all other filter behavior.
        stage = EncodedStreamObject()
        stage._data = data
        stage[NameObject("/Filter")] = name
        if index < len(parameters) and not isinstance(parameters[index], NullObject):
            stage[NameObject("/DecodeParms")] = parameters[index]
        data = stage.get_data()


def _validate_resources(resources, seen: set[int]) -> None:
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    if not isinstance(resources, DictionaryObject) or id(resources) in seen:
        return
    seen.add(id(resources))
    fonts = resources.get("/Font", {})
    fonts = fonts.get_object() if hasattr(fonts, "get_object") else fonts
    fonts = fonts if isinstance(fonts, DictionaryObject) else {}
    for reference in fonts.values():
        font = reference.get_object()
        if isinstance(font, NullObject):
            continue
        mapping = font.get("/ToUnicode")
        if mapping is not None:
            _validate_text_object(mapping, seen)
    objects = resources.get("/XObject", {})
    objects = objects.get_object() if hasattr(objects, "get_object") else objects
    objects = objects if isinstance(objects, DictionaryObject) else {}
    for reference in objects.values():
        obj = reference.get_object()
        if isinstance(obj, NullObject):
            continue
        if obj.get("/Subtype") == "/Form":
            _validate_text_object(obj, seen)
            _validate_resources(obj.get("/Resources"), seen)


def _validate_text_object(value, seen: set[int]) -> None:
    value = value.get_object() if hasattr(value, "get_object") else value
    if value is None or isinstance(value, NullObject) or id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, ArrayObject):
        for stream in value:
            _validate_text_object(stream, seen)
    elif isinstance(value, StreamObject):
        _validate_stream(value)
    else:
        raise TypeError("invalid PDF text stream")  # noqa: TRY003


def validate_pdf_text_streams(reader: PdfReader) -> None:
    """Reject damaged page, form, and font-map streams instead of losing text.

    Images are excluded: they do not contribute extractable text, and PDF image
    codecs are intentionally distinct from page content filters.
    """
    seen: set[int] = set()
    for page in reader.pages:
        _validate_text_object(page.get("/Contents"), seen)
        _validate_resources(page.get("/Resources"), seen)
