SELECT
    competencia,
    SUM(valor_debito_total) AS total_debito,
    SUM(valor_credito_total) AS total_credito
FROM {{ ref('fct_capex_opex_variance') }}
GROUP BY competencia
HAVING SUM(valor_debito_total) <> SUM(valor_credito_total)
