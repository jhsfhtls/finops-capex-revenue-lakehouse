"""AI/Cloud FinOps telemetry connector, simulator, ODCS validator & Silver materializer."""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa

from finops_lakehouse.core.contracts import (
    DECIMAL_18_4_MAX,
    ContractValidationResult,
    load_contract,
)
from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    RATE_QUANT,
    ZERO_MONEY,
    format_money,
    parse_spreadsheet_decimal,
    to_decimal_money,
)
from finops_lakehouse.core.shield import (
    default_repo_root,
    enforce_client_path_shield,
    sanitize_inbound_text,
)
from finops_lakehouse.core.storage import HybridStorageEngine
from finops_lakehouse.finops_ai.pricing_catalog import (
    DEEPSEEK_OFFPEAK_DISCOUNT_RATE,
    DEFAULT_BATCH_DISCOUNT_RATE,
    DEFAULT_CACHE_DISCOUNT_RATE,
    ONE_MILLION,
    ONE_RATE,
    ZERO_RATE,
    calculate_span_cost,
    get_model_spec,
    parse_utc_timestamp,
    require_non_negative_int,
    resolve_effective_rates,
    resolve_model_name,
    validate_unit_rate,
)

# Secret & CPF patterns synchronized with core/scripts/agent_tools/agent_telemetry.py
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(api[_-]?key|api[_-]?secret|auth[_-]?token|access[_-]?token|"
        r"secret[_-]?key|private[_-]?key|password|client[_-]?secret)\b\s*([=:])\s*['\"]?([A-Za-z0-9+/=_-]{8,})"
    ),
    re.compile(
        r"(?<![A-Za-z0-9])(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,})"
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_CPF_RE = re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)")
_COMPETENCIA_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_INT64_MAX = (1 << 63) - 1
_MONEY_BUCKET_KEYS: frozenset[str] = frozenset({"cost_usd", "optimized_cost_usd"})

AI_FINOPS_ARROW_SCHEMA = pa.schema(
    [
        ("id_lancamento", pa.string()),
        ("timestamp_utc", pa.string()),
        ("competencia", pa.string()),
        ("trace_id", pa.string()),
        ("span_id", pa.string()),
        ("parent_span_id", pa.string()),
        ("cod_centro_custo", pa.string()),
        ("repo", pa.string()),
        ("agent_role", pa.string()),
        ("skill", pa.string()),
        ("operation", pa.string()),
        ("model", pa.string()),
        ("provider", pa.string()),
        ("tier", pa.string()),
        ("tokens_in", pa.int64()),
        ("tokens_out", pa.int64()),
        ("tokens_total", pa.int64()),
        ("cached_tokens_in", pa.int64()),
        ("cost_usd", pa.decimal128(18, 4)),
        ("cost_optimized_usd", pa.decimal128(18, 4)),
        ("savings_usd", pa.decimal128(18, 4)),
        ("duration_ms", pa.int64()),
        ("status", pa.string()),
        ("notes", pa.string()),
    ]
)

_SYNTHETIC_COST_CENTERS: tuple[str, ...] = (
    "CC-AI-CORE",
    "CC-AI-DATA",
    "CC-AI-FINOPS",
    "CC-AI-PLAT",
)
_SYNTHETIC_REPOS: tuple[str, ...] = (
    "finops-capex-revenue-lakehouse",
    "public-data-gateway",
    "core-synthetic",
    "operations-synthetic",
    "career-synthetic",
)
_SYNTHETIC_SKILLS: tuple[tuple[str, str, str], ...] = (
    ("tech-code-reviewer", "static_review", "gemini-4-neon"),
    ("tech-architect-auditor", "zero_trust_audit", "claude-opus-5.5"),
    ("tech-engineering-orchestrator", "feature_build", "gemini-4-hydrogen"),
    ("tech-docs-formatter", "markdown_format", "gemini-3.8-flash"),
    ("biz-consulting-orchestrator", "b2b_roi_brief", "claude-sonnet-5.5"),
    ("biz-career-orchestrator", "salary_parity_eval", "gpt-6.2-sol"),
    ("biz-study-orchestrator", "flashcard_batch", "deepseek-v4.1-flash"),
    ("tech-sop-architect", "pop_drafting", "gemini-4-carbon"),
    ("tech-gitops-governance-auditor", "security_hardening", "gpt-6-cyber"),
    ("research-subagent", "long_context_scan", "grok-4.7"),
    ("schema-validator", "odcs_triage", "claude-haiku-5.5"),
    ("escalation-specialist", "cross_repo_fix", "claude-fable-5.2"),
)


@dataclass(frozen=True)
class TelemetryIngestionResult:
    """Structured outcome from reading a raw or synthetic JSONL telemetry file."""

    source_path: Path
    spans: list[dict[str, Any]] = field(default_factory=list)
    quarantined_lines: list[dict[str, Any]] = field(default_factory=list)
    total_lines_read: int = 0

    @property
    def accepted_count(self) -> int:
        return len(self.spans)

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined_lines)


@dataclass(frozen=True)
class AiFinOpsSummary:
    """Aggregated AI/Cloud FinOps metrics and simulated optimization waterfall."""

    span_count: int
    ok_count: int
    error_count: int
    blocked_count: int
    total_tokens_in: int
    total_tokens_out: int
    total_tokens: int
    total_cached_tokens_in: int
    total_cost_usd: Decimal
    optimized_cost_usd: Decimal
    offpeak_savings_usd: Decimal
    cache_savings_usd: Decimal
    batch_savings_usd: Decimal
    total_savings_usd: Decimal
    savings_pct: Decimal
    by_tier: dict[str, dict[str, Any]]
    by_model: dict[str, dict[str, Any]]
    by_skill: dict[str, dict[str, Any]]
    by_operation: dict[str, dict[str, Any]]
    by_competencia: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Serialize summary to JSON-safe dictionary preserving Decimal precision."""
        return {
            "span_count": self.span_count,
            "ok_count": self.ok_count,
            "error_count": self.error_count,
            "blocked_count": self.blocked_count,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_tokens": self.total_tokens,
            "total_cached_tokens_in": self.total_cached_tokens_in,
            "total_cost_usd": format_money(self.total_cost_usd),
            "optimized_cost_usd": format_money(self.optimized_cost_usd),
            "offpeak_savings_usd": format_money(self.offpeak_savings_usd),
            "cache_savings_usd": format_money(self.cache_savings_usd),
            "batch_savings_usd": format_money(self.batch_savings_usd),
            "total_savings_usd": format_money(self.total_savings_usd),
            "savings_pct": f"{self.savings_pct:.6f}",
            "by_tier": _serialize_bucket_map(self.by_tier),
            "by_model": _serialize_bucket_map(self.by_model),
            "by_skill": _serialize_bucket_map(self.by_skill),
            "by_operation": _serialize_bucket_map(self.by_operation),
            "by_competencia": _serialize_bucket_map(self.by_competencia),
        }


@dataclass(frozen=True)
class MaterializationResult:
    """Result of ODCS v3.1 gate validation and Silver Parquet persistence."""

    status: str
    passed: bool
    batch_id: str
    row_count: int
    sha256: str | None = None
    silver_path: Path | None = None
    quarantine_path: Path | None = None
    violations: list[str] = field(default_factory=list)


def _serialize_bucket_map(bucket_map: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    serialized: dict[str, dict[str, Any]] = {}
    for key in sorted(bucket_map):
        entry = bucket_map[key]
        out: dict[str, Any] = {}
        for k, v in entry.items():
            if isinstance(v, Decimal):
                out[k] = format_money(v) if k in _MONEY_BUCKET_KEYS else f"{v:.6f}"
            else:
                out[k] = v
        serialized[key] = out
    return serialized


def redact_telemetry_text(text: str, *, max_length: int | None = None) -> str:
    """Mask credential tokens, private keys, and raw CPFs in telemetry text."""
    if not isinstance(text, str):
        raise TypeError(f"text must be a string, got {type(text).__name__}.")
    if max_length is not None:
        if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 0:
            raise ValueError("max_length must be a non-negative integer or None.")
    if not text:
        return ""
    sanitized = _SECRET_PATTERNS[0].sub(r"\1\2[REDACTED_SECRET]", text)
    sanitized = _CPF_RE.sub("***.***.***-**", sanitized)
    for pattern in _SECRET_PATTERNS[1:]:
        sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
    sanitized = sanitized.strip()
    if max_length is not None and len(sanitized) > max_length:
        return sanitized[:max_length]
    return sanitized


def _redact_nested(value: Any) -> Any:
    """Recursively redact secrets and CPFs inside quarantine sample structures."""
    if isinstance(value, str):
        return redact_telemetry_text(value)
    if isinstance(value, dict):
        return {str(k): _redact_nested(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_nested(item) for item in value]
    return value


def default_ai_finops_contract_path() -> Path:
    """Return default ODCS v3.1 contract path for AI/Cloud FinOps domain."""
    return default_repo_root() / "catalog" / "contracts" / "ai_cloud_finops.contract.yaml"


def _deterministic_hex(rng: random.Random, length: int) -> str:
    return "".join(rng.choices("0123456789abcdef", k=length))


def generate_synthetic_telemetry(
    seed: int = 42,
    num_spans: int = 240,
    months: int = 6,
    start_year: int = 2025,
    start_month: int = 7,
) -> list[dict[str, Any]]:
    """Generate deterministic synthetic telemetry spans matching agent_telemetry.jsonl."""
    valid_seed = require_non_negative_int(seed, "seed")
    valid_spans = require_non_negative_int(num_spans, "num_spans")
    valid_months = require_non_negative_int(months, "months")
    if valid_spans <= 0:
        raise ValueError("num_spans must be positive.")
    if valid_months <= 0:
        raise ValueError("months must be positive.")

    rng = random.Random(valid_seed)
    spans: list[dict[str, Any]] = []

    for idx in range(valid_spans):
        month_offset = idx % valid_months
        total_month_index = (start_year * 12 + (start_month - 1)) + month_offset
        year = total_month_index // 12
        month = (total_month_index % 12) + 1
        day = (idx % 28) + 1
        # Mix Peak UTC hours (02, 07, 08, 09) and Off-Peak UTC hours (05, 14, 18, 22)
        hour_pool = (2, 7, 8, 9, 5, 14, 18, 22)
        hour = hour_pool[(idx + rng.randint(0, 7)) % len(hour_pool)]
        minute = rng.randint(0, 59)
        second = rng.randint(0, 59)
        ts_dt = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
        timestamp_utc = ts_dt.isoformat(timespec="seconds")
        competencia = f"{year:04d}-{month:02d}"

        skill, operation, model = _SYNTHETIC_SKILLS[idx % len(_SYNTHETIC_SKILLS)]
        repo = _SYNTHETIC_REPOS[(idx // 2) % len(_SYNTHETIC_REPOS)]
        cost_center = _SYNTHETIC_COST_CENTERS[idx % len(_SYNTHETIC_COST_CENTERS)]
        agent_role = "subagent" if idx % 3 == 0 else "primary"

        # Inject deliberate billing-cliff boundary spans so T1..T4 and cliff analytics are covered
        if model == "claude-haiku-5.5" and idx % 24 == 10:
            tokens_in = 125_000
            tokens_out = 6_000
        elif model == "grok-4.7" and idx % 24 == 9:
            tokens_in = 225_000
            tokens_out = 10_000
        elif model == "gpt-6-cyber" and idx % 24 == 8:
            tokens_in = 290_000
            tokens_out = 12_000
        else:
            tokens_in = rng.randint(6_000, 65_000)
            tokens_out = rng.randint(1_500, 14_000)

        cached_tokens_in = (tokens_in * 40) // 100 if idx % 2 == 0 else (tokens_in * 15) // 100
        batch_mode = idx % 5 == 0
        breakdown = calculate_span_cost(
            model,
            tokens_in,
            tokens_out,
            cached_tokens_in=cached_tokens_in,
            batch_mode=batch_mode,
            timestamp_utc=timestamp_utc,
        )

        trace_id = _deterministic_hex(rng, 16)
        span_id = _deterministic_hex(rng, 12)
        parent_span_id = _deterministic_hex(rng, 12) if agent_role == "subagent" else ""
        duration_ms = rng.randint(650, 18_500)
        status = "error" if idx % 37 == 36 else ("blocked" if idx % 53 == 52 else "ok")
        id_lancamento = f"AI-SYN-{valid_seed}-{idx + 1:06d}"

        spans.append(
            {
                "id_lancamento": id_lancamento,
                "timestamp_utc": timestamp_utc,
                "competencia": competencia,
                "trace_id": trace_id,
                "span_id": span_id,
                "parent_span_id": parent_span_id,
                "cod_centro_custo": cost_center,
                "repo": repo,
                "agent_role": agent_role,
                "skill": skill,
                "operation": operation,
                "model": breakdown.model,
                "provider": breakdown.provider,
                "tier": breakdown.tier,
                "tokens_in": breakdown.tokens_in,
                "tokens_out": breakdown.tokens_out,
                "tokens_total": breakdown.tokens_total,
                "cached_tokens_in": breakdown.cached_tokens_in,
                "batch_applied": breakdown.batch_applied,
                "cost_usd": breakdown.cost_usd,
                "cost_optimized_usd": breakdown.cost_optimized_usd,
                "savings_usd": breakdown.savings_usd,
                "duration_ms": duration_ms,
                "status": status,
                "notes": f"synthetic benchmark span #{idx + 1} (seed={valid_seed})",
            }
        )

    return spans


def write_telemetry_jsonl(spans: Sequence[dict[str, Any]], output_path: Path) -> Path:
    """Write normalized telemetry spans to UTF-8 LF JSONL matching C.O.R.E. format."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        for row in spans:
            serializable = {
                key: (format_money(val) if isinstance(val, Decimal) else val)
                for key, val in row.items()
            }
            fh.write(json.dumps(serializable, ensure_ascii=False, separators=(",", ":")) + "\n")
    return target


def _normalize_jsonl_record(
    line_num: int,
    raw: dict[str, Any],
    *,
    default_cost_center: str,
    recalculate_cost: bool,
    cache_discount_rate: Decimal,
) -> dict[str, Any]:
    raw_ts = raw.get("timestamp_utc")
    if not isinstance(raw_ts, str):
        raise ValueError("Missing or non-string 'timestamp_utc'.")
    dt_utc = parse_utc_timestamp(raw_ts)
    timestamp_utc = dt_utc.isoformat(timespec="seconds")
    competencia = dt_utc.strftime("%Y-%m")

    model_raw = str(raw.get("model") or "").strip()
    if not model_raw:
        raise ValueError("Missing required 'model'.")
    skill = redact_telemetry_text(str(raw.get("skill") or "").strip())
    if not skill:
        raise ValueError("Missing required 'skill'.")

    tokens_in = require_non_negative_int(raw.get("tokens_in"), "tokens_in")
    tokens_out = require_non_negative_int(raw.get("tokens_out"), "tokens_out")
    raw_total = raw.get("tokens_total")
    if raw_total is not None:
        expected_total = require_non_negative_int(raw_total, "tokens_total")
        if expected_total != tokens_in + tokens_out:
            raise ValueError(
                f"tokens_total ({expected_total}) != tokens_in + tokens_out ({tokens_in + tokens_out})."
            )

    cached_tokens_in = (
        require_non_negative_int(raw["cached_tokens_in"], "cached_tokens_in")
        if raw.get("cached_tokens_in") is not None
        else 0
    )
    duration_ms = (
        require_non_negative_int(raw["duration_ms"], "duration_ms")
        if raw.get("duration_ms") is not None
        else 0
    )

    raw_batch = raw.get("batch_applied", raw.get("batch_mode", False))
    if not isinstance(raw_batch, bool):
        raise ValueError(f"Field 'batch_applied' must be a boolean, got {type(raw_batch).__name__}.")

    agent_role = str(raw.get("agent_role") or "primary").strip().lower()
    if agent_role not in {"primary", "subagent"}:
        raise ValueError(f"Unsupported agent_role '{agent_role}'.")

    status = str(raw.get("status") or "ok").strip().lower()
    if status not in {"ok", "error", "blocked"}:
        raise ValueError(f"Unsupported status '{status}'.")

    trace_id = redact_telemetry_text(str(raw.get("trace_id") or f"trace-{line_num:06d}").strip())
    span_id = redact_telemetry_text(str(raw.get("span_id") or f"span-{line_num:06d}").strip())
    parent_span_id = redact_telemetry_text(str(raw.get("parent_span_id") or "").strip())
    repo = redact_telemetry_text(str(raw.get("repo") or "core").strip()) or "core"
    cost_center = redact_telemetry_text(
        str(raw.get("cod_centro_custo") or default_cost_center or f"CC-AI-{repo[:8].upper()}").strip()
    )
    operation = redact_telemetry_text(str(raw.get("operation") or "execute").strip()) or "execute"
    notes = redact_telemetry_text(str(raw.get("notes") or ""), max_length=400)

    breakdown = calculate_span_cost(
        model_raw,
        tokens_in,
        tokens_out,
        cached_tokens_in=cached_tokens_in,
        cache_discount_rate=cache_discount_rate,
        batch_mode=raw_batch,
        timestamp_utc=timestamp_utc,
    )

    baseline_cost = breakdown.cost_usd
    optimized_cost = breakdown.cost_optimized_usd
    if not recalculate_cost and raw.get("cost_usd") is not None:
        baseline_cost = parse_spreadsheet_decimal(raw["cost_usd"])
        if baseline_cost < ZERO_MONEY:
            raise ValueError(f"Negative inbound cost_usd ({baseline_cost}).")
        if raw.get("cost_optimized_usd") is not None:
            inbound_opt = parse_spreadsheet_decimal(raw["cost_optimized_usd"])
            if inbound_opt < ZERO_MONEY:
                raise ValueError(f"Negative inbound cost_optimized_usd ({inbound_opt}).")
            optimized_cost = min(baseline_cost, inbound_opt)
        else:
            optimized_cost = min(baseline_cost, breakdown.cost_optimized_usd)
    savings = (baseline_cost - optimized_cost).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

    raw_id = redact_telemetry_text(str(raw.get("id_lancamento") or "").strip())
    if raw_id:
        id_lancamento = raw_id
    else:
        digest = hashlib.sha256(
            f"{line_num}:{trace_id}:{span_id}:{timestamp_utc}".encode()
        ).hexdigest()[:16]
        id_lancamento = f"AI-SPAN-{digest}"

    return {
        "id_lancamento": id_lancamento,
        "timestamp_utc": timestamp_utc,
        "competencia": competencia,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "cod_centro_custo": cost_center,
        "repo": repo,
        "agent_role": agent_role,
        "skill": skill,
        "operation": operation,
        "model": breakdown.model,
        "provider": breakdown.provider,
        "tier": breakdown.tier,
        "tokens_in": breakdown.tokens_in,
        "tokens_out": breakdown.tokens_out,
        "tokens_total": breakdown.tokens_total,
        "cached_tokens_in": breakdown.cached_tokens_in,
        "batch_applied": breakdown.batch_applied,
        "cost_usd": baseline_cost,
        "cost_optimized_usd": optimized_cost,
        "savings_usd": savings,
        "duration_ms": duration_ms,
        "status": status,
        "notes": notes,
    }


def read_telemetry_jsonl(
    path: Path,
    *,
    default_cost_center: str = "CC-AI-CORE",
    recalculate_cost: bool = True,
    cache_discount_rate: Decimal | int | str = DEFAULT_CACHE_DISCOUNT_RATE,
) -> TelemetryIngestionResult:
    """Ingest synthetic or C.O.R.E. `.logs/agent_telemetry.jsonl` with quarantine & redaction."""
    # Validate parameters Fail-Fast before touching input files
    cache_disc = validate_unit_rate(cache_discount_rate, "cache_discount_rate")
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Telemetry JSONL file not found: {source}")

    spans: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    total_lines = 0

    with source.open("r", encoding="utf-8", errors="replace") as fh:
        for idx, raw_line in enumerate(fh, 1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            total_lines += 1
            try:
                parsed = json.loads(stripped)
                if not isinstance(parsed, dict):
                    raise ValueError("JSONL line must decode into a JSON object.")
                normalized = _normalize_jsonl_record(
                    idx,
                    parsed,
                    default_cost_center=default_cost_center,
                    recalculate_cost=recalculate_cost,
                    cache_discount_rate=cache_disc,
                )
                spans.append(normalized)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                quarantined.append(
                    {
                        "line_number": idx,
                        "error": redact_telemetry_text(str(exc), max_length=300),
                        "raw_preview": redact_telemetry_text(stripped, max_length=200),
                    }
                )

    return TelemetryIngestionResult(
        source_path=source,
        spans=spans,
        quarantined_lines=quarantined,
        total_lines_read=total_lines,
    )


def _accumulate_bucket(
    target: dict[str, dict[str, Any]],
    key: str,
    *,
    tokens_total: int,
    cost_usd: Decimal,
    optimized_usd: Decimal,
) -> None:
    bucket = target.setdefault(
        key,
        {
            "runs": 0,
            "tokens_total": 0,
            "cost_usd": ZERO_MONEY,
            "optimized_cost_usd": ZERO_MONEY,
        },
    )
    bucket["runs"] += 1
    bucket["tokens_total"] += tokens_total
    bucket["cost_usd"] += cost_usd
    bucket["optimized_cost_usd"] += optimized_usd


def _extract_validated_span_metrics(
    span: dict[str, Any],
) -> tuple[int, int, int, str, str, str, Decimal]:
    """Validate span token/monetary metrics strictly without silent coercion of negatives/floats."""
    t_in = require_non_negative_int(span.get("tokens_in", 0), "tokens_in")
    t_out = require_non_negative_int(span.get("tokens_out", 0), "tokens_out")
    t_cached = require_non_negative_int(span.get("cached_tokens_in", 0), "cached_tokens_in")
    if t_cached > t_in:
        raise ValueError(f"cached_tokens_in ({t_cached}) cannot exceed tokens_in ({t_in}).")

    model_name = str(span.get("model") or "gemini-3.8-flash")
    spec = get_model_spec(model_name)
    if span.get("cost_usd") is not None:
        baseline = to_decimal_money(span["cost_usd"])
        if baseline < ZERO_MONEY:
            raise ValueError(f"cost_usd must be non-negative, got {baseline}.")
    else:
        baseline = calculate_span_cost(spec.model_id, t_in, t_out).cost_usd

    tier = str(span.get("tier") or spec.tier)
    return t_in, t_out, t_cached, spec.model_id, spec.provider, tier, baseline


def _simulate_span_waterfall(
    *,
    span_baseline: Decimal,
    tokens_in: int,
    tokens_out: int,
    model_id: str,
    provider: str,
    hit_ratio: Decimal,
    cache_disc: Decimal,
    batch_ratio: Decimal,
    batch_disc: Decimal,
    enable_deepseek_offpeak: bool,
) -> tuple[Decimal, Decimal, Decimal]:
    """Quantize off-peak -> cache -> batch waterfall per span in DECIMAL(18,4) for zero drift."""
    in_rate, out_rate, _ = resolve_effective_rates(model_id, tokens_in)
    raw_in_cost = Decimal(tokens_in) * in_rate / ONE_MILLION
    raw_out_cost = Decimal(tokens_out) * out_rate / ONE_MILLION
    raw_total = raw_in_cost + raw_out_cost
    in_share = (raw_in_cost / raw_total) if raw_total > Decimal("0") else Decimal("0")

    offpeak_factor = (
        (ONE_RATE - DEEPSEEK_OFFPEAK_DISCOUNT_RATE)
        if (enable_deepseek_offpeak and model_id == "deepseek-v4.1-flash")
        else ONE_RATE
    )
    cache_factor = ONE_RATE - (in_share * hit_ratio * cache_disc)
    batch_factor = ONE_RATE - (batch_ratio * batch_disc) if provider != "local" else ONE_RATE

    s_offpeak = (span_baseline * offpeak_factor).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    s_offpeak = min(span_baseline, max(ZERO_MONEY, s_offpeak))

    s_cache = (s_offpeak * cache_factor).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    s_cache = min(s_offpeak, max(ZERO_MONEY, s_cache))

    span_opt = (s_cache * batch_factor).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    span_opt = min(s_cache, max(ZERO_MONEY, span_opt))
    return s_offpeak, s_cache, span_opt


def summarize_ai_finops(
    spans: Sequence[dict[str, Any]],
    *,
    cache_hit_ratio: Decimal | int | str = Decimal("0.400000"),
    cache_discount_rate: Decimal | int | str = DEFAULT_CACHE_DISCOUNT_RATE,
    batch_eligible_ratio: Decimal | int | str = Decimal("0.300000"),
    batch_discount_rate: Decimal | int | str = DEFAULT_BATCH_DISCOUNT_RATE,
    enable_deepseek_offpeak: bool = True,
) -> AiFinOpsSummary:
    """Aggregate AI FinOps spans and simulate cache/batch/off-peak savings in pure Decimal."""
    if not spans:
        raise ValueError("spans cannot be empty.")
    if not isinstance(enable_deepseek_offpeak, bool):
        raise TypeError("enable_deepseek_offpeak must be a boolean.")

    hit_ratio = validate_unit_rate(cache_hit_ratio, "cache_hit_ratio")
    cache_disc = validate_unit_rate(cache_discount_rate, "cache_discount_rate")
    batch_ratio = validate_unit_rate(batch_eligible_ratio, "batch_eligible_ratio")
    batch_disc = validate_unit_rate(batch_discount_rate, "batch_discount_rate")

    ok_count = 0
    error_count = 0
    blocked_count = 0
    total_in = 0
    total_out = 0
    total_cached = 0

    total_baseline = ZERO_MONEY
    after_offpeak = ZERO_MONEY
    after_cache = ZERO_MONEY
    optimized_total = ZERO_MONEY

    by_tier: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_skill: dict[str, dict[str, Any]] = {}
    by_operation: dict[str, dict[str, Any]] = {}
    by_competencia: dict[str, dict[str, Any]] = {}

    for span in spans:
        t_in, t_out, t_cached, model_id, provider, tier, span_baseline = (
            _extract_validated_span_metrics(span)
        )
        skill_name = str(span.get("skill") or "unknown")
        op_name = str(span.get("operation") or "execute")
        comp = str(span.get("competencia") or str(span.get("timestamp_utc") or "1970-01")[:7])
        status = str(span.get("status") or "ok").lower()
        if status == "ok":
            ok_count += 1
        elif status == "error":
            error_count += 1
        elif status == "blocked":
            blocked_count += 1

        s_offpeak, s_cache, span_opt = _simulate_span_waterfall(
            span_baseline=span_baseline,
            tokens_in=t_in,
            tokens_out=t_out,
            model_id=model_id,
            provider=provider,
            hit_ratio=hit_ratio,
            cache_disc=cache_disc,
            batch_ratio=batch_ratio,
            batch_disc=batch_disc,
            enable_deepseek_offpeak=enable_deepseek_offpeak,
        )

        total_baseline += span_baseline
        after_offpeak += s_offpeak
        after_cache += s_cache
        optimized_total += span_opt

        total_in += t_in
        total_out += t_out
        total_cached += t_cached

        for bucket_map, key in (
            (by_tier, tier),
            (by_model, model_id),
            (by_skill, skill_name),
            (by_operation, op_name),
            (by_competencia, comp),
        ):
            _accumulate_bucket(
                bucket_map,
                key,
                tokens_total=t_in + t_out,
                cost_usd=span_baseline,
                optimized_usd=span_opt,
            )

    offpeak_savings = (total_baseline - after_offpeak).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    cache_savings = (after_offpeak - after_cache).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    batch_savings = (after_cache - optimized_total).quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    total_savings = (offpeak_savings + cache_savings + batch_savings).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )

    savings_pct = (
        ((total_savings / total_baseline) * Decimal("100")).quantize(
            RATE_QUANT, rounding=ROUND_HALF_EVEN
        )
        if total_baseline > ZERO_MONEY
        else ZERO_RATE
    )

    return AiFinOpsSummary(
        span_count=len(spans),
        ok_count=ok_count,
        error_count=error_count,
        blocked_count=blocked_count,
        total_tokens_in=total_in,
        total_tokens_out=total_out,
        total_tokens=total_in + total_out,
        total_cached_tokens_in=total_cached,
        total_cost_usd=total_baseline,
        optimized_cost_usd=optimized_total,
        offpeak_savings_usd=offpeak_savings,
        cache_savings_usd=cache_savings,
        batch_savings_usd=batch_savings,
        total_savings_usd=total_savings,
        savings_pct=savings_pct,
        by_tier=by_tier,
        by_model=by_model,
        by_skill=by_skill,
        by_operation=by_operation,
        by_competencia=by_competencia,
    )


def _validate_single_span_row(
    idx: int,
    row: dict[str, Any],
    *,
    string_cols: tuple[str, ...],
    nullable_cols: set[str],
    allowed_tiers: set[str],
    allowed_statuses: set[str],
    allowed_roles: set[str],
    invariants: dict[str, Any],
    violations: list[str],
) -> tuple[dict[str, Any], int]:
    cleaned: dict[str, Any] = {}
    sanitized_count = 0
    max_lengths = {"notes": 400}

    for col in string_cols:
        raw_val = row.get(col)
        if raw_val is None or not str(raw_val).strip():
            if col not in nullable_cols:
                violations.append(f"Row {idx}: required column '{col}' is null or empty.")
            cleaned[col] = None
            continue
        text = redact_telemetry_text(str(raw_val).strip(), max_length=max_lengths.get(col))
        sanitized, was_sanitized = sanitize_inbound_text(text)
        if was_sanitized:
            sanitized_count += 1
        cleaned[col] = sanitized

    if cleaned.get("tier") and cleaned["tier"] not in allowed_tiers:
        violations.append(f"Row {idx}: invalid tier '{redact_telemetry_text(str(cleaned['tier']))}'.")
    if cleaned.get("status") and cleaned["status"] not in allowed_statuses:
        violations.append(f"Row {idx}: invalid status '{redact_telemetry_text(str(cleaned['status']))}'.")
    if cleaned.get("agent_role") and cleaned["agent_role"] not in allowed_roles:
        violations.append(
            f"Row {idx}: invalid agent_role '{redact_telemetry_text(str(cleaned['agent_role']))}'."
        )

    if cleaned.get("timestamp_utc"):
        try:
            parse_utc_timestamp(str(cleaned["timestamp_utc"]))
        except ValueError as exc:
            violations.append(
                f"Row {idx}: invalid timestamp_utc ({redact_telemetry_text(str(exc))})."
            )
    if cleaned.get("competencia") and not _COMPETENCIA_RE.fullmatch(str(cleaned["competencia"])):
        violations.append(
            f"Row {idx}: invalid competencia format '{redact_telemetry_text(str(cleaned['competencia']))}'."
        )
    if cleaned.get("model"):
        try:
            resolve_model_name(str(cleaned["model"]), strict=True)
        except ValueError as exc:
            violations.append(
                f"Row {idx}: unknown model ({redact_telemetry_text(str(exc))})."
            )

    for int_col in ("tokens_in", "tokens_out", "tokens_total", "cached_tokens_in", "duration_ms"):
        raw_int = row.get(int_col)
        if isinstance(raw_int, bool) or not isinstance(raw_int, int):
            violations.append(f"Row {idx}: column '{int_col}' must be integer.")
            cleaned[int_col] = 0
        elif invariants.get("non_negative_tokens", True) and raw_int < 0:
            violations.append(f"Row {idx}: negative integer in '{int_col}' ({raw_int}).")
            cleaned[int_col] = 0
        elif raw_int > _INT64_MAX:
            violations.append(f"Row {idx}: integer overflow in '{int_col}' ({raw_int}).")
            cleaned[int_col] = 0
        else:
            cleaned[int_col] = raw_int

    if invariants.get("tokens_total_eq_sum", True) and (
        cleaned["tokens_total"] != cleaned["tokens_in"] + cleaned["tokens_out"]
    ):
        violations.append(
            f"Row {idx}: tokens_total ({cleaned['tokens_total']}) != "
            f"tokens_in + tokens_out ({cleaned['tokens_in'] + cleaned['tokens_out']})."
        )
    if invariants.get("cached_le_tokens_in", True) and (
        cleaned["cached_tokens_in"] > cleaned["tokens_in"]
    ):
        violations.append(
            f"Row {idx}: cached_tokens_in ({cleaned['cached_tokens_in']}) > "
            f"tokens_in ({cleaned['tokens_in']})."
        )

    for money_col in ("cost_usd", "cost_optimized_usd", "savings_usd"):
        raw_money = row.get(money_col)
        try:
            dec_val = to_decimal_money(raw_money)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            violations.append(
                f"Row {idx}: invalid decimal in '{money_col}' ({redact_telemetry_text(str(exc))})."
            )
            cleaned[money_col] = ZERO_MONEY
            continue
        if (invariants.get("non_negative_costs", True) and dec_val < ZERO_MONEY) or (
            dec_val > DECIMAL_18_4_MAX
        ):
            violations.append(f"Row {idx}: out-of-range DECIMAL(18,4) in '{money_col}' ({dec_val}).")
            cleaned[money_col] = ZERO_MONEY
        else:
            cleaned[money_col] = dec_val

    if invariants.get("cost_optimized_le_baseline", True):
        if cleaned["cost_optimized_usd"] > cleaned["cost_usd"]:
            violations.append(
                f"Row {idx}: cost_optimized_usd ({cleaned['cost_optimized_usd']}) > "
                f"cost_usd ({cleaned['cost_usd']})."
            )
        elif cleaned["cost_usd"] - cleaned["cost_optimized_usd"] != cleaned["savings_usd"]:
            violations.append(
                f"Row {idx}: savings_usd ({cleaned['savings_usd']}) != "
                f"cost_usd - cost_optimized_usd ({cleaned['cost_usd'] - cleaned['cost_optimized_usd']})."
            )

    return cleaned, sanitized_count


def validate_telemetry_spans(
    spans: Sequence[dict[str, Any]],
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], ContractValidationResult]:
    """Validate telemetry spans against ODCS v3.1 rules in ai_cloud_finops.contract.yaml."""
    if not spans:
        return [], ContractValidationResult(
            passed=False,
            violations=["Empty dataset: minimum volume is 1 telemetry span."],
            row_count=0,
        )
    if not isinstance(spans[0], dict):
        return [], ContractValidationResult(
            passed=False,
            violations=["Invalid row container: expected dict mappings."],
            row_count=len(spans),
        )

    schema_cols = contract.get("columns", {})
    required_cols = set(schema_cols.keys())
    present_cols = set(spans[0].keys())
    missing = sorted(required_cols - present_cols)
    if missing:
        return [], ContractValidationResult(
            passed=False,
            violations=[f"Missing required schema columns: {', '.join(missing)}"],
            row_count=len(spans),
        )

    string_cols = tuple(col for col, spec in schema_cols.items() if spec.get("type") == "STRING")
    nullable_cols = {col for col, spec in schema_cols.items() if bool(spec.get("nullable", False))}
    pk_cols = tuple(col for col, spec in schema_cols.items() if bool(spec.get("primary_key", False)))
    allowed_tiers = set(contract.get("allowed_tiers", ["T1", "T2", "T3", "T4"]))
    allowed_statuses = set(contract.get("allowed_statuses", ["ok", "error", "blocked"]))
    allowed_roles = set(contract.get("allowed_agent_roles", ["primary", "subagent"]))
    invariants = dict(contract.get("invariants", {}))

    violations: list[str] = []
    cleaned_rows: list[dict[str, Any]] = []
    seen_pks: dict[str, set[str]] = {col: set() for col in pk_cols}
    null_counts: dict[str, int] = {col: 0 for col in schema_cols}
    sanitized_total = 0

    for idx, row in enumerate(spans, 1):
        if not isinstance(row, dict):
            violations.append(f"Row {idx}: expected dict mapping, got {type(row).__name__}.")
            continue
        cleaned, san_cnt = _validate_single_span_row(
            idx,
            row,
            string_cols=string_cols,
            nullable_cols=nullable_cols,
            allowed_tiers=allowed_tiers,
            allowed_statuses=allowed_statuses,
            allowed_roles=allowed_roles,
            invariants=invariants,
            violations=violations,
        )
        sanitized_total += san_cnt
        for col in schema_cols:
            if cleaned.get(col) is None:
                null_counts[col] += 1
        for pk_col in pk_cols:
            pk_val = cleaned.get(pk_col)
            if pk_val is None:
                continue
            if pk_val in seen_pks[pk_col]:
                violations.append(f"Row {idx}: duplicate primary key '{pk_col}' value '{pk_val}'.")
            else:
                seen_pks[pk_col].add(pk_val)
        cleaned_rows.append(cleaned)

    total_rows = len(spans)
    for col, spec in schema_cols.items():
        max_ratio = Decimal(str(spec.get("max_null_ratio", "0")))
        observed_ratio = Decimal(null_counts[col]) / Decimal(total_rows)
        if observed_ratio > max_ratio:
            violations.append(
                f"Null ratio exceeded for '{col}': {observed_ratio:.4f} > {max_ratio:.4f}."
            )

    return cleaned_rows, ContractValidationResult(
        passed=not violations,
        violations=violations,
        row_count=total_rows,
        sanitized_cells=sanitized_total,
    )


def rows_to_ai_finops_table(rows: Sequence[dict[str, Any]]) -> pa.Table:
    """Convert validated AI FinOps spans to a typed PyArrow Table."""
    columns = {
        field.name: [row.get(field.name) for row in rows]
        for field in AI_FINOPS_ARROW_SCHEMA
    }
    return pa.table(columns, schema=AI_FINOPS_ARROW_SCHEMA)


def materialize_ai_finops(
    storage: HybridStorageEngine | Path,
    spans: Sequence[dict[str, Any]],
    *,
    contract_path: Path | None = None,
    batch_id: str | None = None,
    is_client_data: bool = False,
    repo_root: Path | None = None,
) -> MaterializationResult:
    """Validate AI/Cloud FinOps spans via ODCS v3.1 and persist Silver ZSTD Parquet."""
    data_dir = storage.data_dir if isinstance(storage, HybridStorageEngine) else Path(storage).resolve()
    target_silver = data_dir / "ai_cloud_finops" / "silver" / "ai_finops_telemetry.parquet"
    # Enforce Client Path Shield BEFORE creating SQLite/filesystem state inside public repo
    enforce_client_path_shield(
        target_silver,
        repo_root=repo_root,
        is_external_client_data=is_client_data,
    )

    engine = storage if isinstance(storage, HybridStorageEngine) else HybridStorageEngine(data_dir)
    resolved_batch = batch_id or f"ai_finops_{len(spans)}"
    contract = load_contract(contract_path or default_ai_finops_contract_path())
    cleaned_rows, validation = validate_telemetry_spans(spans, contract)

    if not validation.passed:
        redacted_violations = [redact_telemetry_text(v) for v in validation.violations]
        redacted_samples = [_redact_nested(row) for row in list(spans)[:25]]
        q_path = engine.write_quarantine(
            "ai_cloud_finops",
            resolved_batch,
            redacted_violations,
            redacted_samples,
        )
        return MaterializationResult(
            status="QUARANTINED",
            passed=False,
            batch_id=resolved_batch,
            row_count=len(spans),
            quarantine_path=q_path,
            violations=redacted_violations,
        )

    table = rows_to_ai_finops_table(cleaned_rows)
    manifest = engine.write_silver_parquet(
        "ai_cloud_finops",
        "ai_finops_telemetry",
        table,
        batch_id=resolved_batch,
    )
    return MaterializationResult(
        status="ACCEPTED",
        passed=True,
        batch_id=resolved_batch,
        row_count=manifest["row_count"],
        sha256=manifest["sha256"],
        silver_path=Path(manifest["silver_path"]),
    )
