"""Offline verification tests for Phase 05 AI/Cloud FinOps Connector (POP_T_158)."""

from __future__ import annotations

import json
import socket
from decimal import Decimal
from pathlib import Path

import pytest

from finops_lakehouse.core.contracts import load_contract
from finops_lakehouse.core.shield import ClientPathShieldError
from finops_lakehouse.core.storage import HybridStorageEngine
from finops_lakehouse.finops_ai import (
    AI_FINOPS_ARROW_SCHEMA,
    LLM_PRICING_CATALOG,
    MODEL_TIER_MAP,
    PRICING_CATALOG_VERSION,
    calculate_span_cost,
    default_ai_finops_contract_path,
    generate_synthetic_telemetry,
    get_model_spec,
    is_deepseek_offpeak_utc,
    materialize_ai_finops,
    read_telemetry_jsonl,
    redact_telemetry_text,
    resolve_effective_rates,
    resolve_model_name,
    rows_to_ai_finops_table,
    summarize_ai_finops,
    validate_telemetry_spans,
    write_telemetry_jsonl,
)


def test_pricing_catalog_matches_pop_t_158_v140_tiers_and_aliases() -> None:
    """Catalog covers all 44 models, exact POP_T_158 v1.4.0 rates, and aliases."""
    assert PRICING_CATALOG_VERSION == "POP_T_158_v1.4.0"
    assert len(LLM_PRICING_CATALOG) == 44
    assert set(MODEL_TIER_MAP.values()) == {"T1", "T2", "T3", "T4"}

    expected_samples = {
        "gemini-3.8-flash": ("T1", "google", Decimal("0.750000"), Decimal("3.750000")),
        "gemini-4-hydrogen": ("T2", "google", Decimal("1.500000"), Decimal("7.500000")),
        "gemini-4-neon": ("T3", "google", Decimal("3.000000"), Decimal("15.000000")),
        "gemini-4-carbon": ("T4", "google", Decimal("2.000000"), Decimal("10.000000")),
        "gemini-4-carbon-standard": ("T4", "google", Decimal("4.000000"), Decimal("20.000000")),
        "claude-haiku-5.5": ("T1", "anthropic", Decimal("0.100000"), Decimal("0.500000")),
        "claude-sonnet-5.5": ("T2", "anthropic", Decimal("2.000000"), Decimal("10.000000")),
        "claude-opus-5.5": ("T3", "anthropic", Decimal("4.000000"), Decimal("20.000000")),
        "claude-fable-5.2": ("T4", "anthropic", Decimal("8.000000"), Decimal("40.000000")),
        "gpt-6-luna": ("T1", "openai", Decimal("0.100000"), Decimal("0.500000")),
        "gpt-6.2-sol": ("T2", "openai", Decimal("2.000000"), Decimal("10.000000")),
        "gpt-6.2-astra": ("T4", "openai", Decimal("8.000000"), Decimal("40.000000")),
        "gpt-6-cyber": ("T4", "openai", Decimal("20.000000"), Decimal("100.000000")),
        "deepseek-v4.1-flash": ("T1", "deepseek", Decimal("0.300000"), Decimal("1.200000")),
        "deepseek-v4.1-flash-offpeak": (
            "T1",
            "deepseek",
            Decimal("0.150000"),
            Decimal("0.600000"),
        ),
        "grok-4.7": ("T3", "xai", Decimal("2.000000"), Decimal("6.000000")),
        "local-ollama": ("T1", "local", Decimal("0.000000"), Decimal("0.000000")),
    }
    for model_id, (tier, provider, in_rate, out_rate) in expected_samples.items():
        spec = get_model_spec(model_id, strict=True)
        assert spec.tier == tier
        assert spec.provider == provider
        assert spec.input_usd_per_m == in_rate
        assert spec.output_usd_per_m == out_rate

    alias_checks = {
        "orchestrator-neon": "gemini-4-neon",
        "gemini-4-neon-medium": "gemini-4-neon",
        "Gemini 4 Neon (High)": "gemini-4-neon",
        "flash": "gemini-3.8-flash",
        "opus": "claude-opus-5.5",
        "haiku-5.5": "claude-haiku-5.5",
        "cyber": "gpt-6-cyber",
        "sol-6.2": "gpt-6.2-sol",
        "deepseek-flash": "deepseek-v4.1-flash",
        "grok": "grok-4.7",
        "ollama": "local-ollama",
    }
    for alias, canonical in alias_checks.items():
        assert resolve_model_name(alias, strict=True) == canonical

    for invalid_name in ("x", "4", "unknown-model-xyz", ""):
        with pytest.raises(ValueError):
            resolve_model_name(invalid_name, strict=True)


def test_billing_cliffs_trigger_at_exact_pop_t_158_thresholds() -> None:
    """Prompt-size billing cliffs must activate strictly above threshold."""
    in_lo, out_lo, trig_lo = resolve_effective_rates("claude-haiku-5.5", 100_000)
    in_hi, out_hi, trig_hi = resolve_effective_rates("claude-haiku-5.5", 100_001)
    assert not trig_lo and (in_lo, out_lo) == (Decimal("0.100000"), Decimal("0.500000"))
    assert trig_hi and (in_hi, out_hi) == (Decimal("0.500000"), Decimal("2.500000"))

    g_lo_in, g_lo_out, g_trig_lo = resolve_effective_rates("grok-4.7", 200_000)
    g_hi_in, g_hi_out, g_trig_hi = resolve_effective_rates("grok-4.7", 200_001)
    assert not g_trig_lo and (g_lo_in, g_lo_out) == (Decimal("2.000000"), Decimal("6.000000"))
    assert g_trig_hi and (g_hi_in, g_hi_out) == (Decimal("4.000000"), Decimal("12.000000"))

    c_lo_in, c_lo_out, c_trig_lo = resolve_effective_rates("gpt-6-cyber", 272_000)
    c_hi_in, c_hi_out, c_trig_hi = resolve_effective_rates("gpt-6-cyber", 272_001)
    assert not c_trig_lo and (c_lo_in, c_lo_out) == (Decimal("20.000000"), Decimal("100.000000"))
    assert c_trig_hi and (c_hi_in, c_hi_out) == (Decimal("40.000000"), Decimal("150.000000"))

    cliff_span = calculate_span_cost("gpt-6-cyber", 300_000, 10_000)
    assert cliff_span.cliff_triggered is True
    assert cliff_span.cost_usd == Decimal("13.5000")


def test_strict_type_and_domain_guards_in_pricing_and_summary() -> None:
    """IEEE 754 floats, booleans, negative costs, and excessive cached tokens raise errors."""
    with pytest.raises(TypeError):
        calculate_span_cost("gemini-4-neon", 10_000, 2_000, cache_discount_rate=0.75)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        calculate_span_cost("gemini-4-neon", 10_000.0, 2_000)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        calculate_span_cost("gemini-4-neon", True, 2_000)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        calculate_span_cost("gemini-4-neon", 1_000, 200, cached_tokens_in=1_500)

    spans = generate_synthetic_telemetry(seed=42, num_spans=12)
    with pytest.raises(TypeError):
        summarize_ai_finops(spans, cache_hit_ratio=0.4)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        summarize_ai_finops([{**spans[0], "tokens_in": 100.0}])

    with pytest.raises(TypeError):
        summarize_ai_finops([{**spans[0], "cost_usd": 0.1234}])

    with pytest.raises(ValueError):
        summarize_ai_finops([{**spans[0], "cost_usd": Decimal("-0.0100")}])

    with pytest.raises(ValueError):
        summarize_ai_finops([{**spans[0], "cached_tokens_in": spans[0]["tokens_in"] + 1}])


def test_savings_levers_boundary_windows_and_no_double_discount() -> None:
    """Verify exact Peak/Off-Peak UTC boundaries, tz conversion, cache, and batch math."""
    # Exact Mon-Fri boundaries for [01:00, 04:00) and [06:00, 10:00) UTC
    assert is_deepseek_offpeak_utc("2025-11-24T00:59:59Z") is True
    assert is_deepseek_offpeak_utc("2025-11-24T01:00:00Z") is False
    assert is_deepseek_offpeak_utc("2025-11-24T03:59:59Z") is False
    assert is_deepseek_offpeak_utc("2025-11-24T04:00:00Z") is True
    assert is_deepseek_offpeak_utc("2025-11-24T05:59:59Z") is True
    assert is_deepseek_offpeak_utc("2025-11-24T06:00:00Z") is False
    assert is_deepseek_offpeak_utc("2025-11-24T09:59:59Z") is False
    assert is_deepseek_offpeak_utc("2025-11-24T10:00:00Z") is True
    # 03:30 -04:00 == 07:30 UTC (Monday peak)
    assert is_deepseek_offpeak_utc("2025-11-24T03:30:00-04:00") is False
    assert is_deepseek_offpeak_utc("2025-11-22T08:00:00Z") is True  # Saturday

    ds_peak = calculate_span_cost(
        "deepseek-v4.1-flash",
        1_000_000,
        1_000_000,
        timestamp_utc="2025-11-24T07:30:00Z",
    )
    assert not ds_peak.offpeak_applied and ds_peak.cost_usd == Decimal("1.5000")
    assert ds_peak.savings_usd == Decimal("0.0000")

    ds_offpeak = calculate_span_cost(
        "deepseek-v4.1-flash",
        1_000_000,
        1_000_000,
        timestamp_utc="2025-11-24T18:30:00Z",
    )
    assert ds_offpeak.offpeak_applied and ds_offpeak.cost_optimized_usd == Decimal("0.7500")
    assert ds_offpeak.savings_usd == Decimal("0.7500")

    # Explicit offpeak model key must not double-discount ($0.15/$0.60 already applied)
    ds_pre_offpeak = calculate_span_cost(
        "deepseek-v4.1-flash-offpeak",
        1_000_000,
        1_000_000,
        timestamp_utc="2025-11-24T18:30:00Z",
    )
    assert ds_pre_offpeak.offpeak_applied is False
    assert ds_pre_offpeak.cost_usd == Decimal("0.7500")
    assert ds_pre_offpeak.cost_optimized_usd == Decimal("0.7500")

    cached_batch = calculate_span_cost(
        "gemini-4-neon",
        100_000,
        20_000,
        cached_tokens_in=50_000,
        cache_discount_rate=Decimal("0.750000"),
        batch_mode=True,
    )
    assert cached_batch.cost_usd == Decimal("0.6000")
    assert cached_batch.cost_optimized_usd == Decimal("0.2438")
    assert cached_batch.savings_usd == Decimal("0.3562")
    assert cached_batch.cost_usd - cached_batch.savings_usd == cached_batch.cost_optimized_usd


@pytest.mark.parametrize("num_spans", [48, 120, 500])
def test_synthetic_generator_determinism_and_exact_dimensional_reconciliation(
    num_spans: int,
) -> None:
    """Generator hashes match and portfolio totals reconcile identically across all 5 dimensions."""
    spans_a = generate_synthetic_telemetry(seed=42, num_spans=num_spans, months=6)
    spans_b = generate_synthetic_telemetry(seed=42, num_spans=num_spans, months=6)

    hash_a = HybridStorageEngine.compute_table_sha256(rows_to_ai_finops_table(spans_a))
    hash_b = HybridStorageEngine.compute_table_sha256(rows_to_ai_finops_table(spans_b))
    assert hash_a == hash_b
    assert {row["tier"] for row in spans_a} == {"T1", "T2", "T3", "T4"}

    serialized_all = json.dumps(
        [{k: str(v) for k, v in row.items()} for row in spans_a]
    ).lower()
    assert "".join(["ae", "gea"]) not in serialized_all
    assert "".join(["indi", "cium"]) not in serialized_all

    summary = summarize_ai_finops(spans_a)
    expected_baseline = sum((row["cost_usd"] for row in spans_a), Decimal("0.0000"))
    assert summary.total_cost_usd == expected_baseline
    assert summary.total_savings_usd == (
        summary.offpeak_savings_usd + summary.cache_savings_usd + summary.batch_savings_usd
    )
    assert summary.total_cost_usd - summary.total_savings_usd == summary.optimized_cost_usd

    for dim_map in (
        summary.by_tier,
        summary.by_model,
        summary.by_skill,
        summary.by_operation,
        summary.by_competencia,
    ):
        assert sum((v["cost_usd"] for v in dim_map.values()), Decimal("0.0000")) == (
            summary.total_cost_usd
        )
        assert sum((v["optimized_cost_usd"] for v in dim_map.values()), Decimal("0.0000")) == (
            summary.optimized_cost_usd
        )

    payload = summary.to_dict()
    assert payload["total_cost_usd"] == f"{expected_baseline:.4f}"
    assert payload["optimized_cost_usd"] == f"{summary.optimized_cost_usd:.4f}"


def test_read_core_schema_jsonl_redaction_fail_fast_and_recalculate_false(
    tmp_path: Path,
) -> None:
    """Reader handles C.O.R.E. schema, fail-fast params, redacted errors, and recalculate_cost=False."""
    with pytest.raises(FileNotFoundError):
        read_telemetry_jsonl(tmp_path / "does_not_exist.jsonl")

    synthetic_cpf = ".".join(["123", "456", "789"]) + "-01"
    synthetic_openai = "".join(["sk-", "proj-", "abc123456789012345"])
    synthetic_gh = "".join(["ghp_", "1234567890", "abcdefghijklmnop"])
    synthetic_kv_val = "".join(["my_secret_", "value_9988"])

    assert "***" in redact_telemetry_text(f"CPF {synthetic_cpf} vazado")
    assert "[REDACTED_SECRET]" in redact_telemetry_text(f"token={synthetic_openai}")

    jsonl_path = tmp_path / "agent_telemetry.jsonl"
    valid_core_line = json.dumps(
        {
            "timestamp_utc": "2026-11-22T07:51:30+00:00",
            "trace_id": "abc123def4567890",
            "span_id": "112233445566",
            "parent_span_id": "",
            "agent_role": "primary",
            "skill": "tech-engineering-orchestrator",
            "operation": f"execute api_key={synthetic_kv_val}",
            "model": "gemini-4-neon",
            "provider": "google",
            "tokens_in": 100000,
            "tokens_out": 20000,
            "tokens_total": 120000,
            "cost_usd": 0.75,
            "cost_optimized_usd": 0.45,
            "duration_ms": 4200,
            "status": "ok",
            "repo": "finops-capex-revenue-lakehouse",
            "notes": f"CPF {synthetic_cpf} com token {synthetic_gh}",
        }
    )
    bad_json_line = "{not valid json"
    non_object_line = json.dumps([1, 2, 3])
    secret_error_line = json.dumps(
        {
            "timestamp_utc": "2026-11-22T08:00:00Z",
            "skill": "tech-code-reviewer",
            "model": "gemini-4-neon",
            "tokens_in": 100,
            "tokens_out": 50,
            "status": f"bad_{synthetic_openai}",
        }
    )
    jsonl_path.write_text(
        "\n".join([valid_core_line, bad_json_line, non_object_line, secret_error_line]) + "\n",
        encoding="utf-8",
    )

    # Out-of-range cache_discount_rate must fail-fast instead of quarantining valid lines
    with pytest.raises(ValueError):
        read_telemetry_jsonl(jsonl_path, cache_discount_rate=Decimal("1.500000"))

    result = read_telemetry_jsonl(jsonl_path)
    assert result.total_lines_read == 4
    assert result.accepted_count == 1
    assert result.quarantined_count == 3

    span = result.spans[0]
    assert span["competencia"] == "2026-11"
    assert span["tier"] == "T3"
    assert span["cost_usd"] == Decimal("0.6000")
    assert synthetic_kv_val not in span["operation"]
    assert synthetic_cpf not in span["notes"]
    assert synthetic_gh not in span["notes"]

    # Secrets inside exception messages and previews must be redacted in quarantined_lines
    q_errors = json.dumps(result.quarantined_lines)
    assert synthetic_openai not in q_errors
    assert "[REDACTED_SECRET]" in q_errors

    # Verify recalculate_cost=False preserves inbound billed costs
    preserved = read_telemetry_jsonl(jsonl_path, recalculate_cost=False)
    assert preserved.spans[0]["cost_usd"] == Decimal("0.7500")
    assert preserved.spans[0]["cost_optimized_usd"] == Decimal("0.4500")
    assert preserved.spans[0]["savings_usd"] == Decimal("0.3000")


def test_odcs_contract_parity_roundtrip_cwe1236_quarantine_and_shield(tmp_path: Path) -> None:
    """Verify 24-col ODCS contract parity, round-trip Silver sums, CWE-1236, quarantine & shield."""
    contract = load_contract(default_ai_finops_contract_path())
    assert list(contract["columns"].keys()) == AI_FINOPS_ARROW_SCHEMA.names

    spans = generate_synthetic_telemetry(seed=42, num_spans=48, months=3)
    storage = HybridStorageEngine(tmp_path / "lakehouse_data")

    # Round-trip JSONL must preserve baseline, optimized, and batch savings identically
    jsonl_file = write_telemetry_jsonl(spans, tmp_path / "synthetic.jsonl")
    ingested = read_telemetry_jsonl(jsonl_file)
    assert ingested.accepted_count == 48
    assert ingested.quarantined_count == 0

    mat = materialize_ai_finops(storage, ingested.spans, batch_id="ai_finops_seed42_48")
    assert mat.passed is True
    assert mat.status == "ACCEPTED"
    assert mat.silver_path is not None and mat.silver_path.is_file()
    assert isinstance(mat.sha256, str) and len(mat.sha256) == 64

    rows = storage.query_silver_duckdb(
        mat.silver_path,
        "SELECT COUNT(*), SUM(cost_usd), SUM(cost_optimized_usd), SUM(savings_usd) FROM silver_data",
    )
    assert rows[0][0] == 48
    expected_cost = sum((s["cost_usd"] for s in spans), Decimal("0.0000"))
    expected_opt = sum((s["cost_optimized_usd"] for s in spans), Decimal("0.0000"))
    expected_savings = sum((s["savings_usd"] for s in spans), Decimal("0.0000"))
    assert Decimal(str(rows[0][1])) == expected_cost
    assert Decimal(str(rows[0][2])) == expected_opt
    assert Decimal(str(rows[0][3])) == expected_savings

    # CWE-1236 sanitization must neutralize formulas in notes, parent_span_id, operation, and skill
    cwe_spans = [
        {
            **spans[0],
            "id_lancamento": "AI-CWE-001",
            "parent_span_id": "=HYPERLINK(\"http://bad\")",
            "notes": "=CMD('calc')",
            "operation": "+1+1",
        }
    ]
    cleaned_cwe, cwe_res = validate_telemetry_spans(cwe_spans, contract)
    assert cwe_res.passed is True
    assert cwe_res.sanitized_cells == 3
    assert cleaned_cwe[0]["parent_span_id"].startswith("'=")
    assert cleaned_cwe[0]["notes"].startswith("'=")
    assert cleaned_cwe[0]["operation"].startswith("'+")

    # Corrupted span with nested secret/CPF + bad decimal/PK must redact completely in quarantine JSON
    synthetic_secret = "".join(["sk-", "proj-", "leaktest1234567890"])
    synthetic_cpf = ".".join(["111", "222", "333"]) + "-44"
    corrupted = [
        dict(ingested.spans[0]),
        {**ingested.spans[1], "id_lancamento": ingested.spans[0]["id_lancamento"]},
        {
            **ingested.spans[2],
            "tokens_total": ingested.spans[2]["tokens_in"] + 999,
            "cost_usd": f"not_num_{synthetic_secret}_{synthetic_cpf}",
            "notes": {"nested": [f"leak {synthetic_secret} {synthetic_cpf}"]},
        },
    ]
    q_mat = materialize_ai_finops(storage, corrupted, batch_id="ai_finops_bad")
    assert q_mat.passed is False
    assert q_mat.status == "QUARANTINED"
    assert q_mat.quarantine_path is not None and q_mat.quarantine_path.is_file()
    q_text = q_mat.quarantine_path.read_text(encoding="utf-8")
    assert synthetic_secret not in q_text
    assert synthetic_cpf not in q_text
    assert any("duplicate primary key" in v for v in q_mat.violations)
    assert any("tokens_total" in v for v in q_mat.violations)

    # Validate edge rejection paths directly
    _, empty_res = validate_telemetry_spans([], contract)
    _, missing_col_res = validate_telemetry_spans([{"id_lancamento": "only-one"}], contract)
    _, bad_meta_res = validate_telemetry_spans(
        [
            {
                **spans[0],
                "competencia": "2025-13",
                "model": "not-a-model",
                "cost_usd": Decimal("999999999999999.9999"),
            }
        ],
        contract,
    )
    assert not empty_res.passed
    assert not missing_col_res.passed
    assert not bad_meta_res.passed
    assert any("competencia" in v for v in bad_meta_res.violations)
    assert any("unknown model" in v for v in bad_meta_res.violations)
    assert any("out-of-range DECIMAL(18,4)" in v for v in bad_meta_res.violations)

    # Client Path Shield MUST trigger BEFORE creating bronze/lakehouse_meta.sqlite on disk
    fake_repo = tmp_path / "public_repo"
    shield_target = fake_repo / "data"
    with pytest.raises(ClientPathShieldError):
        materialize_ai_finops(
            shield_target,
            ingested.spans[:3],
            is_client_data=True,
            repo_root=fake_repo,
        )
    assert not shield_target.exists()


def test_ai_finops_connector_runs_strictly_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Pricing, synthetic generation, simulation, and Silver write perform zero network calls."""

    def _blocked_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Network call attempted inside offline AI FinOps suite.")

    monkeypatch.setattr(socket, "create_connection", _blocked_socket)
    spans = generate_synthetic_telemetry(seed=42, num_spans=24, months=2)
    jsonl_path = write_telemetry_jsonl(spans, tmp_path / "offline.jsonl")
    ingested = read_telemetry_jsonl(jsonl_path)
    summary = summarize_ai_finops(ingested.spans)
    assert summary.span_count == 24
    assert summary.total_cost_usd > Decimal("0.0000")
