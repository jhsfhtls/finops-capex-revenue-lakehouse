"""Streamlit interactive showcase backed by FinOpsLakehouse SDK (zero IEEE 754 float)."""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from finops_lakehouse.sdk import FinOpsLakehouse

st.set_page_config(
    page_title="FinOps, CAPEX/OPEX & Revenue Recovery Lakehouse",
    layout="wide",
)


def main() -> None:
    """Render the interactive multi-tab FinOps Lakehouse BI showcase."""
    st.title("FinOps, CAPEX/OPEX & Revenue Recovery Lakehouse")
    st.caption(
        "100% deterministic synthetic data (seed=42) | Pure DECIMAL(18,4) SDK parity"
    )

    lakehouse = FinOpsLakehouse()
    status = lakehouse.get_status()
    st.sidebar.markdown("### Runtime Governance")
    st.sidebar.json(status)

    tab_capex, tab_aging, tab_b2b, tab_ai = st.tabs(
        [
            "1. CAPEX & OPEX Variance",
            "2. Receivables Aging & ECL",
            "3. B2B Deal Simulator",
            "4. AI/Cloud FinOps (POP_T_158)",
        ]
    )

    with tab_capex:
        col1, col2 = st.columns(2)
        tipo = col1.selectbox("Expense Type", ["ALL", "CAPEX", "OPEX"])
        limit_bv = col2.slider("Detail Rows", min_value=5, max_value=100, value=20, step=5)
        bv = lakehouse.budget_variance(
            tipo_despesa=None if tipo == "ALL" else tipo,
            limit=int(limit_bv),
        )
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Orcado (BRL)", bv["total_orcado"])
        m2.metric("Total Realizado (BRL)", bv["total_realizado"])
        m3.metric("Variacao Orcamentaria", bv["variacao_orcamentaria"])
        m4.metric("Variacao (%)", f"{bv['variacao_pct']}%")
        st.dataframe(bv["rows"], use_container_width=True)

    with tab_aging:
        prioridade = st.selectbox(
            "Priority Tier",
            ["ALL", "P1_CRITICA", "P2_ALTA", "P3_MEDIA", "P4_MONITORAR"],
        )
        ag = lakehouse.aging_priority(
            prioridade=None if prioridade == "ALL" else prioridade,
            limit=25,
        )
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Saldo em Aberto (EAD)", ag["total_em_aberto"])
        k2.metric("Total Vencido", ag["total_vencido"])
        k3.metric("Perda Esperada (ECL)", ag["total_ecl"])
        k4.metric("Reconciliacao", "OK" if ag["reconciliation_ok"] else "ALERT")
        st.subheader("Portfolio Buckets")
        st.json(ag["buckets"])
        st.subheader("Top Collection Priority Queue")
        st.dataframe(ag["top_priority_invoices"], use_container_width=True)

    with tab_b2b:
        c1, c2, c3 = st.columns(3)
        inv_int = int(c1.number_input("Initial Investment (BRL)", min_value=1000, value=100000, step=5000))
        months = int(c2.slider("Horizon (Months)", min_value=3, max_value=36, value=12, step=3))
        monthly_cf_int = int(c3.number_input("Monthly Cashflow (BRL)", min_value=500, value=22000, step=1000))
        rev_int = int(c1.number_input("Contract Revenue (BRL)", min_value=1000, value=300000, step=10000))
        cost_int = int(c2.number_input("Direct Delivery Cost (BRL)", min_value=0, value=90000, step=5000))
        hurdle_pct = int(c3.slider("Annual Discount Rate (%)", min_value=1, max_value=40, value=15, step=1))

        annual_rate = (Decimal(hurdle_pct) / Decimal(100)).quantize(Decimal("0.000001"))
        deal = lakehouse.b2b_roi(
            initial_investment=inv_int,
            monthly_cashflows=[monthly_cf_int for _ in range(months)],
            contract_revenue=rev_int,
            direct_delivery_cost=cost_int,
            annual_discount_rate=annual_rate,
        )
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("NPV (BRL)", deal["npv"])
        r2.metric("Gross Margin (%)", f"{deal['gross_margin_pct']}%")
        r3.metric("Payback Discounted (mo)", deal["payback_discounted_months"] or "N/A")
        r4.metric("Verdict", deal["verdict"])
        st.json(deal)

    with tab_ai:
        a1, a2, a3 = st.columns(3)
        cache_pct = int(a1.slider("Cache Hit Ratio (%)", min_value=0, max_value=95, value=40, step=5))
        batch_pct = int(a2.slider("Batch Eligible (%)", min_value=0, max_value=100, value=30, step=5))
        offpeak = a3.checkbox("Enable DeepSeek Off-Peak (-50%)", value=True)
        ai_res = lakehouse.ai_finops(
            cache_hit_ratio=(Decimal(cache_pct) / Decimal(100)).quantize(Decimal("0.000001")),
            batch_eligible_ratio=(Decimal(batch_pct) / Decimal(100)).quantize(Decimal("0.000001")),
            enable_deepseek_offpeak=offpeak,
        )
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Baseline Cost (USD)", ai_res["total_cost_usd"])
        s2.metric("Optimized Cost (USD)", ai_res["optimized_cost_usd"])
        s3.metric("Total Savings (USD)", ai_res["total_savings_usd"])
        s4.metric("Savings (%)", f"{ai_res['savings_pct']}%")
        st.subheader("Breakdown by POP_T_158 Tier (T1-T4)")
        st.json(ai_res["by_tier"])


if __name__ == "__main__":
    main()
