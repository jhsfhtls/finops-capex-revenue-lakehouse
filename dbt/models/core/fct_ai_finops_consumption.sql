SELECT
    t.id_lancamento,
    t.timestamp_utc,
    t.competencia,
    t.trace_id,
    t.span_id,
    t.parent_span_id,
    t.cod_centro_custo,
    cc.nome_centro_custo,
    t.repo,
    t.agent_role,
    t.skill,
    t.operation,
    t.model,
    m.provider,
    m.tier,
    m.has_billing_cliff,
    t.tokens_in,
    t.tokens_out,
    t.tokens_total,
    t.cached_tokens_in,
    t.cost_usd,
    t.cost_optimized_usd,
    t.savings_usd,
    t.duration_ms,
    t.status,
    t.notes
FROM {{ ref('stg_ai_finops_telemetry') }} AS t
INNER JOIN {{ ref('dim_llm_model') }} AS m
    ON t.model = m.model_id
INNER JOIN {{ ref('dim_centro_custo') }} AS cc
    ON t.cod_centro_custo = cc.cod_centro_custo
INNER JOIN {{ ref('dim_calendario_fiscal') }} AS cal
    ON t.competencia = cal.competencia
ORDER BY t.timestamp_utc, t.id_lancamento
