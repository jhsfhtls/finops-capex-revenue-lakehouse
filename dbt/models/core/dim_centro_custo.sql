WITH centros_unificados AS (
    SELECT cod_centro_custo FROM {{ ref('stg_capex_opex_ledger') }}
    UNION
    SELECT cod_centro_custo FROM {{ ref('stg_b2b_deal_economics') }}
    UNION
    SELECT cod_centro_custo FROM {{ ref('stg_ai_finops_telemetry') }}
)

SELECT
    cod_centro_custo,
    CASE
        WHEN cod_centro_custo LIKE 'CC-AI-%' THEN 'AI_PLATFORM'
        ELSE 'FIN_CORPORATIVO'
    END AS dominio_origem,
    CASE
        WHEN cod_centro_custo IN ('CC-0100-INFRA', 'CC-0300-ENG') THEN 'CAPEX'
        ELSE 'OPEX'
    END AS classificacao_padrao,
    CASE cod_centro_custo
        WHEN 'CC-0100-INFRA' THEN 'Infraestrutura & Ativos Fisicos'
        WHEN 'CC-0200-CLOUD' THEN 'Operacoes Cloud & FinOps'
        WHEN 'CC-0300-ENG' THEN 'Engenharia de Plataforma & Dados'
        WHEN 'CC-0400-DATA' THEN 'Analytics & Governanca de Dados'
        WHEN 'CC-0500-OPS' THEN 'Operacoes B2B & Atendimento'
        WHEN 'CC-0600-COM' THEN 'Comercial & Revenue Operations'
        WHEN 'CC-AI-CORE' THEN 'Agentes Core & Orquestracao'
        WHEN 'CC-AI-DATA' THEN 'Agentes de Engenharia de Dados'
        WHEN 'CC-AI-FINOPS' THEN 'Agentes FinOps & Precificacao'
        WHEN 'CC-AI-PLAT' THEN 'Agentes de Plataforma & Infra'
        ELSE cod_centro_custo
    END AS nome_centro_custo
FROM centros_unificados
ORDER BY cod_centro_custo
