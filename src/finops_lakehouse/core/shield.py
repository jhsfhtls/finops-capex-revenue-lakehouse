"""Security guards: Client Path Shield (POP_T_154), CWE-1236, and Zip-Bomb defense (ADR-003)."""

from __future__ import annotations

import re
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from finops_lakehouse.core.decimal_utils import format_money

DANGEROUS_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
_SIGNED_DECIMAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


class ClientPathShieldError(PermissionError):
    """Raised when external client, career, or study payloads attempt to write inside repo."""


class SpreadsheetSecurityError(ValueError):
    """Raised when an input spreadsheet fails magic-byte, extension, or zip-bomb checks."""


def default_repo_root() -> Path:
    """Resolve repository root cleanly from module location without CWD dependency."""
    return Path(__file__).resolve().parents[3]


def enforce_client_path_shield(
    target_path: Path,
    repo_root: Path | None = None,
    *,
    is_external_client_data: bool = True,
) -> Path:
    """Block persisting external client/career/study files inside the public repository."""
    resolved_target = Path(target_path).resolve()
    if not is_external_client_data:
        return resolved_target

    resolved_root = (repo_root or default_repo_root()).resolve()
    if resolved_target.is_relative_to(resolved_root):
        raise ClientPathShieldError(
            f"Client Path Shield blocked write inside repository root '{resolved_root}': "
            f"'{resolved_target}'. External payloads must target ../operations/Clientes/<Cliente>/, "
            "../career/, ../studies/, or another path outside the public repo."
        )
    return resolved_target


def is_signed_decimal_literal(text: str) -> bool:
    """Return True if text is strictly a plain signed numeric literal (safe in spreadsheets)."""
    return bool(_SIGNED_DECIMAL_RE.fullmatch(text.strip()))


def sanitize_inbound_text(value: str) -> tuple[str, bool]:
    """Neutralize CWE-1236 formula injection prefixes in inbound text fields."""
    if not value:
        return "", False

    stripped = value.lstrip("\t\r")
    changed = stripped != value
    probe = stripped.lstrip()

    if probe.startswith(("=", "+", "-", "@")) and not is_signed_decimal_literal(probe):
        return f"'{stripped}", True

    return stripped, changed


def sanitize_cell_for_export(value: Any) -> Any:
    """Neutralize executable formula prefixes when exporting CSV or XLSX cells."""
    if isinstance(value, Decimal):
        return format_money(value)
    if not isinstance(value, str):
        return value
    sanitized, _ = sanitize_inbound_text(value)
    return sanitized


def validate_spreadsheet_file(file_path: Path, *, max_zip_ratio: int = 100) -> str:
    """Validate spreadsheet existence, extension, magic bytes, and Zip-Bomb ratio."""
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Spreadsheet file not found: {path}")

    suffix = path.suffix.lower()
    with path.open("rb") as fh:
        header = fh.read(4096)

    if suffix == ".xlsx":
        if not header.startswith(b"PK\x03\x04"):
            raise SpreadsheetSecurityError(
                f"Invalid XLSX magic bytes in '{path.name}'; expected ZIP/OOXML signature."
            )
        try:
            with zipfile.ZipFile(path, "r") as zf:
                infos = zf.infolist()
                if not infos:
                    raise SpreadsheetSecurityError(f"Empty XLSX archive: '{path.name}'.")
                compressed_total = sum(max(info.compress_size, 1) for info in infos)
                uncompressed_total = sum(info.file_size for info in infos)
        except zipfile.BadZipFile as exc:
            raise SpreadsheetSecurityError(f"Corrupted XLSX archive: '{path.name}'.") from exc

        ratio = uncompressed_total / compressed_total
        if ratio > max_zip_ratio:
            raise SpreadsheetSecurityError(
                f"XLSX Zip-Bomb guard triggered ({ratio:.1f}:1 > {max_zip_ratio}:1)."
            )
        return "xlsx"

    if suffix == ".csv":
        if header.startswith((b"PK\x03\x04", b"%PDF")) or b"\x00" in header:
            raise SpreadsheetSecurityError(
                f"Binary content detected in CSV file '{path.name}'."
            )
        return "csv"

    raise SpreadsheetSecurityError(
        f"Unsupported spreadsheet extension '{suffix}'. Expected '.csv' or '.xlsx'."
    )
