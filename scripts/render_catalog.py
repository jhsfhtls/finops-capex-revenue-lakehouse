"""Script to validate catalog/domains_registry.yaml and render CATALOG.md.

Enforces executive headers, Clean Code standards, and deterministic output
without wall-clock timestamps for CI --check stability.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "catalog" / "domains_registry.yaml"
OUTPUT_MD_PATH = REPO_ROOT / "CATALOG.md"

VALID_STATUSES = {"CATALOGED", "IN_PROGRESS", "HOMOLOGATED"}
VALID_PRIVACY_TIERS = {
    "SYNTHETIC_DETERMINISTIC",
    "CLIENT_SHIELDED_CALC",
    "CAREER_SHIELDED_CALC",
    "INTERNAL_TELEMETRY_REDACTED",
    "PUBLIC_NON_PERSONAL",
}
VALID_CANONICAL_KEYS = {
    "id_lancamento",
    "cod_centro_custo",
    "cod_conta_contabil",
    "id_contrato_masked",
    "id_fatura_masked",
    "cnpj_14",
    "id_municipio",
    "sigla_uf",
}

CANONICAL_KEYS_INFO = [
    ("id_lancamento", "STRING", "Chave unica deterministica do lancamento", "Unicidade por lote e seed", "ADR-002"),
    ("cod_centro_custo", "STRING", "Codigo canonico de centro de custo (ex.: 'CC-0100-ENG')", "Prefixo e dominio validos", "ADR-002"),
    ("cod_conta_contabil", "STRING", "Plano de contas hierarquico (ex.: '4.01.01.001')", "Mascara contabil hierarquica", "ADR-002"),
    ("id_contrato_masked", "STRING", "Identificador mascarado de contrato B2B", "Tokenizacao sem PII real", "ADR-002, ADR-003"),
    ("id_fatura_masked", "STRING", "Identificador mascarado de fatura/titulo", "Tokenizacao sem PII real", "ADR-002, ADR-003"),
    ("cnpj_14", "STRING(14)", "CNPJ canonico zfill 14D", "Modulo 11 (2 digitos verificadores)", "ADR-002, ADR-003"),
    ("id_municipio", "STRING(7)", "Codigo IBGE 7 digitos (ex.: '1302603')", "Crosswalk IBGE / public-data-gateway", "ADR-002"),
    ("sigla_uf", "STRING(2)", "UF oficial 2 letras maiusculas (ex.: 'AM')", "Tabela IBGE das 27 UFs", "ADR-002"),
]

PRIVACY_TIERS_INFO = [
    ("SYNTHETIC_DETERMINISTIC", "ADR-003 / POP_T_154", "Dados 100% sinteticos gerados com seed=42; zero dados de empresas reais.", "capex_opex_ledger, receivables_aging"),
    ("CLIENT_SHIELDED_CALC", "ADR-003 / Client Path Shield", "Motor deterministico puro; cargas reais externas proibidas em data/.", "b2b_deal_economics"),
    ("CAREER_SHIELDED_CALC", "ADR-003 / Client Path Shield", "Simulador CLT/PJ/USD; insumos pessoais permanecem em ../career/.", "career_tax_parity"),
    ("INTERNAL_TELEMETRY_REDACTED", "POP_T_154 / POP_T_158", "Spans de telemetria LLM sem tokens/segredos ou CPFs.", "ai_cloud_finops"),
    ("PUBLIC_NON_PERSONAL", "LAI / LGPD Art. 1", "Series macroeconomicas publicas (PTAX, Selic, IPCA).", "public_macro_bridge"),
]


def load_registry(yaml_path: Path) -> dict[str, Any]:
    """Load and parse the YAML domain registry strictly in UTF-8."""
    if not yaml_path.exists():
        raise FileNotFoundError(f"Registry file not found at: {yaml_path}")
    with open(yaml_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("Invalid registry YAML: root must be a mapping.")
    return data


def validate_registry(data: dict[str, Any]) -> None:
    """Validate schema completeness and governance invariants of domains_registry.yaml."""
    for req in ("schema_version", "updated_at", "domains"):
        if req not in data:
            raise ValueError(f"Missing required top-level field: '{req}'")

    domains = data.get("domains")
    if not isinstance(domains, list) or not domains:
        raise ValueError("'domains' must be a non-empty list.")

    seen_ids: set[str] = set()
    for dom in domains:
        if not isinstance(dom, dict):
            raise ValueError("Each domain entry must be a dictionary.")
        did = dom.get("domain_id")
        if not did or not isinstance(did, str):
            raise ValueError(f"Domain missing valid 'domain_id': {dom}")
        if did in seen_ids:
            raise ValueError(f"Duplicate domain_id detected: '{did}'")
        seen_ids.add(did)

        for str_field in ("name", "phase", "status", "granularity", "monetary_precision", "privacy_tier", "description"):
            val = dom.get(str_field)
            if not val or not isinstance(val, str):
                raise ValueError(f"Domain '{did}' missing or invalid string field '{str_field}'")

        if dom["status"] not in VALID_STATUSES:
            raise ValueError(f"Domain '{did}' invalid status: '{dom['status']}'")
        if dom["privacy_tier"] not in VALID_PRIVACY_TIERS:
            raise ValueError(f"Domain '{did}' invalid privacy_tier: '{dom['privacy_tier']}'")

        keys = dom.get("canonical_keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError(f"Domain '{did}' canonical_keys must be a non-empty list")
        for key in keys:
            if key not in VALID_CANONICAL_KEYS:
                raise ValueError(f"Domain '{did}' invalid canonical_key: '{key}'")

        sla_hours = dom.get("sla_freshness_hours")
        if isinstance(sla_hours, bool) or not isinstance(sla_hours, int) or sla_hours <= 0:
            raise ValueError(f"Domain '{did}' invalid sla_freshness_hours: {sla_hours}")


def render_markdown(data: dict[str, Any]) -> str:
    """Render CATALOG.md deterministically from validated domain metadata."""
    updated_at = str(data.get("updated_at", "2026-11-22"))
    domains = sorted(data["domains"], key=lambda d: (d["phase"], d["domain_id"]))
    total_domains = len(domains)

    lines: list[str] = [
        '<div align="center">',
        "  <h2>Catálogo Mestre de Domínios — FinOps, CAPEX/OPEX & Revenue Lakehouse</h2>",
        f"  <p><i>Mapeamento Unificado de {total_domains} Domínios Financeiros, Chaves Canônicas e Contratos ODCS v3.1</i></p>",
        f"  <p><b>Versão:</b> 1.0.0 &nbsp;|&nbsp; <b>Última Atualização:</b> {updated_at} &nbsp;|&nbsp; <b>Padrões:</b> ADR-001 • ADR-002 • ADR-003 • DECIMAL(18,4)</p>",
        "  <hr/>",
        "</div>",
        "<br/>",
        "",
        f"# Catálogo Mestre de Domínios Financeiros ({total_domains} Domínios)",
        "",
        "Este catálogo consolida os domínios analíticos e calculadoras determinísticas do `finops-capex-revenue-lakehouse`, garantindo precisão monetária estrita em `DECIMAL(18,4)` (`ADR-001`), chaves relacionais `STRING` com validação `ODCS v3.1` (`ADR-002`) e isolamento LGPD/B2B via *Client Path Shield* (`ADR-003`).",
        "",
        "---",
        "",
        "## 1. Domínios Catalogados & Ciclo de Entrega",
        "",
        "| ID Domínio | Nome / Escopo | Fase | Precisão | SLA | Tier de Privacidade | Chaves Canônicas | Status |",
        "| :--- | :--- | :---: | :---: | :---: | :--- | :--- | :---: |",
    ]

    for dom in domains:
        keys_str = "<br/>".join(f"`{k}`" for k in dom["canonical_keys"])
        lines.append(
            f"| `{dom['domain_id']}` | **{dom['name']}**<br/>*{dom['description']}* | "
            f"**Fase {dom['phase']}** | `{dom['monetary_precision']}` | `{dom['sla_freshness_hours']}h` | "
            f"`{dom['privacy_tier']}` | {keys_str} | `{dom['status']}` |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 2. Dicionário de Chaves Canônicas & Tipagem Estrita (`ADR-002`)",
        "",
        "| Chave Canônica | Tipo Estrito | Padrão / Formato | Validação | Referência |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for key_name, col_type, pattern, validation, ref in CANONICAL_KEYS_INFO:
        lines.append(f"| `{key_name}` | `{col_type}` | {pattern} | {validation} | {ref} |")

    lines.extend([
        "",
        "---",
        "",
        "## 3. Governança de Privacidade & Isolamento (`ADR-003`)",
        "",
        "| Tier de Privacidade | Base Normativa | Regra de Isolamento | Domínios |",
        "| :--- | :--- | :--- | :--- |",
    ])
    for tier, basis, rule, examples in PRIVACY_TIERS_INFO:
        lines.append(f"| `{tier}` | {basis} | {rule} | `{examples}` |")

    lines.extend([
        "",
        "---",
        "",
        "## 4. Instruções de Manutenção & Verificação (`--check`)",
        "",
        "O arquivo `CATALOG.md` é gerado de forma determinística a partir de `catalog/domains_registry.yaml`:",
        "",
        "```powershell",
        "python scripts/render_catalog.py --check",
        "python scripts/render_catalog.py",
        "```",
        "",
    ])

    return "\n".join(lines)


def main() -> None:
    """CLI entrypoint for rendering or checking CATALOG.md."""
    parser = argparse.ArgumentParser(description="Render or check CATALOG.md from domains_registry.yaml.")
    parser.add_argument("--check", action="store_true", help="Check synchronization without writing.")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH, help="Path to domains_registry.yaml")
    parser.add_argument("--output", type=Path, default=OUTPUT_MD_PATH, help="Path to CATALOG.md")
    args = parser.parse_args()

    try:
        data = load_registry(args.registry)
        validate_registry(data)
        rendered = render_markdown(data)
    except FileNotFoundError as err:
        sys.stderr.write(f"[ERROR 2] File not found: {err}\n")
        sys.exit(2)
    except Exception as err:
        sys.stderr.write(f"[ERROR 2] Validation error: {err}\n")
        sys.exit(2)

    if args.check:
        if not args.output.exists():
            sys.stderr.write(f"[CHECK FAILED] Output file does not exist: {args.output}\n")
            sys.exit(1)
        current = args.output.read_bytes().decode("utf-8")
        if current != rendered:
            sys.stderr.write(f"[CHECK FAILED] {args.output} is out of sync with {args.registry}.\n")
            diff = difflib.unified_diff(
                current.splitlines(),
                rendered.splitlines(),
                fromfile=str(args.output),
                tofile="rendered_from_yaml",
                lineterm="",
                n=3,
            )
            for line in list(diff)[:20]:
                sys.stderr.write(f"{line}\n")
            sys.exit(1)
        print(f"[CHECK PASSED] {args.output} is synchronized with {args.registry}.")
        sys.exit(0)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)
    print(f"[RENDER SUCCESS] CATALOG.md generated at {args.output} ({len(data['domains'])} domains).")
    sys.exit(0)


if __name__ == "__main__":
    main()
