"""Offline property and materialization tests for Phase 03 receivables aging engine."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finops_lakehouse.core.storage import HybridStorageEngine
from finops_lakehouse.generators.receivables_invoices import (
    generate_roll_rate_snapshots,
    generate_synthetic_invoices,
    make_synthetic_cnpj,
)
from finops_lakehouse.receivables.aging_engine import (
    AGING_BUCKETS,
    ROLL_RATE_STATES,
    build_staging_invoices,
    classify_aging_bucket,
    compute_roll_rate_matrix,
    materialize_receivables_aging,
    score_and_rank_receivables,
    summarize_aging_portfolio,
)


def test_classify_aging_bucket_boundaries() -> None:
    assert classify_aging_bucket(-10) == "a_vencer"
    assert classify_aging_bucket(0) == "a_vencer"
    assert classify_aging_bucket(1) == "0-30"
    assert classify_aging_bucket(30) == "0-30"
    assert classify_aging_bucket(31) == "31-60"
    assert classify_aging_bucket(60) == "31-60"
    assert classify_aging_bucket(61) == "61-90"
    assert classify_aging_bucket(90) == "61-90"
    assert classify_aging_bucket(91) == "91-180"
    assert classify_aging_bucket(180) == "91-180"
    assert classify_aging_bucket(181) == ">180"

    with pytest.raises(TypeError):
        classify_aging_bucket(True)


def test_synthetic_cnpj_is_reserved_prefix_and_valid_mod11() -> None:
    cnpj = make_synthetic_cnpj(1)
    assert len(cnpj) == 14
    assert cnpj.startswith("990000010001")
    assert cnpj.isdigit()

    w1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    w2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    r1 = sum(int(d) * w for d, w in zip(cnpj[:12], w1, strict=True)) % 11
    d1 = 0 if r1 < 2 else 11 - r1
    r2 = sum(int(d) * w for d, w in zip(cnpj[:13], w2, strict=True)) % 11
    d2 = 0 if r2 < 2 else 11 - r2
    assert cnpj[12:] == f"{d1}{d2}"

    with pytest.raises(TypeError):
        make_synthetic_cnpj(True)
    with pytest.raises(ValueError):
        make_synthetic_cnpj(0)
    with pytest.raises(ValueError):
        make_synthetic_cnpj(1_000_000)


@pytest.mark.parametrize("seed", [42, 7, 101, 2026])
def test_property_bucket_sum_equals_portfolio_total_across_seeds(seed: int) -> None:
    raw = generate_synthetic_invoices(seed=seed, count=600, as_of_date="2025-12-31")
    stg = build_staging_invoices(raw, as_of_date="2025-12-31")
    fct = score_and_rank_receivables(stg)
    summary = summarize_aging_portfolio(fct)

    assert summary["reconciliation_ok"] is True

    bucket_sum = sum(
        (Decimal(summary["buckets"][b]["valor_em_aberto"]) for b in AGING_BUCKETS),
        Decimal("0.0000"),
    )
    total_open = Decimal(summary["total_em_aberto"])
    total_a_vencer = Decimal(summary["total_a_vencer"])
    total_vencido = Decimal(summary["total_vencido"])

    assert bucket_sum == total_open
    assert total_a_vencer + total_vencido == total_open

    # Every bucket must be represented across the 600 synthetic invoices
    for b in AGING_BUCKETS:
        assert summary["buckets"][b]["qtd_faturas"] > 0


def test_ecl_and_ranking_invariants_without_floats() -> None:
    raw = generate_synthetic_invoices(seed=42, count=500, as_of_date="2025-12-31")
    stg = build_staging_invoices(raw, as_of_date="2025-12-31")
    fct = score_and_rank_receivables(stg)

    ranks = [row["ranking_cobranca"] for row in fct]
    assert ranks == list(range(1, len(fct) + 1))

    prev_score: Decimal | None = None
    for row in fct:
        for money_col in ("valor_original", "valor_pago", "valor_em_aberto", "valor_ecl"):
            assert isinstance(row[money_col], Decimal)
            assert not isinstance(row[money_col], float)
        for rate_col in ("pd_aplicada", "lgd_aplicada", "score_cobranca"):
            assert isinstance(row[rate_col], Decimal)
            assert not isinstance(row[rate_col], float)

        assert Decimal("0.0000") <= row["valor_ecl"] <= row["valor_em_aberto"]
        assert Decimal("0.000000") <= row["score_cobranca"] <= Decimal("100.000000")
        if prev_score is not None:
            assert row["score_cobranca"] <= prev_score
        prev_score = row["score_cobranca"]


def test_markov_roll_rate_matrix_probabilities_and_row_sums() -> None:
    prev_raw, curr_raw = generate_roll_rate_snapshots(seed=42, count=1200)
    prev_stg = build_staging_invoices(prev_raw, as_of_date="2025-11-30")
    curr_stg = build_staging_invoices(curr_raw, as_of_date="2025-12-31")

    matrix = compute_roll_rate_matrix(prev_stg, curr_stg)
    assert set(matrix.keys()) == set(AGING_BUCKETS)

    for src_bucket, transitions in matrix.items():
        assert set(transitions.keys()) == set(ROLL_RATE_STATES)
        for prob in transitions.values():
            assert isinstance(prob, Decimal)
            assert Decimal("0.000000") <= prob <= Decimal("1.000000")

        row_sum = sum(transitions.values(), Decimal("0.000000"))
        assert abs(row_sum - Decimal("1.000000")) <= Decimal("0.000010"), (
            f"Row sum mismatch for {src_bucket}: {row_sum}"
        )


def test_materialize_receivables_determinism_and_duckdb_parity(tmp_path: Path) -> None:
    res_a = materialize_receivables_aging(tmp_path / "run_a", seed=42, count=1200)
    res_b = materialize_receivables_aging(tmp_path / "run_b", seed=42, count=1200)

    assert res_a["status"] == "ACCEPTED"
    assert res_a["stg_manifest"]["sha256"] == res_b["stg_manifest"]["sha256"]
    assert res_a["fct_manifest"]["sha256"] == res_b["fct_manifest"]["sha256"]

    fct_path = Path(res_a["fct_manifest"]["silver_path"])
    pf = pq.ParquetFile(fct_path)
    assert pf.metadata.row_group(0).column(0).compression == "ZSTD"
    arrow_schema = pf.schema_arrow
    assert arrow_schema.field("valor_em_aberto").type == pa.decimal128(18, 4)
    assert arrow_schema.field("valor_ecl").type == pa.decimal128(18, 4)
    assert arrow_schema.field("score_cobranca").type == pa.decimal128(12, 6)

    storage = HybridStorageEngine(tmp_path / "run_a")
    db_rows = storage.query_silver_duckdb(
        fct_path,
        """
        SELECT
            faixa_aging,
            COUNT(*) AS cnt,
            SUM(valor_em_aberto) AS open_sum,
            SUM(valor_ecl) AS ecl_sum
        FROM silver_data
        GROUP BY faixa_aging
        """,
    )
    total_db_open = sum((r[2] for r in db_rows), Decimal("0.0000"))
    total_db_ecl = sum((r[3] for r in db_rows), Decimal("0.0000"))
    assert total_db_open == Decimal(res_a["portfolio"]["total_em_aberto"])
    assert total_db_ecl == Decimal(res_a["portfolio"]["total_ecl"])


def test_staging_rejects_broken_accounting_invariants_and_floats() -> None:
    base = generate_synthetic_invoices(seed=42, count=2, as_of_date="2025-12-31")

    # 1. Float rejection
    bad_float = [{**base[0], "valor_original": 100.0}]
    with pytest.raises(TypeError):
        build_staging_invoices(bad_float, as_of_date="2025-12-31")

    # 2. Paid + open != original
    bad_sum = [{**base[0], "valor_pago": Decimal("1.0000")}]
    with pytest.raises(ValueError, match="!="):
        build_staging_invoices(bad_sum, as_of_date="2025-12-31")

    # 3. Non-positive valor_original
    bad_orig = [
        {
            **base[0],
            "valor_original": Decimal("0.0000"),
            "valor_pago": Decimal("0.0000"),
            "valor_em_aberto": Decimal("0.0000"),
        }
    ]
    with pytest.raises(ValueError, match="valor_original must be > 0"):
        build_staging_invoices(bad_orig, as_of_date="2025-12-31")

    # 4. Duplicate invoice ID
    dup_rows = [base[0], {**base[1], "id_fatura_masked": base[0]["id_fatura_masked"]}]
    with pytest.raises(ValueError, match="duplicate id_fatura_masked"):
        build_staging_invoices(dup_rows, as_of_date="2025-12-31")


def test_all_paid_portfolio_and_roll_rate_zero_exposure_edge_case() -> None:
    base = generate_synthetic_invoices(seed=42, count=3, as_of_date="2025-12-31")
    all_paid = [
        {
            **row,
            "status_fatura": "PAID",
            "valor_pago": row["valor_original"],
            "valor_em_aberto": Decimal("0.0000"),
        }
        for row in base
    ]
    stg = build_staging_invoices(all_paid, as_of_date="2025-12-31")
    fct = score_and_rank_receivables(stg)
    summary = summarize_aging_portfolio(fct)

    assert summary["total_em_aberto"] == "0.0000"
    assert summary["total_ecl"] == "0.0000"
    assert summary["reconciliation_ok"] is True
    for row in fct:
        assert row["score_cobranca"] == Decimal("0.000000")
        assert row["valor_ecl"] == Decimal("0.0000")
        assert row["prioridade_cobranca"] == "P4_MONITORAR"
    for b in AGING_BUCKETS:
        assert summary["buckets"][b]["pct_carteira"] == "0.000000"

    zero_matrix = compute_roll_rate_matrix(stg, stg)
    for src in AGING_BUCKETS:
        assert all(prob == Decimal("0.000000") for prob in zero_matrix[src].values())


def test_missing_keys_and_unknown_buckets_raise_value_error() -> None:
    base = generate_synthetic_invoices(seed=42, count=2, as_of_date="2025-12-31")
    stg = build_staging_invoices(base, as_of_date="2025-12-31")

    with pytest.raises(ValueError, match="Missing PD rate"):
        score_and_rank_receivables(stg, pd_by_bucket={})
    with pytest.raises(ValueError, match="Missing LGD rate"):
        score_and_rank_receivables(stg, lgd_by_segment={})
    with pytest.raises(ValueError, match="within \\[0, 1\\]"):
        score_and_rank_receivables(stg, pd_by_bucket={"a_vencer": Decimal("1.500000")})
    with pytest.raises(ValueError, match="Unknown aging bucket"):
        score_and_rank_receivables([{**stg[0], "faixa_aging": "INVALID_BUCKET"}])
    with pytest.raises(ValueError, match="Unknown source aging bucket"):
        compute_roll_rate_matrix(
            [{**stg[0], "faixa_aging": "INVALID_SRC", "valor_em_aberto": Decimal("10.0000")}],
            stg,
        )
    with pytest.raises(ValueError, match="Unknown target aging bucket"):
        compute_roll_rate_matrix(
            [{**stg[0], "valor_em_aberto": Decimal("10.0000")}],
            [{**stg[0], "faixa_aging": "INVALID_DST", "valor_em_aberto": Decimal("10.0000")}],
        )
