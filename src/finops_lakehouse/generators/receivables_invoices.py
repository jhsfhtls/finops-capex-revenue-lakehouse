"""Deterministic synthetic receivables invoice & roll-rate snapshot generator (seed=42)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from finops_lakehouse.core.decimal_utils import MONEY_QUANT, ZERO_MONEY

_SEGMENTS: tuple[str, ...] = ("ENTERPRISE", "MID_MARKET", "SMB", "PUBLIC_UTILITY_SYN")
_UFS: tuple[str, ...] = ("AM", "SP", "RJ", "MG", "PR", "BA", "RS", "SC")
_BUCKET_DAY_OFFSETS: tuple[tuple[int, int], ...] = (
    (-45, -1),   # a_vencer
    (1, 30),     # 0-30
    (31, 60),    # 31-60
    (61, 90),    # 61-90
    (91, 180),   # 91-180
    (181, 260),  # >180
)
_BPS_DIVISOR = Decimal("10000")
_CENTS_DIVISOR = Decimal("100")


def _compute_mod11_digit(digits: str, weights: tuple[int, ...]) -> int:
    total = sum(int(ch) * w for ch, w in zip(digits, weights, strict=True))
    rem = total % 11
    return 0 if rem < 2 else 11 - rem


def make_synthetic_cnpj(index: int) -> str:
    """Build a valid Mod-11 14-digit synthetic CNPJ using reserved 99xxxxxx prefix (zero PII)."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("Synthetic CNPJ index must be an integer.")
    if index <= 0 or index > 999_999:
        raise ValueError("Synthetic CNPJ index must be between 1 and 999,999.")
    base12 = f"{99_000_000 + index:08d}0001"
    d1 = _compute_mod11_digit(base12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    d2 = _compute_mod11_digit(f"{base12}{d1}", (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return f"{base12}{d1}{d2}"


def _allocate_payment(
    rng: random.Random,
    valor_original: Decimal,
    *,
    force_open_bias: bool = False,
) -> tuple[str, Decimal, Decimal]:
    """Deterministically assign invoice status and exact paid/open amounts."""
    roll = rng.randint(1, 100)
    if roll <= 20 and not force_open_bias:
        return "PAID", valor_original, ZERO_MONEY
    if roll <= 45:
        paid_bps = rng.randint(1500, 7500)
        paid = ((valor_original * Decimal(paid_bps)) / _BPS_DIVISOR).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_EVEN
        )
        return "PARTIAL", paid, valor_original - paid
    return "OPEN", ZERO_MONEY, valor_original


def generate_synthetic_invoices(
    seed: int = 42,
    count: int = 4000,
    as_of_date: str = "2025-12-31",
) -> list[dict[str, Any]]:
    """Generate deterministic synthetic receivables across all 6 aging buckets."""
    if count <= 0:
        raise ValueError("count must be a positive integer.")

    ref_date = date.fromisoformat(as_of_date)
    rng = random.Random(seed)
    cnpjs = [make_synthetic_cnpj(i) for i in range(1, 251)]
    invoices: list[dict[str, Any]] = []

    for idx in range(1, count + 1):
        day_low, day_high = _BUCKET_DAY_OFFSETS[(idx - 1) % len(_BUCKET_DAY_OFFSETS)]
        delay_days = rng.randint(day_low, day_high)
        due_date = ref_date - timedelta(days=delay_days)
        issue_date = due_date - timedelta(days=rng.randint(15, 45))

        base_cents = rng.randint(450_00, 120_000_00)
        valor_original = (Decimal(base_cents) / _CENTS_DIVISOR).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_EVEN
        )
        status, valor_pago, valor_em_aberto = _allocate_payment(rng, valor_original)
        cust_idx = (idx - 1) % len(cnpjs)

        invoices.append(
            {
                "id_fatura_masked": f"FAT-SYN-{idx:06d}",
                "id_contrato_masked": f"CTR-SYN-{(cust_idx % 90) + 10:04d}",
                "cnpj_14": cnpjs[cust_idx],
                "sigla_uf": _UFS[cust_idx % len(_UFS)],
                "segmento_cliente": _SEGMENTS[cust_idx % len(_SEGMENTS)],
                "competencia": issue_date.strftime("%Y-%m"),
                "data_emissao": issue_date.isoformat(),
                "data_vencimento": due_date.isoformat(),
                "status_fatura": status,
                "moeda": "BRL",
                "valor_original": valor_original,
                "valor_pago": valor_pago,
                "valor_em_aberto": valor_em_aberto,
            }
        )

    return invoices


def generate_roll_rate_snapshots(
    seed: int = 42,
    count: int = 2000,
    prev_as_of_date: str = "2025-11-30",
    curr_as_of_date: str = "2025-12-31",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate coherent t-1 and t invoice snapshots for Markov roll-rate transitions."""
    if count <= 0:
        raise ValueError("count must be a positive integer.")

    prev_ref = date.fromisoformat(prev_as_of_date)
    curr_ref = date.fromisoformat(curr_as_of_date)
    if curr_ref <= prev_ref:
        raise ValueError("curr_as_of_date must be strictly after prev_as_of_date.")

    rng = random.Random(seed + 101)
    cnpjs = [make_synthetic_cnpj(i) for i in range(1, 201)]
    prev_rows: list[dict[str, Any]] = []
    curr_rows: list[dict[str, Any]] = []

    for idx in range(1, count + 1):
        day_low, day_high = _BUCKET_DAY_OFFSETS[(idx - 1) % len(_BUCKET_DAY_OFFSETS)]
        delay_prev = rng.randint(day_low, day_high)
        due_date = prev_ref - timedelta(days=delay_prev)
        issue_date = due_date - timedelta(days=30)

        base_cents = rng.randint(500_00, 95_000_00)
        valor_original = (Decimal(base_cents) / _CENTS_DIVISOR).quantize(
            MONEY_QUANT, rounding=ROUND_HALF_EVEN
        )
        prev_status, prev_paid, prev_open = _allocate_payment(
            rng, valor_original, force_open_bias=True
        )
        cust_idx = (idx - 1) % len(cnpjs)

        base_record = {
            "id_fatura_masked": f"FAT-RR-{idx:06d}",
            "id_contrato_masked": f"CTR-SYN-{(cust_idx % 90) + 10:04d}",
            "cnpj_14": cnpjs[cust_idx],
            "sigla_uf": _UFS[cust_idx % len(_UFS)],
            "segmento_cliente": _SEGMENTS[cust_idx % len(_SEGMENTS)],
            "competencia": issue_date.strftime("%Y-%m"),
            "data_emissao": issue_date.isoformat(),
            "data_vencimento": due_date.isoformat(),
            "moeda": "BRL",
            "valor_original": valor_original,
        }
        prev_rows.append(
            {
                **base_record,
                "status_fatura": prev_status,
                "valor_pago": prev_paid,
                "valor_em_aberto": prev_open,
            }
        )

        # Simulate collection cure (full payment), partial recovery, or roll forward.
        cure_roll = rng.randint(1, 100)
        cure_threshold = 42 if delay_prev <= 30 else (28 if delay_prev <= 90 else 12)
        if cure_roll <= cure_threshold:
            curr_status, curr_paid, curr_open = "PAID", valor_original, ZERO_MONEY
        elif cure_roll <= cure_threshold + 18 and prev_open > Decimal("1.0000"):
            recovery_bps = rng.randint(2000, 6000)
            extra_paid = ((prev_open * Decimal(recovery_bps)) / _BPS_DIVISOR).quantize(
                MONEY_QUANT, rounding=ROUND_HALF_EVEN
            )
            curr_paid = prev_paid + extra_paid
            curr_open = valor_original - curr_paid
            curr_status = "PARTIAL" if curr_open > ZERO_MONEY else "PAID"
        else:
            curr_status, curr_paid, curr_open = prev_status, prev_paid, prev_open

        curr_rows.append(
            {
                **base_record,
                "status_fatura": curr_status,
                "valor_pago": curr_paid,
                "valor_em_aberto": curr_open,
            }
        )

    return prev_rows, curr_rows
