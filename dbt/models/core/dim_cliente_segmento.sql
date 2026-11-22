WITH clientes_ranqueados AS (
    SELECT
        cnpj_14,
        segmento_cliente,
        sigla_uf,
        CAST(lgd_aplicada AS DECIMAL(12, 6)) AS lgd_padrao,
        ROW_NUMBER() OVER (
            PARTITION BY cnpj_14
            ORDER BY data_referencia DESC, id_fatura_masked DESC
        ) AS rn
    FROM {{ ref('stg_receivables_aging') }}
)

SELECT
    cnpj_14,
    segmento_cliente,
    sigla_uf,
    lgd_padrao,
    CASE
        WHEN lgd_padrao <= CAST('0.400000' AS DECIMAL(12, 6)) THEN 'BAIXO'
        WHEN lgd_padrao <= CAST('0.550000' AS DECIMAL(12, 6)) THEN 'MEDIO'
        ELSE 'ALTO'
    END AS perfil_risco_lgd
FROM clientes_ranqueados
WHERE rn = 1
ORDER BY cnpj_14
