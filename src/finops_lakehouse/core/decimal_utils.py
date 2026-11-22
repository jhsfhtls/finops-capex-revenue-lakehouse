"""Strict monetary and rate decimal utilities forbidding binary float drift (ADR-001)."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

MONEY_QUANT = Decimal("0.0001")
RATE_QUANT = Decimal("0.000001")
ZERO_MONEY = Decimal("0.0000")


def _coerce_decimal(value: object, *, quant: Decimal, allow_float: bool = False) -> Decimal:
    """Convert supported numeric or string inputs into quantized finite Decimal."""
    if value is None or isinstance(value, bool):
        raise TypeError(f"Unsupported value type for decimal conversion: {type(value).__name__}")

    if isinstance(value, float):
        if not allow_float:
            raise TypeError(
                "IEEE 754 float is strictly forbidden for monetary/rate precision; "
                "use Decimal, int, or str."
            )
        if not math.isfinite(value):
            raise ValueError("Non-finite float cannot be converted to Decimal.")
        raw = Decimal(str(value))
    elif isinstance(value, Decimal):
        raw = value
    elif isinstance(value, int):
        raw = Decimal(value)
    elif isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Empty string cannot be converted to Decimal.")
        try:
            raw = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid decimal string: {value!r}") from exc
    else:
        raise TypeError(f"Unsupported value type for decimal conversion: {type(value).__name__}")

    if not raw.is_finite():
        raise ValueError(f"Non-finite decimal value is not allowed: {value!r}")

    try:
        return raw.quantize(quant, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise ValueError(
            f"Decimal value exceeds quantization precision limits: {value!r}"
        ) from exc


def require_non_negative_int(value: object, name: str) -> int:
    """Require a strict non-negative integer (rejecting booleans and floats)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}.")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}.")
    return value


def to_decimal_money(value: Decimal | int | str) -> Decimal:
    """Convert Decimal, int, or str to DECIMAL(18,4) with ROUND_HALF_EVEN; forbid float."""
    return _coerce_decimal(value, quant=MONEY_QUANT, allow_float=False)


def to_decimal_rate(value: Decimal | int | str) -> Decimal:
    """Convert Decimal, int, or str to DECIMAL(12,6) with ROUND_HALF_EVEN; forbid float."""
    return _coerce_decimal(value, quant=RATE_QUANT, allow_float=False)


def parse_spreadsheet_decimal(value: object) -> Decimal:
    """Boundary parser for Excel/CSV cells avoiding float arithmetic after ingestion."""
    return _coerce_decimal(value, quant=MONEY_QUANT, allow_float=True)


def format_money(value: Decimal) -> str:
    """Format a Decimal value canonically with 4 fractional digits."""
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError("format_money requires a Decimal instance.")
    if not value.is_finite():
        raise ValueError("Non-finite Decimal cannot be formatted.")
    try:
        return f"{value.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN):.4f}"
    except InvalidOperation as exc:
        raise ValueError(f"Decimal value exceeds money formatting precision: {value!r}") from exc
