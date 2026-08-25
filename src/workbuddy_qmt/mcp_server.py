import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from . import __version__
from .config import load_config
from .modes import RUN_MODES
from .util import new_id


def _object(properties=None, required=None):
    return {
        "type": "object", "properties": properties or {},
        "required": required or [], "additionalProperties": False,
    }


STRING = {"type": "string"}
ACCOUNT = {"type": "string", "minLength": 1}
SYMBOL = {"type": "string", "pattern": "^[0-9]{6}(\\.(SH|SZ|BJ))?$"}


TRADE_REQUEST = _object({
    "account_alias": ACCOUNT,
    "account_type": {"type": "string", "enum": ["STOCK", "CREDIT"]},
    "asset_type": {"type": "string", "enum": ["STOCK"]},
    "instrument": _object({"canonical_symbol": SYMBOL, "qmt_symbol": SYMBOL}, ["canonical_symbol"]),
    "action": {"type": "string", "enum": [
        "BUY", "SELL", "TARGET_POSITION", "COLLATERAL_BUY", "COLLATERAL_SELL",
        "MARGIN_BUY", "SHORT_SELL", "BUY_TO_REPAY", "SELL_TO_REPAY",
    ]},
    "sizing": _object({
        "type": {"type": "string", "enum": [
            "FIXED_VOLUME", "FIXED_NOTIONAL", "AVAILABLE_CASH_PERCENT",
            "TARGET_PORTFOLIO_PERCENT", "TARGET_POSITION",
        ]},
        "value": {"type": "number", "exclusiveMinimum": 0},
        "max_volume": {"type": "integer", "minimum": 1},
    }, ["type", "value"]),
    "price_policy": _object({
        "type": {"type": "string", "enum": ["FIXED_LIMIT", "LIMIT_FROM_LATEST", "LIMIT_FROM_BOOK"]},
        "limit_price": {"type": "number", "exclusiveMinimum": 0},
        "offset_bps": {"type": "number", "minimum": -1000, "maximum": 1000},
        "max_deviation_pct": {"type": "number", "minimum": 0, "maximum": 1},
    }, ["type"]),
    "signal_evidence": _object({
        "occurred_at": STRING, "quote_at": STRING,
        "reference_price": {"type": "number", "exclusiveMinimum": 0},
    }),
    "execution_mode": {"type": "string", "enum": list(RUN_MODES)},
    "source": _object(
        {"type": STRING, "signal_id": STRING, "rule_set_id": STRING, "rule_version": STRING},
        ["type", "signal_id"],
    ),
    "credit": _object({"debt_contract_ref": {"type": ["string", "null"]}, "capacity_snapshot_id": {"type": ["string", "null"]}}),
}, ["account_alias", "instrument", "action", "sizing", "price_policy", "execution_mode", "source"])


def _tool(name, description, schema, read_only=True, destructive=False):
    return {
        "name": name, "description": description, "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only, "destructiveHint": destructive,
            "idempotentHint": read_only, "openWorldHint": False,
        },
    }


TOOLS = [
    _tool("qmt_health", "Return Worker, QMT adapter, snapshot, queue, mode and halt health.", _object()),
    _tool("list_account_aliases", "List configured safe account aliases; never returns real broker account IDs.", _object({"account_type": {"type": "string", "enum": ["STOCK", "CREDIT"]}})),
    _tool("get_account_snapshot", "Get the latest normalized stock account snapshot.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("get_quote_snapshot", "Get QMT-derived quotes, exchange daily limits, tick sizes and dynamic price-cage boundaries. Refresh with request_sync first.", _object({
        "account_alias": ACCOUNT, "symbols": {"type": "array", "items": SYMBOL, "minItems": 1, "maxItems": 100},
    }, ["account_alias", "symbols"])),
    _tool("get_positions", "Get positions from one coherent latest snapshot.", _object({
        "account_alias": ACCOUNT, "symbols": {"type": "array", "items": SYMBOL, "maxItems": 100}, "include_zero": {"type": "boolean"},
    }, ["account_alias"])),
    _tool("get_orders", "Get normalized QMT orders.", _object({
        "account_alias": ACCOUNT, "status": {"oneOf": [STRING, {"type": "array", "items": STRING}]}, "trading_day": STRING,
    }, ["account_alias"])),
    _tool("get_trades", "Get normalized QMT trades.", _object({"account_alias": ACCOUNT, "trading_day": STRING}, ["account_alias"])),
    _tool("get_credit_account_snapshot", "Get the latest CREDIT assets, debt, capacity and maintenance ratio snapshot.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("get_credit_debt_contracts", "Get redacted credit debt references from the latest snapshot.", _object({
        "account_alias": ACCOUNT, "symbol": SYMBOL, "status": STRING,
    }, ["account_alias"])),
    _tool("get_credit_instrument_eligibility", "Get collateral, margin and short eligibility for symbols.", _object({
        "account_alias": ACCOUNT, "symbols": {"type": "array", "items": SYMBOL, "minItems": 1, "maxItems": 100},
    }, ["account_alias", "symbols"])),
    _tool("get_credit_capacity", "Get recent serialized credit capacity query results.", _object({
        "account_alias": ACCOUNT,
    }, ["account_alias"])),
    _tool("get_trade_intent", "Get one intent plus linked order and trade facts.", _object({"intent_id": STRING}, ["intent_id"])),
    _tool("list_trade_intents", "List persisted intents in stable sequence order.", _object({
        "status": {"oneOf": [STRING, {"type": "array", "items": STRING}]}, "after_seq": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    })),
    _tool("get_risk_limits", "Get versioned local hard-risk limits for an alias.", _object({"account_alias": ACCOUNT}, ["account_alias"])),
    _tool("preview_trade", "Normalize and hard-risk-check a trade using a fresh QMT quote. Price is favorably tick-rounded and constrained by daily limits and exchange dynamic price cages; no executable command is created. Call request_sync with QUOTE immediately before preview.", _object({"trade_request": TRADE_REQUEST}, ["trade_request"])),
    _tool(
        "authorize_manual_trade",
        "HIGH RISK: authorize one exact, unexpired MANUAL_LIVE preview through MCP. The Worker must already have been placed in MANUAL_LIVE locally. Rechecks risk, opens a short LIVE window, creates a one-time preview approval, and returns approval_context for submit_trade_intent. This can enable a real QMT order; call only after the user explicitly confirms the displayed preview.",
        _object({
            "preview_id": STRING,
            "live_minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
            "approval_ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30},
            "reason": {"type": "string", "minLength": 1, "maxLength": 200},
            "confirm": {"type": "string", "enum": ["AUTHORIZE-MANUAL-TRADE"]},
        }, ["preview_id", "reason", "confirm"]),
        read_only=False, destructive=True,
    ),
    _tool(
        "get_manual_authorization_status",
        "Get the account's active timed MANUAL_LIVE session, remaining seconds, LIVE window and count of unused one-time approvals.",
        _object({"account_alias": ACCOUNT}, ["account_alias"]),
    ),
    _tool(
        "authorize_manual_session",
        "HIGH RISK: authorize unlimited real-order submissions for one account during a short MANUAL_LIVE time window. The Worker must already be in MANUAL_LIVE. Every order still requires a fresh preview, unchanged risk snapshot and passing hard-risk checks, but no per-order approval_context is required until expiry or revocation. Call only after the user explicitly confirms the duration and account.",
        _object({
            "account_alias": ACCOUNT,
            "minutes": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
            "reason": {"type": "string", "minLength": 1, "maxLength": 200},
            "confirm": {"type": "string", "enum": ["AUTHORIZE-TIMED-MANUAL-TRADING"]},
        }, ["account_alias", "reason", "confirm"]),
        read_only=False, destructive=True,
    ),
    _tool(
        "revoke_manual_session",
        "Immediately revoke an account's timed MANUAL_LIVE session and any unused one-time approvals. New real-order submissions require a new authorization.",
        _object({
            "account_alias": ACCOUNT,
            "reason": {"type": "string", "minLength": 1, "maxLength": 200},
            "confirm": {"type": "string", "enum": ["REVOKE-TIMED-MANUAL-TRADING"]},
        }, ["account_alias", "reason", "confirm"]),
        read_only=False,
    ),
    _tool(
        "check_limited_auto_readiness",
        "Run the fail-closed P1 readiness and reconciliation checks for one account. Reports mode/Adapter/snapshot/queue/dead-letter/SUBMIT_UNKNOWN blockers and never places an order.",
        _object({"account_alias": ACCOUNT}, ["account_alias"]),
    ),
    _tool(
        "get_limited_auto_status",
        "Return the latest P1 limited-auto permit, exact policy, remaining time/order/notional budgets, pause reason and current health blockers.",
        _object({"account_alias": ACCOUNT}, ["account_alias"]),
    ),
    _tool(
        "authorize_limited_auto",
        "HIGH RISK: create a same-trading-day P1 automatic-order permit. The Worker and Adapter must already be in LIMITED_AUTO and healthy. Every order remains bound to the exact account, symbols, actions, sizing types, strategy/rule version, time windows, order/session budgets, rate, concurrency, position exposure, account drawdown and consecutive-failure guards. This can enable repeated real QMT orders until expiry, pause or revocation; show the complete policy and obtain explicit user confirmation first.",
        _object({
            "account_alias": ACCOUNT,
            "symbols": {"type": "array", "items": SYMBOL, "minItems": 1, "maxItems": 100, "uniqueItems": True},
            "actions": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "enum": ["BUY", "SELL", "COLLATERAL_BUY", "COLLATERAL_SELL", "MARGIN_BUY", "SHORT_SELL", "BUY_TO_REPAY", "SELL_TO_REPAY"]}},
            "source_type": {"type": "string", "minLength": 1, "maxLength": 100},
            "rule_set_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "rule_version": {"type": "string", "minLength": 1, "maxLength": 100},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 720, "default": 480},
            "max_order_notional": {"type": "number", "exclusiveMinimum": 0},
            "max_order_volume": {"type": "integer", "minimum": 1},
            "max_session_notional": {"type": "number", "exclusiveMinimum": 0},
            "max_orders": {"type": "integer", "minimum": 1},
            "min_order_interval_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 1},
            "max_concurrent_orders": {"type": "integer", "minimum": 1},
            "max_symbol_position_notional": {"type": "number", "exclusiveMinimum": 0},
            "max_account_drawdown": {"type": "number", "exclusiveMinimum": 0},
            "max_consecutive_failures": {"type": "integer", "minimum": 1, "maximum": 100, "default": 3},
            "trading_windows": {"type": "array", "minItems": 1, "maxItems": 8, "items": _object({
                "start": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"},
                "end": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"},
            }, ["start", "end"])},
            "allowed_sizing_types": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "enum": ["FIXED_VOLUME", "FIXED_NOTIONAL", "AVAILABLE_CASH_PERCENT", "TARGET_PORTFOLIO_PERCENT", "TARGET_POSITION"]}},
            "allow_credit_new_debt": {"type": "boolean", "default": False},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "confirm": {"type": "string", "enum": ["AUTHORIZE-LIMITED-AUTO-P1"]},
        }, [
            "account_alias", "symbols", "actions", "source_type", "rule_set_id",
            "rule_version", "max_order_notional", "max_order_volume",
            "max_session_notional", "max_orders", "max_concurrent_orders",
            "max_symbol_position_notional", "max_account_drawdown", "reason", "confirm",
        ]),
        read_only=False, destructive=True,
    ),
    _tool(
        "resume_limited_auto",
        "HIGH RISK: resume a P1 permit paused by a health/drawdown/failure guard after readiness is restored. Rotates the authorization generation so commands signed before the pause remain invalid.",
        _object({
            "account_alias": ACCOUNT,
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "confirm": {"type": "string", "enum": ["RESUME-LIMITED-AUTO-P1"]},
        }, ["account_alias", "reason", "confirm"]),
        read_only=False, destructive=True,
    ),
    _tool(
        "revoke_limited_auto",
        "Immediately revoke an active or paused P1 permit and rotate its authorization generation. Existing broker orders are not cancelled automatically.",
        _object({
            "account_alias": ACCOUNT,
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "confirm": {"type": "string", "enum": ["REVOKE-LIMITED-AUTO-P1"]},
        }, ["account_alias", "reason", "confirm"]),
        read_only=False,
    ),
    _tool("submit_trade_intent", "Submit one unexpired preview after rechecking snapshots and hard risk, plus MANUAL_LIVE approval/session or an exact P1 limited-auto permit with atomically reserved budgets.", _object({
        "preview_id": STRING,
        "approval_context": _object({"local_approval_id": STRING}),
    }, ["preview_id"]), read_only=False),
    _tool("cancel_order", "Request cancellation of one exact active QMT order owned by this bridge.", _object({
        "account_alias": ACCOUNT, "order_id": STRING, "reason": {"type": "string", "minLength": 1, "maxLength": 200},
    }, ["account_alias", "order_id", "reason"]), read_only=False, destructive=True),
    _tool("request_sync", "Request fresh adapter snapshots. QUOTE requires symbols and obtains QMT daily limits, tick size, book benchmark and dynamic cage; this never places an order.", _object({
        "account_alias": ACCOUNT,
        "scopes": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "enum": ["ACCOUNT", "POSITION", "ORDER", "DEAL", "QUOTE", "CREDIT_ACCOUNT", "CREDIT_DEBT", "CREDIT_ELIGIBILITY"]}},
        "symbols": {"type": "array", "items": SYMBOL, "minItems": 1, "maxItems": 100},
    }, ["account_alias", "scopes"]), read_only=False),
    _tool("request_credit_precheck", "Start one serialized and rate-limited CREDIT capacity query; never creates a trade intent.", _object({
        "account_alias": ACCOUNT,
        "requests": {"type": "array", "minItems": 1, "maxItems": 20, "items": _object({
            "symbol": SYMBOL, "credit_action": {"type": "string", "enum": list(sorted(["COLLATERAL_BUY", "COLLATERAL_SELL", "MARGIN_BUY", "SHORT_SELL", "BUY_TO_REPAY", "SELL_TO_REPAY"]))},
            "price_type": {"type": "string", "enum": ["LIMIT"]}, "price": {"type": "number", "exclusiveMinimum": 0},
        }, ["symbol", "credit_action", "price_type", "price"])},
    }, ["account_alias", "requests"]), read_only=False),
    _tool("halt_trading", "Fail closed and remotely halt all new trading. Only the local console can clear it.", _object({
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    }, ["reason"]), read_only=False, destructive=True),
]


class WorkerClient:
    def __init__(self, endpoint, token, timeout=10):
        self.url = endpoint.rstrip("/") + "/rpc"
        self.token = token
        self.timeout = timeout

    def call(self, method, params):
        payload = json.dumps({"method": method, "params": params, "request_id": new_id("req")}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            return {
                "ok": False, "request_id": new_id("req"), "as_of": None, "data": None, "warnings": [],
                "error": {"code": "BRIDGE_UNAVAILABLE", "message": "cannot reach local bridge worker", "details": {}},
            }


def read_message(stream):
    while True:
        line = stream.readline()
        if not line:
            return None
        if not line.strip():
            continue
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
            while True:
                header = stream.readline()
                if header in (b"\r\n", b"\n", b""):
                    break
            return json.loads(stream.read(length).decode("utf-8"))
        return json.loads(line.decode("utf-8"))


def write_message(stream, message):
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    stream.write(encoded)
    stream.flush()


def serve(client, input_stream=None, output_stream=None):
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    while True:
        try:
            message = read_message(input_stream)
            if message is None:
                return 0
            if "id" not in message:
                continue
            request_id = message["id"]
            method = message.get("method")
            if method == "initialize":
                requested = (message.get("params") or {}).get("protocolVersion")
                result = {
                    "protocolVersion": requested or "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "workbuddy-qmt-bridge", "version": __version__},
                    "instructions": "Trade tools are fail-closed. Preview before submit. LIMITED_AUTO requires an exact P1 permit and health gate; mode changes and halt recovery remain local-console operations.",
                }
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": result})
            elif method == "ping":
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": {}})
            elif method == "tools/list":
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                known = any(tool["name"] == name for tool in TOOLS)
                if not known:
                    raise ValueError("unknown tool")
                response = client.call(name, arguments)
                text = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                write_message(output_stream, {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": not response.get("ok", False)},
                })
            else:
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
        except Exception as exc:
            request_id = locals().get("request_id")
            if request_id is not None:
                write_message(output_stream, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)}})


def main(argv=None):
    parser = argparse.ArgumentParser(description="stdio MCP frontend for WorkBuddy-QMT")
    parser.add_argument("--config", default=None)
    parser.add_argument("--endpoint", default=None)
    parser.add_argument("--token-file", default=None)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    endpoint = args.endpoint or "http://%s:%d" % (config.host, config.port)
    token_file = args.token_file or config.worker_token_file
    try:
        with open(token_file, "r", encoding="ascii") as stream:
            token = stream.read().strip()
    except OSError as exc:
        print("cannot read worker token: %s" % exc, file=sys.stderr)
        return 2
    return serve(WorkerClient(endpoint, token))


if __name__ == "__main__":
    sys.exit(main())
