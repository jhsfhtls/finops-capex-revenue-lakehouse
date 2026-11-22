"""Read-only bridge for PTAX, Selic, and IPCA from ../public-data-gateway or offline reference."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from finops_lakehouse.core.decimal_utils import RATE_QUANT

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REFERENCE_PATH = _REPO_ROOT / "data" / "reference" / "macro_rates_reference.json"
DEFAULT_GATEWAY_DIR = _REPO_ROOT.parent / "public-data-gateway" / "data"

_ONE = Decimal("1")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class MacroRatesSnapshot:
    """Immutable macroeconomic snapshot with strict DECIMAL(12,6) rates."""

    reference_date: str
    ptax_usd_brl: Decimal
    ptax_eur_brl: Decimal
    selic_annual_pct: Decimal
    ipca_12m_pct: Decimal
    source: str


def _parse_rate_boundary(raw: object) -> Decimal:
    """Convert raw numeric/string boundary value into quantized DECIMAL(12,6)."""
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"Invalid macroeconomic rate boundary value: {raw!r}")
    try:
        val = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid macroeconomic rate boundary value: {raw!r}") from exc
    if not val.is_finite():
        raise ValueError(f"Non-finite macroeconomic rate: {raw!r}")
    return val.quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def _load_offline_reference(reference_path: Path) -> MacroRatesSnapshot:
    if not reference_path.is_file():
        raise FileNotFoundError(f"Offline macro reference file not found: {reference_path}")
    with open(reference_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return MacroRatesSnapshot(
        reference_date=str(payload["reference_date"]),
        ptax_usd_brl=_parse_rate_boundary(payload["ptax_usd_brl"]),
        ptax_eur_brl=_parse_rate_boundary(payload["ptax_eur_brl"]),
        selic_annual_pct=_parse_rate_boundary(payload["selic_annual_pct"]),
        ipca_12m_pct=_parse_rate_boundary(payload["ipca_12m_pct"]),
        source=str(payload.get("source", "offline_reference_snapshot")),
    )


def _latest_ptax(parquet_path: Path, currency: str) -> tuple[Decimal, str]:
    table = pq.read_table(parquet_path)
    rows = [
        r
        for r in table.to_pylist()
        if r.get("simbolo_moeda") == currency and r.get("cotacao_venda") is not None
    ]
    if not rows:
        raise ValueError(f"No valid PTAX rows found for currency '{currency}' in {parquet_path}")
    rows.sort(
        key=lambda r: (
            str(r.get("data_cotacao", "")),
            1 if r.get("tipo_boletim") == "Fechamento PTAX" else 0,
            str(r.get("data_hora_cotacao", "")),
        ),
        reverse=True,
    )
    latest = rows[0]
    return _parse_rate_boundary(latest["cotacao_venda"]), str(latest["data_cotacao"])


def _latest_selic_annual_pct(parquet_path: Path) -> Decimal:
    table = pq.read_table(parquet_path)
    rows = [r for r in table.to_pylist() if r.get("valor") is not None]
    if not rows:
        raise ValueError(f"No valid Selic rows found in {parquet_path}")

    meta_rows = [r for r in rows if str(r.get("id_serie") or "") == "432"]
    if meta_rows:
        meta_rows.sort(key=lambda r: str(r.get("data_referencia", "")), reverse=True)
        return _parse_rate_boundary(meta_rows[0]["valor"])

    daily_rows = [r for r in rows if str(r.get("id_serie") or "") in ("11", "")]
    target_rows = daily_rows if daily_rows else rows
    target_rows.sort(key=lambda r: str(r.get("data_referencia", "")), reverse=True)
    daily_pct = _parse_rate_boundary(target_rows[0]["valor"])
    annual_factor = (_ONE + daily_pct / _HUNDRED) ** 252 - _ONE
    return (annual_factor * _HUNDRED).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def _trailing_12m_ipca_pct(parquet_path: Path) -> Decimal:
    table = pq.read_table(parquet_path)
    rows = [r for r in table.to_pylist() if r.get("valor") is not None]
    if not rows:
        raise ValueError(f"No valid IPCA rows found in {parquet_path}")

    acc12_rows = [r for r in rows if str(r.get("id_serie") or "") == "13522"]
    if acc12_rows:
        acc12_rows.sort(key=lambda r: str(r.get("data_referencia", "")), reverse=True)
        return _parse_rate_boundary(acc12_rows[0]["valor"])

    monthly_rows = [r for r in rows if str(r.get("id_serie") or "") in ("433", "")]
    target_rows = monthly_rows if monthly_rows else rows
    target_rows.sort(key=lambda r: str(r.get("data_referencia", "")), reverse=True)
    acc = _ONE
    for row in target_rows[:12]:
        monthly_pct = _parse_rate_boundary(row["valor"])
        acc *= _ONE + monthly_pct / _HUNDRED
    return ((acc - _ONE) * _HUNDRED).quantize(RATE_QUANT, rounding=ROUND_HALF_EVEN)


def load_macro_rates(
    *,
    gateway_dir: Path | None = None,
    reference_path: Path | None = None,
    prefer_live_gateway: bool = False,
) -> MacroRatesSnapshot:
    """Load PTAX/Selic/IPCA from ../public-data-gateway when requested, else offline snapshot."""
    ref_path = Path(reference_path) if reference_path is not None else DEFAULT_REFERENCE_PATH
    if not prefer_live_gateway:
        return _load_offline_reference(ref_path)

    gw_dir = Path(gateway_dir) if gateway_dir is not None else DEFAULT_GATEWAY_DIR
    usd_pq = gw_dir / "bcb_olinda_ptax" / "silver" / "ptax_cotacao_dolar.parquet"
    fx_pq = gw_dir / "bcb_olinda_ptax" / "silver" / "ptax_cotacao_moedas.parquet"
    selic_pq = gw_dir / "bcb_sgs" / "silver" / "sgs_selic.parquet"
    ipca_pq = gw_dir / "bcb_sgs" / "silver" / "sgs_ipca.parquet"

    if not all(p.is_file() for p in (usd_pq, fx_pq, selic_pq, ipca_pq)):
        return _load_offline_reference(ref_path)

    usd_rate, usd_date = _latest_ptax(usd_pq, "USD")
    eur_rate, _ = _latest_ptax(fx_pq, "EUR")
    selic_pct = _latest_selic_annual_pct(selic_pq)
    ipca_pct = _trailing_12m_ipca_pct(ipca_pq)

    return MacroRatesSnapshot(
        reference_date=usd_date,
        ptax_usd_brl=usd_rate,
        ptax_eur_brl=eur_rate,
        selic_annual_pct=selic_pct,
        ipca_12m_pct=ipca_pct,
        source="public_data_gateway_silver",
    )


def macro_snapshot_to_dict(snapshot: MacroRatesSnapshot) -> dict[str, Any]:
    """Serialize MacroRatesSnapshot cleanly without binary floats."""
    return {
        "reference_date": snapshot.reference_date,
        "ptax_usd_brl": f"{snapshot.ptax_usd_brl:.6f}",
        "ptax_eur_brl": f"{snapshot.ptax_eur_brl:.6f}",
        "selic_annual_pct": f"{snapshot.selic_annual_pct:.6f}",
        "ipca_12m_pct": f"{snapshot.ipca_12m_pct:.6f}",
        "source": snapshot.source,
    }
