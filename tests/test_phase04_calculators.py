"""Offline golden-case tests for Phase 04 calculators and public macro bridge."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finops_lakehouse.calculators.b2b_economics import (
    annual_to_monthly_rate,
    compute_irr,
    compute_npv,
    compute_payback,
    evaluate_b2b_deal,
)
from finops_lakehouse.calculators.career_parity import (
    compare_career_offers,
    compute_clt_net_package,
    compute_contractor_fx_net,
    compute_pj_net_package,
    run_career_monte_carlo,
)
from finops_lakehouse.gateway_bridge.macro_rates import (
    MacroRatesSnapshot,
    load_macro_rates,
    macro_snapshot_to_dict,
)


def test_b2b_golden_case_hand_verified_npv_irr_payback() -> None:
    """Hand-verified case: I0=10000, CF=[4000,4000,4000], r=10% -> NPV=-52.5920, IRR=9.7010%."""
    cfs = [Decimal("4000"), Decimal("4000"), Decimal("4000")]
    npv = compute_npv(Decimal("10000"), cfs, Decimal("0.10"))
    assert abs(npv - Decimal("-52.5920")) <= Decimal("0.0001")

    irr = compute_irr(Decimal("10000"), cfs)
    assert irr is not None
    assert abs(irr - Decimal("0.097010")) <= Decimal("0.000010")

    pb_simple = compute_payback(Decimal("10000"), cfs, periodic_rate=Decimal("0"))
    assert pb_simple == Decimal("2.500000")

    pb_disc = compute_payback(Decimal("10000"), cfs, periodic_rate=Decimal("0.05"))
    assert pb_disc is not None
    assert pb_disc > pb_simple


def test_b2b_deal_evaluation_margin_gate_and_inaction_cost() -> None:
    m_rate = annual_to_monthly_rate(Decimal("0.120000"))
    # (1 + m_rate)^12 == 1.120000 within 1e-5
    assert abs(((Decimal("1") + m_rate) ** 12) - Decimal("1.12")) <= Decimal("0.000010")

    go_deal = evaluate_b2b_deal(
        initial_investment="50000",
        monthly_cashflows=["12000"] * 6,
        contract_revenue="120000",
        direct_delivery_cost="36000",
        annual_discount_rate="0.120000",
        monthly_inaction_cost="15000",
        delay_months=3,
    )
    assert go_deal["gross_margin_pct"] == "70.000000"
    assert go_deal["margin_gate_passed"] is True
    assert go_deal["verdict"] == "GO"
    assert go_deal["cumulative_inaction_cost"] == "45000.0000"
    assert go_deal["annualized_inaction_cost"] == "180000.0000"
    assert Decimal(go_deal["npv"]) > Decimal("0.0000")

    reprice_deal = evaluate_b2b_deal(
        initial_investment="50000",
        monthly_cashflows=["12000"] * 6,
        contract_revenue="120000",
        direct_delivery_cost="48000",
        annual_discount_rate="0.120000",
    )
    assert reprice_deal["gross_margin_pct"] == "60.000000"
    assert reprice_deal["margin_gate_passed"] is False
    assert reprice_deal["verdict"] == "REPRICE_MARGIN"

    nogo_deal = evaluate_b2b_deal(
        initial_investment="90000",
        monthly_cashflows=["12000"] * 6,
        contract_revenue="120000",
        direct_delivery_cost="36000",
        annual_discount_rate="0.120000",
    )
    assert nogo_deal["verdict"] == "NO_GO"


def test_clt_golden_case_progressive_inss_and_irpf() -> None:
    high_clt = compute_clt_net_package(
        "15000.0000",
        vr_va_monthly="1200.0000",
        health_monthly="800.0000",
    )
    # Exact cumulative INSS ceiling across 4 brackets = 951.6344
    assert high_clt["inss_monthly"] == "951.6344"
    # IRPF: (15000 - 951.6344) * 0.275 - 908.73 = 2954.5705
    assert high_clt["irpf_monthly"] == "2954.5705"
    assert high_clt["net_cash_monthly"] == "11093.7951"
    assert Decimal(high_clt["total_monthly_equivalent"]) > Decimal(high_clt["net_cash_monthly"])

    # Low salary chooses simplified IRPF discount (607.20) resulting in zero IRPF
    low_clt = compute_clt_net_package("3000.0000")
    assert low_clt["irpf_monthly"] == "0.0000"


def test_pj_fator_r_and_iss_manaus_golden_case() -> None:
    # RBT12 = 20,000 * 12 = 240,000 (Bracket 2)
    pj_iii = compute_pj_net_package("20000.0000", pro_labore_ratio="0.280000")
    pj_v = compute_pj_net_package("20000.0000", pro_labore_ratio="0.270000")

    assert pj_iii["anexo"] == "ANEXO_III"
    assert pj_iii["applied_das_rate"] == "0.073000"
    assert pj_iii["das_tax_monthly"] == "1460.0000"

    assert pj_v["anexo"] == "ANEXO_V"
    assert pj_v["applied_das_rate"] == "0.161250"
    assert pj_v["das_tax_monthly"] == "3225.0000"
    assert Decimal(pj_v["net_monthly"]) < Decimal(pj_iii["net_monthly"])

    # Custom ISS 5.00% vs Manaus Simples default 2.01% adds exactly +2.99% (+598.0000/mo)
    pj_iss5 = compute_pj_net_package(
        "20000.0000",
        pro_labore_ratio="0.280000",
        iss_rate="0.050000",
    )
    assert Decimal(pj_iss5["das_tax_monthly"]) - Decimal(pj_iii["das_tax_monthly"]) == Decimal(
        "598.0000"
    )


def test_macro_bridge_offline_fallback_and_contractor_fx() -> None:
    snap = load_macro_rates(prefer_live_gateway=False)
    serialized = macro_snapshot_to_dict(snap)
    assert serialized["source"] == "offline_reference_snapshot"
    assert snap.ptax_usd_brl == Decimal("5.500000")
    assert snap.ptax_eur_brl == Decimal("6.000000")

    # Missing gateway directory falls back cleanly to offline reference
    fallback = load_macro_rates(
        gateway_dir=Path("/nonexistent/gateway/data"),
        prefer_live_gateway=True,
    )
    assert fallback == snap

    fx_usd = compute_contractor_fx_net("5000.0000", currency="USD", macro_rates=snap)
    fx_eur = compute_contractor_fx_net("5000.0000", currency="EUR", macro_rates=snap)
    assert fx_usd["effective_fx_rate"] == "5.445000"
    assert fx_usd["gross_monthly"] == "27225.0000"
    assert fx_usd["export_service_exempt"] is True
    assert Decimal(fx_eur["net_monthly"]) > Decimal(fx_usd["net_monthly"])


def test_monte_carlo_10k_determinism_and_compare_offers() -> None:
    mc_a = run_career_monte_carlo("13000.0000", "21000.0000", n_scenarios=10_000, seed=42)
    mc_b = run_career_monte_carlo("13000.0000", "21000.0000", n_scenarios=10_000, seed=42)
    mc_other = run_career_monte_carlo("13000.0000", "21000.0000", n_scenarios=10_000, seed=99)

    assert mc_a == mc_b
    assert mc_a["p50"] != mc_other["p50"]
    assert (
        Decimal(mc_a["p10"])
        <= Decimal(mc_a["p25"])
        <= Decimal(mc_a["p50"])
        <= Decimal(mc_a["p75"])
        <= Decimal(mc_a["p90"])
    )
    assert Decimal("0.000000") <= Decimal(mc_a["prob_beats_clt"]) <= Decimal("1.000000")

    comparison = compare_career_offers(
        clt_gross_monthly="15000",
        pj_gross_monthly="22000",
        contractor_fx_monthly="5500",
        fx_currency="USD",
        seed=42,
        n_scenarios=10_000,
    )
    assert comparison["breakeven_pj_gross_brl"] is not None
    assert comparison["breakeven_contractor_fx"] is not None
    assert comparison["monte_carlo_fx_vs_clt"]["n_scenarios"] == 10_000


def test_calculators_strictly_forbid_floats() -> None:
    with pytest.raises(TypeError):
        compute_npv(10000.0, [4000], "0.10")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_irr("10000", [4000.0, 4000])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        compute_clt_net_package(15000.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_pj_net_package("20000", iss_rate=0.02)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        run_career_monte_carlo(13000.0, "20000")  # type: ignore[arg-type]
    assert isinstance(MacroRatesSnapshot.__name__, str)


def test_macro_bridge_parquet_reader_handles_multi_series(tmp_path: Path) -> None:
    ptax_dir = tmp_path / "bcb_olinda_ptax" / "silver"
    sgs_dir = tmp_path / "bcb_sgs" / "silver"
    ptax_dir.mkdir(parents=True)
    sgs_dir.mkdir(parents=True)

    pq.write_table(
        pa.table(
            {
                "simbolo_moeda": ["USD", "USD"],
                "data_cotacao": ["2026-10-01", "2026-10-02"],
                "data_hora_cotacao": ["2026-10-01 13:00:00", "2026-10-02 13:05:00"],
                "tipo_boletim": ["Fechamento PTAX", "Fechamento PTAX"],
                "cotacao_venda": ["5.4000", "5.5200"],
            }
        ),
        ptax_dir / "ptax_cotacao_dolar.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "simbolo_moeda": ["EUR"],
                "data_cotacao": ["2026-10-02"],
                "data_hora_cotacao": ["2026-10-02 13:05:00"],
                "tipo_boletim": ["Fechamento PTAX"],
                "cotacao_venda": ["6.1100"],
            }
        ),
        ptax_dir / "ptax_cotacao_moedas.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "id_serie": ["11", "432"],
                "data_referencia": ["2026-10-02", "2026-10-02"],
                "valor": ["0.050788", "13.75"],
            }
        ),
        sgs_dir / "sgs_selic.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "id_serie": ["433", "13522"],
                "data_referencia": ["2026-09-01", "2026-09-01"],
                "valor": ["0.82", "4.58"],
            }
        ),
        sgs_dir / "sgs_ipca.parquet",
    )

    loaded = load_macro_rates(gateway_dir=tmp_path, prefer_live_gateway=True)
    assert loaded.source == "public_data_gateway_silver"
    assert loaded.reference_date == "2026-10-02"
    assert loaded.ptax_usd_brl == Decimal("5.520000")
    assert loaded.ptax_eur_brl == Decimal("6.110000")
    assert loaded.selic_annual_pct == Decimal("13.750000")
    assert loaded.ipca_12m_pct == Decimal("4.580000")


def test_phase04_edge_cases_irr_rates_fallback_series_and_guards(tmp_path: Path) -> None:
    # 1. IRR edge cases: no positive cashflows -> None; deep-loss deal -> negative IRR
    assert compute_irr("10000", ["-1000", "0"]) is None
    assert compute_irr("0", ["4000"]) is None
    loss_irr = compute_irr("10000", ["50", "50"])
    assert loss_irr is not None
    assert loss_irr < Decimal("-0.800000")

    # 2. Zero/negative periodic discount rates & str cashflows rejection
    assert compute_npv("1000", ["600", "600"], "0") == Decimal("200.0000")
    assert compute_npv("1000", ["600", "600"], "-0.10") > Decimal("200.0000")
    with pytest.raises(ValueError, match="greater than -1"):
        compute_npv("1000", ["600"], "-1.0")
    with pytest.raises(ValueError, match="non-empty sequence"):
        compute_npv("1000", "4000", "0.10")  # type: ignore[arg-type]

    # 3. Monte Carlo rejects float/bool/negative bps and non-string currency
    with pytest.raises(ValueError, match="fx_volatility_bps"):
        run_career_monte_carlo("10000", "15000", fx_volatility_bps=1200.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inflation_volatility_bps"):
        run_career_monte_carlo("10000", "15000", inflation_volatility_bps=True)
    with pytest.raises(TypeError, match="currency"):
        compute_contractor_fx_net("5000", currency=123)  # type: ignore[arg-type]

    # 4. Macro bridge fallback to daily Selic ("11") & monthly IPCA ("433") + null filtering
    ptax_dir = tmp_path / "bcb_olinda_ptax" / "silver"
    sgs_dir = tmp_path / "bcb_sgs" / "silver"
    ptax_dir.mkdir(parents=True)
    sgs_dir.mkdir(parents=True)

    pq.write_table(
        pa.table(
            {
                "simbolo_moeda": ["USD", "USD"],
                "data_cotacao": ["2026-10-02", "2026-10-03"],
                "data_hora_cotacao": ["2026-10-02 13:00:00", "2026-10-03 10:00:00"],
                "tipo_boletim": ["Fechamento PTAX", "Abertura"],
                "cotacao_venda": ["5.4500", None],
            }
        ),
        ptax_dir / "ptax_cotacao_dolar.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "simbolo_moeda": ["EUR"],
                "data_cotacao": ["2026-10-02"],
                "data_hora_cotacao": ["2026-10-02 13:00:00"],
                "tipo_boletim": ["Fechamento PTAX"],
                "cotacao_venda": ["6.0500"],
            }
        ),
        ptax_dir / "ptax_cotacao_moedas.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "id_serie": ["11"],
                "data_referencia": ["2026-10-02"],
                "valor": ["0.050788"],
            }
        ),
        sgs_dir / "sgs_selic.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "id_serie": ["433"] * 12,
                "data_referencia": [f"2026-{m:02d}-01" for m in range(1, 13)],
                "valor": ["0.40"] * 12,
            }
        ),
        sgs_dir / "sgs_ipca.parquet",
    )

    fb = load_macro_rates(gateway_dir=tmp_path, prefer_live_gateway=True)
    assert fb.ptax_usd_brl == Decimal("5.450000")
    assert Decimal("13.000000") < fb.selic_annual_pct < Decimal("14.500000")
    # (1.004^12 - 1) * 100 ~= 4.907021
    assert abs(fb.ipca_12m_pct - Decimal("4.907021")) <= Decimal("0.000010")
