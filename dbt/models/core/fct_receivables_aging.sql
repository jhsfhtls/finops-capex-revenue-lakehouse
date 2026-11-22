SELECT
    r.id_fatura_masked,
    r.id_contrato_masked,
    r.cnpj_14,
    cli.segmento_cliente,
    cli.sigla_uf,
    cli.perfil_risco_lgd,
    r.competencia,
    r.data_emissao,
    r.data_vencimento,
    r.data_referencia,
    r.status_fatura,
    r.moeda,
    r.dias_atraso,
    r.faixa_aging,
    r.valor_original,
    r.valor_pago,
    r.valor_em_aberto,
    r.pd_aplicada,
    r.lgd_aplicada,
    r.valor_ecl,
    r.score_cobranca,
    r.prioridade_cobranca,
    r.ranking_cobranca,
    CASE
        WHEN r.dias_atraso > 0
            AND r.valor_em_aberto > CAST('0.0000' AS DECIMAL(18, 4))
            THEN TRUE
        ELSE FALSE
    END AS is_vencido
FROM {{ ref('stg_receivables_aging') }} AS r
INNER JOIN {{ ref('dim_cliente_segmento') }} AS cli
    ON r.cnpj_14 = cli.cnpj_14
INNER JOIN {{ ref('dim_calendario_fiscal') }} AS cal
    ON r.competencia = cal.competencia
ORDER BY r.ranking_cobranca, r.id_fatura_masked
