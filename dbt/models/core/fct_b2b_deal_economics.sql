SELECT
    d.id_contrato_masked,
    d.id_cliente_masked,
    d.competencia,
    d.cod_centro_custo,
    cc.nome_centro_custo,
    d.segmento_cliente,
    d.moeda,
    d.initial_investment,
    d.horizon_months,
    d.monthly_discount_rate,
    d.npv,
    d.irr_monthly,
    d.irr_annualized,
    d.payback_simple_months,
    d.payback_discounted_months,
    d.roi_pct,
    d.contract_revenue,
    d.direct_delivery_cost,
    d.gross_margin_ratio,
    d.gross_margin_pct,
    d.target_gross_margin_pct,
    d.margin_gate_passed,
    d.monthly_inaction_cost,
    d.delay_months,
    d.cumulative_inaction_cost,
    d.annualized_inaction_cost,
    d.verdict
FROM {{ ref('stg_b2b_deal_economics') }} AS d
INNER JOIN {{ ref('dim_centro_custo') }} AS cc
    ON d.cod_centro_custo = cc.cod_centro_custo
INNER JOIN {{ ref('dim_calendario_fiscal') }} AS cal
    ON d.competencia = cal.competencia
ORDER BY d.competencia, d.id_contrato_masked
