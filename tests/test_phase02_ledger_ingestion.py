"""Offline tests for Phase 02: deterministic ledger, ODCS contracts, CWE-1236 & Path Shield."""

from __future__ import annotations

import csv
import json
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from finops_lakehouse.core.decimal_utils import (
    format_money,
    parse_spreadsheet_decimal,
    to_decimal_money,
    to_decimal_rate,
)
from finops_lakehouse.core.shield import (
    ClientPathShieldError,
    SpreadsheetSecurityError,
    enforce_client_path_shield,
    sanitize_cell_for_export,
    sanitize_inbound_text,
)
from finops_lakehouse.core.storage import HybridStorageEngine
from finops_lakehouse.generators.capex_opex_ledger import (
    generate_synthetic_ledger,
    materialize_synthetic_ledger,
)
from finops_lakehouse.ingestion.spreadsheet_ingestor import (
    export_rows_to_csv_safe,
    export_rows_to_xlsx_safe,
    ingest_ledger_file,
)


def _sample_balanced_rows(*, description: str = "Cloud infra", contrato: str = "CTR-001") -> list[dict[str, object]]:
    return [
        {
            "id_lancamento": "LNC-001-D",
            "id_lote": "LOTE-202501",
            "competencia": "2025-01",
            "data_lancamento": "2025-01-10",
            "cod_centro_custo": "CC-0100-INFRA",
            "cod_conta_contabil": "1.02.01.001",
            "id_contrato_masked": contrato,
            "tipo_despesa": "CAPEX",
            "moeda": "BRL",
            "valor_orcado": "1000.0000",
            "valor_debito": "1250.5000",
            "valor_credito": "0.0000",
            "descricao": description,
        },
        {
            "id_lancamento": "LNC-001-C",
            "id_lote": "LOTE-202501",
            "competencia": "2025-01",
            "data_lancamento": "2025-01-10",
            "cod_centro_custo": "CC-0100-INFRA",
            "cod_conta_contabil": "2.01.01.001",
            "id_contrato_masked": contrato,
            "tipo_despesa": "CAPEX",
            "moeda": "BRL",
            "valor_orcado": "0.0000",
            "valor_debito": "0.0000",
            "valor_credito": "1250.5000",
            "descricao": "Offset payable",
        },
    ]


def test_strict_no_float_precision_enforcement() -> None:
    with pytest.raises(TypeError):
        to_decimal_money(12.34)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_decimal_rate(0.05)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_decimal_money(True)  # type: ignore[arg-type]

    assert to_decimal_money("100.12345") == Decimal("100.1234")  # ROUND_HALF_EVEN
    assert to_decimal_money("100.12355") == Decimal("100.1236")
    assert format_money(Decimal("42.5")) == "42.5000"
    assert parse_spreadsheet_decimal(19.99) == Decimal("19.9900")


def test_synthetic_ledger_determinism_and_50k_volume() -> None:
    table_a, meta_a = generate_synthetic_ledger(seed=42)
    table_b, meta_b = generate_synthetic_ledger(seed=42)
    _, meta_other = generate_synthetic_ledger(seed=99)

    assert meta_a["row_count"] == 50_016
    assert meta_a["months"] == 24
    assert meta_a["double_entry_balanced"] is True
    assert meta_a["total_debit"] == meta_a["total_credit"]
    assert meta_a["sha256"] == meta_b["sha256"]
    assert meta_a["sha256"] != meta_other["sha256"]

    assert table_a.schema.field("valor_orcado").type == pa.decimal128(18, 4)
    assert table_a.schema.field("valor_debito").type == pa.decimal128(18, 4)
    assert table_a.schema.field("valor_credito").type == pa.decimal128(18, 4)
    assert table_a.schema.field("cod_centro_custo").type == pa.string()


def test_materialize_synthetic_ledger_duckdb_reconciliation(tmp_path: Path) -> None:
    manifest = materialize_synthetic_ledger(tmp_path, seed=42)
    assert manifest["status"] == "ACCEPTED"
    assert manifest["row_count"] == 50_016

    silver_path = Path(manifest["silver_path"])
    assert silver_path.is_file()

    storage = HybridStorageEngine(tmp_path)
    db_manifest = storage.get_manifest(manifest["batch_id"])
    assert db_manifest is not None
    assert db_manifest["sha256"] == manifest["sha256"]

    rows = storage.query_silver_duckdb(
        silver_path,
        """
        SELECT
            COUNT(*) AS cnt,
            COUNT(DISTINCT competencia) AS months_cnt,
            SUM(valor_debito) - SUM(valor_credito) AS net_balance
        FROM silver_data
        """,
    )
    cnt, months_cnt, net_balance = rows[0]
    assert cnt == 50_016
    assert months_cnt == 24
    assert net_balance == Decimal("0.0000")

    pf = pq.ParquetFile(silver_path)
    assert pf.metadata.row_group(0).column(0).compression == "ZSTD"


def test_csv_and_xlsx_ingestion_with_cwe1236_and_sentinels(tmp_path: Path) -> None:
    rows = [
        *_sample_balanced_rows(description="=CMD('calc')", contrato="SEM INFORMACAO"),
        *[
            {**r, "id_lancamento": r["id_lancamento"].replace("001", "002")}
            for r in _sample_balanced_rows(description="Regular software", contrato="CTR-002")
        ],
        *[
            {**r, "id_lancamento": r["id_lancamento"].replace("001", "003")}
            for r in _sample_balanced_rows(description="Annual support", contrato="CTR-003")
        ],
    ]

    csv_in = tmp_path / "inbound" / "ledger.csv"
    csv_in.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_in, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    csv_data_dir = tmp_path / "lake_csv"
    res_csv = ingest_ledger_file(csv_in, output_data_dir=csv_data_dir, batch_id="csv_batch_1")
    assert res_csv["status"] == "ACCEPTED"
    assert res_csv["sanitized_cells"] >= 1
    assert res_csv["sentinels_normalized"] == 2

    storage_csv = HybridStorageEngine(csv_data_dir)
    q_rows = storage_csv.query_silver_duckdb(
        Path(res_csv["silver_path"]),
        "SELECT id_contrato_masked, descricao FROM silver_data WHERE id_lancamento = 'LNC-001-D'",
    )
    assert q_rows[0][0] is None
    assert q_rows[0][1] == "'=CMD('calc')"

    # Null ceiling breach (> 35% nulls after sentinel normalization) must quarantine cleanly
    null_breach_rows = _sample_balanced_rows(description="All sentinel", contrato="99")
    breach_csv = tmp_path / "inbound" / "null_breach.csv"
    with open(breach_csv, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(null_breach_rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(null_breach_rows)
    res_breach = ingest_ledger_file(breach_csv, output_data_dir=tmp_path / "lake_breach", batch_id="breach_1")
    assert res_breach["status"] == "QUARANTINED"
    assert any("null ceiling" in v.lower() for v in res_breach["violations"])

    xlsx_in = tmp_path / "inbound" / "ledger.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(rows[0].keys()))
    for r in rows:
        ws.append(list(r.values()))
    wb.save(xlsx_in)
    wb.close()

    xlsx_data_dir = tmp_path / "lake_xlsx"
    res_xlsx = ingest_ledger_file(xlsx_in, output_data_dir=xlsx_data_dir, batch_id="xlsx_batch_1")
    assert res_xlsx["status"] == "ACCEPTED"
    assert res_xlsx["sha256"] == res_csv["sha256"]


def test_unbalanced_batch_quarantined_without_corrupting_silver(tmp_path: Path) -> None:
    data_dir = tmp_path / "lake"
    valid_csv = tmp_path / "valid.csv"
    valid_rows = _sample_balanced_rows()
    with open(valid_csv, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(valid_rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(valid_rows)

    first = ingest_ledger_file(valid_csv, output_data_dir=data_dir, batch_id="good_batch")
    assert first["status"] == "ACCEPTED"
    silver_bytes_before = Path(first["silver_path"]).read_bytes()

    bad_rows = _sample_balanced_rows()
    bad_rows[1]["valor_credito"] = "999.0000"  # Breaks double-entry balance
    bad_csv = tmp_path / "bad.csv"
    with open(bad_csv, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(bad_rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(bad_rows)

    second = ingest_ledger_file(bad_csv, output_data_dir=data_dir, batch_id="bad_batch")
    assert second["status"] == "QUARANTINED"
    assert any("imbalance" in v.lower() for v in second["violations"])
    q_report = Path(second["quarantine_path"])
    assert q_report.is_file()
    q_payload = json.loads(q_report.read_text(encoding="utf-8"))
    assert q_payload["status"] == "QUARANTINED"
    assert Path(first["silver_path"]).read_bytes() == silver_bytes_before


def test_client_path_shield_and_formula_export_and_magic_bytes(tmp_path: Path) -> None:
    fake_repo = tmp_path / "public_repo"
    (fake_repo / "data").mkdir(parents=True)
    external_dir = tmp_path / "operations" / "Clientes" / "ClienteX"

    with pytest.raises(ClientPathShieldError):
        enforce_client_path_shield(
            fake_repo / "data" / "client_ledger",
            fake_repo,
            is_external_client_data=True,
        )

    allowed = enforce_client_path_shield(
        external_dir / "ledger",
        fake_repo,
        is_external_client_data=True,
    )
    assert allowed == (external_dir / "ledger").resolve()

    # CWE-1236 inbound/outbound sanitization vs valid negative numeric literals
    assert sanitize_inbound_text("=SUM(A1)") == ("'=SUM(A1)", True)
    assert sanitize_inbound_text("\t+CMD()") == ("'+CMD()", True)
    assert sanitize_inbound_text("-123.45") == ("-123.45", False)
    assert sanitize_cell_for_export("@EVIL") == "'@EVIL"
    assert sanitize_cell_for_export(Decimal("-250.5")) == "-250.5000"

    export_rows = [{"nota": "=HYPERLINK(\"http://bad\")", "valor": Decimal("-10.25")}]
    csv_out = export_rows_to_csv_safe(
        export_rows,
        external_dir / "out.csv",
        repo_root=fake_repo,
        is_external_client_data=True,
    )
    csv_text = csv_out.read_text(encoding="utf-8")
    assert "'=HYPERLINK" in csv_text
    assert "-10.2500" in csv_text

    xlsx_out = export_rows_to_xlsx_safe(
        export_rows,
        external_dir / "out.xlsx",
        repo_root=fake_repo,
        is_external_client_data=True,
    )
    assert xlsx_out.is_file()

    # Spoofed magic bytes in .xlsx must fail with SpreadsheetSecurityError
    spoof_xlsx = tmp_path / "fake.xlsx"
    spoof_xlsx.write_bytes(b"NOT_A_ZIP_HEADER")
    with pytest.raises(SpreadsheetSecurityError):
        ingest_ledger_file(spoof_xlsx, output_data_dir=tmp_path / "out_lake")


def test_zip_bomb_batch_imbalance_pk_duplicate_overflow_and_sql_guard(tmp_path: Path) -> None:
    # 1. Zip-Bomb guard (> 100:1)
    bomb_xlsx = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(bomb_xlsx, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/sheet1.xml", b"A" * 500_000)
    with pytest.raises(SpreadsheetSecurityError, match="Zip-Bomb"):
        ingest_ledger_file(bomb_xlsx, output_data_dir=tmp_path / "bomb_lake")

    # 2. Per-(id_lote, competencia) imbalance that cancels globally must still quarantine
    lote_1 = _sample_balanced_rows()
    lote_1[0]["valor_debito"] = "1350.5000"  # +100.0000 net debit in LOTE-202501
    lote_2 = [
        {
            **r,
            "id_lancamento": r["id_lancamento"].replace("001", "002"),
            "id_lote": "LOTE-202502",
        }
        for r in _sample_balanced_rows()
    ]
    lote_2[1]["valor_credito"] = "1350.5000"  # +100.0000 net credit in LOTE-202502 (global cancels!)
    cancelling_csv = tmp_path / "cancelling.csv"
    all_cancel = [*lote_1, *lote_2]
    with open(cancelling_csv, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(all_cancel[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(all_cancel)
    res_cancel = ingest_ledger_file(
        cancelling_csv,
        output_data_dir=tmp_path / "cancel_lake",
        batch_id="cancel_batch",
    )
    assert res_cancel["status"] == "QUARANTINED"
    assert any("batch/period imbalance" in v.lower() for v in res_cancel["violations"])

    # 3. Duplicate primary key + DECIMAL(18,4) overflow must quarantine without ArrowInvalid
    dup_overflow = _sample_balanced_rows()
    dup_overflow[1]["id_lancamento"] = dup_overflow[0]["id_lancamento"]
    dup_overflow[0]["valor_orcado"] = "999999999999999.9999"
    bad_pk_csv = tmp_path / "dup_overflow.csv"
    with open(bad_pk_csv, "w", encoding="utf-8", newline="\n") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(dup_overflow[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(dup_overflow)
    res_pk = ingest_ledger_file(
        bad_pk_csv,
        output_data_dir=tmp_path / "pk_lake",
        batch_id="pk_batch",
    )
    assert res_pk["status"] == "QUARANTINED"
    assert any("duplicate primary key" in v.lower() for v in res_pk["violations"])
    assert any("exceeds decimal(18,4)" in v.lower() for v in res_pk["violations"])

    # 4. SQLite WAL handles close cleanly on Windows TemporaryDirectory + DuckDB mutating SQL blocked
    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_data = Path(tmp_dir)
        manifest = materialize_synthetic_ledger(temp_data, seed=42, months=1, pairs_per_month=5)
        storage = HybridStorageEngine(temp_data)
        with pytest.raises(ValueError, match="Mutating SQL"):
            storage.query_silver_duckdb(
                Path(manifest["silver_path"]),
                "DROP TABLE silver_data",
            )
