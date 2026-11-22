SELECT
    id_lancamento,
    cost_usd,
    cost_optimized_usd,
    savings_usd
FROM {{ ref('fct_ai_finops_consumption') }}
WHERE cost_usd - cost_optimized_usd <> savings_usd
