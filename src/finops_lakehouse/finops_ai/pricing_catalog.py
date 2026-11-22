"""POP_T_158 v1.4.0 multi-provider LLM pricing catalog & tier rules in pure Decimal (ADR-001)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

from finops_lakehouse.core.decimal_utils import (
    MONEY_QUANT,
    RATE_QUANT,
    ZERO_MONEY,
    require_non_negative_int,
    to_decimal_rate,
)

PRICING_CATALOG_VERSION: Final[str] = "POP_T_158_v1.4.0"
ONE_MILLION: Final[Decimal] = Decimal("1000000")
ZERO_RATE: Final[Decimal] = Decimal("0.000000")
ONE_RATE: Final[Decimal] = Decimal("1.000000")
DEFAULT_CACHE_DISCOUNT_RATE: Final[Decimal] = Decimal("0.750000")
DEFAULT_BATCH_DISCOUNT_RATE: Final[Decimal] = Decimal("0.500000")
DEEPSEEK_OFFPEAK_DISCOUNT_RATE: Final[Decimal] = Decimal("0.500000")
_PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


@dataclass(frozen=True)
class ModelPricingSpec:
    """Canonical model pricing specification synchronized with POP_T_158 v1.4.0."""

    model_id: str
    provider: str
    tier: str
    input_usd_per_m: Decimal
    output_usd_per_m: Decimal


def _spec(
    model_id: str,
    provider: str,
    tier: str,
    input_usd_per_m: str,
    output_usd_per_m: str,
) -> ModelPricingSpec:
    return ModelPricingSpec(
        model_id=model_id,
        provider=provider,
        tier=tier,
        input_usd_per_m=to_decimal_rate(Decimal(input_usd_per_m)),
        output_usd_per_m=to_decimal_rate(Decimal(output_usd_per_m)),
    )


# All 44 canonical models synchronized with core/scripts/agent_tools/finops_budget_guard.py
# and core/scripts/agent_tools/model_router.py (POP_T_158 v1.4.0).
LLM_PRICING_CATALOG: Final[dict[str, ModelPricingSpec]] = {
    "gemini-4-carbon": _spec("gemini-4-carbon", "google", "T4", "2.00", "10.00"),
    "gemini-4-carbon-standard": _spec("gemini-4-carbon-standard", "google", "T4", "4.00", "20.00"),
    "gemini-4-neon": _spec("gemini-4-neon", "google", "T3", "3.00", "15.00"),
    "gemini-4-hydrogen": _spec("gemini-4-hydrogen", "google", "T2", "1.50", "7.50"),
    "gemini-4-argon": _spec("gemini-4-argon", "google", "T3", "3.00", "15.00"),
    "gemini-3.8-flash": _spec("gemini-3.8-flash", "google", "T1", "0.75", "3.75"),
    "claude-fable-5.2": _spec("claude-fable-5.2", "anthropic", "T4", "8.00", "40.00"),
    "claude-fable-5.2-fast": _spec("claude-fable-5.2-fast", "anthropic", "T4", "16.00", "80.00"),
    "claude-mythos-5.2": _spec("claude-mythos-5.2", "anthropic", "T4", "8.00", "40.00"),
    "claude-opus-5.5": _spec("claude-opus-5.5", "anthropic", "T3", "4.00", "20.00"),
    "claude-opus-5.5-fast": _spec("claude-opus-5.5-fast", "anthropic", "T3", "8.00", "40.00"),
    "claude-sonnet-5": _spec("claude-sonnet-5", "anthropic", "T2", "2.00", "10.00"),
    "claude-sonnet-5.5": _spec("claude-sonnet-5.5", "anthropic", "T2", "2.00", "10.00"),
    "claude-haiku-4.5": _spec("claude-haiku-4.5", "anthropic", "T1", "1.00", "5.00"),
    "claude-haiku-5.5": _spec("claude-haiku-5.5", "anthropic", "T1", "0.10", "0.50"),
    "gpt-6-cyber": _spec("gpt-6-cyber", "openai", "T4", "20.00", "100.00"),
    "gpt-6.2-astra": _spec("gpt-6.2-astra", "openai", "T4", "8.00", "40.00"),
    "gpt-6-astra": _spec("gpt-6-astra", "openai", "T4", "10.00", "50.00"),
    "gpt-6.2-sol": _spec("gpt-6.2-sol", "openai", "T2", "2.00", "10.00"),
    "gpt-6-sol": _spec("gpt-6-sol", "openai", "T2", "2.00", "10.00"),
    "gpt-6.1-sol": _spec("gpt-6.1-sol", "openai", "T2", "2.00", "10.00"),
    "gpt-6.1-terra": _spec("gpt-6.1-terra", "openai", "T2", "1.00", "5.00"),
    "gpt-6-luna": _spec("gpt-6-luna", "openai", "T1", "0.10", "0.50"),
    "kimi-k3.1-pro": _spec("kimi-k3.1-pro", "moonshot", "T3", "2.00", "10.00"),
    "kimi-k2": _spec("kimi-k2", "moonshot", "T2", "1.00", "3.00"),
    "kimi-k1.5": _spec("kimi-k1.5", "moonshot", "T2", "1.00", "3.00"),
    "muse-spark-1.3": _spec("muse-spark-1.3", "meta", "T2", "1.25", "4.25"),
    "muse-ember": _spec("muse-ember", "meta", "T1", "0.10", "0.50"),
    "llama-4": _spec("llama-4", "meta", "T2", "0.40", "1.20"),
    "llama-3.3-70b": _spec("llama-3.3-70b", "meta", "T1", "0.40", "1.20"),
    "deepseek-v4.1": _spec("deepseek-v4.1", "deepseek", "T1", "0.30", "1.20"),
    "deepseek-v4.1-flash": _spec("deepseek-v4.1-flash", "deepseek", "T1", "0.30", "1.20"),
    "deepseek-v4.1-flash-offpeak": _spec(
        "deepseek-v4.1-flash-offpeak", "deepseek", "T1", "0.15", "0.60"
    ),
    "deepseek-v4-pro": _spec("deepseek-v4-pro", "deepseek", "T3", "0.66", "1.98"),
    "deepseek-r1": _spec("deepseek-r1", "deepseek", "T2", "0.30", "1.20"),
    "grok-4.7": _spec("grok-4.7", "xai", "T3", "2.00", "6.00"),
    "mistral-large-4": _spec("mistral-large-4", "mistral", "T2", "0.70", "2.10"),
    "mistral-medium-3.5": _spec("mistral-medium-3.5", "mistral", "T3", "1.50", "7.50"),
    "mistral-small-4": _spec("mistral-small-4", "mistral", "T1", "0.15", "0.60"),
    "minimax-m3.1-flash": _spec("minimax-m3.1-flash", "minimax", "T1", "0.15", "0.60"),
    "glm-5.3": _spec("glm-5.3", "zhipu", "T2", "1.10", "4.69"),
    "qwen4-max": _spec("qwen4-max", "alibaba", "T2", "1.50", "4.50"),
    "qwen3.8-flash": _spec("qwen3.8-flash", "alibaba", "T1", "0.15", "0.47"),
    "local-ollama": _spec("local-ollama", "local", "T1", "0.00", "0.00"),
}

MODEL_TIER_MAP: Final[dict[str, str]] = {
    model_id: spec.tier for model_id, spec in LLM_PRICING_CATALOG.items()
}

MODEL_ALIASES: Final[dict[str, str]] = {
    "carbon": "gemini-4-carbon",
    "carbon-standard": "gemini-4-carbon-standard",
    "neon": "gemini-4-neon",
    "gemini-neon": "gemini-4-neon",
    "gemini-4-neon-low": "gemini-4-neon",
    "gemini-4-neon-medium": "gemini-4-neon",
    "gemini-4-neon-high": "gemini-4-neon",
    "gemini-4-neon-xhigh": "gemini-4-neon",
    "hydrogen": "gemini-4-hydrogen",
    "gemini-4-hydrogen-low": "gemini-4-hydrogen",
    "gemini-4-hydrogen-medium": "gemini-4-hydrogen",
    "gemini-4-hydrogen-high": "gemini-4-hydrogen",
    "gemini-4-hydrogen-xhigh": "gemini-4-hydrogen",
    "orchestrator": "gemini-4-carbon",
    "orchestrator-neon": "gemini-4-neon",
    "gemini-pro-orchestrator-neon": "gemini-4-neon",
    "orchestrator-argon": "gemini-4-argon",
    "gemini-pro-orchestrator-g1-pro": "gemini-4-argon",
    "orchestrator-carbon": "gemini-4-carbon",
    "argon": "gemini-4-argon",
    "gemini-argon": "gemini-4-argon",
    "gemini-4-argon-low": "gemini-4-argon",
    "gemini-4-argon-medium": "gemini-4-argon",
    "gemini-4-argon-high": "gemini-4-argon",
    "gemini-4-argon-xhigh": "gemini-4-argon",
    "flash": "gemini-3.8-flash",
    "gemini-flash": "gemini-3.8-flash",
    "gemini-3.8-flash-low": "gemini-3.8-flash",
    "gemini-3.8-flash-medium": "gemini-3.8-flash",
    "gemini-3.8-flash-high": "gemini-3.8-flash",
    "gemini-1.5-flash": "gemini-3.8-flash",
    "fable": "claude-fable-5.2",
    "fable-fast": "claude-fable-5.2-fast",
    "mythos": "claude-mythos-5.2",
    "opus": "claude-opus-5.5",
    "opus-fast": "claude-opus-5.5-fast",
    "sonnet": "claude-sonnet-5",
    "sonnet-5.5": "claude-sonnet-5.5",
    "haiku": "claude-haiku-4.5",
    "haiku-5.5": "claude-haiku-5.5",
    "cyber": "gpt-6-cyber",
    "astra": "gpt-6-astra",
    "astra-6.2": "gpt-6.2-astra",
    "gpt-6": "gpt-6-astra",
    "gpt-6.2": "gpt-6.2-astra",
    "gpt-6.1": "gpt-6.1-sol",
    "sol": "gpt-6-sol",
    "sol-6.1": "gpt-6.1-sol",
    "sol-6.2": "gpt-6.2-sol",
    "terra": "gpt-6.1-terra",
    "luna": "gpt-6-luna",
    "kimi": "kimi-k2",
    "kimi-pro": "kimi-k3.1-pro",
    "kimi-k3.1": "kimi-k3.1-pro",
    "kimi-3.1": "kimi-k3.1-pro",
    "ember": "muse-ember",
    "spark": "muse-spark-1.3",
    "llama": "llama-4",
    "llama-4-scout": "llama-4",
    "llama-scout": "llama-4",
    "llama-4-maverick": "llama-4",
    "deepseek": "deepseek-v4.1",
    "deepseek-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1-flash-1017": "deepseek-v4.1-flash",
    "deepseek-offpeak": "deepseek-v4.1-flash-offpeak",
    "deepseek-pro": "deepseek-v4-pro",
    "grok": "grok-4.7",
    "mistral-large": "mistral-large-4",
    "mistral-medium": "mistral-medium-3.5",
    "mistral-small": "mistral-small-4",
    "minimax": "minimax-m3.1-flash",
    "glm": "glm-5.3",
    "qwen": "qwen3.8-flash",
    "qwen4": "qwen4-max",
    "qwen-4-max": "qwen4-max",
    "qwen-max": "qwen4-max",
    "qwen-flash": "qwen3.8-flash",
    "qwen3.8-flash-next": "qwen3.8-flash",
    "qwen3.8-local": "local-ollama",
    "qwen3.8-27b-local": "local-ollama",
    "ollama": "local-ollama",
}

# Billing cliffs: (input_token_threshold, tiered_input_usd_per_m, tiered_output_usd_per_m)
TIERED_RATE_OVERRIDES: Final[dict[str, tuple[int, Decimal, Decimal]]] = {
    "claude-haiku-5.5": (100_000, to_decimal_rate(Decimal("0.50")), to_decimal_rate(Decimal("2.50"))),
    "grok-4.7": (200_000, to_decimal_rate(Decimal("4.00")), to_decimal_rate(Decimal("12.00"))),
    "gpt-6-cyber": (272_000, to_decimal_rate(Decimal("40.00")), to_decimal_rate(Decimal("150.00"))),
}


@dataclass(frozen=True)
class SpanCostBreakdown:
    """Deterministic Decimal breakdown of a single LLM span cost and optimization delta."""

    model: str
    provider: str
    tier: str
    tokens_in: int
    tokens_out: int
    tokens_total: int
    cached_tokens_in: int
    cliff_triggered: bool
    offpeak_applied: bool
    batch_applied: bool
    effective_input_rate_usd_per_m: Decimal
    effective_output_rate_usd_per_m: Decimal
    cost_usd: Decimal
    cost_optimized_usd: Decimal
    savings_usd: Decimal
    cost_usd_precise: Decimal


def validate_unit_rate(value: Decimal | int | str, name: str) -> Decimal:
    """Validate a Decimal/int/str ratio strictly within [0, 1] with DECIMAL(12,6)."""
    rate = to_decimal_rate(value)
    if not (ZERO_RATE <= rate <= ONE_RATE):
        raise ValueError(f"{name} must be within [0, 1], got {rate}.")
    return rate


def parse_utc_timestamp(timestamp_utc: str) -> datetime:
    """Parse an ISO-8601 timestamp string and normalize it to UTC datetime."""
    if not isinstance(timestamp_utc, str) or not timestamp_utc.strip():
        raise ValueError("timestamp_utc must be a non-empty ISO-8601 string.")
    normalized = timestamp_utc.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def resolve_model_name(model_name: str, *, strict: bool = False) -> str:
    """Normalize a model identifier or alias to its canonical POP_T_158 catalog key."""
    if not isinstance(model_name, str):
        raise TypeError("model_name must be a string.")
    without_effort = _PAREN_SUFFIX_RE.sub("", model_name.strip()).lower()
    cleaned = re.sub(r"[\s_]+", "-", without_effort)
    if not cleaned:
        if strict:
            raise ValueError("model_name cannot be empty.")
        return "gemini-3.8-flash"
    if cleaned in LLM_PRICING_CATALOG:
        return cleaned
    if cleaned in MODEL_ALIASES:
        return MODEL_ALIASES[cleaned]
    if not strict and len(cleaned) >= 4:
        for key in LLM_PRICING_CATALOG:
            if cleaned in key:
                return key
    if strict:
        raise ValueError(f"Unknown model identifier '{model_name}' in {PRICING_CATALOG_VERSION}.")
    return "gemini-3.8-flash"


def get_model_spec(model_name: str, *, strict: bool = False) -> ModelPricingSpec:
    """Return the canonical POP_T_158 pricing specification for a model or alias."""
    canonical = resolve_model_name(model_name, strict=strict)
    return LLM_PRICING_CATALOG[canonical]


def _rates_for_spec(spec: ModelPricingSpec, tokens_in: int) -> tuple[Decimal, Decimal, bool]:
    cliff = TIERED_RATE_OVERRIDES.get(spec.model_id)
    if cliff and tokens_in > cliff[0]:
        return cliff[1], cliff[2], True
    return spec.input_usd_per_m, spec.output_usd_per_m, False


def resolve_effective_rates(model_name: str, tokens_in: int) -> tuple[Decimal, Decimal, bool]:
    """Return (input_usd_per_m, output_usd_per_m, cliff_triggered) for prompt size."""
    validated_in = require_non_negative_int(tokens_in, "tokens_in")
    spec = get_model_spec(model_name)
    return _rates_for_spec(spec, validated_in)


def is_deepseek_offpeak_utc(timestamp_utc: str) -> bool:
    """Return True when UTC timestamp falls outside Mon-Fri [01:00, 04:00) and [06:00, 10:00) Peak."""
    dt_utc = parse_utc_timestamp(timestamp_utc)
    if dt_utc.weekday() >= 5:
        return True
    in_morning_peak = 1 <= dt_utc.hour < 4
    in_day_peak = 6 <= dt_utc.hour < 10
    return not (in_morning_peak or in_day_peak)


def calculate_span_cost(
    model_name: str,
    tokens_in: int,
    tokens_out: int,
    *,
    cached_tokens_in: int = 0,
    cache_discount_rate: Decimal | int | str = DEFAULT_CACHE_DISCOUNT_RATE,
    batch_mode: bool = False,
    offpeak_mode: bool | None = None,
    timestamp_utc: str | None = None,
    batch_discount_rate: Decimal | int | str = DEFAULT_BATCH_DISCOUNT_RATE,
) -> SpanCostBreakdown:
    """Compute baseline and optimized span costs in pure DECIMAL(18,4) with zero float."""
    if not isinstance(batch_mode, bool):
        raise TypeError("batch_mode must be a boolean.")
    if offpeak_mode is not None and not isinstance(offpeak_mode, bool):
        raise TypeError("offpeak_mode must be a boolean or None.")

    valid_in = require_non_negative_int(tokens_in, "tokens_in")
    valid_out = require_non_negative_int(tokens_out, "tokens_out")
    valid_cached = require_non_negative_int(cached_tokens_in, "cached_tokens_in")
    if valid_cached > valid_in:
        raise ValueError(
            f"cached_tokens_in ({valid_cached}) cannot exceed tokens_in ({valid_in})."
        )

    cache_disc = validate_unit_rate(cache_discount_rate, "cache_discount_rate")
    batch_disc = validate_unit_rate(batch_discount_rate, "batch_discount_rate")

    spec = get_model_spec(model_name)
    base_in_rate, base_out_rate, cliff_triggered = _rates_for_spec(spec, valid_in)

    raw_baseline = (
        Decimal(valid_in) * base_in_rate + Decimal(valid_out) * base_out_rate
    ) / ONE_MILLION
    cost_usd_precise = raw_baseline.quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)
    baseline_money = raw_baseline.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)

    apply_offpeak = False
    if spec.model_id == "deepseek-v4.1-flash":
        if offpeak_mode is True:
            apply_offpeak = True
        elif offpeak_mode is None and timestamp_utc:
            apply_offpeak = is_deepseek_offpeak_utc(timestamp_utc)

    offpeak_multiplier = (ONE_RATE - DEEPSEEK_OFFPEAK_DISCOUNT_RATE) if apply_offpeak else ONE_RATE
    batch_multiplier = (ONE_RATE - batch_disc) if batch_mode else ONE_RATE

    uncached_in = valid_in - valid_cached
    cached_multiplier = ONE_RATE - cache_disc

    effective_in_rate = (base_in_rate * offpeak_multiplier * batch_multiplier).quantize(
        RATE_QUANT, rounding=ROUND_HALF_EVEN
    )
    effective_out_rate = (base_out_rate * offpeak_multiplier * batch_multiplier).quantize(
        RATE_QUANT, rounding=ROUND_HALF_EVEN
    )

    opt_in_tokens_weighted = Decimal(uncached_in) + Decimal(valid_cached) * cached_multiplier
    raw_optimized = (
        (opt_in_tokens_weighted * base_in_rate + Decimal(valid_out) * base_out_rate)
        * offpeak_multiplier
        * batch_multiplier
    ) / ONE_MILLION
    optimized_money = raw_optimized.quantize(MONEY_QUANT, rounding=ROUND_HALF_EVEN)
    optimized_money = min(baseline_money, max(ZERO_MONEY, optimized_money))
    savings_money = (baseline_money - optimized_money).quantize(
        MONEY_QUANT, rounding=ROUND_HALF_EVEN
    )

    return SpanCostBreakdown(
        model=spec.model_id,
        provider=spec.provider,
        tier=spec.tier,
        tokens_in=valid_in,
        tokens_out=valid_out,
        tokens_total=valid_in + valid_out,
        cached_tokens_in=valid_cached,
        cliff_triggered=cliff_triggered,
        offpeak_applied=apply_offpeak,
        batch_applied=batch_mode,
        effective_input_rate_usd_per_m=effective_in_rate,
        effective_output_rate_usd_per_m=effective_out_rate,
        cost_usd=baseline_money,
        cost_optimized_usd=optimized_money,
        savings_usd=savings_money,
        cost_usd_precise=cost_usd_precise,
    )
