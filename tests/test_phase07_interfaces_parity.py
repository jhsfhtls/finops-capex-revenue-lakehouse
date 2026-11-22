"""Offline tests for Phase 07: exact 4-interface parity (SDK/CLI/FastAPI/MCP), SLA <= 60s, BI."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from finops_lakehouse import FinOpsLakehouse
from finops_lakehouse.api.app import create_app
from finops_lakehouse.cli.main import main as cli_main
from finops_lakehouse.mcp.server import (
    MCP_TOOL_DEFINITIONS,
    handle_jsonrpc_request,
    wrap_untrusted_data,
)
from finops_lakehouse.mcp.server import (
    main as mcp_main,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(module_name: str, rel_path: str) -> ModuleType:
    target = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bootstrap_mod = _load_script_module("bootstrap_lakehouse", "scripts/bootstrap_lakehouse.py")
_render_bi_mod = _load_script_module("render_bi_showcase", "scripts/render_bi_showcase.py")


def _run_cli_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict:
    rc = cli_main(argv)
    assert rc == 0, f"CLI command failed for {argv}"
    out = capsys.readouterr().out
    return json.loads(out)


def _call_mcp_tool(
    lakehouse: FinOpsLakehouse,
    name: str,
    arguments: dict,
    *,
    req_id: int = 1,
) -> dict:
    resp = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        lakehouse,
    )
    assert resp is not None and "result" in resp, f"MCP error response: {resp}"
    text = resp["result"]["content"][0]["text"]
    assert text.startswith("<untrusted_data>")
    assert text.endswith("</untrusted_data>")
    inner = text.removeprefix("<untrusted_data>").removesuffix("</untrusted_data>")
    return json.loads(inner)


@pytest.fixture(scope="module")
def bootstrapped_lakehouse(tmp_path_factory: pytest.TempPathFactory) -> tuple[FinOpsLakehouse, dict]:
    """Run full Silver + dbt Gold bootstrap once in an isolated temp dir (<= 60s SLA)."""
    tmp_root = tmp_path_factory.mktemp("phase07_bootstrap")
    data_dir = tmp_root / "data"
    duckdb_path = data_dir / "gold" / "finops_lakehouse.duckdb"
    summary = _bootstrap_mod.run_bootstrap(
        data_dir=data_dir,
        duckdb_path=duckdb_path,
        dbt_target_dir=tmp_root / "dbt_target",
        dbt_log_dir=tmp_root / "dbt_logs",
        seed=42,
    )
    lakehouse = FinOpsLakehouse(data_dir=data_dir, duckdb_path=duckdb_path, seed=42)
    return lakehouse, summary


def test_bootstrap_sla_and_gold_vs_memory_parity(
    bootstrapped_lakehouse: tuple[FinOpsLakehouse, dict],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full bootstrap completes within 60s and Gold == Silver == memory fallback."""
    gold_lh, boot_summary = bootstrapped_lakehouse
    assert boot_summary["status"] == "BOOTSTRAPPED"
    assert boot_summary["docs_lint"] == "APPROVED"
    assert boot_summary["elapsed_seconds"] <= 60.0
    assert boot_summary["within_60s_sla"] is True
    assert gold_lh.get_status()["gold_ready"] is True

    empty_dir = tmp_path / "no_gold_or_silver"
    # Explicit data_dir must never be overridden by FCL_DUCKDB_PATH in env
    monkeypatch.setenv("FCL_DUCKDB_PATH", str(gold_lh.duckdb_path))
    mem_lh = FinOpsLakehouse(data_dir=empty_dir, seed=42)
    assert mem_lh.get_status()["gold_ready"] is False
    monkeypatch.delenv("FCL_DUCKDB_PATH", raising=False)

    with pytest.raises(ValueError, match="empty string"):
        FinOpsLakehouse(data_dir="")
    with pytest.raises(ValueError, match="empty string"):
        FinOpsLakehouse(data_dir=empty_dir, duckdb_path="   ")

    silver_lh = FinOpsLakehouse(
        data_dir=gold_lh.data_dir,
        duckdb_path=tmp_path / "absent.duckdb",
        seed=42,
    )
    assert silver_lh.get_status()["gold_ready"] is False

    gold_bv = gold_lh.budget_variance(limit=10)
    silver_bv = silver_lh.budget_variance(limit=10)
    mem_bv = mem_lh.budget_variance(limit=10)
    assert gold_bv["source_layer"] == "gold_duckdb"
    assert silver_bv["source_layer"] == "silver_parquet"
    assert mem_bv["source_layer"] == "synthetic_memory"
    assert {k: v for k, v in gold_bv.items() if k != "source_layer"} == {
        k: v for k, v in silver_bv.items() if k != "source_layer"
    } == {k: v for k, v in mem_bv.items() if k != "source_layer"}

    gold_ag = gold_lh.aging_priority(limit=10)
    silver_ag = silver_lh.aging_priority(limit=10)
    mem_ag = mem_lh.aging_priority(limit=10)
    assert gold_ag["source_layer"] == "gold_duckdb"
    assert silver_ag["source_layer"] == "silver_parquet"
    assert mem_ag["source_layer"] == "synthetic_memory"
    assert {k: v for k, v in gold_ag.items() if k != "source_layer"} == {
        k: v for k, v in silver_ag.items() if k != "source_layer"
    } == {k: v for k, v in mem_ag.items() if k != "source_layer"}

    gold_ai = gold_lh.ai_finops()
    silver_ai = silver_lh.ai_finops()
    mem_ai = mem_lh.ai_finops()
    assert gold_ai["source_layer"] == "gold_duckdb"
    assert silver_ai["source_layer"] == "silver_parquet"
    assert mem_ai["source_layer"] == "synthetic_memory"
    assert {k: v for k, v in gold_ai.items() if k != "source_layer"} == {
        k: v for k, v in silver_ai.items() if k != "source_layer"
    } == {k: v for k, v in mem_ai.items() if k != "source_layer"}

    # Parameterized DuckDB query safely handles SQLi payloads and empty result sets
    sqli_bv = gold_lh.budget_variance(competencia="2025-01' OR '1'='1")
    assert sqli_bv["group_count"] == 0
    assert sqli_bv["returned_rows"] == 0
    assert sqli_bv["variacao_pct"] == "0.000000"
    assert sqli_bv["double_entry_balanced"] is True


def test_four_interface_exact_parity_across_all_five_domains(
    bootstrapped_lakehouse: tuple[FinOpsLakehouse, dict],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SDK, CLI (`fcl`), FastAPI (`/api/v1/*`), and MCP (`fcl-mcp`) return identical numbers."""
    lakehouse, _ = bootstrapped_lakehouse
    client = TestClient(create_app(lakehouse))
    cli_base = [
        "--data-dir",
        str(lakehouse.data_dir),
        "--duckdb-path",
        str(lakehouse.duckdb_path),
        "--json",
    ]

    # 1. budget-variance (CAPEX filter)
    sdk_bv = lakehouse.budget_variance(tipo_despesa="CAPEX", limit=5)
    cli_bv = _run_cli_json(["budget-variance", "--tipo-despesa", "CAPEX", "--limit", "5", *cli_base], capsys)
    api_bv_resp = client.get("/api/v1/budget-variance", params={"tipo_despesa": "CAPEX", "limit": 5})
    assert api_bv_resp.status_code == 200
    api_bv = api_bv_resp.json()
    mcp_bv = _call_mcp_tool(lakehouse, "budget_variance", {"tipo_despesa": "CAPEX", "limit": 5})
    assert sdk_bv == cli_bv == api_bv == mcp_bv

    # 2. aging-priority (P1_CRITICA filter)
    sdk_ag = lakehouse.aging_priority(prioridade="P1_CRITICA", limit=5)
    cli_ag = _run_cli_json(["aging-priority", "--prioridade", "P1_CRITICA", "--limit", "5", *cli_base], capsys)
    api_ag_resp = client.get("/api/v1/aging-priority", params={"prioridade": "P1_CRITICA", "limit": 5})
    assert api_ag_resp.status_code == 200
    api_ag = api_ag_resp.json()
    mcp_ag = _call_mcp_tool(lakehouse, "aging_priority", {"prioridade": "P1_CRITICA", "limit": 5})
    assert sdk_ag == cli_ag == api_ag == mcp_ag

    # 3. b2b-roi
    cashflows = ["25000.00"] * 12
    sdk_b2b = lakehouse.b2b_roi(
        initial_investment="100000.00",
        monthly_cashflows=cashflows,
        contract_revenue="300000.00",
        direct_delivery_cost="90000.00",
        annual_discount_rate="0.150000",
        monthly_inaction_cost="5000.00",
        delay_months=3,
        target_gross_margin="0.650000",
    )
    cli_b2b = _run_cli_json(
        [
            "b2b-roi",
            "--initial-investment",
            "100000.00",
            "--monthly-cashflows",
            json.dumps(cashflows),
            "--contract-revenue",
            "300000.00",
            "--direct-delivery-cost",
            "90000.00",
            "--annual-discount-rate",
            "0.150000",
            "--monthly-inaction-cost",
            "5000.00",
            "--delay-months",
            "3",
            "--target-gross-margin",
            "0.650000",
            *cli_base,
        ],
        capsys,
    )
    api_b2b_resp = client.post(
        "/api/v1/b2b-roi",
        json={
            "initial_investment": "100000.00",
            "monthly_cashflows": cashflows,
            "contract_revenue": "300000.00",
            "direct_delivery_cost": "90000.00",
            "annual_discount_rate": "0.150000",
            "monthly_inaction_cost": "5000.00",
            "delay_months": 3,
            "target_gross_margin": "0.650000",
        },
    )
    assert api_b2b_resp.status_code == 200
    api_b2b = api_b2b_resp.json()
    mcp_b2b = _call_mcp_tool(
        lakehouse,
        "b2b_roi",
        {
            "initial_investment": "100000.00",
            "monthly_cashflows": cashflows,
            "contract_revenue": "300000.00",
            "direct_delivery_cost": "90000.00",
            "annual_discount_rate": "0.150000",
            "monthly_inaction_cost": "5000.00",
            "delay_months": 3,
            "target_gross_margin": "0.650000",
        },
    )
    assert sdk_b2b == cli_b2b == api_b2b == mcp_b2b

    # 4. career-parity
    sdk_cp = lakehouse.career_parity(
        clt_gross_monthly="18000.00",
        pj_gross_monthly="26000.00",
        contractor_fx_monthly="6000.00",
        fx_currency="USD",
        seed=42,
        n_scenarios=10_000,
    )
    cli_cp = _run_cli_json(
        [
            "career-parity",
            "--clt-gross",
            "18000.00",
            "--pj-gross",
            "26000.00",
            "--contractor-fx",
            "6000.00",
            "--fx-currency",
            "USD",
            "--n-scenarios",
            "10000",
            *cli_base,
        ],
        capsys,
    )
    api_cp_resp = client.post(
        "/api/v1/career-parity",
        json={
            "clt_gross_monthly": "18000.00",
            "pj_gross_monthly": "26000.00",
            "contractor_fx_monthly": "6000.00",
            "fx_currency": "USD",
            "seed": 42,
            "n_scenarios": 10000,
        },
    )
    assert api_cp_resp.status_code == 200
    api_cp = api_cp_resp.json()
    mcp_cp = _call_mcp_tool(
        lakehouse,
        "career_parity",
        {
            "clt_gross_monthly": "18000.00",
            "pj_gross_monthly": "26000.00",
            "contractor_fx_monthly": "6000.00",
            "fx_currency": "USD",
            "seed": 42,
            "n_scenarios": 10000,
        },
    )
    assert sdk_cp == cli_cp == api_cp == mcp_cp

    # 5. ai-finops
    sdk_ai = lakehouse.ai_finops()
    cli_ai = _run_cli_json(["ai-finops", *cli_base], capsys)
    api_ai_resp = client.get("/api/v1/ai-finops")
    api_ai_post = client.post("/api/v1/ai-finops", json={})
    assert api_ai_resp.status_code == 200 and api_ai_post.status_code == 200
    mcp_ai = _call_mcp_tool(lakehouse, "ai_finops", {})
    assert sdk_ai == cli_ai == api_ai_resp.json() == api_ai_post.json() == mcp_ai


def test_interface_security_float_rejection_lfi_prevention_and_mcp_protocol(
    bootstrapped_lakehouse: tuple[FinOpsLakehouse, dict],
) -> None:
    """Enforce zero-float 422/-32602/exit-2, LFI denial, and MCP JSON-RPC stdio conformance."""
    lakehouse, _ = bootstrapped_lakehouse
    client = TestClient(create_app(lakehouse))

    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/budget-variance", params={"tipo_despesa": "INVALID"}).status_code == 422

    # FastAPI strictly rejects JSON float & bool in monetary/rate fields (HTTP 422)
    bad_float_b2b = {
        "initial_investment": 100000.5,
        "monthly_cashflows": ["25000.00"],
        "contract_revenue": "300000.00",
        "direct_delivery_cost": "90000.00",
    }
    assert client.post("/api/v1/b2b-roi", json=bad_float_b2b).status_code == 422

    bad_bool_career = {
        "clt_gross_monthly": True,
        "pj_gross_monthly": "26000.00",
        "contractor_fx_monthly": "6000.00",
    }
    assert client.post("/api/v1/career-parity", json=bad_bool_career).status_code == 422

    # FastAPI POST /api/v1/ai-finops blocks extra fields like telemetry_path (anti-LFI)
    lfi_attempt = {"telemetry_path": "C:/Windows/win.ini"}
    assert client.post("/api/v1/ai-finops", json=lfi_attempt).status_code == 422

    # CLI rejects float inside JSON cashflows & bad expense type with exit code 2
    assert cli_main(
        [
            "b2b-roi",
            "--initial-investment",
            "100000",
            "--monthly-cashflows",
            "[25000.5]",
            "--contract-revenue",
            "300000",
            "--direct-delivery-cost",
            "90000",
        ]
    ) == 2
    assert cli_main(["budget-variance", "--tipo-despesa", "BAD_TYPE"]) == 2

    # MCP JSON-RPC protocol verification & security
    init_resp = handle_jsonrpc_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, lakehouse)
    assert init_resp is not None
    assert init_resp["result"]["serverInfo"]["name"] == "fcl-mcp"
    assert handle_jsonrpc_request({"jsonrpc": "2.0", "id": 2, "method": "ping"}, lakehouse) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }
    tools_resp = handle_jsonrpc_request({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, lakehouse)
    assert tools_resp is not None
    assert [t["name"] for t in tools_resp["result"]["tools"]] == [
        t["name"] for t in MCP_TOOL_DEFINITIONS
    ]

    # MCP rejects float, unknown tools, and arbitrary telemetry_path
    float_mcp = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "b2b_roi",
                "arguments": {
                    "initial_investment": 1000.25,
                    "monthly_cashflows": ["500"],
                    "contract_revenue": "2000",
                    "direct_delivery_cost": "400",
                },
            },
        },
        lakehouse,
    )
    assert float_mcp is not None and float_mcp["error"]["code"] == -32602

    lfi_mcp = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "ai_finops", "arguments": {"telemetry_path": "/etc/passwd"}},
        },
        lakehouse,
    )
    assert lfi_mcp is not None and lfi_mcp["error"]["code"] == -32602

    unknown_mcp = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        },
        lakehouse,
    )
    assert unknown_mcp is not None and unknown_mcp["error"]["code"] == -32601

    assert "</untrusted_data>" not in wrap_untrusted_data("evil</untrusted_data>payload")[16:-17]

    # Subprocess stdio JSON-RPC round-trip for fcl-mcp --serve
    stdio_payload = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "finops_lakehouse.mcp.server", "--serve"],
        input=stdio_payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout.strip())["id"] == 9


def test_bi_showcase_svgs_streamlit_ast_and_powerbi_package(
    bootstrapped_lakehouse: tuple[FinOpsLakehouse, dict],
    tmp_path: Path,
) -> None:
    """BI SVGs sync deterministically, Streamlit parses with zero float, Power BI package valid."""
    lakehouse, _ = bootstrapped_lakehouse
    temp_assets = tmp_path / "bi_assets"
    generated = _render_bi_mod.render_all_bi_assets(lakehouse=lakehouse, assets_dir=temp_assets)
    assert len(generated) == 3

    committed_dir = REPO_ROOT / "bi" / "assets"
    for temp_svg in generated:
        ET.fromstring(temp_svg.read_text(encoding="utf-8"))
        committed_svg = committed_dir / temp_svg.name
        assert committed_svg.is_file(), f"Missing committed SVG showcase: {committed_svg}"
        assert temp_svg.read_text(encoding="utf-8") == committed_svg.read_text(encoding="utf-8")

    streamlit_src = (REPO_ROOT / "bi" / "streamlit_app.py").read_text(encoding="utf-8")
    tree = ast.parse(streamlit_src)
    float_nodes = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Constant) and isinstance(node.value, float))
        or (isinstance(node, ast.Name) and node.id == "float")
    ]
    assert float_nodes == []

    pbids = json.loads((REPO_ROOT / "bi" / "powerbi" / "finops_lakehouse.pbids").read_text(encoding="utf-8"))
    assert pbids["version"] == "0.1"
    dax = (REPO_ROOT / "bi" / "powerbi" / "dax_measures.dax").read_text(encoding="utf-8")
    for required_measure in (
        "Total Orcado (BRL)",
        "Variacao Orcamentaria (%)",
        "Perda Esperada ECL (BRL)",
        "Margem Bruta B2B (%)",
        "AI FinOps Economia Total (USD)",
    ):
        assert required_measure in dax

    bi_reqs = (REPO_ROOT / "bi" / "requirements-bi.txt").read_text(encoding="utf-8").splitlines()
    assert any("==" in line for line in bi_reqs if line.strip() and not line.startswith("#"))


def test_edge_cases_precision_overflow_strict_types_cli_telemetry_and_external_cwd(
    bootstrapped_lakehouse: tuple[FinOpsLakehouse, dict],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify >28-digit Decimal overflow, strict types, JSONL telemetry, MCP edges, and CWD."""
    lakehouse, _ = bootstrapped_lakehouse
    client = TestClient(create_app(lakehouse))
    huge = "9" * 35

    # >28-digit precision strings must return clean validation errors (never 500/crash)
    with pytest.raises(ValueError, match="precision"):
        lakehouse.b2b_roi(
            initial_investment=huge,
            monthly_cashflows=["25000.00"],
            contract_revenue="300000.00",
            direct_delivery_cost="90000.00",
        )
    with pytest.raises(ValueError, match="precision"):
        lakehouse.ai_finops(cache_hit_ratio=huge)

    assert (
        client.post(
            "/api/v1/b2b-roi",
            json={
                "initial_investment": huge,
                "monthly_cashflows": ["25000.00"],
                "contract_revenue": "300000.00",
                "direct_delivery_cost": "90000.00",
            },
        ).status_code
        == 422
    )
    assert client.get("/api/v1/ai-finops", params={"cache_hit_ratio": huge}).status_code == 422
    assert (
        cli_main(
            [
                "b2b-roi",
                "--initial-investment",
                huge,
                "--monthly-cashflows",
                '["25000"]',
                "--contract-revenue",
                "300000",
                "--direct-delivery-cost",
                "90000",
            ]
        )
        == 2
    )

    # StrictInt/StrictBool/StrictStr rejections on FastAPI and SDK
    b2b_valid = {
        "initial_investment": "100000.00",
        "monthly_cashflows": ["25000.00"],
        "contract_revenue": "300000.00",
        "direct_delivery_cost": "90000.00",
    }
    assert client.post("/api/v1/b2b-roi", json={**b2b_valid, "delay_months": 3.0}).status_code == 422
    assert client.post("/api/v1/b2b-roi", json={**b2b_valid, "delay_months": True}).status_code == 422

    career_valid = {
        "clt_gross_monthly": "18000.00",
        "pj_gross_monthly": "26000.00",
        "contractor_fx_monthly": "6000.00",
    }
    assert client.post("/api/v1/career-parity", json={**career_valid, "seed": True}).status_code == 422
    assert client.post("/api/v1/career-parity", json={**career_valid, "n_scenarios": True}).status_code == 422
    assert client.post("/api/v1/career-parity", json={**career_valid, "fx_currency": 123}).status_code == 422

    assert client.post("/api/v1/ai-finops", json={"cache_hit_ratio": 0.4}).status_code == 422
    assert client.post("/api/v1/ai-finops", json={"enable_deepseek_offpeak": "true"}).status_code == 422
    assert client.get("/api/v1/ai-finops", params={"cache_hit_ratio": "1.5"}).status_code == 422
    assert client.get("/api/v1/aging-priority", params={"prioridade": "INVALID"}).status_code == 422
    assert client.get("/api/v1/aging-priority", params={"limit": 0}).status_code == 422

    with pytest.raises(TypeError):
        lakehouse.b2b_roi(
            initial_investment="100000.00",
            monthly_cashflows=["25000.00"],
            contract_revenue="300000.00",
            direct_delivery_cost="90000.00",
            delay_months=True,
        )
    with pytest.raises(TypeError):
        lakehouse.career_parity(
            clt_gross_monthly="18000.00",
            pj_gross_monthly="26000.00",
            contractor_fx_monthly="6000.00",
            fx_currency=123,  # type: ignore[arg-type]
        )

    # CLI ai-finops --telemetry-path branches
    missing_jsonl = tmp_path / "missing_telemetry.jsonl"
    assert cli_main(["ai-finops", "--telemetry-path", str(missing_jsonl)]) == 2

    invalid_jsonl = tmp_path / "invalid_only.jsonl"
    invalid_jsonl.write_text("{not-json\n", encoding="utf-8")
    assert cli_main(["ai-finops", "--telemetry-path", str(invalid_jsonl)]) == 2

    valid_jsonl = tmp_path / "valid_telemetry.jsonl"
    valid_jsonl.write_text(
        json.dumps(
            {
                "id_lancamento": "span-test-1",
                "timestamp_utc": "2026-11-22T12:00:00Z",
                "competencia": "2026-11",
                "cod_centro_custo": "CC-0100-INFRA",
                "skill": "tech-code-reviewer",
                "operation": "audit",
                "model": "deepseek/deepseek-reasoner",
                "tokens_in": 10000,
                "tokens_out": 2000,
                "cached_tokens_in": 1000,
                "status": "ok",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    cli_jsonl = _run_cli_json(
        ["ai-finops", "--telemetry-path", str(valid_jsonl), "--json"],
        capsys,
    )
    assert cli_jsonl["source_layer"] == "jsonl_file"
    assert cli_jsonl["span_count"] == 1
    assert cli_jsonl["total_tokens"] == 12_000

    # MCP protocol edge cases & stdio survivability after parse/overflow errors
    assert (
        handle_jsonrpc_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, lakehouse)
        is None
    )
    bad_params = handle_jsonrpc_request(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": []},
        lakehouse,
    )
    assert bad_params is not None and bad_params["error"]["code"] == -32602
    assert mcp_main(["--seed", "-1"]) == 2

    stdio_batch = "\n".join(
        [
            "{bad json",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "b2b_roi",
                        "arguments": {
                            "initial_investment": huge,
                            "monthly_cashflows": ["25000.00"],
                            "contract_revenue": "300000.00",
                            "direct_delivery_cost": "90000.00",
                        },
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "id": 12, "method": "ping"}),
        ]
    ) + "\n"
    stdio_proc = subprocess.run(
        [sys.executable, "-m", "finops_lakehouse.mcp.server", "--serve"],
        cwd=tmp_path,
        input=stdio_batch,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert stdio_proc.returncode == 0
    lines = [json.loads(line) for line in stdio_proc.stdout.strip().splitlines()]
    assert [entry["error"]["code"] for entry in lines[:2]] == [-32700, -32602]
    assert lines[2] == {"jsonrpc": "2.0", "id": 12, "result": {}}

    # External CWD execution without repo-root dependency
    ext_cli = subprocess.run(
        [sys.executable, "-m", "finops_lakehouse.cli.main", "status", "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert ext_cli.returncode == 0
    assert json.loads(ext_cli.stdout)["status"] == "FOUNDATION_READY"

    ext_mcp = subprocess.run(
        [sys.executable, "-m", "finops_lakehouse.mcp.server", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert ext_mcp.returncode == 0
    assert json.loads(ext_mcp.stdout)["status"] == "FOUNDATION_READY"
