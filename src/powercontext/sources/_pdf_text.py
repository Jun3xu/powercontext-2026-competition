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

"""Extract positioned PDF glyphs without accepting a lossy layout reconstruction.

The conservative comparison of plain and layout extraction was inspired by the
Apache-2.0 Point-yang/powercontext-competition implementation. This implementation
uses its own run-based fragmentation check and preserves every extracted glyph.
https://github.com/Point-yang/powercontext-competition/blob/eea10e3568db51de8d0640742cf32c5618526b32/src/powercontext/sources/pdf_pages.py
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from pypdf import PageObject


def _fragmented_letters(text: str) -> int:
    fragmented = 0
    for line in text.splitlines():
        run = 0
        for token in [*line.split(), ""]:
            if len(token) == 1 and token.isalpha():
                run += 1
            else:
                if run >= 4:
                    fragmented += run
                run = 0
    return fragmented


def _glyphs(text: str) -> Counter[str]:
    return Counter(character for character in text if not character.isspace())


def extract_pdf_page_text(page: PageObject) -> str:
    """Keep plain extraction unless layout repairs widespread letter fragmentation.

    Content validation is a separate prerequisite: optional layout failures may
    fall back to the successful plain extraction, but must not hide input damage.
    Text uses NFC, matching the Memory persistence normalization.
    """
    plain = unicodedata.normalize("NFC", page.extract_text() or "")
    # Malformed font maps can produce lone surrogates that cannot be persisted.
    # Reject them at parsing time rather than returning an unusable page.
    plain.encode("utf-8")
    fragmentation = _fragmented_letters(plain)
    if fragmentation < 4:
        return plain
    try:
        layout = unicodedata.normalize(
            "NFC",
            page.extract_text(
                extraction_mode="layout", layout_mode_strip_rotated=False, layout_mode_space_vertically=False
            )
            or "",
        )
    except Exception:
        return plain
    if (
        _fragmented_letters(layout) > fragmentation // 2
        or _glyphs(layout) != _glyphs(plain)
        or Counter(re.findall(r"\d+", layout)) != Counter(re.findall(r"\d+", plain))
    ):
        return plain
    # Layout's large horizontal padding is not original content. Preserve line
    # boundaries while removing only whitespace that has no semantic glyphs.
    return "\n".join(" ".join(line.split()) for line in layout.splitlines()).strip()
