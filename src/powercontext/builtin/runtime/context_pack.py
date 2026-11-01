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

"""Canonical full and prefix-delta wire forms for prepared context."""

# The wire decoder deliberately raises precise validation messages at every
# trust boundary so callers can distinguish malformed fields.
# ruff: noqa: TRY003

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PREFIX = "sha256:"


class ContextPackError(ValueError):
    """A Context Pack or delta violates the public wire contract."""


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encoded_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _validate_items(items: object) -> tuple[bytes, ...]:
    if not isinstance(items, tuple):
        raise ContextPackError("items must be a tuple of bytes")
    for item in items:
        if not isinstance(item, bytes):
            raise ContextPackError("items must contain bytes")
        try:
            item.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("each item must contain valid UTF-8") from error
    return items


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ContextPackError("wire items must be a string array")
    decoded: list[bytes] = []
    try:
        for value in values:
            item = base64.b64decode(value, validate=True)
            if base64.b64encode(item).decode("ascii") != value:
                raise ValueError("non-canonical Base64")  # noqa: TRY301
            item.decode("utf-8")
            decoded.append(item)
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ContextPackError("wire items must be canonical Base64 encoded UTF-8") from error
    return tuple(decoded)


def _validate_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != len(_DIGEST_PREFIX) + 64
        or not value.startswith(_DIGEST_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[len(_DIGEST_PREFIX) :])
    ):
        raise ContextPackError("invalid SHA-256 digest")
    return value


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": _PACK_SCHEMA, "items": _encoded_items(items)}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _pack_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    count = 0
    while True:
        payload = {
            "schema": _PACK_SCHEMA,
            "items": _encoded_items(items),
            "digest": digest,
            "byte_count": count,
        }
        measured = len(_canonical_json(payload))
        if measured == count:
            return count
        count = measured


@dataclass(frozen=True, slots=True)
class ContextPack:
    items: tuple[bytes, ...]
    digest: str

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        checked = _validate_items(items)
        return cls(items=checked, digest=_pack_digest(checked))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = 512) -> ContextPack:
        if not isinstance(content, str):
            raise ContextPackError("content must be text")
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if not content:
            return cls.build(())

        items: list[bytes] = []
        current = bytearray()
        for character in content:
            encoded = character.encode("utf-8")
            if len(encoded) > chunk_bytes:
                raise ContextPackError("chunk limit cannot contain one UTF-8 code point")
            if current and len(current) + len(encoded) > chunk_bytes:
                items.append(bytes(current))
                current.clear()
            current.extend(encoded)
        if current:
            items.append(bytes(current))
        return cls.build(tuple(items))

    @property
    def byte_count(self) -> int:
        checked = _validate_items(self.items)
        checked_digest = _validate_digest(self.digest)
        if _pack_digest(checked) != checked_digest:
            raise ContextPackError("Context Pack digest does not match its items")
        return _pack_byte_count(checked, checked_digest)

    def to_bytes(self) -> bytes:
        checked = _validate_items(self.items)
        checked_digest = _validate_digest(self.digest)
        if _pack_digest(checked) != checked_digest:
            raise ContextPackError("Context Pack digest does not match its items")
        return _canonical_json({
            "schema": _PACK_SCHEMA,
            "items": _encoded_items(checked),
            "digest": checked_digest,
            "byte_count": _pack_byte_count(checked, checked_digest),
        })

    @classmethod
    def from_bytes(cls, value: bytes, /) -> ContextPack:
        if not isinstance(value, bytes):
            raise ContextPackError("Context Pack wire value must be bytes")
        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContextPackError("invalid Context Pack JSON") from error
        if not isinstance(payload, dict) or set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("invalid Context Pack fields")
        if payload["schema"] != _PACK_SCHEMA:
            raise ContextPackError("unsupported Context Pack schema")
        if type(payload["byte_count"]) is not int or payload["byte_count"] != len(value):
            raise ContextPackError("Context Pack byte count does not match wire bytes")
        pack = cls.build(_decode_items(payload["items"]))
        if _validate_digest(payload["digest"]) != pack.digest:
            raise ContextPackError("Context Pack digest does not match its items")
        return pack


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("base and target must be Context Packs")
        prefix = 0
        while prefix < min(len(base.items), len(target.items)) and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        _validate_digest(self.base_digest)
        _validate_digest(self.target_digest)
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        _validate_items(self.appended)
        return _canonical_json({
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encoded_items(self.appended),
        })

    @classmethod
    def from_bytes(cls, value: bytes, /) -> ContextPackDelta:
        if not isinstance(value, bytes):
            raise ContextPackError("Context Pack delta wire value must be bytes")
        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContextPackError("invalid Context Pack delta JSON") from error
        expected = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ContextPackError("invalid Context Pack delta fields")
        if payload["schema"] != _DELTA_SCHEMA:
            raise ContextPackError("unsupported Context Pack delta schema")
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        return cls(
            base_digest=_validate_digest(payload["base_digest"]),
            target_digest=_validate_digest(payload["target_digest"]),
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("base and delta must be Context Pack values")
    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the supplied pack")
    if type(delta.prefix_items) is not int or not 0 <= delta.prefix_items <= len(base.items):
        raise ContextPackError("delta prefix is outside the base pack")
    target = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match reconstructed items")
    return target


__all__ = ["ContextPack", "ContextPackDelta", "ContextPackError", "apply_context_pack_delta"]
