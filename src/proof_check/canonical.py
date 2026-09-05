"""JCS-style canonical JSON and SHA-256 digests.

Reference behavior is RFC 8785 (JSON Canonicalization Scheme): UTF-8 output,
no insignificant whitespace, no trailing LF, strings escaped the way RFC 8785
prescribes, numbers rendered the way ECMAScript ``Number.prototype.toString``
renders them. One deviation is fixed by the receipt schema: object keys sort
by Unicode code point (RFC 8785 sorts by UTF-16 code unit). The two orders
differ only when keys mix supplementary-plane characters with characters in
U+E000..U+FFFF, which no receipt key does.

Digests are hexadecimal SHA-256 over the exact bytes named by the caller.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any

__all__ = ["CanonicalizationError", "canonical_bytes", "canonical_sha256", "sha256_hex"]

# Integers beyond this magnitude are not exact IEEE-754 doubles, so JCS cannot
# round-trip them. GitHub identifiers stay far below it.
_MAX_EXACT_INTEGER = 2**53


class CanonicalizationError(ValueError):
    """The value cannot be expressed as canonical JSON."""


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical JSON encoding of ``value`` as UTF-8 bytes, no trailing LF."""
    return _encode(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Hexadecimal SHA-256 over exactly ``data``."""
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    """Hexadecimal SHA-256 over the canonical JSON bytes of ``value``."""
    return sha256_hex(canonical_bytes(value))


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > _MAX_EXACT_INTEGER:
            raise CanonicalizationError(f"integer {value} exceeds the exact IEEE-754 range")
        return str(value)
    if isinstance(value, float):
        return _encode_number(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalizationError(f"object key {key!r} is not a string")
        members = (f"{_encode_string(key)}:{_encode(value[key])}" for key in sorted(value))
        return "{" + ",".join(members) + "}"
    raise CanonicalizationError(f"type {type(value).__name__} has no canonical JSON form")


def _encode_string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CanonicalizationError("string contains a lone surrogate and is not valid Unicode") from error
    # json.dumps without ensure_ascii escapes exactly the set RFC 8785 requires:
    # quotation mark, reverse solidus, and the C0 controls (short forms for
    # \b \t \n \f \r, lowercase \u00XX for the rest). Everything else is literal.
    return json.dumps(value, ensure_ascii=False)


def _encode_number(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN and infinities have no JSON form")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    sign = "-" if value < 0 else ""
    # repr() yields the shortest digit string that round-trips, which is the
    # digit sequence ECMAScript Number::toString also starts from.
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(str(d) for d in digit_tuple).rstrip("0") or "0"
    exponent += len(digit_tuple) - len(digits)
    k = len(digits)
    n = k + exponent  # value = 0.d1...dk x 10^n
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
        body = digits if k == 1 else f"{digits[0]}.{digits[1:]}"
        body += exp
    return sign + body
