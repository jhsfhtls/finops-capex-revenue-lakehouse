SELECT
    CAST(model_id AS VARCHAR) AS model_id,
    CAST(provider AS VARCHAR) AS provider,
    CAST(tier AS VARCHAR) AS tier,
    CAST(input_usd_per_m AS DECIMAL(12, 6)) AS input_usd_per_m,
    CAST(output_usd_per_m AS DECIMAL(12, 6)) AS output_usd_per_m,
    CAST(cliff_threshold_tokens AS BIGINT) AS cliff_threshold_tokens,
    CAST(cliff_input_usd_per_m AS DECIMAL(12, 6)) AS cliff_input_usd_per_m,
    CAST(cliff_output_usd_per_m AS DECIMAL(12, 6)) AS cliff_output_usd_per_m,
    CAST(has_billing_cliff AS BOOLEAN) AS has_billing_cliff
FROM {{ source('ai_cloud_finops', 'llm_model_catalog') }}
WHERE model_id IS NOT NULL
