WITH aggregated AS (
    SELECT
        l.competencia,
        l.cod_centro_custo,
        l.cod_conta_contabil,
        l.tipo_despesa,
        l.moeda,
        CAST(SUM(l.valor_orcado) AS DECIMAL(18, 4)) AS valor_orcado_total,
        CAST(SUM(l.valor_debito) AS DECIMAL(18, 4)) AS valor_debito_total,
        CAST(SUM(l.valor_credito) AS DECIMAL(18, 4)) AS valor_credito_total,
        CAST(COUNT(*) AS INTEGER) AS qtd_lancamentos
    FROM {{ ref('stg_capex_opex_ledger') }} AS l
    INNER JOIN {{ ref('dim_calendario_fiscal') }} AS cal
        ON l.competencia = cal.competencia
    INNER JOIN {{ ref('dim_centro_custo') }} AS cc
        ON l.cod_centro_custo = cc.cod_centro_custo
    INNER JOIN {{ ref('dim_conta_contabil') }} AS cta
        ON l.cod_conta_contabil = cta.cod_conta_contabil
    GROUP BY
        l.competencia,
        l.cod_centro_custo,
        l.cod_conta_contabil,
        l.tipo_despesa,
        l.moeda
)

SELECT
    competencia || '|' || cod_centro_custo || '|' || cod_conta_contabil || '|' || tipo_despesa || '|' || moeda
        AS sk_capex_opex_variance,
    competencia,
    cod_centro_custo,
    cod_conta_contabil,
    tipo_despesa,
    moeda,
    valor_orcado_total,
    valor_debito_total,
    valor_credito_total,
    valor_debito_total AS valor_realizado_total,
    CAST(valor_orcado_total - valor_debito_total AS DECIMAL(18, 4)) AS variacao_orcamentaria,
    CASE
        WHEN valor_orcado_total > CAST('0.0000' AS DECIMAL(18, 4))
            THEN {{ decimal_divide('(valor_orcado_total - valor_debito_total) * CAST(100 AS DECIMAL(5, 0))', 'valor_orcado_total', 6) }}
        ELSE CAST('0.000000' AS DECIMAL(12, 6))
    END AS variacao_pct,
    qtd_lancamentos
FROM aggregated
ORDER BY competencia, cod_centro_custo, cod_conta_contabil, tipo_despesa, moeda
