"""Script to validate state/project_progress.json and render JORNADA_IMPLEMENTACAO.md.

Cross-references state/project_progress.json and catalog/domains_registry.yaml
to compute live completion metrics and operational runbooks deterministically.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = REPO_ROOT / "state" / "project_progress.json"
REGISTRY_PATH = REPO_ROOT / "catalog" / "domains_registry.yaml"
OUTPUT_MD_PATH = REPO_ROOT / "JORNADA_IMPLEMENTACAO.md"


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON file strictly in UTF-8."""
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Invalid JSON: root must be an object.")
    return data


def load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML file strictly in UTF-8."""
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("Invalid YAML: root must be a mapping.")
    return data


def validate_progress(data: dict[str, Any]) -> None:
    """Validate project_progress.json schema."""
    required = (
        "schema_version",
        "last_updated",
        "project_name",
        "current_macro_phase_id",
        "current_macro_phase",
        "overall_status",
        "structural_phases",
        "quality_gates",
        "history",
    )
    for key in required:
        if key not in data:
            raise ValueError(f"Missing required key in project_progress.json: '{key}'")

    phases = data.get("structural_phases")
    if not isinstance(phases, list) or len(phases) < 2:
        raise ValueError("structural_phases must be a list of phase objects.")


def _phase_node_status(phases_by_id: dict[str, dict[str, Any]], phase_ids: tuple[str, ...]) -> str:
    statuses = [phases_by_id.get(pid, {}).get("status", "PENDING") for pid in phase_ids]
    if all(st == "COMPLETED" for st in statuses):
        return "CONCLUIDO"
    if any(st in ("COMPLETED", "IN_PROGRESS") for st in statuses):
        return "EM ANDAMENTO"
    return "PENDENTE"


def render_markdown(progress: dict[str, Any], registry: dict[str, Any]) -> str:
    """Render JORNADA_IMPLEMENTACAO.md deterministically."""
    last_updated = str(progress.get("last_updated", "2026-11-22"))
    structural_phases = progress.get("structural_phases", [])
    gates = progress.get("quality_gates", [])
    history = progress.get("history", [])
    domains = registry.get("domains", [])

    completed_phases = sum(1 for p in structural_phases if p.get("status") == "COMPLETED")
    total_phases = len(structural_phases)
    progress_pct = round((completed_phases / total_phases) * 100, 1)
    phases_by_id = {str(p["phase_id"]): p for p in structural_phases}

    g00_01 = _phase_node_status(phases_by_id, ("00", "01"))
    g02_03 = _phase_node_status(phases_by_id, ("02", "03"))
    g04_05 = _phase_node_status(phases_by_id, ("04", "05"))
    g06_07 = _phase_node_status(phases_by_id, ("06", "07"))
    g08 = _phase_node_status(phases_by_id, ("08",))

    lines: list[str] = [
        '<div align="center">',
        "  <h2>Jornada de Implementação — FinOps, CAPEX/OPEX & Revenue Lakehouse</h2>",
        "  <p><i>Painel Vivo de Evolução Arquitetural, Contratos ODCS v3.1 & Entregas por Fase</i></p>",
        f"  <p><b>Versão:</b> 1.0.0 &nbsp;|&nbsp; <b>Última Atualização:</b> {last_updated} &nbsp;|&nbsp; <b>Padrões:</b> ADR-001 • ADR-002 • ADR-003 • DECIMAL(18,4)</p>",
        "  <hr/>",
        "</div>",
        "<br/>",
        "",
        "# Jornada de Implementação & Painel Vivo de Governança",
        "",
        "Este artefato constitui a **Fonte Única da Verdade Operacional (SSOT)** do progresso de implementação do `finops-capex-revenue-lakehouse`, cruzando `state/project_progress.json` com `catalog/domains_registry.yaml`.",
        "",
        "---",
        "",
        "## 1. Painel de Status Executivo",
        "",
        f"- **Macrofase Ativa:** `{progress.get('current_macro_phase')}`",
        f"- **Status Geral de Governança:** `{progress.get('overall_status')}`",
        f"- **Próxima Fase Planejada:** `{progress.get('next_phase')}`",
        f"- **Progresso Estrutural das Fases:** **{completed_phases} de {total_phases} Concluídas ({progress_pct}%)**",
        f"- **Domínios Catalogados:** **{len(domains)} domínios financeiros e analíticos**",
        "",
        "```mermaid",
        "flowchart LR",
        f'    F01["Fases 00..01: ADRs & Fundacao<br/>{g00_01}"] --> F03["Fases 02..03: Razao & Aging<br/>{g02_03}"]',
        f'    F03 --> F05["Fases 04..05: Calculadoras & AI FinOps<br/>{g04_05}"]',
        f'    F05 --> F07["Fases 06..07: dbt Gold & 4 Interfaces<br/>{g06_07}"]',
        f'    F07 --> F08["Fase 08: Acoplamento C.O.R.E.<br/>{g08}"]',
        "```",
        "",
        "---",
        "",
        "## 2. Acompanhamento das Fases Estruturais (Fases 00 a 08)",
        "",
        "| ID | Fase de Implementação | Status | Conclusão | Entregáveis Principais & Evidências |",
        "| :---: | :--- | :---: | :---: | :--- |",
    ]

    for phase in structural_phases:
        pid = f"**Fase {phase['phase_id']}**"
        name = phase["name"]
        st = phase["status"]
        if st == "COMPLETED":
            badge = "CONCLUIDO"
        elif st == "IN_PROGRESS":
            badge = "EM ANDAMENTO"
        else:
            badge = "PENDENTE"
        dt = phase.get("completion_date") or "-"
        delivs = "<br/>".join(f"- {d}" for d in phase.get("deliverables", []))
        lines.append(f"| {pid} | **{name}** | `{badge}` | `{dt}` | {delivs} |")

    lines.extend([
        "",
        "---",
        "",
        "## 3. Gates Transversais de Qualidade & Segurança",
        "",
        "| Gate | Nome do Gate | Critério de Aceite | Status |",
        "| :---: | :--- | :--- | :---: |",
    ])

    for gate in gates:
        lines.append(
            f"| **{gate['gate_id']}** | **{gate['name']}** | {gate['criteria']} | `{gate['status']}` |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 4. Trilha Histórica de Marcos Concluídos",
        "",
        "| Data | Fase | Marco Realizado | Resumo Executivo |",
        "| :---: | :---: | :--- | :--- |",
    ])
    for item in history:
        lines.append(f"| `{item['date']}` | **Fase {item['phase_id']}** | **{item['milestone']}** | {item['summary']} |")

    lines.extend([
        "",
        "---",
        "",
        "## 5. Runbook Operacional (Setup & Verificação Local)",
        "",
        "```powershell",
        "py -3.12 -m venv .venv",
        ".\\.venv\\Scripts\\Activate.ps1",
        "pip install -r requirements.txt -r requirements-dev.txt",
        "pip install --no-deps -e .",
        "ruff check .",
        "python scripts/render_catalog.py --check",
        "python scripts/render_progress.py --check",
        'pytest -m "not live"',
        "```",
        "",
    ])

    return "\n".join(lines)


def main() -> None:
    """CLI entrypoint for rendering or checking JORNADA_IMPLEMENTACAO.md."""
    parser = argparse.ArgumentParser(description="Render or check JORNADA_IMPLEMENTACAO.md.")
    parser.add_argument("--check", action="store_true", help="Check synchronization without writing.")
    parser.add_argument("--progress", type=Path, default=PROGRESS_PATH, help="Path to project_progress.json")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH, help="Path to domains_registry.yaml")
    parser.add_argument("--output", type=Path, default=OUTPUT_MD_PATH, help="Path to JORNADA_IMPLEMENTACAO.md")
    args = parser.parse_args()

    try:
        progress_data = load_json(args.progress)
        validate_progress(progress_data)
        registry_data = load_yaml(args.registry)
        rendered = render_markdown(progress_data, registry_data)
    except FileNotFoundError as err:
        sys.stderr.write(f"[ERROR 2] File not found: {err}\n")
        sys.exit(2)
    except Exception as err:
        sys.stderr.write(f"[ERROR 2] Runtime error: {err}\n")
        sys.exit(2)

    if args.check:
        if not args.output.exists():
            sys.stderr.write(f"[CHECK FAILED] Output file does not exist: {args.output}\n")
            sys.exit(1)
        current = args.output.read_bytes().decode("utf-8")
        if current != rendered:
            sys.stderr.write(f"[CHECK FAILED] {args.output} is out of sync with state and registry.\n")
            diff = difflib.unified_diff(
                current.splitlines(),
                rendered.splitlines(),
                fromfile=str(args.output),
                tofile="rendered_progress",
                lineterm="",
                n=3,
            )
            for line in list(diff)[:20]:
                sys.stderr.write(f"{line}\n")
            sys.exit(1)
        print(f"[CHECK PASSED] {args.output} is synchronized with state and registry.")
        sys.exit(0)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)
    print(f"[RENDER SUCCESS] JORNADA_IMPLEMENTACAO.md generated at {args.output}.")
    sys.exit(0)


if __name__ == "__main__":
    main()
