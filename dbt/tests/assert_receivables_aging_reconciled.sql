SELECT
    id_fatura_masked,
    valor_original,
    valor_pago,
    valor_em_aberto
FROM {{ ref('fct_receivables_aging') }}
WHERE valor_pago + valor_em_aberto <> valor_original
