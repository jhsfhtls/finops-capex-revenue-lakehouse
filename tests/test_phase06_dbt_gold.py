"""Offline tests for Phase 06 dbt-duckdb Gold Star Schema and 100% docs governance."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import duckdb
import pyarrow.parquet as pq
import pytest

from finops_lakehouse.core.silver_bootstrap import (
    generate_synthetic_b2b_deals,
    materialize_b2b_deals,
    materialize_silver_layer,
)
from finops_lakehouse.core.storage import HybridStorageEngine

REPO_ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = REPO_ROOT / "dbt"


def _load_script_module(module_name: str, rel_path: str) -> ModuleType:
    """Load a repository script by absolute path without depending on process CWD."""
    target = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_lint_dbt_docs = _load_script_module("lint_dbt_docs", "scripts/lint_dbt_docs.py")
_materialize_silver_cli = _load_script_module(
    "materialize_silver_cli", "scripts/materialize_silver.py"
)
MODEL_PREFIX: str = _lint_dbt_docs.MODEL_PREFIX
evaluate_manifest = _lint_dbt_docs.evaluate_manifest


def _run_dbt_command(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run dbt CLI using the active Python environment cross-platform."""
    cmd = [sys.executable, "-m", "dbt.cli.main", *args]
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_silver_bootstrap_determinism_odcs_quarantine_and_fail_fast(tmp_path: Path) -> None:
    """Silver bootstrap validates Fail-Fast params, B2B ODCS quarantine, and SHA-256 parity."""
    with pytest.raises(TypeError):
        generate_synthetic_b2b_deals(seed=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        generate_synthetic_b2b_deals(count=0)

    # Fail-Fast must raise BEFORE creating disk state
    fail_fast_dir = tmp_path / "should_not_exist"
    with pytest.raises(ValueError):
        materialize_silver_layer(fail_fast_dir, seed=42, b2b_deals_count=0)
    assert not fail_fast_dir.exists()

    with pytest.raises(ValueError):
        materialize_silver_layer(fail_fast_dir, seed=42, ai_spans_count=0)
    assert not fail_fast_dir.exists()

    deals = generate_synthetic_b2b_deals(seed=42, count=36)
    assert len(deals) == 36
    assert {d["verdict"] for d in deals} == {"GO", "REPRICE_MARGIN", "NO_GO"}

    serialized = json.dumps([{k: str(v) for k, v in d.items()} for d in deals]).lower()
    assert "".join(["ae", "gea"]) not in serialized
    assert "".join(["indi", "cium"]) not in serialized

    # B2B ODCS contract violation must route to quarantine/ with secret/CPF redaction & CWE-1236 guard
    q_storage = HybridStorageEngine(tmp_path / "b2b_quarantine_test")
    synthetic_secret = "".join(["sk-", "proj-", "b2bleak1234567890"])
    synthetic_cpf = ".".join(["111", "222", "333"]) + "-44"
    corrupt_deals = [
        dict(deals[0]),
        {
            **deals[1],
            "id_contrato_masked": deals[0]["id_contrato_masked"],
            "id_cliente_masked": f"=CMD('calc') {synthetic_cpf}",
            "verdict": f"INVALID_{synthetic_secret}",
            "direct_delivery_cost": Decimal("-10.0000"),
        },
    ]
    q_res = materialize_b2b_deals(q_storage, corrupt_deals, batch_id="bad_b2b_batch")
    assert q_res["status"] == "QUARANTINED"
    q_file = Path(q_res["quarantine_path"])
    assert q_file.is_file()
    q_text = q_file.read_text(encoding="utf-8")
    assert synthetic_secret not in q_text
    assert synthetic_cpf not in q_text
    assert "'=CMD('calc')" in q_text
    assert any("duplicate primary key" in v for v in q_res["violations"])
    assert any("unsupported verdict" in v for v in q_res["violations"])

    res_a = materialize_silver_layer(tmp_path / "run_a", seed=42)
    res_b = materialize_silver_layer(tmp_path / "run_b", seed=42)
    assert res_a["status"] == "READY"

    expected_keys = {
        "capex_opex_ledger",
        "receivables_aging",
        "b2b_deal_economics",
        "ai_finops_telemetry",
        "llm_model_catalog",
    }
    assert set(res_a["silver_datasets"].keys()) == expected_keys
    for key in expected_keys:
        assert res_a["silver_datasets"][key]["sha256"] == res_b["silver_datasets"][key]["sha256"]
        assert Path(res_a["silver_datasets"][key]["path"]).is_file()


def test_materialize_silver_cli_success_and_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI creates nested FCL_DUCKDB_PATH parent dir on success and returns 2 on invalid seed."""
    nested_duckdb = tmp_path / "nested_gold" / "sub" / "finops.duckdb"
    monkeypatch.setenv("FCL_DUCKDB_PATH", str(nested_duckdb))
    target_data = tmp_path / "cli_data"

    rc = _materialize_silver_cli.main(["--data-dir", str(target_data), "--seed", "42"])
    assert rc == 0
    assert nested_duckdb.parent.is_dir()
    assert (target_data / "gold").is_dir()

    bad_rc = _materialize_silver_cli.main(["--data-dir", str(target_data), "--seed", "-1"])
    assert bad_rc == 2


def test_dbt_build_gold_star_schema_100pct_docs_and_reconciliation(tmp_path: Path) -> None:
    """dbt build, dbt docs generate, physical catalog lint (100%), zero float & Gold parity."""
    data_dir = tmp_path / "lake data"
    silver_meta = materialize_silver_layer(data_dir, seed=42)
    gold_db = data_dir / "gold" / "finops_lakehouse.duckdb"
    dbt_target = tmp_path / "dbt_target"
    dbt_logs = tmp_path / "dbt_logs"

    env = os.environ.copy()
    # Native str(data_dir) tests Windows backslash normalization in sources.yml as well as spaces
    env["FCL_DATA_DIR"] = str(data_dir)
    env["FCL_DUCKDB_PATH"] = gold_db.as_posix()
    env["DBT_PROFILES_DIR"] = DBT_DIR.as_posix()

    common_flags = [
        "--project-dir",
        DBT_DIR.as_posix(),
        "--profiles-dir",
        DBT_DIR.as_posix(),
        "--target-path",
        dbt_target.as_posix(),
        "--log-path",
        dbt_logs.as_posix(),
    ]

    build_res = _run_dbt_command(["build", *common_flags], env=env)
    assert build_res.returncode == 0, f"dbt build failed:\n{build_res.stdout}\n{build_res.stderr}"

    docs_res = _run_dbt_command(["docs", "generate", *common_flags], env=env)
    assert docs_res.returncode == 0, f"dbt docs generate failed:\n{docs_res.stdout}\n{docs_res.stderr}"

    manifest_path = dbt_target / "manifest.json"
    catalog_path = dbt_target / "catalog.json"
    assert manifest_path.is_file()
    assert catalog_path.is_file()
    assert evaluate_manifest(manifest_path, catalog_path=catalog_path) == 0

    # Negative check: lint_dbt_docs MUST reject when catalog.json has an undocumented SQL column
    tampered_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    dim_uid = f"{MODEL_PREFIX}dim_conta_contabil"
    tampered_catalog["nodes"][dim_uid]["columns"]["undocumented_ghost_col"] = {
        "type": "VARCHAR",
        "index": 99,
        "name": "undocumented_ghost_col",
    }
    tampered_catalog_path = tmp_path / "tampered_catalog.json"
    tampered_catalog_path.write_text(json.dumps(tampered_catalog), encoding="utf-8")
    assert evaluate_manifest(manifest_path, catalog_path=tampered_catalog_path) == 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_dims = {
        "dim_calendario_fiscal",
        "dim_centro_custo",
        "dim_conta_contabil",
        "dim_cliente_segmento",
        "dim_llm_model",
    }
    expected_facts = {
        "fct_capex_opex_variance",
        "fct_receivables_aging",
        "fct_b2b_deal_economics",
        "fct_ai_finops_consumption",
    }
    expected_staging = {
        "stg_capex_opex_ledger",
        "stg_receivables_aging",
        "stg_b2b_deal_economics",
        "stg_ai_finops_telemetry",
        "stg_llm_model_catalog",
    }

    ledger_tbl = pq.read_table(silver_meta["silver_datasets"]["capex_opex_ledger"]["path"])
    silver_budget = sum(
        (v.as_py() for v in ledger_tbl["valor_orcado"]), Decimal("0.0000")
    )
    aging_tbl = pq.read_table(silver_meta["silver_datasets"]["receivables_aging"]["path"])
    silver_ecl = sum((v.as_py() for v in aging_tbl["valor_ecl"]), Decimal("0.0000"))

    with duckdb.connect(str(gold_db), read_only=True) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        views = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_type='VIEW'"
            ).fetchall()
        }
        assert tables == (expected_dims | expected_facts)
        assert views == expected_staging

        float_cols = conn.execute(
            "SELECT table_name, column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema='main' "
            "AND (UPPER(data_type) LIKE '%DOUBLE%' OR UPPER(data_type) LIKE '%FLOAT%')"
        ).fetchall()
        assert float_cols == [], f"Unexpected floating-point SQL columns: {float_cols}"

        for model_name in sorted(expected_dims | expected_facts | expected_staging):
            actual_cols = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='main' AND table_name = ?",
                    [model_name],
                ).fetchall()
            }
            node = manifest["nodes"][f"{MODEL_PREFIX}{model_name}"]
            documented_cols = {
                col_name
                for col_name, col_meta in node["columns"].items()
                if str(col_meta.get("description", "")).strip()
            }
            assert actual_cols == documented_cols, f"Column doc drift in {model_name}"

        assert conn.execute("SELECT COUNT(*) FROM dim_calendario_fiscal").fetchone()[0] >= 24
        assert conn.execute("SELECT COUNT(*) FROM dim_centro_custo").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM dim_conta_contabil").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM dim_llm_model").fetchone()[0] == 44
        assert conn.execute("SELECT COUNT(*) FROM dim_cliente_segmento").fetchone()[0] == 250

        capex_stats = conn.execute(
            "SELECT SUM(qtd_lancamentos), SUM(valor_debito_total) - SUM(valor_credito_total), "
            "SUM(valor_orcado_total), SUM(valor_realizado_total), SUM(variacao_orcamentaria) "
            "FROM fct_capex_opex_variance"
        ).fetchone()
        assert capex_stats[0] == silver_meta["silver_datasets"]["capex_opex_ledger"]["row_count"]
        assert Decimal(str(capex_stats[1])) == Decimal("0.0000")
        assert Decimal(str(capex_stats[2])) == silver_budget
        assert Decimal(str(capex_stats[2])) - Decimal(str(capex_stats[3])) == Decimal(
            str(capex_stats[4])
        )

        aging_stats = conn.execute(
            "SELECT COUNT(*), SUM(valor_original) - (SUM(valor_pago) + SUM(valor_em_aberto)), "
            "SUM(valor_ecl), "
            "SUM(CASE WHEN is_vencido AND NOT (dias_atraso > 0 AND valor_em_aberto > 0) "
            "THEN 1 ELSE 0 END) "
            "FROM fct_receivables_aging"
        ).fetchone()
        assert aging_stats[0] == silver_meta["silver_datasets"]["receivables_aging"]["row_count"]
        assert Decimal(str(aging_stats[1])) == Decimal("0.0000")
        assert Decimal(str(aging_stats[2])) == silver_ecl
        assert aging_stats[3] == 0

        b2b_cnt = conn.execute("SELECT COUNT(*) FROM fct_b2b_deal_economics").fetchone()[0]
        assert b2b_cnt == silver_meta["silver_datasets"]["b2b_deal_economics"]["row_count"]
        b2b_verdicts = {
            r[0] for r in conn.execute("SELECT DISTINCT verdict FROM fct_b2b_deal_economics").fetchall()
        }
        assert b2b_verdicts == {"GO", "REPRICE_MARGIN", "NO_GO"}

        ai_stats = conn.execute(
            "SELECT COUNT(*), SUM(cost_usd) - SUM(cost_optimized_usd) - SUM(savings_usd) "
            "FROM fct_ai_finops_consumption"
        ).fetchone()
        assert ai_stats[0] == silver_meta["silver_datasets"]["ai_finops_telemetry"]["row_count"]
        assert Decimal(str(ai_stats[1])) == Decimal("0.0000")
