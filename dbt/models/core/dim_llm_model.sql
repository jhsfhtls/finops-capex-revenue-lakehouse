SELECT
    model_id,
    provider,
    tier,
    input_usd_per_m,
    output_usd_per_m,
    cliff_threshold_tokens,
    cliff_input_usd_per_m,
    cliff_output_usd_per_m,
    has_billing_cliff
FROM {{ ref('stg_llm_model_catalog') }}
ORDER BY tier, provider, model_id
