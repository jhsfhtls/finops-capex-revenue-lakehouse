SELECT
    CAST(id_lancamento AS VARCHAR) AS id_lancamento,
    CAST(id_lote AS VARCHAR) AS id_lote,
    CAST(competencia AS VARCHAR) AS competencia,
    CAST(data_lancamento AS DATE) AS data_lancamento,
    CAST(cod_centro_custo AS VARCHAR) AS cod_centro_custo,
    CAST(cod_conta_contabil AS VARCHAR) AS cod_conta_contabil,
    CAST(id_contrato_masked AS VARCHAR) AS id_contrato_masked,
    CAST(tipo_despesa AS VARCHAR) AS tipo_despesa,
    CAST(moeda AS VARCHAR) AS moeda,
    CAST(valor_orcado AS DECIMAL(18, 4)) AS valor_orcado,
    CAST(valor_debito AS DECIMAL(18, 4)) AS valor_debito,
    CAST(valor_credito AS DECIMAL(18, 4)) AS valor_credito,
    CAST(descricao AS VARCHAR) AS descricao
FROM {{ source('capex_opex_ledger', 'ledger_entries') }}
WHERE id_lancamento IS NOT NULL
