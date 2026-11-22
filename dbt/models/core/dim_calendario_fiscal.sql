WITH competencias_unificadas AS (
    SELECT competencia FROM {{ ref('stg_capex_opex_ledger') }}
    UNION
    SELECT competencia FROM {{ ref('stg_receivables_aging') }}
    UNION
    SELECT competencia FROM {{ ref('stg_b2b_deal_economics') }}
    UNION
    SELECT competencia FROM {{ ref('stg_ai_finops_telemetry') }}
),

parsed AS (
    SELECT
        competencia,
        CAST(SUBSTRING(competencia, 1, 4) AS INTEGER) AS ano_fiscal,
        CAST(SUBSTRING(competencia, 6, 2) AS INTEGER) AS mes_fiscal
    FROM competencias_unificadas
)

SELECT
    competencia,
    ano_fiscal,
    mes_fiscal,
    CASE
        WHEN mes_fiscal <= 3 THEN 1
        WHEN mes_fiscal <= 6 THEN 2
        WHEN mes_fiscal <= 9 THEN 3
        ELSE 4
    END AS trimestre_fiscal,
    CASE
        WHEN mes_fiscal <= 6 THEN 1
        ELSE 2
    END AS semestre_fiscal,
    SUBSTRING(competencia, 1, 4) || '-Q' || (
        CASE
            WHEN mes_fiscal <= 3 THEN '1'
            WHEN mes_fiscal <= 6 THEN '2'
            WHEN mes_fiscal <= 9 THEN '3'
            ELSE '4'
        END
    ) AS label_trimestre,
    CAST(competencia || '-01' AS DATE) AS data_inicio_mes
FROM parsed
ORDER BY competencia
