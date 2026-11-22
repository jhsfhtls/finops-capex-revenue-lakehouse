"""Stdio MCP server (`fcl-mcp`) with JSON-RPC 2.0 tools and untrusted-data isolation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from finops_lakehouse import __version__
from finops_lakehouse.core.shield import configure_stdio_utf8
from finops_lakehouse.sdk import FinOpsLakehouse

_CLOSE_TAG_RE = re.compile(r"</\s*untrusted_data\s*>", re.IGNORECASE)
_DECIMAL_NUMBER_TYPE = {"type": ["string", "integer"]}

MCP_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "budget_variance",
        "description": "Query CAPEX/OPEX budget vs realized variance from Gold/Silver lakehouse.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "competencia": {"type": "string"},
                "cod_centro_custo": {"type": "string"},
                "tipo_despesa": {"type": "string", "enum": ["CAPEX", "OPEX"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        },
    },
    {
        "name": "aging_priority",
        "description": "Query receivables aging buckets, ECL, and top collection priority queue.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "faixa_aging": {"type": "string"},
                "prioridade": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
        },
    },
    {
        "name": "b2b_roi",
        "description": "Compute deterministic B2B NPV, IRR, Payback, Gross Margin Gate & COI.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "initial_investment",
                "monthly_cashflows",
                "contract_revenue",
                "direct_delivery_cost",
            ],
            "properties": {
                "initial_investment": _DECIMAL_NUMBER_TYPE,
                "monthly_cashflows": {"type": "array", "items": _DECIMAL_NUMBER_TYPE, "minItems": 1},
                "contract_revenue": _DECIMAL_NUMBER_TYPE,
                "direct_delivery_cost": _DECIMAL_NUMBER_TYPE,
                "annual_discount_rate": _DECIMAL_NUMBER_TYPE,
                "monthly_inaction_cost": _DECIMAL_NUMBER_TYPE,
                "delay_months": {"type": "integer", "minimum": 0},
                "target_gross_margin": _DECIMAL_NUMBER_TYPE,
            },
        },
    },
    {
        "name": "career_parity",
        "description": "Compare CLT vs PJ Simples (ISS Manaus) vs USD/EUR + 10k Monte Carlo.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["clt_gross_monthly", "pj_gross_monthly", "contractor_fx_monthly"],
            "properties": {
                "clt_gross_monthly": _DECIMAL_NUMBER_TYPE,
                "pj_gross_monthly": _DECIMAL_NUMBER_TYPE,
                "contractor_fx_monthly": _DECIMAL_NUMBER_TYPE,
                "fx_currency": {"type": "string", "enum": ["USD", "EUR"]},
                "vr_va_monthly": _DECIMAL_NUMBER_TYPE,
                "health_monthly": _DECIMAL_NUMBER_TYPE,
                "iss_rate": _DECIMAL_NUMBER_TYPE,
                "pro_labore_ratio": _DECIMAL_NUMBER_TYPE,
                "seed": {"type": "integer", "minimum": 0},
                "n_scenarios": {"type": "integer", "minimum": 1, "maximum": 50000},
            },
        },
    },
    {
        "name": "ai_finops",
        "description": "Summarize POP_T_158 AI/Cloud FinOps consumption & savings levers.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cache_hit_ratio": _DECIMAL_NUMBER_TYPE,
                "cache_discount_rate": _DECIMAL_NUMBER_TYPE,
                "batch_eligible_ratio": _DECIMAL_NUMBER_TYPE,
                "batch_discount_rate": _DECIMAL_NUMBER_TYPE,
                "enable_deepseek_offpeak": {"type": "boolean"},
            },
        },
    },
)
_TOOL_NAMES = frozenset(tool["name"] for tool in MCP_TOOL_DEFINITIONS)
_ALLOWED_TOOL_KEYS = {
    tool["name"]: frozenset(tool["inputSchema"]["properties"].keys())
    for tool in MCP_TOOL_DEFINITIONS
}


def wrap_untrusted_data(text: str) -> str:
    """Wrap external untrusted text in explicit XML delimiters (OWASP LLM01)."""
    sanitized = _CLOSE_TAG_RE.sub("&lt;/untrusted_data&gt;", text)
    return f"<untrusted_data>{sanitized}</untrusted_data>"


def _reject_nested_floats(value: Any, path: str = "arguments") -> None:
    if isinstance(value, float):
        raise TypeError(
            f"IEEE 754 float forbidden in '{path}'; pass exact string decimal or integer."
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_nested_floats(v, f"{path}.{k}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _reject_nested_floats(item, f"{path}[{idx}]")


def _dispatch_mcp_tool(
    name: str,
    arguments: dict[str, Any],
    lakehouse: FinOpsLakehouse,
) -> dict[str, Any]:
    if name not in _TOOL_NAMES:
        raise KeyError(f"Unknown MCP tool: {name}")
    if not isinstance(arguments, dict):
        raise TypeError("Tool arguments must be a JSON object.")

    extra_keys = set(arguments.keys()) - _ALLOWED_TOOL_KEYS[name]
    if extra_keys:
        raise ValueError(f"Unsupported argument(s) for '{name}': {', '.join(sorted(extra_keys))}")
    _reject_nested_floats(arguments)

    if name == "budget_variance":
        return lakehouse.budget_variance(**arguments)
    if name == "aging_priority":
        return lakehouse.aging_priority(**arguments)
    if name == "b2b_roi":
        return lakehouse.b2b_roi(**arguments)
    if name == "career_parity":
        return lakehouse.career_parity(**arguments)
    if name == "ai_finops":
        return lakehouse.ai_finops(**arguments)
    raise KeyError(f"Unknown MCP tool: {name}")


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_jsonrpc_request(
    request: dict[str, Any],
    lakehouse: FinOpsLakehouse,
) -> dict[str, Any] | None:
    """Handle one JSON-RPC 2.0 MCP message and return response dict (or None for notifications)."""
    if not isinstance(request, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request: expected JSON object.")

    req_id = request.get("id")
    method = request.get("method")
    raw_params = request.get("params")
    params = {} if raw_params is None else raw_params

    if not isinstance(method, str):
        return _jsonrpc_error(req_id, -32600, "Invalid Request: missing string 'method'.")
    if method.startswith("notifications/") and req_id is None:
        return None
    if not isinstance(params, dict):
        return _jsonrpc_error(req_id, -32602, "Invalid params: expected JSON object.")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fcl-mcp", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": list(MCP_TOOL_DEFINITIONS)},
        }
    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params["arguments"] if params.get("arguments") is not None else {}
        if not isinstance(tool_name, str) or not tool_name:
            return _jsonrpc_error(req_id, -32602, "Missing tool 'name' in tools/call.")
        try:
            payload = _dispatch_mcp_tool(tool_name, arguments, lakehouse)
        except KeyError as exc:
            return _jsonrpc_error(req_id, -32601, str(exc))
        except (TypeError, ValueError, FileNotFoundError, InvalidOperation) as exc:
            return _jsonrpc_error(req_id, -32602, str(exc))
        except Exception as exc:
            return _jsonrpc_error(req_id, -32603, f"Internal error: {exc}")

        serialized = json.dumps(payload, indent=2, ensure_ascii=False)
        wrapped = wrap_untrusted_data(serialized)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": wrapped}],
                "isError": False,
            },
        }

    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


def serve_stdio(lakehouse: FinOpsLakehouse) -> int:
    """Process newline-delimited JSON-RPC 2.0 MCP requests over stdin/stdout."""
    for raw_line in sys.stdin:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            req = json.loads(stripped)
        except json.JSONDecodeError as exc:
            err = _jsonrpc_error(None, -32700, f"Parse error: {exc}")
            print(json.dumps(err, ensure_ascii=False), flush=True)
            continue
        resp = handle_jsonrpc_request(req, lakehouse)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build parser for the `fcl-mcp` CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="fcl-mcp",
        description="FinOps CAPEX/Revenue Lakehouse MCP server.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify MCP server metadata without opening a stdio loop.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start newline-delimited JSON-RPC 2.0 stdio server loop.",
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--duckdb-path", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """MCP entrypoint returning explicit exit codes."""
    configure_stdio_utf8()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        lakehouse = FinOpsLakehouse(
            data_dir=args.data_dir,
            duckdb_path=args.duckdb_path,
            seed=args.seed,
        )
    except (TypeError, ValueError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.serve:
        return serve_stdio(lakehouse)

    print(
        json.dumps(
            {
                "server": "fcl-mcp",
                "version": __version__,
                "prompt_injection_delimiter": "untrusted_data",
                "tools": sorted(_TOOL_NAMES),
                "status": "FOUNDATION_READY" if args.check else "READY",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
