"""Deterministic CLT vs. PJ (Simples III/V + Fator R + ISS Manaus) vs. USD/EUR & Monte Carlo."""

from __future__ import annotations

import random
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    RATE_QUANT,
    ZERO_MONEY,
    format_money,
    to_decimal_money,
    to_decimal_rate,
)
from finops_lakehouse.gateway_bridge.macro_rates import (
    MacroRatesSnapshot,
    load_macro_rates,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_SCALE_10K = Decimal("10000")

# Statutory 2025/2026 Brazilian INSS progressive brackets (limit, rate).
_INSS_BRACKETS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("1518.00"), Decimal("0.075")),
    (Decimal("2793.88"), Decimal("0.090")),
    (Decimal("4190.83"), Decimal("0.120")),
    (Decimal("8157.41"), Decimal("0.140")),
)
_INSS_CEILING = Decimal("8157.41")

# Statutory progressive IRPF monthly table (upper_bound, rate, deduction).
_IRPF_BRACKETS: tuple[tuple[Decimal | None, Decimal, Decimal], ...] = (
    (Decimal("2428.80"), Decimal("0.000"), Decimal("0.00")),
    (Decimal("2826.65"), Decimal("0.075"), Decimal("182.16")),
    (Decimal("3751.05"), Decimal("0.150"), Decimal("394.16")),
    (Decimal("4664.68"), Decimal("0.225"), Decimal("675.49")),
    (None, Decimal("0.275"), Decimal("908.73")),
)
_IRPF_DEPENDENT_DEDUCTION = Decimal("189.59")
_IRPF_SIMPLIFIED_DISCOUNT = Decimal("607.20")

# Simples Nacional progressive brackets (rbt12_upper, nominal_rate, deduction).
_SIMPLES_ANEXO_III: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("180000.00"), Decimal("0.060"), Decimal("0.00")),
    (Decimal("360000.00"), Decimal("0.112"), Decimal("9360.00")),
    (Decimal("720000.00"), Decimal("0.135"), Decimal("17640.00")),
    (Decimal("1800000.00"), Decimal("0.160"), Decimal("35640.00")),
    (Decimal("3600000.00"), Decimal("0.210"), Decimal("125640.00")),
    (Decimal("4800000.00"), Decimal("0.330"), Decimal("648000.00")),
)

_SIMPLES_ANEXO_V: tuple[tuple[Decimal, Decimal, Decimal], ...] = (
    (Decimal("180000.00"), Decimal("0.155"), Decimal("0.00")),
    (Decimal("360000.00"), Decimal("0.180"), Decimal("4500.00")),
    (Decimal("720000.00"), Decimal("0.195"), Decimal("9900.00")),
    (Decimal("1800000.00"), Decimal("0.205"), Decimal("17100.00")),
    (Decimal("3600000.00"), Decimal("0.230"), Decimal("62100.00")),
    (Decimal("4800000.00"), Decimal("0.305"), Decimal("540000.00")),
)

DEFAULT_ISS_MANAUS_SIMPLES = Decimal("0.020100")
_EXPORT_PIS_COFINS_REDUCTION = Decimal("0.845000")
_FATOR_R_THRESHOLD = Decimal("0.280000")


def _compute_progressive_inss(gross: Decimal) -> Decimal:
    """Compute cumulative progressive Brazilian employee INSS capped at ceiling."""
    if gross <= ZERO_MONEY:
        return ZERO_MONEY

    total_inss = _ZERO
    prev_limit = _ZERO
    capped = min(gross, _INSS_CEILING)
    for limit, rate in _INSS_BRACKETS:
        if capped <= prev_limit:
            break
        slice_amt = min(capped, limit) - prev_limit
        total_inss += slice_amt * rate
        prev_limit = limit
    return total_inss.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)


def _compute_irpf(gross: Decimal, inss_paid: Decimal, dependents: int = 0) -> Decimal:
    """Compute monthly Brazilian IRPF choosing better of legal vs simplified deduction."""
    if gross <= ZERO_MONEY:
        return ZERO_MONEY

    legal_deduction = inss_paid + (Decimal(dependents) * _IRPF_DEPENDENT_DEDUCTION)
    effective_deduction = max(legal_deduction, _IRPF_SIMPLIFIED_DISCOUNT)
    taxable_base = max(_ZERO, gross - effective_deduction)

    for upper, rate, parcel_deduction in _IRPF_BRACKETS:
        if upper is None or taxable_base <= upper:
            tax = max(_ZERO, taxable_base * rate - parcel_deduction)
            return tax.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    return ZERO_MONEY


def _effective_simples_rate(rbt12: Decimal, anexo: str) -> Decimal:
    """Compute effective Simples Nacional DAS rate from RBT12 and Anexo III/V tables."""
    if rbt12 <= ZERO_MONEY:
        return Decimal("0.000000")

    table = _SIMPLES_ANEXO_III if anexo == "ANEXO_III" else _SIMPLES_ANEXO_V
    for upper, nominal, deduction in table:
        if rbt12 <= upper:
            eff = (rbt12 * nominal - deduction) / rbt12
            return max(_ZERO, eff).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)

    _, max_nom, max_ded = table[-1]
    eff = (rbt12 * max_nom - max_ded) / rbt12
    return max(_ZERO, eff).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def compute_clt_net_package(
    gross_monthly: Decimal | int | str,
    *,
    vr_va_monthly: Decimal | int | str = ZERO_MONEY,
    health_monthly: Decimal | int | str = ZERO_MONEY,
    other_benefits_monthly: Decimal | int | str = ZERO_MONEY,
    dependents: int = 0,
) -> dict[str, Any]:
    """Compute deterministic CLT monthly net cash, accruals (13th + 1/3 vac + FGTS), and total."""
    if isinstance(dependents, bool) or not isinstance(dependents, int) or dependents < 0:
        raise ValueError("dependents must be a non-negative integer.")

    gross = to_decimal_money(gross_monthly)
    if gross <= ZERO_MONEY:
        raise ValueError("gross_monthly must be strictly positive.")
    vr_va = to_decimal_money(vr_va_monthly)
    health = to_decimal_money(health_monthly)
    other_ben = to_decimal_money(other_benefits_monthly)
    if min(vr_va, health, other_ben) < ZERO_MONEY:
        raise ValueError("Benefit values cannot be negative.")

    inss = _compute_progressive_inss(gross)
    irpf = _compute_irpf(gross, inss, dependents=dependents)
    net_cash = (gross - inss - irpf).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

    thirteenth_net_monthly = (net_cash / Decimal(12)).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    vacation_bonus_net_monthly = (net_cash / Decimal(36)).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    fgts_monthly = (
        (gross + gross / Decimal(12) + gross / Decimal(36)) * Decimal("0.08")
    ).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    benefits_total = (vr_va + health + other_ben).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    total_monthly_equivalent = (
        net_cash
        + thirteenth_net_monthly
        + vacation_bonus_net_monthly
        + fgts_monthly
        + benefits_total
    ).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

    return {
        "regime": "CLT",
        "gross_monthly": format_money(gross),
        "inss_monthly": format_money(inss),
        "irpf_monthly": format_money(irpf),
        "net_cash_monthly": format_money(net_cash),
        "thirteenth_net_monthly": format_money(thirteenth_net_monthly),
        "vacation_bonus_net_monthly": format_money(vacation_bonus_net_monthly),
        "fgts_monthly": format_money(fgts_monthly),
        "benefits_monthly": format_money(benefits_total),
        "total_monthly_equivalent": format_money(total_monthly_equivalent),
    }


def compute_pj_net_package(
    gross_monthly: Decimal | int | str,
    *,
    pro_labore_ratio: Decimal | int | str = Decimal("0.280000"),
    iss_rate: Decimal | int | str = DEFAULT_ISS_MANAUS_SIMPLES,
    accounting_fee_monthly: Decimal | int | str = Decimal("350.0000"),
    benefits_cost_monthly: Decimal | int | str = ZERO_MONEY,
    export_service_exempt: bool = False,
) -> dict[str, Any]:
    """Compute PJ Simples Nacional net monthly value with Fator R & ISS Manaus parameterization."""
    gross = to_decimal_money(gross_monthly)
    if gross <= ZERO_MONEY:
        raise ValueError("gross_monthly must be strictly positive.")

    fator_r = to_decimal_rate(pro_labore_ratio)
    if not (_ZERO <= fator_r <= _ONE):
        raise ValueError("pro_labore_ratio must be within [0, 1].")

    iss = to_decimal_rate(iss_rate)
    if not (_ZERO <= iss <= Decimal("0.050000")):
        raise ValueError("iss_rate must be within [0, 0.05].")

    accounting = to_decimal_money(accounting_fee_monthly)
    benefits_cost = to_decimal_money(benefits_cost_monthly)
    if accounting < ZERO_MONEY or benefits_cost < ZERO_MONEY:
        raise ValueError("accounting_fee_monthly and benefits_cost_monthly cannot be negative.")

    anexo = "ANEXO_III" if fator_r >= _FATOR_R_THRESHOLD else "ANEXO_V"
    rbt12 = (gross * Decimal(12)).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    base_simples_rate = _effective_simples_rate(rbt12, anexo)

    if export_service_exempt:
        # Export of services: ISS + PIS/COFINS exemption per LC 116/2003 & Lei 10.637/10.833.
        applied_das_rate = (
            max(_ZERO, base_simples_rate - min(DEFAULT_ISS_MANAUS_SIMPLES, base_simples_rate))
            * _EXPORT_PIS_COFINS_REDUCTION
        ).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    else:
        iss_delta = iss - DEFAULT_ISS_MANAUS_SIMPLES
        applied_das_rate = max(_ZERO, base_simples_rate + iss_delta).quantize(
            RATE_QUANT, rounding=ROUND_HALF_EVEN
        )

    das_tax = (gross * applied_das_rate).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    pro_labore = (gross * fator_r).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    inss_pro_labore = (min(pro_labore, _INSS_CEILING) * Decimal("0.11")).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    irpf_pro_labore = _compute_irpf(pro_labore, inss_pro_labore, dependents=0)

    total_taxes = (das_tax + inss_pro_labore + irpf_pro_labore).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    effective_tax_rate = (total_taxes / gross).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    net_monthly = (gross - total_taxes - accounting - benefits_cost).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )

    return {
        "regime": f"PJ_SIMPLES_{anexo}",
        "anexo": anexo,
        "fator_r": f"{fator_r:.6f}",
        "iss_rate": f"{iss:.6f}",
        "export_service_exempt": export_service_exempt,
        "gross_monthly": format_money(gross),
        "rbt12": format_money(rbt12),
        "applied_das_rate": f"{applied_das_rate:.6f}",
        "das_tax_monthly": format_money(das_tax),
        "pro_labore_monthly": format_money(pro_labore),
        "inss_pro_labore_monthly": format_money(inss_pro_labore),
        "irpf_pro_labore_monthly": format_money(irpf_pro_labore),
        "total_taxes_monthly": format_money(total_taxes),
        "effective_tax_rate": f"{effective_tax_rate:.6f}",
        "accounting_fee_monthly": format_money(accounting),
        "benefits_cost_monthly": format_money(benefits_cost),
        "net_monthly": format_money(net_monthly),
    }


def compute_contractor_fx_net(
    monthly_fx_gross: Decimal | int | str,
    *,
    currency: str = "USD",
    macro_rates: MacroRatesSnapshot | None = None,
    fx_spread_rate: Decimal | int | str = Decimal("0.010000"),
    pro_labore_ratio: Decimal | int | str = Decimal("0.280000"),
    accounting_fee_monthly: Decimal | int | str = Decimal("350.0000"),
    benefits_cost_monthly: Decimal | int | str = ZERO_MONEY,
) -> dict[str, Any]:
    """Compute net BRL monthly package for international USD/EUR B2B contractor."""
    if not isinstance(currency, str):
        raise TypeError("currency must be a string ('USD' or 'EUR').")
    curr = currency.strip().upper()
    if curr not in {"USD", "EUR"}:
        raise ValueError("currency must be 'USD' or 'EUR'.")

    fx_gross = to_decimal_money(monthly_fx_gross)
    if fx_gross <= ZERO_MONEY:
        raise ValueError("monthly_fx_gross must be strictly positive.")

    spread = to_decimal_rate(fx_spread_rate)
    if not (_ZERO <= spread <= Decimal("0.100000")):
        raise ValueError("fx_spread_rate must be within [0, 0.10].")

    macro = macro_rates or load_macro_rates(prefer_live_gateway=False)
    ptax_rate = macro.ptax_usd_brl if curr == "USD" else macro.ptax_eur_brl
    effective_fx_rate = (ptax_rate * (_ONE - spread)).quantize(
        RATE_QUANT, rounding=ROUND_HALF_EVEN
    )
    gross_brl = (fx_gross * effective_fx_rate).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

    pj_res = compute_pj_net_package(
        gross_brl,
        pro_labore_ratio=pro_labore_ratio,
        iss_rate=DEFAULT_ISS_MANAUS_SIMPLES,
        accounting_fee_monthly=accounting_fee_monthly,
        benefits_cost_monthly=benefits_cost_monthly,
        export_service_exempt=True,
    )
    return {
        **pj_res,
        "regime": f"CONTRACTOR_{curr}_EXPORT",
        "currency": curr,
        "gross_fx_monthly": format_money(fx_gross),
        "ptax_rate": f"{ptax_rate:.6f}",
        "fx_spread_rate": f"{spread:.6f}",
        "effective_fx_rate": f"{effective_fx_rate:.6f}",
        "macro_reference_date": macro.reference_date,
        "macro_source": macro.source,
    }


def _scaled_percentile(sorted_vals: list[int], pct: int) -> Decimal:
    idx = min(len(sorted_vals) - 1, max(0, (len(sorted_vals) * pct) // 100))
    return (Decimal(sorted_vals[idx]) / _SCALE_10K).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )


def run_career_monte_carlo(
    clt_net_monthly: Decimal | int | str,
    pj_or_fx_base_net_monthly: Decimal | int | str,
    *,
    n_scenarios: int = 10_000,
    seed: int = 42,
    fx_volatility_bps: int = 1200,
    inflation_volatility_bps: int = 200,
) -> dict[str, Any]:
    """Run deterministic int64-scaled Monte Carlo simulation (zero IEEE 754 float)."""
    if isinstance(n_scenarios, bool) or not isinstance(n_scenarios, int) or n_scenarios <= 0:
        raise ValueError("n_scenarios must be a positive integer.")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer.")
    for name, bps in (
        ("fx_volatility_bps", fx_volatility_bps),
        ("inflation_volatility_bps", inflation_volatility_bps),
    ):
        if isinstance(bps, bool) or not isinstance(bps, int) or bps < 0:
            raise ValueError(f"{name} must be a non-negative integer.")

    clt_base = to_decimal_money(clt_net_monthly)
    cand_base = to_decimal_money(pj_or_fx_base_net_monthly)
    if clt_base <= ZERO_MONEY or cand_base <= ZERO_MONEY:
        raise ValueError("clt_net_monthly and pj_or_fx_base_net_monthly must be > 0.")

    clt_scaled = int((clt_base * _SCALE_10K).to_integral_value(rounding=ROUND_HALF_EVEN))
    base_scaled = int((cand_base * _SCALE_10K).to_integral_value(rounding=ROUND_HALF_EVEN))

    rng = random.Random(seed)
    outcomes: list[int] = []
    beats_clt = 0
    total_scaled = 0

    for _ in range(n_scenarios):
        # Sum of 12 Uniform(-5000, 5000) integers yields zero-mean std=10000 without float.
        z_fx = sum(rng.randint(-5000, 5000) for _ in range(12))
        z_inf = sum(rng.randint(-5000, 5000) for _ in range(12))

        fx_factor_bps = max(2000, 10_000 + (fx_volatility_bps * z_fx) // 10_000)
        inf_factor_bps = max(5000, 10_000 - (inflation_volatility_bps * z_inf) // 10_000)
        sim_val = (base_scaled * fx_factor_bps * inf_factor_bps) // 100_000_000

        outcomes.append(sim_val)
        total_scaled += sim_val
        if sim_val > clt_scaled:
            beats_clt += 1

    outcomes.sort()
    mean_val = (Decimal(total_scaled) / (Decimal(n_scenarios) * _SCALE_10K)).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )
    prob_beats = (Decimal(beats_clt) / Decimal(n_scenarios)).quantize(
        RATE_QUANT, rounding=ROUND_HALF_EVEN
    )

    return {
        "n_scenarios": n_scenarios,
        "seed": seed,
        "clt_baseline": format_money(clt_base),
        "p10": format_money(_scaled_percentile(outcomes, 10)),
        "p25": format_money(_scaled_percentile(outcomes, 25)),
        "p50": format_money(_scaled_percentile(outcomes, 50)),
        "p75": format_money(_scaled_percentile(outcomes, 75)),
        "p90": format_money(_scaled_percentile(outcomes, 90)),
        "mean": format_money(mean_val),
        "prob_beats_clt": f"{prob_beats:.6f}",
    }


def compare_career_offers(
    *,
    clt_gross_monthly: Decimal | int | str,
    pj_gross_monthly: Decimal | int | str,
    contractor_fx_monthly: Decimal | int | str,
    fx_currency: str = "USD",
    vr_va_monthly: Decimal | int | str = Decimal("1200.0000"),
    health_monthly: Decimal | int | str = Decimal("800.0000"),
    iss_rate: Decimal | int | str = DEFAULT_ISS_MANAUS_SIMPLES,
    pro_labore_ratio: Decimal | int | str = Decimal("0.280000"),
    macro_rates: MacroRatesSnapshot | None = None,
    seed: int = 42,
    n_scenarios: int = 10_000,
) -> dict[str, Any]:
    """Compare CLT, PJ Simples Nacional, and International USD/EUR with 10k Monte Carlo."""
    macro = macro_rates or load_macro_rates(prefer_live_gateway=False)
    clt = compute_clt_net_package(
        clt_gross_monthly,
        vr_va_monthly=vr_va_monthly,
        health_monthly=health_monthly,
    )
    pj = compute_pj_net_package(
        pj_gross_monthly,
        pro_labore_ratio=pro_labore_ratio,
        iss_rate=iss_rate,
    )
    fx = compute_contractor_fx_net(
        contractor_fx_monthly,
        currency=fx_currency,
        macro_rates=macro,
        pro_labore_ratio=pro_labore_ratio,
    )
    mc = run_career_monte_carlo(
        Decimal(clt["total_monthly_equivalent"]),
        Decimal(fx["net_monthly"]),
        n_scenarios=n_scenarios,
        seed=seed,
    )

    clt_eq = Decimal(clt["total_monthly_equivalent"])
    pj_net = Decimal(pj["net_monthly"])
    fx_net = Decimal(fx["net_monthly"])
    pj_gross = to_decimal_money(pj_gross_monthly)
    fx_gross = to_decimal_money(contractor_fx_monthly)

    breakeven_pj_gross = ((clt_eq / pj_net) * pj_gross).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    ) if pj_net > ZERO_MONEY else None
    breakeven_fx_gross = ((clt_eq / fx_net) * fx_gross).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    ) if fx_net > ZERO_MONEY else None

    return {
        "clt": clt,
        "pj": pj,
        "contractor_fx": fx,
        "monte_carlo_fx_vs_clt": mc,
        "breakeven_pj_gross_brl": format_money(breakeven_pj_gross) if breakeven_pj_gross else None,
        "breakeven_contractor_fx": format_money(breakeven_fx_gross) if breakeven_fx_gross else None,
    }
