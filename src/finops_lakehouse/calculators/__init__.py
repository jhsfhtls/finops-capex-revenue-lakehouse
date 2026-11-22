"""Deterministic financial calculators (B2B Deal Economics & Career Tax Parity)."""

from finops_lakehouse.calculators.b2b_economics import (
    annual_to_monthly_rate,
    compute_irr,
    compute_npv,
    compute_payback,
    evaluate_b2b_deal,
    monthly_to_annual_rate,
)
from finops_lakehouse.calculators.career_parity import (
    DEFAULT_ISS_MANAUS_SIMPLES,
    compare_career_offers,
    compute_clt_net_package,
    compute_contractor_fx_net,
    compute_pj_net_package,
    run_career_monte_carlo,
)

__all__ = [
    "DEFAULT_ISS_MANAUS_SIMPLES",
    "annual_to_monthly_rate",
    "compare_career_offers",
    "compute_clt_net_package",
    "compute_contractor_fx_net",
    "compute_irr",
    "compute_npv",
    "compute_payback",
    "compute_pj_net_package",
    "evaluate_b2b_deal",
    "monthly_to_annual_rate",
    "run_career_monte_carlo",
]
