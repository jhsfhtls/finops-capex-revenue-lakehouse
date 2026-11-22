"""Deterministic SVG dashboard showcase renderer backed by FinOpsLakehouse SDK."""

from __future__ import annotations

import argparse
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from finops_lakehouse.core.shield import configure_stdio_utf8
from finops_lakehouse.sdk import FinOpsLakehouse

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASSETS_DIR = REPO_ROOT / "bi" / "assets"


def _format_pct(value: str, *, places: int = 2) -> str:
    quant = Decimal("1").scaleb(-places)
    return f"{Decimal(value).quantize(quant, rounding=ROUND_HALF_EVEN)}"


def _bar_width_px(value: Decimal, max_value: Decimal, *, max_px: int = 360) -> int:
    if value <= Decimal("0") or max_value <= Decimal("0"):
        return 0
    scaled = (value * Decimal(max_px) / max_value).to_integral_value(rounding=ROUND_HALF_EVEN)
    return max(2, min(max_px, int(scaled)))


def _svg_wrapper(
    title: str,
    subtitle: str,
    body: str,
    *,
    seed: int = 42,
    width: int = 920,
    height: int = 420,
) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(title)}">\n'
        '  <defs>\n'
        '    <linearGradient id="bgGrad" x1="0" y1="0" x2="1" y2="1">\n'
        '      <stop offset="0%" stop-color="#0f172a"/>\n'
        '      <stop offset="100%" stop-color="#1e293b"/>\n'
        '    </linearGradient>\n'
        '  </defs>\n'
        f'  <rect width="{width}" height="{height}" rx="12" fill="url(#bgGrad)"/>\n'
        f'  <text x="28" y="40" fill="#f8fafc" font-family="Segoe UI, Arial, sans-serif" '
        f'font-size="20" font-weight="700">{escape(title)}</text>\n'
        f'  <text x="28" y="64" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" '
        f'font-size="13">{escape(subtitle)}</text>\n'
        f"{body}\n"
        '  <text x="28" y="400" fill="#64748b" font-family="Segoe UI, Arial, sans-serif" '
        f'font-size="11">Synthetic deterministic data (seed={seed}) | Exact DECIMAL(18,4) SDK parity</text>\n'
        '</svg>\n'
    )


def render_capex_opex_svg(lakehouse: FinOpsLakehouse, out_path: Path) -> Path:
    """Render CAPEX vs OPEX budget variance SVG card."""
    bv_all = lakehouse.budget_variance(limit=1000)
    bv_capex = lakehouse.budget_variance(tipo_despesa="CAPEX", limit=10)
    bv_opex = lakehouse.budget_variance(tipo_despesa="OPEX", limit=10)

    bars = [
        ("CAPEX Orcado", Decimal(bv_capex["total_orcado"]), "#38bdf8"),
        ("CAPEX Realizado", Decimal(bv_capex["total_realizado"]), "#0284c7"),
        ("OPEX Orcado", Decimal(bv_opex["total_orcado"]), "#34d399"),
        ("OPEX Realizado", Decimal(bv_opex["total_realizado"]), "#059669"),
    ]
    max_val = max((v for _, v, _ in bars), default=Decimal("1"))

    lines = [
        '  <rect x="28" y="84" width="272" height="66" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="42" y="108" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Total Orcado (24m)</text>',
        f'  <text x="42" y="134" fill="#38bdf8" font-family="Segoe UI, Arial, sans-serif" font-size="15" font-weight="700">R$ {escape(bv_all["total_orcado"])}</text>',
        '  <rect x="316" y="84" width="272" height="66" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="330" y="108" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Total Realizado</text>',
        f'  <text x="330" y="134" fill="#34d399" font-family="Segoe UI, Arial, sans-serif" font-size="15" font-weight="700">R$ {escape(bv_all["total_realizado"])}</text>',
        '  <rect x="604" y="84" width="288" height="66" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="618" y="108" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Variacao Orcamentaria</text>',
        f'  <text x="618" y="134" fill="#fbbf24" font-family="Segoe UI, Arial, sans-serif" font-size="14" font-weight="700">R$ {escape(bv_all["variacao_orcamentaria"])} ({escape(_format_pct(bv_all["variacao_pct"], places=4))}%)</text>',
    ]
    y = 188
    for label, val, color in bars:
        w = _bar_width_px(val, max_val, max_px=380)
        lines.append(
            f'  <text x="28" y="{y + 18}" fill="#e2e8f0" font-family="Segoe UI, Arial, sans-serif" font-size="13">{escape(label)}</text>'
        )
        lines.append(
            f'  <rect x="210" y="{y}" width="{w}" height="24" rx="4" fill="{color}"/>'
        )
        lines.append(
            f'  <text x="{220 + w}" y="{y + 17}" fill="#cbd5e1" font-family="Segoe UI, Arial, sans-serif" font-size="12">R$ {escape(f"{val:.4f}")}</text>'
        )
        y += 46

    svg = _svg_wrapper(
        "CAPEX & OPEX Budget Governance (24 Months)",
        f"Double-Entry Balanced: {bv_all['double_entry_balanced']} | Groups: {bv_all['group_count']}",
        "\n".join(lines),
        seed=lakehouse.seed,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8", newline="\n")
    return out_path


def render_receivables_aging_svg(lakehouse: FinOpsLakehouse, out_path: Path) -> Path:
    """Render Receivables Aging & Expected Credit Loss (ECL) SVG card."""
    ag = lakehouse.aging_priority(limit=5)
    buckets = ag["buckets"]
    palette = ["#38bdf8", "#22c55e", "#eab308", "#f97316", "#ef4444", "#dc2626"]
    max_open = max((Decimal(b["valor_em_aberto"]) for b in buckets.values()), default=Decimal("1"))

    lines = [
        '  <rect x="28" y="84" width="272" height="62" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="42" y="106" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Saldo em Aberto (EAD)</text>',
        f'  <text x="42" y="132" fill="#f8fafc" font-family="Segoe UI, Arial, sans-serif" font-size="15" font-weight="700">R$ {escape(ag["total_em_aberto"])}</text>',
        '  <rect x="316" y="84" width="272" height="62" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="330" y="106" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Total Vencido</text>',
        f'  <text x="330" y="132" fill="#f97316" font-family="Segoe UI, Arial, sans-serif" font-size="15" font-weight="700">R$ {escape(ag["total_vencido"])}</text>',
        '  <rect x="604" y="84" width="288" height="62" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="618" y="106" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Perda Esperada (ECL)</text>',
        f'  <text x="618" y="132" fill="#ef4444" font-family="Segoe UI, Arial, sans-serif" font-size="15" font-weight="700">R$ {escape(ag["total_ecl"])}</text>',
    ]

    y = 168
    for (bucket_name, stats), color in zip(buckets.items(), palette, strict=True):
        val = Decimal(stats["valor_em_aberto"])
        w = _bar_width_px(val, max_open, max_px=340)
        lines.append(
            f'  <text x="28" y="{y + 17}" fill="#e2e8f0" font-family="Segoe UI, Arial, sans-serif" font-size="12">{escape(bucket_name)} ({stats["qtd_faturas"]})</text>'
        )
        lines.append(
            f'  <rect x="175" y="{y}" width="{w}" height="22" rx="4" fill="{color}"/>'
        )
        lines.append(
            f'  <text x="{185 + w}" y="{y + 16}" fill="#cbd5e1" font-family="Segoe UI, Arial, sans-serif" font-size="12">R$ {escape(stats["valor_em_aberto"])} | ECL {escape(stats["valor_ecl"])}</text>'
        )
        y += 34

    svg = _svg_wrapper(
        "Receivables Aging & Credit Loss Exposure (4,000 Invoices)",
        f"Exact Bucket Reconciliation: {ag['reconciliation_ok']}",
        "\n".join(lines),
        seed=lakehouse.seed,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8", newline="\n")
    return out_path


def render_ai_finops_svg(lakehouse: FinOpsLakehouse, out_path: Path) -> Path:
    """Render AI/Cloud FinOps (POP_T_158) tier spend & savings waterfall SVG card."""
    ai = lakehouse.ai_finops()
    by_tier = ai["by_tier"]
    max_tier = max((Decimal(v["cost_usd"]) for v in by_tier.values()), default=Decimal("1"))
    tier_colors = {"T1": "#34d399", "T2": "#38bdf8", "T3": "#a78bfa", "T4": "#f472b6"}

    lines = [
        '  <rect x="28" y="84" width="272" height="62" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="42" y="106" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Baseline Cost (USD)</text>',
        f'  <text x="42" y="132" fill="#f8fafc" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700">$ {escape(ai["total_cost_usd"])}</text>',
        '  <rect x="316" y="84" width="272" height="62" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="330" y="106" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Optimized Cost (USD)</text>',
        f'  <text x="330" y="132" fill="#34d399" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700">$ {escape(ai["optimized_cost_usd"])}</text>',
        '  <rect x="604" y="84" width="288" height="62" rx="8" fill="#1e293b" stroke="#334155"/>',
        '  <text x="618" y="106" fill="#94a3b8" font-family="Segoe UI, Arial, sans-serif" font-size="12">Cache + Batch + Off-Peak Savings</text>',
        f'  <text x="618" y="132" fill="#fbbf24" font-family="Segoe UI, Arial, sans-serif" font-size="16" font-weight="700">$ {escape(ai["total_savings_usd"])} ({escape(_format_pct(ai["savings_pct"], places=2))}%)</text>',
    ]

    y = 182
    for tier in ("T1", "T2", "T3", "T4"):
        info = by_tier.get(tier, {"runs": 0, "cost_usd": "0.0000", "optimized_cost_usd": "0.0000"})
        val = Decimal(info["cost_usd"])
        w = _bar_width_px(val, max_tier, max_px=380)
        lines.append(
            f'  <text x="28" y="{y + 18}" fill="#e2e8f0" font-family="Segoe UI, Arial, sans-serif" font-size="13">Tier {tier} ({info["runs"]} spans)</text>'
        )
        lines.append(
            f'  <rect x="195" y="{y}" width="{w}" height="24" rx="4" fill="{tier_colors.get(tier, "#38bdf8")}"/>'
        )
        lines.append(
            f'  <text x="{205 + w}" y="{y + 17}" fill="#cbd5e1" font-family="Segoe UI, Arial, sans-serif" font-size="12">$ {escape(info["cost_usd"])} -> $ {escape(info["optimized_cost_usd"])}</text>'
        )
        y += 46

    svg = _svg_wrapper(
        "AI & Cloud FinOps Telemetry by POP_T_158 Tier (T1-T4)",
        f"Total Tokens: {ai['total_tokens']:,} | Cache Hit 40% | Batch 30% | DeepSeek Off-Peak -50%",
        "\n".join(lines),
        seed=lakehouse.seed,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg, encoding="utf-8", newline="\n")
    return out_path


def render_all_bi_assets(
    *,
    lakehouse: FinOpsLakehouse | None = None,
    assets_dir: Path | None = None,
) -> list[Path]:
    """Generate all deterministic SVG analytics previews into bi/assets/."""
    client = lakehouse or FinOpsLakehouse()
    target_dir = (assets_dir or DEFAULT_ASSETS_DIR).resolve()
    return [
        render_capex_opex_svg(client, target_dir / "dashboard_capex_opex.svg"),
        render_receivables_aging_svg(client, target_dir / "dashboard_receivables_aging.svg"),
        render_ai_finops_svg(client, target_dir / "dashboard_ai_finops.svg"),
    ]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint rendering bi/assets/*.svg deterministically."""
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(description="Render deterministic SVG analytics showcase.")
    parser.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS_DIR)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--duckdb-path", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    try:
        client = FinOpsLakehouse(
            data_dir=args.data_dir,
            duckdb_path=args.duckdb_path,
            seed=args.seed,
        )
        outputs = render_all_bi_assets(lakehouse=client, assets_dir=args.assets_dir)
    except (TypeError, ValueError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    for item in outputs:
        print(f"[RENDER SUCCESS] {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
