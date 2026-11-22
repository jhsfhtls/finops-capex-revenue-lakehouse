WITH contas_unicas AS (
    SELECT DISTINCT cod_conta_contabil
    FROM {{ ref('stg_capex_opex_ledger') }}
)

SELECT
    cod_conta_contabil,
    CASE
        WHEN cod_conta_contabil LIKE '1.02%' THEN 'ATIVO_IMOBILIZADO'
        WHEN cod_conta_contabil LIKE '1.01%' THEN 'DISPONIBILIDADES'
        WHEN cod_conta_contabil LIKE '2.01%' THEN 'PASSIVO_FORNECEDORES'
        WHEN cod_conta_contabil LIKE '4.01%' THEN 'DESPESAS_OPERACIONAIS'
        ELSE 'OUTRAS_CONTAS'
    END AS grupo_contabil,
    CASE
        WHEN cod_conta_contabil LIKE '1.02%' THEN 'CAPEX'
        WHEN cod_conta_contabil LIKE '4.01%' THEN 'OPEX'
        ELSE 'CONTRAPARTIDA'
    END AS tipo_natureza,
    CASE cod_conta_contabil
        WHEN '1.01.01.001' THEN 'Caixa e Equivalentes Bancarios'
        WHEN '1.02.01.001' THEN 'Ativos de Infraestrutura Cloud Reservada'
        WHEN '1.02.01.002' THEN 'Hardware & Clusters Dedicados'
        WHEN '1.02.02.001' THEN 'Licencas & Ativos Intangiveis Capitalizaveis'
        WHEN '2.01.01.001' THEN 'Fornecedores a Pagar'
        WHEN '4.01.01.001' THEN 'Servicos Cloud & Observabilidade'
        WHEN '4.01.01.002' THEN 'Pessoal Tecnico & Consultoria'
        WHEN '4.01.02.001' THEN 'Licenciamento SaaS Recorrente'
        WHEN '4.01.03.001' THEN 'Operacao Administrativa & Comercial'
        ELSE 'Conta Contabil ' || cod_conta_contabil
    END AS descricao_conta
FROM contas_unicas
ORDER BY cod_conta_contabil
