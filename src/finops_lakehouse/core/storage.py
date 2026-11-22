"""Hybrid Medallion Storage Engine: SQLite WAL Bronze + DuckDB Parquet ZSTD Silver (ADR-001)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

_BANNED_SQL_PATTERN = re.compile(
    r"\b(DROP|DELETE|TRUNCATE|ALTER|UPDATE|INSERT|CREATE|ATTACH|COPY)\b",
    re.IGNORECASE,
)


def _json_default(obj: Any) -> str:
    if isinstance(obj, Decimal):
        return f"{obj:.4f}"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class HybridStorageEngine:
    """Manages SQLite WAL lineage manifests and DuckDB + ZSTD Parquet Silver storage."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.bronze_dir = self.data_dir / "bronze"
        self.bronze_dir.mkdir(parents=True, exist_ok=True)
        self._meta_db_path = self.bronze_dir / "lakehouse_meta.sqlite"
        self._init_sqlite_meta()

    def _init_sqlite_meta(self) -> None:
        """Initialize SQLite WAL metadata registry."""
        with closing(sqlite3.connect(self._meta_db_path, timeout=5.0)) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_manifests (
                    batch_id TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    silver_path TEXT,
                    quarantine_path TEXT,
                    created_at_utc TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def compute_table_sha256(table: pa.Table) -> str:
        """Compute deterministic SHA-256 over PyArrow schema and ordered row contents."""
        hasher = hashlib.sha256()
        schema_sig = [(field.name, str(field.type)) for field in table.schema]
        hasher.update(json.dumps(schema_sig, separators=(",", ":")).encode("utf-8"))

        for batch in table.to_batches(max_chunksize=4096):
            rows = batch.to_pylist()
            payload = json.dumps(
                rows,
                default=_json_default,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            hasher.update(payload.encode("utf-8"))
        return hasher.hexdigest()

    def _record_manifest(
        self,
        *,
        batch_id: str,
        domain: str,
        sha256: str,
        row_count: int,
        status: str,
        silver_path: str | None,
        quarantine_path: str | None,
    ) -> None:
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self._meta_db_path, timeout=5.0)) as conn, conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ingestion_manifests
                (batch_id, domain, sha256, row_count, status, silver_path, quarantine_path, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    domain,
                    sha256,
                    row_count,
                    status,
                    silver_path,
                    quarantine_path,
                    created_at,
                ),
            )

    def get_manifest(self, batch_id: str) -> dict[str, Any] | None:
        """Fetch an ingestion manifest record by batch_id."""
        with closing(sqlite3.connect(self._meta_db_path, timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM ingestion_manifests WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def write_silver_parquet(
        self,
        domain: str,
        table_name: str,
        table: pa.Table,
        *,
        batch_id: str,
    ) -> dict[str, Any]:
        """Atomically write ZSTD-compressed Silver Parquet and record ACCEPTED manifest."""
        silver_dir = self.data_dir / domain / "silver"
        silver_dir.mkdir(parents=True, exist_ok=True)
        final_path = silver_dir / f"{table_name}.parquet"
        tmp_path = silver_dir / f"{table_name}.tmp.parquet"

        pq.write_table(table, tmp_path, compression="ZSTD")
        os.replace(tmp_path, final_path)

        sha256 = self.compute_table_sha256(table)
        self._record_manifest(
            batch_id=batch_id,
            domain=domain,
            sha256=sha256,
            row_count=table.num_rows,
            status="ACCEPTED",
            silver_path=str(final_path),
            quarantine_path=None,
        )
        return {
            "batch_id": batch_id,
            "domain": domain,
            "sha256": sha256,
            "row_count": table.num_rows,
            "silver_path": str(final_path),
            "status": "ACCEPTED",
        }

    def write_quarantine(
        self,
        domain: str,
        batch_id: str,
        violations: list[str],
        sample_rows: list[dict[str, Any]],
    ) -> Path:
        """Write ODCS quarantine report without altering Silver partitions."""
        quarantine_dir = self.data_dir / domain / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        report_path = quarantine_dir / f"{batch_id}_violations.json"

        payload = {
            "batch_id": batch_id,
            "domain": domain,
            "status": "QUARANTINED",
            "violation_count": len(violations),
            "violations": violations,
            "sample_rows": sample_rows[:25],
            "quarantined_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        serialized = json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False) + "\n"
        with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialized)

        sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        self._record_manifest(
            batch_id=batch_id,
            domain=domain,
            sha256=sha256,
            row_count=len(sample_rows),
            status="QUARANTINED",
            silver_path=None,
            quarantine_path=str(report_path),
        )
        return report_path

    def query_silver_duckdb(self, parquet_path: Path, sql: str) -> list[tuple[Any, ...]]:
        """Execute a read-only SQL query over a Silver Parquet file exposed as `silver_data`."""
        target = Path(parquet_path)
        if not target.is_file():
            raise FileNotFoundError(f"Silver Parquet file not found: {target}")
        if _BANNED_SQL_PATTERN.search(sql):
            raise ValueError("Mutating SQL statements are forbidden in read-only Silver queries.")

        with duckdb.connect(database=":memory:") as conn:
            conn.read_parquet(str(target)).create_view("silver_data")
            return conn.execute(sql).fetchall()
