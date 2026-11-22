"""Deterministic B2B deal economics calculator in pure Decimal (zero float)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    RATE_QUANT,
    ZERO_MONEY,
    format_money,
    require_non_negative_int,
    to_decimal_money,
    to_decimal_rate,
)

_ONE = Decimal("1")
_ZERO = Decimal("0")
_HUNDRED = Decimal("100")


def _high_prec_npv(
    initial_investment: Decimal,
    cashflows: Sequence[Decimal],
    periodic_rate: Decimal,
) -> Decimal:
    """Compute exact unrounded NPV for internal root-finding and precision control."""
    base = _ONE + periodic_rate
    if base <= _ZERO:
        raise ValueError("periodic_rate must be strictly greater than -1.")

    pv_total = -initial_investment
    discount_factor = _ONE
    for cf in cashflows:
        discount_factor *= base
        pv_total += cf / discount_factor
    return pv_total


def _parse_cashflows(cashflows: Sequence[Decimal | int | str]) -> list[Decimal]:
    if isinstance(cashflows, (str, bytes)) or not cashflows:
        raise ValueError("cashflows must be a non-empty sequence of numeric values.")
    return [to_decimal_money(cf) for cf in cashflows]


def compute_npv(
    initial_investment: Decimal | int | str,
    cashflows: Sequence[Decimal | int | str],
    periodic_rate: Decimal | int | str,
) -> Decimal:
    """Compute Net Present Value (NPV) quantized to DECIMAL(18,4) with ROUND_HALF_EVEN."""
    inv = to_decimal_money(initial_investment)
    if inv < ZERO_MONEY:
        raise ValueError("initial_investment must be non-negative.")
    cfs = _parse_cashflows(cashflows)
    rate = to_decimal_rate(periodic_rate)
    raw_npv = _high_prec_npv(inv, cfs, rate)
    return raw_npv.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)


def annual_to_monthly_rate(annual_rate: Decimal | int | str) -> Decimal:
    """Convert effective annual rate to monthly compound rate via pure Decimal bisection."""
    ann = to_decimal_rate(annual_rate)
    if ann <= Decimal("-1.000000"):
        raise ValueError("annual_rate must be strictly greater than -1.")
    if ann == _ZERO:
        return Decimal("0.000000")

    target = _ONE + ann
    lo = Decimal("-0.99")
    hi = max(Decimal("1.0"), ann + Decimal("1.0"))
    for _ in range(80):
        mid = (lo + hi) / Decimal("2")
        if (_ONE + mid) ** 12 < target:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / Decimal("2")).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def monthly_to_annual_rate(monthly_rate: Decimal | int | str) -> Decimal:
    """Convert periodic monthly rate to effective annual compound rate in DECIMAL(12,6)."""
    m_rate = to_decimal_rate(monthly_rate)
    if m_rate <= Decimal("-1.000000"):
        raise ValueError("monthly_rate must be strictly greater than -1.")
    return ((_ONE + m_rate) ** 12 - _ONE).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def compute_irr(
    initial_investment: Decimal | int | str,
    cashflows: Sequence[Decimal | int | str],
    *,
    tolerance: Decimal = Decimal("0.000000001"),
) -> Decimal | None:
    """Compute periodic Internal Rate of Return (IRR) via pure Decimal bisection."""
    inv = to_decimal_money(initial_investment)
    if inv < ZERO_MONEY:
        raise ValueError("initial_investment must be non-negative.")
    if inv == ZERO_MONEY or isinstance(cashflows, (str, bytes)) or not cashflows:
        return None
    cfs = _parse_cashflows(cashflows)
    if not any(cf > ZERO_MONEY for cf in cfs):
        return None

    grid = (
        Decimal("-0.99"),
        Decimal("-0.95"),
        Decimal("-0.50"),
        Decimal("0.00"),
        Decimal("0.05"),
        Decimal("0.15"),
        Decimal("0.30"),
        Decimal("0.60"),
        Decimal("1.20"),
        Decimal("3.00"),
        Decimal("10.00"),
    )

    bracket: tuple[Decimal, Decimal] | None = None
    prev_r = grid[0]
    prev_npv = _high_prec_npv(inv, cfs, prev_r)
    if abs(prev_npv) <= tolerance:
        return prev_r.quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)

    for curr_r in grid[1:]:
        curr_npv = _high_prec_npv(inv, cfs, curr_r)
        if abs(curr_npv) <= tolerance:
            return curr_r.quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
        if (prev_npv > _ZERO and curr_npv < _ZERO) or (prev_npv < _ZERO and curr_npv > _ZERO):
            bracket = (prev_r, curr_r)
            break
        prev_r, prev_npv = curr_r, curr_npv

    if bracket is None:
        return None

    lo, hi = bracket
    npv_lo = _high_prec_npv(inv, cfs, lo)
    for _ in range(90):
        mid = (lo + hi) / Decimal("2")
        npv_mid = _high_prec_npv(inv, cfs, mid)
        if abs(npv_mid) <= tolerance or (hi - lo) <= tolerance:
            return mid.quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
        if (npv_lo > _ZERO and npv_mid > _ZERO) or (npv_lo < _ZERO and npv_mid < _ZERO):
            lo = mid
            npv_lo = npv_mid
        else:
            hi = mid

    return ((lo + hi) / Decimal("2")).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def compute_payback(
    initial_investment: Decimal | int | str,
    cashflows: Sequence[Decimal | int | str],
    *,
    periodic_rate: Decimal | int | str = Decimal("0"),
) -> Decimal | None:
    """Compute fractional simple (rate=0) or discounted (rate>0) payback period."""
    inv = to_decimal_money(initial_investment)
    if inv < ZERO_MONEY:
        raise ValueError("initial_investment must be non-negative.")
    if inv == ZERO_MONEY:
        return Decimal("0.000000")
    if isinstance(cashflows, (str, bytes)) or not cashflows:
        return None

    rate = to_decimal_rate(periodic_rate)
    base = _ONE + rate
    if base <= _ZERO:
        raise ValueError("periodic_rate must be > -1.")

    cumulative = _ZERO
    discount_factor = _ONE
    for idx, raw_cf in enumerate(cashflows, 1):
        cf = to_decimal_money(raw_cf)
        discount_factor *= base
        pv = cf / discount_factor
        prev_cum = cumulative
        cumulative += pv
        if cumulative >= inv and pv > _ZERO:
            remaining = inv - prev_cum
            fraction = remaining / pv
            return (Decimal(idx - 1) + fraction).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)

    return None


def evaluate_b2b_deal(
    *,
    initial_investment: Decimal | int | str,
    monthly_cashflows: Sequence[Decimal | int | str],
    contract_revenue: Decimal | int | str,
    direct_delivery_cost: Decimal | int | str,
    annual_discount_rate: Decimal | int | str = Decimal("0.120000"),
    monthly_discount_rate: Decimal | int | str | None = None,
    monthly_inaction_cost: Decimal | int | str = ZERO_MONEY,
    delay_months: int = 3,
    target_gross_margin: Decimal | int | str = Decimal("0.650000"),
) -> dict[str, Any]:
    """Evaluate B2B deal economics with deterministic NPV, IRR, Payback, Margin Gate & COI."""
    require_non_negative_int(delay_months, "delay_months")

    inv = to_decimal_money(initial_investment)
    if inv <= ZERO_MONEY:
        raise ValueError("initial_investment must be strictly positive.")
    cfs = _parse_cashflows(monthly_cashflows)
    revenue = to_decimal_money(contract_revenue)
    cost = to_decimal_money(direct_delivery_cost)
    if revenue <= ZERO_MONEY:
        raise ValueError("contract_revenue must be strictly positive.")
    if cost < ZERO_MONEY:
        raise ValueError("direct_delivery_cost cannot be negative.")

    inaction_monthly = to_decimal_money(monthly_inaction_cost)
    if inaction_monthly < ZERO_MONEY:
        raise ValueError("monthly_inaction_cost cannot be negative.")

    target_margin = to_decimal_rate(target_gross_margin)
    if not (_ZERO <= target_margin <= _ONE):
        raise ValueError("target_gross_margin must be between 0 and 1.")

    m_rate = (
        to_decimal_rate(monthly_discount_rate)
        if monthly_discount_rate is not None
        else annual_to_monthly_rate(annual_discount_rate)
    )

    npv = compute_npv(inv, cfs, m_rate)
    irr_m = compute_irr(inv, cfs)
    irr_a = monthly_to_annual_rate(irr_m) if irr_m is not None else None
    pb_simple = compute_payback(inv, cfs, periodic_rate=Decimal("0"))
    pb_disc = compute_payback(inv, cfs, periodic_rate=m_rate)

    total_cf = sum(cfs, ZERO_MONEY)
    roi_pct = (((total_cf - inv) / inv) * _HUNDRED).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    gross_margin_ratio = ((revenue - cost) / revenue).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    gross_margin_pct = (gross_margin_ratio * _HUNDRED).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    target_margin_pct = (target_margin * _HUNDRED).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    margin_gate_passed = gross_margin_ratio >= target_margin

    cum_inaction = (inaction_monthly * Decimal(delay_months)).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    ann_inaction = (inaction_monthly * Decimal(12)).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )

    if npv <= ZERO_MONEY:
        verdict = "NO_GO"
    elif not margin_gate_passed:
        verdict = "REPRICE_MARGIN"
    else:
        verdict = "GO"

    return {
        "initial_investment": format_money(inv),
        "horizon_months": len(cfs),
        "monthly_discount_rate": f"{m_rate:.6f}",
        "npv": format_money(npv),
        "irr_monthly": f"{irr_m:.6f}" if irr_m is not None else None,
        "irr_annualized": f"{irr_a:.6f}" if irr_a is not None else None,
        "payback_simple_months": f"{pb_simple:.6f}" if pb_simple is not None else None,
        "payback_discounted_months": f"{pb_disc:.6f}" if pb_disc is not None else None,
        "roi_pct": f"{roi_pct:.6f}",
        "contract_revenue": format_money(revenue),
        "direct_delivery_cost": format_money(cost),
        "gross_margin_ratio": f"{gross_margin_ratio:.6f}",
        "gross_margin_pct": f"{gross_margin_pct:.6f}",
        "target_gross_margin_pct": f"{target_margin_pct:.6f}",
        "margin_gate_passed": margin_gate_passed,
        "monthly_inaction_cost": format_money(inaction_monthly),
        "delay_months": delay_months,
        "cumulative_inaction_cost": format_money(cum_inaction),
        "annualized_inaction_cost": format_money(ann_inaction),
        "verdict": verdict,
    }
