import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import time

from .errors import BridgeError, ValidationError
from .modes import RUN_MODES
from .security import make_envelope
from .util import (
    atomic_write_json,
    canonical_json,
    iso_now,
    json_text,
    new_client_order_key,
    new_id,
    normalize_symbol,
    parse_time,
    require_exact_keys,
    utc_now,
)

STOCK_ACTIONS = {"BUY", "SELL", "TARGET_POSITION"}
CREDIT_EXPLICIT_ACTIONS = {
    "COLLATERAL_BUY", "COLLATERAL_SELL", "MARGIN_BUY", "SHORT_SELL",
    "BUY_TO_REPAY", "SELL_TO_REPAY",
}
CREDIT_ACTIONS = CREDIT_EXPLICIT_ACTIONS | {"BUY", "SELL"}
BUY_LIKE = {"BUY", "COLLATERAL_BUY", "MARGIN_BUY", "BUY_TO_REPAY"}
SELL_LIKE = {"SELL", "COLLATERAL_SELL", "SHORT_SELL", "SELL_TO_REPAY"}
ACTIVE_ORDER_STATES = {
    "QUEUED", "REPORTED", "ACCEPTED", "ORDER_ACCEPTED", "PARTIALLY_FILLED",
    "CANCEL_REQUESTED", "SUBMIT_CALLED",
}
TERMINAL_INTENT_STATES = {
    "PREVIEW_EXPIRED", "APPROVAL_REJECTED", "RISK_REJECTED", "QMT_REJECTED",
    "BROKER_REJECTED", "EXPIRED", "CANCELLED", "PARTIALLY_CANCELLED", "FAILED", "FILLED",
    "OBSERVE_ONLY_ACKNOWLEDGED",
}
AUTO_FAILURE_STATES = {"QMT_REJECTED", "BROKER_REJECTED", "FAILED", "SUBMIT_UNKNOWN"}
AUTO_NEW_DEBT_ACTIONS = {"MARGIN_BUY", "SHORT_SELL"}
AUTO_PAUSE_REASONS = {
    "AUTO_ADAPTER_NOT_READY", "AUTO_ADAPTER_MODE_MISMATCH",
    "AUTO_ADAPTER_LOCALLY_HALTED", "AUTO_ADAPTER_LOCALLY_PAUSED",
    "AUTO_DEAD_LETTER_PRESENT",
    "AUTO_QUEUE_BACKLOG", "AUTO_SUBMIT_UNKNOWN_PRESENT",
    "AUTO_ACCOUNT_SNAPSHOT_STALE", "AUTO_POSITION_SNAPSHOT_STALE",
    "AUTO_ACCOUNT_DRAWDOWN_EXCEEDED", "AUTO_CONSECUTIVE_FAILURE_LIMIT",
}
CHINA_TZ = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def _payload(row):
    return json.loads(row["payload_json"]) if row else None


def _number(value, name, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError("%s must be a finite number" % name)
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValidationError("%s is below its minimum" % name)
    if maximum is not None and result > maximum:
        raise ValidationError("%s is above its maximum" % name)
    return result


_SNAPSHOT_METADATA_FIELDS = {
    "type", "snapshot_id", "seq", "captured_at", "received_at",
    "occurred_at", "event_id", "account_alias", "account_type",
}


def _material_payload(payload):
    """Return snapshot content without transport/version metadata."""
    if not isinstance(payload, dict):
        return payload
    return {
        key: value for key, value in payload.items()
        if key not in _SNAPSHOT_METADATA_FIELDS
    }


class BridgeCore:
    def __init__(self, config, database, keyring, file_queue, ingester):
        self.config = config
        self.db = database
        self.keyring = keyring
        self.queue = file_queue
        self.ingester = ingester

    def call(self, method, params=None):
        params = params or {}
        functions = {
            "qmt_health": self.qmt_health,
            "list_account_aliases": self.list_account_aliases,
            "get_account_snapshot": self.get_account_snapshot,
            "get_quote_snapshot": self.get_quote_snapshot,
            "get_positions": self.get_positions,
            "get_orders": self.get_orders,
            "get_trades": self.get_trades,
            "get_credit_account_snapshot": self.get_credit_account_snapshot,
            "get_credit_debt_contracts": self.get_credit_debt_contracts,
            "get_credit_instrument_eligibility": self.get_credit_instrument_eligibility,
            "get_credit_capacity": self.get_credit_capacity,
            "get_trade_intent": self.get_trade_intent,
            "list_trade_intents": self.list_trade_intents,
            "get_risk_limits": self.get_risk_limits,
            "prepare_trade": self.prepare_trade,
            "preview_trade": self.preview_trade,
            "authorize_manual_trade": self.authorize_manual_trade,
            "get_manual_authorization_status": self.get_manual_authorization_status,
            "authorize_manual_session": self.authorize_manual_session,
            "revoke_manual_session": self.revoke_manual_session,
            "check_limited_auto_readiness": self.check_limited_auto_readiness,
            "authorize_limited_auto": self.authorize_limited_auto,
            "get_limited_auto_status": self.get_limited_auto_status,
            "resume_limited_auto": self.resume_limited_auto,
            "revoke_limited_auto": self.revoke_limited_auto,
            "submit_trade_intent": self.submit_trade_intent,
            "wait_trade_intent": self.wait_trade_intent,
            "cancel_order": self.cancel_order,
            "request_sync": self.request_sync,
            "request_credit_precheck": self.request_credit_precheck,
            "halt_trading": self.halt_trading,
        }
        function = functions.get(method)
        if not function:
            raise BridgeError("METHOD_NOT_FOUND", "unknown bridge method")
        if not isinstance(params, dict):
            raise ValidationError("params must be an object")
        return function(**params)

    def _state(self, connection, key, default=None):
        row = connection.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _mode(self, connection):
        return self._state(connection, "mode", self.config.default_mode)

    def _halted(self, connection):
        return self._state(connection, "halted", "false") == "true"

    def _latest_account_row(self, connection, alias):
        return connection.execute(
            "SELECT * FROM account_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
            (alias,),
        ).fetchone()

    def _latest_credit_row(self, connection, alias):
        return connection.execute(
            "SELECT * FROM credit_account_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
            (alias,),
        ).fetchone()

    def _latest_quote_row(self, connection, alias, symbol):
        return connection.execute(
            "SELECT * FROM quote_snapshots WHERE account_alias=? AND symbol=? ORDER BY captured_at DESC LIMIT 1",
            (alias, symbol),
        ).fetchone()

    def _latest_positions(self, connection, alias):
        marker = connection.execute(
            "SELECT snapshot_id,captured_at FROM position_snapshot_runs WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
            (alias,),
        ).fetchone()
        if not marker:
            return None, []
        rows = connection.execute(
            "SELECT * FROM position_snapshots WHERE account_alias=? AND snapshot_id=? ORDER BY symbol",
            (alias, marker["snapshot_id"]),
        ).fetchall()
        return marker, [_payload(row) for row in rows]

    @staticmethod
    def _age_seconds(timestamp):
        try:
            return max(0.0, (utc_now() - parse_time(timestamp)).total_seconds())
        except (TypeError, ValueError):
            return float("inf")

    def qmt_health(self):
        with self.db.connect() as connection:
            mode = self._mode(connection)
            halt_reason = self._state(connection, "halt_reason", "")
            accounts = []
            now = utc_now()
            unresolved = connection.execute(
                "SELECT COUNT(*) AS n FROM trade_intents WHERE status='SUBMIT_UNKNOWN'"
            ).fetchone()["n"]
            for account in self.config.accounts.values():
                heartbeat = connection.execute(
                    "SELECT * FROM heartbeats WHERE adapter_instance=?", (account.adapter_instance,)
                ).fetchone()
                snapshot = self._latest_account_row(connection, account.alias)
                depths = self.queue.depths(account.adapter_instance)
                heartbeat_age = self._age_seconds(heartbeat["received_at"]) if heartbeat else None
                snapshot_age = self._age_seconds(snapshot["captured_at"]) if snapshot else None
                ready = bool(heartbeat and heartbeat["status"] == "READY" and heartbeat_age <= 15)
                auto_status = self._limited_auto_status(connection, account, include_health=False)
                accounts.append({
                    "account_alias": account.alias,
                    "account_type": account.account_type,
                    "adapter_instance": account.adapter_instance,
                    "adapter_status": heartbeat["status"] if heartbeat else "OFFLINE",
                    "heartbeat_age_seconds": heartbeat_age,
                    "account_snapshot_age_seconds": snapshot_age,
                    "ready": ready,
                    "queue_depths": depths,
                    "limited_auto": auto_status,
                })
            return {
                "worker_status": "READY",
                "mode": mode,
                "halted": self._halted(connection),
                "halt_reason": halt_reason or None,
                "accounts": accounts,
                "unresolved_submit_unknown": unresolved,
                "as_of": now.isoformat(timespec="milliseconds"),
            }

    def list_account_aliases(self, account_type=None):
        if account_type is not None and account_type not in {"STOCK", "CREDIT"}:
            raise ValidationError("account_type must be STOCK or CREDIT")
        return [{
            "account_alias": account.alias,
            "account_type": account.account_type,
            "adapter_instance": account.adapter_instance,
            "enabled": account.enabled,
        } for account in self.config.accounts.values() if account_type in (None, account.account_type)]

    def get_account_snapshot(self, account_alias):
        self.config.account(account_alias)
        with self.db.connect() as connection:
            row = self._latest_account_row(connection, account_alias)
            if not row:
                raise BridgeError("ACCOUNT_SNAPSHOT_STALE", "no account snapshot is available")
            return {"snapshot": _payload(row), "age_seconds": self._age_seconds(row["captured_at"])}

    def get_positions(self, account_alias, symbols=None, include_zero=False):
        self.config.account(account_alias)
        wanted = None
        if symbols is not None:
            if not isinstance(symbols, list):
                raise ValidationError("symbols must be an array")
            try:
                wanted = {normalize_symbol(item) for item in symbols}
            except ValueError as exc:
                raise ValidationError(str(exc))
        with self.db.connect() as connection:
            marker, positions = self._latest_positions(connection, account_alias)
            if not marker:
                raise BridgeError("ACCOUNT_SNAPSHOT_STALE", "no position snapshot is available")
            result = []
            for item in positions:
                if wanted is not None and item["symbol"] not in wanted:
                    continue
                if not include_zero and int(item.get("total_volume", 0) or 0) == 0:
                    continue
                result.append(item)
            return {
                "snapshot_id": marker["snapshot_id"],
                "captured_at": marker["captured_at"],
                "age_seconds": self._age_seconds(marker["captured_at"]),
                "positions": result,
            }

    def get_quote_snapshot(self, account_alias, symbols):
        self.config.account(account_alias)
        if not isinstance(symbols, list) or not symbols or len(symbols) > 100:
            raise ValidationError("symbols must be a non-empty array with at most 100 entries")
        try:
            wanted = sorted({normalize_symbol(item) for item in symbols})
        except ValueError as exc:
            raise ValidationError(str(exc))
        quotes = []
        missing = []
        with self.db.connect() as connection:
            for symbol in wanted:
                row = self._latest_quote_row(connection, account_alias, symbol)
                if not row:
                    missing.append(symbol)
                    continue
                payload = _payload(row)
                age = max(self._age_seconds(row["captured_at"]), self._age_seconds(payload.get("tick_at")))
                quotes.append({
                    "snapshot_id": row["snapshot_id"], "captured_at": row["captured_at"],
                    "age_seconds": age, "quote": payload,
                })
        return {"quotes": quotes, "missing_symbols": missing}

    def get_orders(self, account_alias, status=None, trading_day=None):
        self.config.account(account_alias)
        clauses = ["account_alias=?"]
        values = [account_alias]
        if status is not None:
            statuses = [status] if isinstance(status, str) else status
            if not isinstance(statuses, list) or not statuses:
                raise ValidationError("status must be a string or non-empty array")
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            values.extend(statuses)
        if trading_day:
            clauses.append("trading_day=?")
            values.append(str(trading_day).replace("-", ""))
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM orders WHERE %s ORDER BY updated_at DESC" % " AND ".join(clauses), values
            ).fetchall()
            return [_payload(row) for row in rows]

    def get_trades(self, account_alias, trading_day=None):
        self.config.account(account_alias)
        query = "SELECT payload_json FROM trades WHERE account_alias=?"
        values = [account_alias]
        if trading_day:
            query += " AND trading_day=?"
            values.append(str(trading_day).replace("-", ""))
        query += " ORDER BY traded_at DESC"
        with self.db.connect() as connection:
            return [_payload(row) for row in connection.execute(query, values).fetchall()]

    def get_credit_account_snapshot(self, account_alias):
        account = self.config.account(account_alias)
        if account.account_type != "CREDIT":
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "account is not CREDIT")
        with self.db.connect() as connection:
            row = self._latest_credit_row(connection, account_alias)
            if not row:
                raise BridgeError("CREDIT_SNAPSHOT_STALE", "no credit account snapshot is available")
            return {"snapshot": _payload(row), "age_seconds": self._age_seconds(row["captured_at"])}

    def get_credit_debt_contracts(self, account_alias, symbol=None, status="OPEN"):
        account = self.config.account(account_alias)
        if account.account_type != "CREDIT":
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "account is not CREDIT")
        wanted = normalize_symbol(symbol) if symbol else None
        with self.db.connect() as connection:
            marker = connection.execute(
                "SELECT snapshot_id,captured_at FROM credit_debt_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
                (account_alias,),
            ).fetchone()
            if not marker:
                return {"snapshot_id": None, "debts": []}
            query = "SELECT payload_json FROM credit_debt_snapshots WHERE account_alias=? AND snapshot_id=?"
            values = [account_alias, marker["snapshot_id"]]
            if wanted:
                query += " AND symbol=?"
                values.append(wanted)
            if status:
                query += " AND status=?"
                values.append(status)
            debts = [_payload(row) for row in connection.execute(query, values).fetchall()]
            return {"snapshot_id": marker["snapshot_id"], "captured_at": marker["captured_at"], "debts": debts}

    def get_credit_instrument_eligibility(self, account_alias, symbols):
        account = self.config.account(account_alias)
        if account.account_type != "CREDIT":
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "account is not CREDIT")
        if not isinstance(symbols, list) or not symbols:
            raise ValidationError("symbols must be a non-empty array")
        wanted = [normalize_symbol(item) for item in symbols]
        with self.db.connect() as connection:
            marker = connection.execute(
                "SELECT snapshot_id,captured_at FROM credit_eligibility_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
                (account_alias,),
            ).fetchone()
            if not marker:
                return {"snapshot_id": None, "instruments": [], "missing": wanted}
            rows = connection.execute(
                "SELECT symbol,payload_json FROM credit_eligibility_snapshots WHERE account_alias=? AND snapshot_id=?",
                (account_alias, marker["snapshot_id"]),
            ).fetchall()
            items = {row["symbol"]: _payload(row) for row in rows if row["symbol"] in wanted}
            return {
                "snapshot_id": marker["snapshot_id"], "captured_at": marker["captured_at"],
                "instruments": list(items.values()), "missing": [item for item in wanted if item not in items],
            }

    def get_credit_capacity(self, account_alias):
        account = self.config.account(account_alias)
        if account.account_type != "CREDIT":
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "account is not CREDIT")
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM credit_capacity_queries WHERE account_alias=? ORDER BY requested_at DESC LIMIT 50",
                (account_alias,),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["request"] = json.loads(item.pop("request_json"))
                item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
                item.pop("result_json", None)
                results.append(item)
            return results

    def get_trade_intent(self, intent_id):
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_intents WHERE intent_id=? ORDER BY intent_revision DESC LIMIT 1", (intent_id,)
            ).fetchone()
            if not row:
                raise BridgeError("INTENT_NOT_FOUND", "trade intent was not found")
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["orders"] = [_payload(order) for order in connection.execute(
                "SELECT payload_json FROM orders WHERE intent_id=? ORDER BY updated_at", (intent_id,)
            ).fetchall()]
            item["trades"] = [_payload(trade) for trade in connection.execute(
                "SELECT payload_json FROM trades WHERE intent_id=? ORDER BY traded_at", (intent_id,)
            ).fetchall()]
            return item

    def wait_trade_intent(self, intent_id, timeout_seconds=2.5):
        timeout = _number(timeout_seconds, "timeout_seconds", 0.1, 5.0)
        deadline = time.monotonic() + timeout
        while True:
            intent = self.get_trade_intent(intent_id)
            if intent["status"] != "QUEUED":
                return {
                    "status_observed": True,
                    "timed_out": False,
                    "intent": intent,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "status_observed": False,
                    "timed_out": True,
                    "intent": intent,
                    "next_action": "Query this intent later; do not resubmit the preview.",
                }
            time.sleep(min(0.1, remaining))

    def list_trade_intents(self, status=None, after_seq=None, limit=100):
        limit = int(limit)
        if limit < 1 or limit > 500:
            raise ValidationError("limit must be between 1 and 500")
        query = "SELECT rowid AS seq,* FROM trade_intents WHERE 1=1"
        values = []
        if status:
            statuses = [status] if isinstance(status, str) else status
            query += " AND status IN (%s)" % ",".join("?" for _ in statuses)
            values.extend(statuses)
        if after_seq is not None:
            query += " AND rowid>?"
            values.append(int(after_seq))
        query += " ORDER BY rowid LIMIT ?"
        values.append(limit)
        with self.db.connect() as connection:
            result = []
            for row in connection.execute(query, values).fetchall():
                item = dict(row)
                item.pop("payload_json")
                result.append(item)
            return result

    def get_risk_limits(self, account_alias):
        account = self.config.account(account_alias)
        limits = self.config.risks(account_alias)
        result = {
            "account_alias": account.alias,
            "account_type": account.account_type,
            "lot_size": account.lot_size,
            "odd_lot_sell_allowed": account.odd_lot_sell_allowed,
            "order_volume_rules": {
                "default": {
                    "min_buy": account.default_min_buy_volume,
                    "min_sell": account.default_min_sell_volume,
                },
                "prefix_overrides": [{
                    "prefixes": list(rule.prefixes), "min_buy": rule.min_buy,
                    "min_sell": rule.min_sell,
                } for rule in account.order_volume_overrides],
            },
            **{name: getattr(limits, name) for name in limits.__dataclass_fields__},
        }
        if account.account_type == "CREDIT":
            result["credit_action_mapping"] = {
                "buy": account.credit_buy_action,
                "sell": account.credit_sell_action,
            }
        return result

    def _validate_trade_request(self, request):
        allowed = {
            "account_alias", "account_type", "asset_type", "instrument", "action", "sizing",
            "price_policy", "signal_evidence", "execution_mode", "source", "credit",
        }
        required = {"account_alias", "instrument", "action", "sizing", "price_policy", "execution_mode", "source"}
        try:
            require_exact_keys(request, allowed, required)
            account = self.config.account(request["account_alias"])
            if request.get("account_type", account.account_type) != account.account_type:
                raise BridgeError("ACCOUNT_TYPE_MISMATCH", "request account_type does not match account alias")
            if request.get("asset_type", "STOCK") != "STOCK":
                raise ValidationError("asset_type must be STOCK")
            require_exact_keys(request["instrument"], {"canonical_symbol", "qmt_symbol"}, {"canonical_symbol"})
            symbol = normalize_symbol(request["instrument"]["canonical_symbol"])
            qmt_symbol = normalize_symbol(request["instrument"].get("qmt_symbol", symbol))
            if symbol != qmt_symbol:
                raise ValidationError("canonical_symbol and qmt_symbol must match in v1")
            action = request["action"]
            actions = STOCK_ACTIONS if account.account_type == "STOCK" else CREDIT_ACTIONS
            if action not in actions:
                raise BridgeError("ACCOUNT_TYPE_MISMATCH", "action is not valid for the account type")
            mode = request["execution_mode"]
            if mode not in RUN_MODES:
                raise ValidationError("invalid execution_mode")
            sizing = request["sizing"]
            require_exact_keys(sizing, {"type", "value", "max_volume"}, {"type", "value"})
            sizing_type = sizing["type"]
            supported = {"FIXED_VOLUME", "FIXED_NOTIONAL", "AVAILABLE_CASH_PERCENT", "TARGET_PORTFOLIO_PERCENT", "TARGET_POSITION"}
            if sizing_type not in supported:
                raise ValidationError("invalid sizing type")
            if account.account_type == "CREDIT" and sizing_type in {"TARGET_PORTFOLIO_PERCENT", "TARGET_POSITION"}:
                raise ValidationError("credit accounts do not support target-position sizing")
            if action == "TARGET_POSITION" and sizing_type != "TARGET_POSITION":
                raise ValidationError("TARGET_POSITION action requires TARGET_POSITION sizing")
            if sizing_type in {"TARGET_POSITION", "TARGET_PORTFOLIO_PERCENT"} and action != "TARGET_POSITION":
                raise ValidationError("target sizing requires TARGET_POSITION action")
            price = request["price_policy"]
            require_exact_keys(price, {"type", "limit_price", "offset_bps", "max_deviation_pct"}, {"type"})
            if price["type"] not in {"FIXED_LIMIT", "LIMIT_FROM_LATEST", "LIMIT_FROM_BOOK"}:
                raise ValidationError("invalid price policy")
            if price["type"] == "FIXED_LIMIT":
                if "limit_price" not in price or "offset_bps" in price:
                    raise ValidationError("FIXED_LIMIT requires limit_price and does not accept offset_bps")
                _number(price["limit_price"], "limit_price", 0.000001)
            else:
                if "limit_price" in price:
                    raise ValidationError("derived price policies do not accept limit_price")
                if "offset_bps" in price:
                    _number(price["offset_bps"], "offset_bps", -1000, 1000)
            if "max_deviation_pct" in price:
                _number(price["max_deviation_pct"], "max_deviation_pct", 0, 1)

            evidence = request.get("signal_evidence")
            if evidence is not None:
                require_exact_keys(evidence, {"occurred_at", "quote_at", "reference_price"})
                if not evidence:
                    raise ValidationError("signal_evidence must not be empty")
                for name in ("occurred_at", "quote_at"):
                    if name in evidence:
                        parse_time(evidence[name])
                if "reference_price" in evidence:
                    _number(evidence["reference_price"], "signal_evidence.reference_price", 0.000001)
            if "max_deviation_pct" in price and (
                    not evidence or "reference_price" not in evidence):
                raise ValidationError("max_deviation_pct requires signal_evidence.reference_price")
            credit = request.get("credit") or {}
            require_exact_keys(credit, {"debt_contract_ref", "capacity_snapshot_id"})
            if account.account_type == "STOCK" and credit:
                raise ValidationError("credit fields are not allowed for STOCK accounts")
            require_exact_keys(
                request["source"], {"type", "signal_id", "rule_set_id", "rule_version"},
                {"type", "signal_id"},
            )
            if not all(isinstance(request["source"][name], str) and request["source"][name].strip() for name in ("type", "signal_id")):
                raise ValidationError("source.type and source.signal_id must be non-empty strings")
            return account, symbol
        except BridgeError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(str(exc))

    def _resolve_limit_price(self, request, quote, action=None):
        policy = request["price_policy"]
        kind = policy["type"]
        if kind == "FIXED_LIMIT":
            price = _number(policy.get("limit_price"), "limit_price", 0.000001)
        elif kind == "LIMIT_FROM_BOOK":
            field = "ask_price" if action in BUY_LIKE else "bid_price" if action in SELL_LIKE else "last_price"
            base = _number((quote or {}).get(field), "QMT quote.%s" % field, 0.000001)
            price = base * (1 + _number(policy.get("offset_bps", 0), "offset_bps", -1000, 1000) / 10000)
        else:
            base = _number((quote or {}).get("last_price"), "QMT quote.last_price", 0.000001)
            price = base * (1 + _number(policy.get("offset_bps", 0), "offset_bps", -1000, 1000) / 10000)
        return round(price + 1e-10, 6)

    @staticmethod
    def _favorable_tick_price(price, tick, action):
        units = price / tick
        if action in BUY_LIKE:
            units = math.floor(units + 1e-9)
        elif action in SELL_LIKE:
            units = math.ceil(units - 1e-9)
        return round(units * tick, 6)

    def _price_guard(self, quote, action, requested_price):
        result = {
            "allowed": True, "reasons": [], "requested_limit_price": requested_price,
            "resolved_limit_price": requested_price, "adjusted": False,
            "adjustments": [], "trading_phase": None, "daily_limit": None,
            "dynamic_cage": None,
        }
        if not quote:
            result.update({"allowed": False, "reasons": ["QUOTE_SNAPSHOT_STALE"]})
            return result
        result["trading_phase"] = quote.get("trading_phase")
        try:
            tick = _number(quote.get("price_tick"), "QMT quote.price_tick", 0.000001)
        except BridgeError:
            result.update({"allowed": False, "reasons": ["PRICE_TICK_UNAVAILABLE"]})
            return result
        price = self._favorable_tick_price(requested_price, tick, action)
        if abs(price - requested_price) > 1e-8:
            result["adjusted"] = True
            result["adjustments"].append("PRICE_TICK_FAVORABLE_ROUNDING")
        lower = quote.get("lower_limit")
        upper = quote.get("upper_limit")
        try:
            lower = float(lower or 0)
            upper = float(upper or 0)
        except (TypeError, ValueError):
            lower = upper = -1
        if lower < 0 or upper < 0 or bool(lower) != bool(upper):
            result["reasons"].append("DAILY_PRICE_LIMIT_UNAVAILABLE")
        elif lower and upper:
            result["daily_limit"] = {"lower": lower, "upper": upper}
            if price < lower - 1e-8 or price > upper + 1e-8:
                result["reasons"].append("PRICE_OUTSIDE_DAILY_LIMIT")
        if quote.get("trading_phase") == "CONTINUOUS_AUCTION":
            cage = quote.get("dynamic_cage")
            result["dynamic_cage"] = cage
            if not isinstance(cage, dict) or not cage.get("applied"):
                result["reasons"].append("PRICE_CAGE_UNAVAILABLE")
            elif action in BUY_LIKE:
                boundary = cage.get("buy_upper")
                if not isinstance(boundary, (int, float)) or boundary <= 0:
                    result["reasons"].append("PRICE_CAGE_UNAVAILABLE")
                elif price > float(boundary) + 1e-8:
                    price = float(boundary)
                    result["adjusted"] = True
                    result["adjustments"].append("BUY_PRICE_CAPPED_BY_DYNAMIC_CAGE")
            elif action in SELL_LIKE:
                boundary = cage.get("sell_lower")
                if not isinstance(boundary, (int, float)) or boundary <= 0:
                    result["reasons"].append("PRICE_CAGE_UNAVAILABLE")
                elif price < float(boundary) - 1e-8:
                    price = float(boundary)
                    result["adjusted"] = True
                    result["adjustments"].append("SELL_PRICE_RAISED_BY_DYNAMIC_CAGE")
        result["resolved_limit_price"] = round(price, 6)
        result["allowed"] = not result["reasons"]
        return result

    def _position_for(self, positions, symbol):
        return next((item for item in positions if item.get("symbol") == symbol), {})

    def _snapshot_context(self, connection, account, symbol):
        account_row = self._latest_account_row(connection, account.alias)
        marker, positions = self._latest_positions(connection, account.alias)
        credit_row = self._latest_credit_row(connection, account.alias) if account.account_type == "CREDIT" else None
        quote_row = self._latest_quote_row(connection, account.alias, symbol)
        active_orders = connection.execute(
            """SELECT qmt_order_id,status,updated_at,requested_volume,filled_volume,limit_price
               FROM orders WHERE account_alias=? AND symbol=?""",
            (account.alias, symbol),
        ).fetchall()
        active = [dict(row) for row in active_orders if row["status"] in ACTIVE_ORDER_STATES]
        snapshot_versions = {
            "account_snapshot_id": account_row["snapshot_id"] if account_row else None,
            "position_snapshot_id": marker["snapshot_id"] if marker else None,
            "credit_snapshot_id": credit_row["snapshot_id"] if credit_row else None,
            "quote_snapshot_id": quote_row["snapshot_id"] if quote_row else None,
            "quote_captured_at": quote_row["captured_at"] if quote_row else None,
            "active_orders": [(row["qmt_order_id"], row["status"], row["updated_at"]) for row in active],
        }
        return {
            "account_row": account_row, "account": _payload(account_row),
            "position_marker": marker, "positions": positions,
            "credit_row": credit_row, "credit": _payload(credit_row),
            "quote_row": quote_row, "quote": _payload(quote_row),
            "active_orders": active,
            "snapshot_versions": snapshot_versions,
        }

    def _credit_eligibility(self, connection, alias, symbol):
        marker = connection.execute(
            "SELECT snapshot_id,captured_at FROM credit_eligibility_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
            (alias,),
        ).fetchone()
        if not marker:
            return None, None
        row = connection.execute(
            "SELECT payload_json FROM credit_eligibility_snapshots WHERE account_alias=? AND snapshot_id=? AND symbol=?",
            (alias, marker["snapshot_id"], symbol),
        ).fetchone()
        return marker, _payload(row)

    def _matching_capacity(self, connection, alias, symbol, action, price):
        rows = connection.execute(
            "SELECT * FROM credit_capacity_queries WHERE account_alias=? AND status='COMPLETED' ORDER BY completed_at DESC LIMIT 20",
            (alias,),
        ).fetchall()
        for row in rows:
            request = json.loads(row["request_json"])
            for item in request.get("requests", []):
                if (item.get("symbol") == symbol and item.get("credit_action") == action and
                        item.get("price_type") == "LIMIT" and abs(float(item.get("price", -1)) - price) < 1e-6):
                    result = json.loads(row["result_json"])
                    age = self._age_seconds(row["completed_at"])
                    return row, result, age
        return None, None, None

    def _volume_rule_check(self, account, symbol, action, volume, available_volume):
        if action in BUY_LIKE:
            minimum = account.minimum_order_volume(symbol, "BUY")
            return (
                ["MINIMUM_BUY_VOLUME_NOT_MET"] if 0 < volume < minimum else [],
                {"side": "BUY", "minimum_volume": minimum, "odd_lot_liquidation": False},
            )
        if action in SELL_LIKE:
            minimum = account.minimum_order_volume(symbol, "SELL")
            odd_lot = (
                account.odd_lot_sell_allowed
                and action in {"SELL", "COLLATERAL_SELL", "SELL_TO_REPAY"}
                and 0 < available_volume < minimum
                and volume == available_volume
            )
            reasons = []
            if 0 < volume < minimum and not odd_lot:
                reasons.append("MINIMUM_SELL_VOLUME_NOT_MET")
            if action in {"SELL", "COLLATERAL_SELL", "SELL_TO_REPAY"} and volume > available_volume:
                reasons.append("INSUFFICIENT_AVAILABLE_VOLUME")
            return reasons, {
                "side": "SELL", "minimum_volume": minimum,
                "odd_lot_liquidation": odd_lot,
            }
        return [], {"side": None, "minimum_volume": None, "odd_lot_liquidation": False}

    def _credit_action_check(
        self, connection, account, symbol, action, price, volume, available_cash,
        available_volume, request, limits, credit_data, eligibility,
    ):
        reasons = []
        detail = {
            "action": action, "capacity_seq": None, "capacity_snapshot_id": None,
            "capacity_age_seconds": None, "capacity_max_volume": None,
            "debt_contract_ref": None, "debt_snapshot_age_seconds": None,
            "debt_state": None,
        }
        flag_names = {
            "COLLATERAL_BUY": "collateral_buy_eligible",
            "COLLATERAL_SELL": "collateral_sell_eligible",
            "MARGIN_BUY": "margin_buy_eligible",
            "SHORT_SELL": "short_sell_eligible",
            "BUY_TO_REPAY": "buy_to_repay_eligible",
            "SELL_TO_REPAY": "sell_to_repay_eligible",
        }
        if not eligibility or not eligibility.get(flag_names[action], False):
            reasons.append("CREDIT_INSTRUMENT_NOT_ELIGIBLE")

        ratio = credit_data.get("maintenance_ratio")
        if action in {"MARGIN_BUY", "SHORT_SELL"}:
            if ratio is None or float(ratio) < limits.min_maintenance_ratio:
                reasons.append("CREDIT_RISK_LIMIT")
        if action in {"COLLATERAL_BUY", "BUY_TO_REPAY"} and volume * price > available_cash:
            reasons.append("INSUFFICIENT_AVAILABLE_CASH")
        if action == "SHORT_SELL" and volume > int((eligibility or {}).get("short_available_volume", 0) or 0):
            reasons.append("CREDIT_CAPACITY_UNAVAILABLE")

        volume_reasons, volume_rule = self._volume_rule_check(
            account, symbol, action, volume, available_volume
        )
        reasons.extend(volume_reasons)
        detail["volume_rule"] = volume_rule

        capacity_row = capacity_result = capacity_age = None
        if action in {"MARGIN_BUY", "SHORT_SELL", "BUY_TO_REPAY", "SELL_TO_REPAY"}:
            capacity_row, capacity_result, capacity_age = self._matching_capacity(
                connection, account.alias, symbol, action, price
            )
            detail["capacity_seq"] = capacity_row["seq"] if capacity_row else None
            detail["capacity_age_seconds"] = capacity_age
            if not capacity_row or capacity_age > limits.max_credit_snapshot_age_seconds:
                reasons.append("CREDIT_CAPACITY_UNAVAILABLE")
            else:
                result_items = capacity_result.get("results", [])
                matched = next((item for item in result_items if
                                item.get("symbol") == symbol and
                                item.get("credit_action") == action), None)
                if matched:
                    detail["capacity_max_volume"] = int(matched.get("max_volume", 0) or 0)
                if not matched or volume > int(matched.get("max_volume", 0) or 0):
                    reasons.append("CREDIT_CAPACITY_UNAVAILABLE")
                expected_id = capacity_result.get("snapshot_id") or ("ccap_" + capacity_row["seq"])
                detail["capacity_snapshot_id"] = expected_id
                supplied_id = (request.get("credit") or {}).get("capacity_snapshot_id")
                if supplied_id != expected_id:
                    reasons.append("CREDIT_CAPACITY_UNAVAILABLE")

        if action in {"BUY_TO_REPAY", "SELL_TO_REPAY"}:
            debt_ref = (request.get("credit") or {}).get("debt_contract_ref")
            detail["debt_contract_ref"] = debt_ref
            debt = None
            latest_debt = None
            if debt_ref:
                latest_debt = connection.execute(
                    "SELECT snapshot_id,captured_at FROM credit_debt_snapshots WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
                    (account.alias,),
                ).fetchone()
                if latest_debt:
                    debt_row = connection.execute(
                        """SELECT payload_json FROM credit_debt_snapshots
                           WHERE account_alias=? AND snapshot_id=? AND debt_contract_ref=? AND symbol=? AND status='OPEN'""",
                        (account.alias, latest_debt["snapshot_id"], debt_ref, symbol),
                    ).fetchone()
                    debt = _payload(debt_row)
                    detail["debt_snapshot_id"] = latest_debt["snapshot_id"]
                    detail["debt_snapshot_age_seconds"] = self._age_seconds(latest_debt["captured_at"])
                    detail["debt_state"] = _material_payload(debt)
            if (not debt or detail["debt_snapshot_age_seconds"] is None or
                    detail["debt_snapshot_age_seconds"] > limits.max_credit_snapshot_age_seconds):
                reasons.append("CREDIT_DEBT_NOT_FOUND")
            else:
                debt_type = str(debt.get("debt_type", "")).upper()
                if action == "BUY_TO_REPAY":
                    if debt_type not in {"SHORT", "49"} or volume > int(debt.get("outstanding_volume", 0) or 0):
                        reasons.append("CREDIT_DEBT_NOT_FOUND")
                elif debt_type not in {"MARGIN", "48"} or float(debt.get("outstanding_amount", 0) or 0) <= 0:
                    reasons.append("CREDIT_DEBT_NOT_FOUND")

        detail["allowed"] = not reasons
        detail["reasons"] = sorted(set(reasons))
        return detail

    def _build_preview(self, request):
        account, symbol = self._validate_trade_request(request)
        limits = self.config.risks(account.alias)
        reasons = []
        warnings = []
        with self.db.connect() as connection:
            context = self._snapshot_context(connection, account, symbol)
            account_data = context["account"] or {}
            quote = context["quote"] or None
            position = self._position_for(context["positions"], symbol)
            if not context["account_row"] or self._age_seconds(context["account_row"]["captured_at"]) > limits.max_snapshot_age_seconds:
                reasons.append("ACCOUNT_SNAPSHOT_STALE")
            if not context["position_marker"] or self._age_seconds(context["position_marker"]["captured_at"]) > limits.max_snapshot_age_seconds:
                reasons.append("POSITION_SNAPSHOT_STALE")
            if account.instrument_allowlist and symbol not in account.instrument_allowlist:
                reasons.append("INSTRUMENT_NOT_ALLOWED")
            quote_age = None
            if context["quote_row"]:
                quote_age = max(
                    self._age_seconds(context["quote_row"]["captured_at"]),
                    self._age_seconds((quote or {}).get("tick_at")),
                )
            if quote_age is None or quote_age > limits.max_quote_age_seconds:
                reasons.append("QUOTE_SNAPSHOT_STALE")

            sizing = request["sizing"]
            kind = sizing["type"]
            value = _number(sizing["value"], "sizing.value", 0.000001)
            current_volume = int(position.get("total_volume", 0) or 0)
            available_cash = float(account_data.get("available_cash", 0) or 0)
            resolved_action = request["action"]
            target = None
            if kind == "TARGET_POSITION":
                target = int(value)
                if target != value:
                    raise ValidationError("target position must be an integer")
                resolved_action = "BUY" if target > current_volume else "SELL"
            elif kind == "TARGET_PORTFOLIO_PERCENT":
                if value > 1:
                    raise ValidationError("portfolio percentage must not exceed 1")
                preliminary_price = self._resolve_limit_price(request, quote, None)
                target = int(float(account_data.get("total_asset", 0) or 0) * value / preliminary_price)
                resolved_action = "BUY" if target > current_volume else "SELL"

            requested_price = self._resolve_limit_price(request, quote, resolved_action)
            price_guard = self._price_guard(quote, resolved_action, requested_price)
            reasons.extend(price_guard["reasons"])
            price = price_guard["resolved_limit_price"]
            if price_guard["adjusted"]:
                warnings.extend(price_guard["adjustments"])

            if kind == "FIXED_VOLUME":
                volume = int(value)
                if volume != value:
                    raise ValidationError("FIXED_VOLUME must be a positive integer")
            elif kind == "FIXED_NOTIONAL":
                volume = int(value / price)
            elif kind == "AVAILABLE_CASH_PERCENT":
                if account.account_type != "STOCK" and request["action"] != "COLLATERAL_BUY":
                    raise ValidationError("AVAILABLE_CASH_PERCENT is not supported for this action")
                if value > 1:
                    raise ValidationError("cash percentage must not exceed 1")
                volume = int(available_cash * value / price)
            elif kind == "TARGET_PORTFOLIO_PERCENT":
                target = int(float(account_data.get("total_asset", 0) or 0) * value / price)
                volume = abs(target - current_volume)
            else:
                volume = abs(target - current_volume)

            if request["action"] == "TARGET_POSITION":
                resolved_action = "BUY" if target > current_volume else "SELL"

            max_requested = sizing.get("max_volume")
            if max_requested is not None:
                volume = min(volume, int(_number(max_requested, "sizing.max_volume", 1)))
            if resolved_action in BUY_LIKE:
                volume = (volume // account.lot_size) * account.lot_size
            notional = round(volume * price, 3)
            if volume <= 0:
                if resolved_action in BUY_LIKE:
                    reasons.append("ZERO_BUY_VOLUME_NOT_ALLOWED")
                elif resolved_action in SELL_LIKE:
                    reasons.append("ZERO_SELL_VOLUME_NOT_ALLOWED")
                else:
                    reasons.append("ZERO_RESOLVED_VOLUME")
            if volume > limits.max_order_volume:
                reasons.append("MAX_ORDER_VOLUME_EXCEEDED")
            if notional > limits.max_order_notional:
                reasons.append("MAX_ORDER_NOTIONAL_EXCEEDED")
            volume_rule = None
            if account.account_type == "STOCK":
                available_volume = int(position.get("available_volume", 0) or 0)
                volume_reasons, volume_rule = self._volume_rule_check(
                    account, symbol, resolved_action, volume, available_volume
                )
                reasons.extend(volume_reasons)
                if resolved_action in BUY_LIKE and notional > available_cash:
                    reasons.append("INSUFFICIENT_AVAILABLE_CASH")
            if context["active_orders"]:
                reasons.append("ACTIVE_ORDER_CONFLICT")
            if request.get("signal_evidence"):
                evidence = request["signal_evidence"]
                quote_at = evidence.get("quote_at")
                if quote_at and self._age_seconds(quote_at) > limits.max_quote_age_seconds:
                    reasons.append("MARKET_DATA_STALE")
                reference = evidence.get("reference_price")
                maximum = request["price_policy"].get("max_deviation_pct")
                if reference is not None and maximum is not None:
                    deviation = abs(price / float(reference) - 1)
                    if deviation > _number(maximum, "max_deviation_pct", 0, 1):
                        reasons.append("PRICE_DEVIATION_EXCEEDED")

            credit_summary = None
            credit_material = None
            if account.account_type == "CREDIT":
                credit_data = context["credit"] or {}
                credit_age = self._age_seconds(context["credit_row"]["captured_at"]) if context["credit_row"] else None
                if credit_age is None or credit_age > limits.max_credit_snapshot_age_seconds:
                    reasons.append("CREDIT_SNAPSHOT_STALE")
                ratio = credit_data.get("maintenance_ratio")
                if ratio is None:
                    reasons.append("CREDIT_CAPACITY_UNAVAILABLE")
                eligibility_marker, eligibility = self._credit_eligibility(connection, account.alias, symbol)
                context["snapshot_versions"]["credit_eligibility_snapshot_id"] = (
                    eligibility_marker["snapshot_id"] if eligibility_marker else None
                )
                available_volume = int(position.get("available_volume", 0) or 0)
                configured_action = account.configured_credit_action(request["action"])
                selected = self._credit_action_check(
                    connection, account, symbol, configured_action, price, volume,
                    available_cash, available_volume, request, limits, credit_data,
                    eligibility,
                )
                if not selected["allowed"]:
                    reasons.extend(selected["reasons"])
                    if request["action"] in {"BUY", "SELL"}:
                        reasons.append("CONFIGURED_CREDIT_ACTION_REJECTED")
                resolved_action = selected["action"]
                volume_rule = selected["volume_rule"]
                if selected["capacity_seq"]:
                    context["snapshot_versions"]["credit_capacity_seq"] = selected["capacity_seq"]
                if selected.get("debt_snapshot_id"):
                    context["snapshot_versions"]["credit_debt_snapshot_id"] = selected["debt_snapshot_id"]
                credit_material = {
                    "account": _material_payload(credit_data),
                    "eligibility": _material_payload(eligibility),
                    "action_decision": {
                        "action": selected["action"],
                        "allowed": selected["allowed"],
                        "reasons": selected["reasons"],
                        "volume_rule": selected["volume_rule"],
                        "capacity_max_volume": selected["capacity_max_volume"],
                        "debt_contract_ref": selected["debt_contract_ref"],
                        "debt_state": selected["debt_state"],
                    },
                }
                credit_summary = {
                    "snapshot_id": context["credit_row"]["snapshot_id"] if context["credit_row"] else None,
                    "age_seconds": credit_age,
                    "maintenance_ratio": ratio,
                    "total_debt": credit_data.get("total_debt"),
                    "eligibility_snapshot_id": eligibility_marker["snapshot_id"] if eligibility_marker else None,
                    "requested_action": request["action"],
                    "configured_action": configured_action,
                    "resolved_action": resolved_action,
                    "action_decision": selected,
                    "capacity_seq": selected["capacity_seq"],
                    "capacity_snapshot_id": selected["capacity_snapshot_id"],
                    "capacity_age_seconds": selected["capacity_age_seconds"],
                    "debt_contract_ref": selected["debt_contract_ref"],
                    "debt_snapshot_age_seconds": selected["debt_snapshot_age_seconds"],
                }

            trading_day = utc_now().strftime("%Y%m%d")
            daily = connection.execute(
                "SELECT COALESCE(SUM(volume*price),0) AS total FROM trades WHERE account_alias=? AND trading_day=?",
                (account.alias, trading_day),
            ).fetchone()["total"]
            pending = connection.execute(
                "SELECT COALESCE(SUM(requested_volume*limit_price),0) AS total FROM trade_intents WHERE account_alias=? AND status NOT IN (%s)" %
                ",".join("?" for _ in TERMINAL_INTENT_STATES),
                [account.alias] + sorted(TERMINAL_INTENT_STATES),
            ).fetchone()["total"]
            if float(daily) + float(pending) + notional > limits.max_daily_notional:
                reasons.append("MAX_DAILY_NOTIONAL_EXCEEDED")

            resolved_order = {
                "volume": volume, "price_type": "LIMIT", "limit_price": price,
                "requested_limit_price": requested_price,
                "notional": notional, "volume_rule": volume_rule,
            }
            limited_auto_summary = None
            if request["execution_mode"] == "LIMITED_AUTO":
                permit_row = self._active_auto_permit_row(connection, account.alias)
                if not permit_row:
                    reasons.append("AUTO_PERMIT_REQUIRED")
                else:
                    auto_current = {
                        "account_alias": account.alias,
                        "instrument": {"qmt_symbol": symbol},
                        "resolved_action": resolved_action,
                        "resolved_order": resolved_order,
                        "account_summary": {
                            "total_asset": account_data.get("total_asset"),
                            "position_total_volume": current_volume,
                        },
                    }
                    auto_reasons, _, limited_auto_summary = self._limited_auto_policy_reasons(
                        connection, permit_row, request, auto_current,
                    )
                    reasons.extend(auto_reasons)
            decision_data = {
                "version": 1,
                "account_alias": account.alias,
                "symbol": symbol,
                "resolved_action": resolved_action,
                "resolved_order": resolved_order,
                "account": {"available_cash": available_cash},
                "position": {
                    "total_volume": current_volume,
                    "available_volume": int(position.get("available_volume", 0) or 0),
                    "frozen_volume": int(position.get("frozen_volume", 0) or 0),
                },
                "price_guard": price_guard,
                "active_orders": sorted([{
                    "qmt_order_id": row["qmt_order_id"],
                    "status": row["status"],
                    "requested_volume": row["requested_volume"],
                    "filled_volume": row["filled_volume"],
                    "limit_price": row["limit_price"],
                } for row in context["active_orders"]], key=lambda item: item["qmt_order_id"]),
                "notional_usage": {
                    "daily_traded": float(daily),
                    "pending_intents": float(pending),
                },
                "credit": credit_material,
                "limited_auto": limited_auto_summary,
            }
            decision_fingerprint = hashlib.sha256(canonical_json(decision_data)).hexdigest()

            return {
                "account_alias": account.alias,
                "account_type": account.account_type,
                "asset_type": "STOCK",
                "instrument": {"canonical_symbol": symbol, "qmt_symbol": symbol},
                "action": request["action"],
                "resolved_action": resolved_action,
                "execution_mode": request["execution_mode"],
                "resolved_order": resolved_order,
                "price_guard": dict(price_guard, quote_snapshot_id=(context["quote_row"]["snapshot_id"] if context["quote_row"] else None), quote_age_seconds=quote_age),
                "account_summary": {
                    "snapshot_id": context["account_row"]["snapshot_id"] if context["account_row"] else None,
                    "available_cash": account_data.get("available_cash"),
                    "total_asset": account_data.get("total_asset"),
                    "position_total_volume": current_volume,
                    "position_available_volume": position.get("available_volume"),
                },
                "credit_summary": credit_summary,
                "limited_auto": limited_auto_summary,
                "active_order_conflicts": [row["qmt_order_id"] for row in context["active_orders"]],
                "snapshot_fingerprint": decision_fingerprint,
                "snapshot_versions": context["snapshot_versions"],
                "risk": {"allowed": not reasons, "reasons": sorted(set(reasons))},
                "warnings": warnings,
            }

    def preview_trade(self, trade_request):
        result = self._build_preview(trade_request)
        if trade_request.get("execution_mode") == "LIMITED_AUTO":
            self._pause_active_auto_permit(
                result["account_alias"], result["risk"]["reasons"], actor="worker",
            )
        limits = self.config.risks(result["account_alias"])
        created = utc_now()
        expires = created + dt.timedelta(seconds=limits.preview_ttl_seconds)
        preview_id = new_id("preview")
        result.update({
            "preview_id": preview_id,
            "created_at": created.isoformat(timespec="milliseconds"),
            "expires_at": expires.isoformat(timespec="milliseconds"),
        })
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO market_signals(signal_id,source_type,received_at,payload_json) VALUES(?,?,?,?)",
                (trade_request["source"]["signal_id"], trade_request["source"]["type"], result["created_at"], json_text({
                    "source": trade_request["source"], "signal_evidence": trade_request.get("signal_evidence"),
                })),
            )
            connection.execute(
                "INSERT INTO trade_previews(preview_id,account_alias,request_json,result_json,snapshot_fingerprint,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (preview_id, result["account_alias"], json_text(trade_request), json_text(result), result["snapshot_fingerprint"], result["created_at"], result["expires_at"]),
            )
            connection.execute(
                "INSERT INTO risk_decisions(risk_decision_id,preview_id,account_alias,allowed,reasons_json,snapshot_fingerprint,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("risk"), preview_id, result["account_alias"], int(result["risk"]["allowed"]), json_text(result["risk"]["reasons"]), result["snapshot_fingerprint"], result["created_at"]),
            )
        return result

    def prepare_trade(self, trade_request, timeout_seconds=3.0):
        account, symbol = self._validate_trade_request(trade_request)
        timeout = _number(timeout_seconds, "timeout_seconds", 0.5, 5.0)
        scopes = ["ACCOUNT", "POSITION", "ORDER", "DEAL", "QUOTE"]
        sync = self.request_sync(account.alias, scopes, [symbol])
        deadline = time.monotonic() + timeout
        while True:
            with self.db.connect() as connection:
                row = connection.execute(
                    "SELECT status FROM qmt_commands WHERE message_id=?",
                    (sync["message_id"],),
                ).fetchone()
            status = row["status"] if row else None
            queue_depths = self.queue.depths(account.adapter_instance)
            if status == "SYNC_COMPLETED" and queue_depths.get("events", 0) == 0:
                break
            if status in {"QMT_REJECTED", "RISK_REJECTED", "FAILED"}:
                raise BridgeError(
                    "SYNC_FAILED",
                    "QMT rejected the snapshot refresh required for trade preparation",
                    {"message_id": sync["message_id"], "status": status},
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError(
                    "SYNC_TIMEOUT",
                    "fresh QMT snapshots were not received before the preparation timeout",
                    {
                        "message_id": sync["message_id"],
                        "status": status,
                        "retry_safe": True,
                    },
                )
            time.sleep(min(0.1, remaining))
        preview = self.preview_trade(trade_request)
        return {
            "sync": {
                "message_id": sync["message_id"],
                "status": "SYNC_COMPLETED",
                "scopes": scopes,
                "symbols": [symbol],
                "event_queue_drained": True,
            },
            "preview": preview,
        }

    def _write_local_authorization(
        self, account, allowed_modes, live_until, actor, correlation_id,
        authorization_type, auto_permit=None,
    ):
        payload = {
            "account_alias": account.alias,
            "allowed_modes": list(allowed_modes),
            "live_until": live_until.isoformat(timespec="milliseconds"),
            "actor": actor,
            "authorization_type": authorization_type,
        }
        if auto_permit is not None:
            payload["auto_permit"] = auto_permit
        authorization = make_envelope(
            self.keyring, "LOCAL_AUTHORIZATION", payload,
            max(1, int((live_until - utc_now()).total_seconds())),
            "qmt-bridge-mcp", correlation_id,
        )
        atomic_write_json(
            os.path.join(
                self.config.data_dir, "qmt_runtime", account.adapter_instance,
                "local_authorization.json",
            ),
            authorization,
        )

    def authorize_manual_trade(
        self, preview_id, reason, confirm, live_minutes=10,
        approval_ttl_seconds=30,
    ):
        """Authorize one MANUAL_LIVE preview through the local MCP boundary."""
        if confirm != "AUTHORIZE-MANUAL-TRADE":
            raise BridgeError(
                "LOCAL_CONFIRMATION_REQUIRED",
                "confirm must be AUTHORIZE-MANUAL-TRADE",
            )
        if isinstance(live_minutes, bool) or not isinstance(live_minutes, int) or not 1 <= live_minutes <= 60:
            raise ValidationError("live_minutes must be an integer between 1 and 60")
        if (isinstance(approval_ttl_seconds, bool) or
                not isinstance(approval_ttl_seconds, int) or
                not 1 <= approval_ttl_seconds <= 300):
            raise ValidationError("approval_ttl_seconds must be an integer between 1 and 300")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason is required and must be at most 200 characters")

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_previews WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if not row:
                raise BridgeError("PREVIEW_NOT_FOUND", "preview was not found")
            if row["consumed_intent_id"]:
                raise BridgeError("PREVIEW_ALREADY_CONSUMED", "preview has already been submitted")
            if parse_time(row["expires_at"]) <= utc_now():
                raise BridgeError("PREVIEW_EXPIRED", "preview has expired")
            request = json.loads(row["request_json"])
            stored = json.loads(row["result_json"])
            if stored.get("execution_mode") != "MANUAL_LIVE":
                raise BridgeError(
                    "LIVE_NOT_ENABLED",
                    "only a MANUAL_LIVE preview can be authorized",
                )

        current = self._build_preview(request)
        if current["snapshot_fingerprint"] != row["snapshot_fingerprint"]:
            raise BridgeError(
                "RISK_REJECTED",
                "risk-relevant state or resolved order changed; create a new preview",
                {"reason": "SNAPSHOT_CHANGED"},
            )
        if not current["risk"]["allowed"]:
            raise BridgeError(
                "RISK_REJECTED", "trade failed hard risk checks",
                {"reasons": current["risk"]["reasons"]},
            )

        now = utc_now()
        live_until = now + dt.timedelta(minutes=live_minutes)
        approval_expires = min(
            now + dt.timedelta(seconds=approval_ttl_seconds),
            parse_time(row["expires_at"]),
        )
        approval_id = new_id("approval")
        account = self.config.account(row["account_alias"])
        created = now.isoformat(timespec="milliseconds")
        with self.db.transaction(immediate=True) as connection:
            fresh = connection.execute(
                "SELECT * FROM trade_previews WHERE preview_id=?", (preview_id,)
            ).fetchone()
            if fresh["consumed_intent_id"]:
                raise BridgeError("PREVIEW_ALREADY_CONSUMED", "preview has already been submitted")
            if parse_time(fresh["expires_at"]) <= utc_now():
                raise BridgeError("PREVIEW_EXPIRED", "preview has expired")
            if self._halted(connection):
                raise BridgeError("LIVE_NOT_ENABLED", "trading is halted")
            if self._mode(connection) != "MANUAL_LIVE":
                raise BridgeError(
                    "LIVE_NOT_ENABLED",
                    "Worker must already be in MANUAL_LIVE mode",
                )
            active_session = self._active_manual_session(
                connection, account.alias
            )
            if active_session:
                live_until = max(
                    live_until, parse_time(active_session["expires_at"])
                )
            connection.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (
                    "live_until:%s" % account.alias,
                    live_until.isoformat(timespec="milliseconds"), created,
                ),
            )
            connection.execute(
                """INSERT INTO approvals(
                       approval_id,preview_id,decision,actor,reason,created_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    approval_id, preview_id, "APPROVED", "mcp",
                    reason.strip(), created,
                    approval_expires.isoformat(timespec="milliseconds"),
                ),
            )
            connection.execute(
                """INSERT INTO audit_log(
                       occurred_at,actor,action,account_alias,object_id,details_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    created, "mcp", "AUTHORIZE_MANUAL_TRADE", account.alias,
                    preview_id, json_text({
                        "approval_id": approval_id,
                        "approval_expires_at": approval_expires.isoformat(timespec="milliseconds"),
                        "live_until": live_until.isoformat(timespec="milliseconds"),
                        "reason": reason.strip(),
                    }),
                ),
            )

        self._write_local_authorization(
            account, ["MANUAL_LIVE"], live_until, "mcp", approval_id,
            "SINGLE_TRADE",
        )
        return {
            "account_alias": account.alias,
            "preview_id": preview_id,
            "approval_id": approval_id,
            "approval_context": {"local_approval_id": approval_id},
            "approval_expires_at": approval_expires.isoformat(timespec="milliseconds"),
            "live_until": live_until.isoformat(timespec="milliseconds"),
            "authorized_by": "mcp",
        }

    def _active_manual_session(self, connection, account_alias):
        raw = self._state(connection, "manual_session:%s" % account_alias)
        if not raw:
            return None
        try:
            session = json.loads(raw)
            expires_at = parse_time(session["expires_at"])
        except (TypeError, ValueError, KeyError):
            return None
        if (
            session.get("account_alias") != account_alias or
            not isinstance(session.get("session_id"), str) or
            session.get("unlimited_orders") is not True or
            expires_at <= utc_now()
        ):
            return None
        return session

    def get_manual_authorization_status(self, account_alias):
        self.config.account(account_alias)
        with self.db.connect() as connection:
            mode = self._mode(connection)
            halted = self._halted(connection)
            live_until = self._state(
                connection, "live_until:%s" % account_alias
            )
            session = self._active_manual_session(connection, account_alias)
            single_approvals = connection.execute(
                """SELECT COUNT(*) AS n FROM approvals a
                   JOIN trade_previews p ON p.preview_id=a.preview_id
                   WHERE p.account_alias=? AND a.decision='APPROVED'
                     AND a.used_at IS NULL AND a.expires_at>?""",
                (account_alias, iso_now()),
            ).fetchone()["n"]
        live_active = False
        if live_until:
            try:
                live_active = parse_time(live_until) > utc_now()
            except (TypeError, ValueError):
                live_active = False
        active = bool(
            session and live_active and not halted and mode == "MANUAL_LIVE"
        )
        remaining_seconds = 0
        if active:
            remaining_seconds = max(
                0, int((parse_time(session["expires_at"]) - utc_now()).total_seconds())
            )
        return {
            "account_alias": account_alias,
            "mode": mode,
            "halted": halted,
            "timed_session_active": active,
            "timed_session": session if active else None,
            "remaining_seconds": remaining_seconds,
            "live_until": live_until,
            "active_single_trade_approvals": single_approvals,
        }

    def authorize_manual_session(
        self, account_alias, reason, confirm, minutes=10,
    ):
        """Authorize unlimited MANUAL_LIVE submissions for one account and time window."""
        if confirm != "AUTHORIZE-TIMED-MANUAL-TRADING":
            raise BridgeError(
                "LOCAL_CONFIRMATION_REQUIRED",
                "confirm must be AUTHORIZE-TIMED-MANUAL-TRADING",
            )
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 60:
            raise ValidationError("minutes must be an integer between 1 and 60")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason is required and must be at most 200 characters")
        account = self.config.account(account_alias)
        now = utc_now()
        created = now.isoformat(timespec="milliseconds")
        expires = now + dt.timedelta(minutes=minutes)
        expires_text = expires.isoformat(timespec="milliseconds")
        session_id = new_id("manual_session")
        session = {
            "session_id": session_id,
            "account_alias": account.alias,
            "authorized_by": "mcp",
            "reason": reason.strip(),
            "created_at": created,
            "expires_at": expires_text,
            "unlimited_orders": True,
            "still_requires_preview_and_risk_check": True,
        }
        with self.db.transaction(immediate=True) as connection:
            if self._halted(connection):
                raise BridgeError("LIVE_NOT_ENABLED", "trading is halted")
            if self._mode(connection) != "MANUAL_LIVE":
                raise BridgeError(
                    "LIVE_NOT_ENABLED",
                    "Worker must already be in MANUAL_LIVE mode",
                )
            connection.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                ("manual_session:%s" % account.alias, json_text(session), created),
            )
            connection.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                ("live_until:%s" % account.alias, expires_text, created),
            )
            connection.execute(
                """INSERT INTO audit_log(
                       occurred_at,actor,action,account_alias,object_id,details_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    created, "mcp", "AUTHORIZE_MANUAL_SESSION", account.alias,
                    session_id, json_text(session),
                ),
            )
        self._write_local_authorization(
            account, ["MANUAL_LIVE"], expires, "mcp", session_id,
            "TIMED_SESSION",
        )
        return {
            "account_alias": account.alias,
            "session_id": session_id,
            "expires_at": expires_text,
            "minutes": minutes,
            "unlimited_orders": True,
            "still_requires_preview_and_risk_check": True,
            "authorized_by": "mcp",
        }

    def revoke_manual_session(self, account_alias, reason, confirm):
        if confirm != "REVOKE-TIMED-MANUAL-TRADING":
            raise BridgeError(
                "LOCAL_CONFIRMATION_REQUIRED",
                "confirm must be REVOKE-TIMED-MANUAL-TRADING",
            )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason is required and must be at most 200 characters")
        account = self.config.account(account_alias)
        now = utc_now()
        now_text = now.isoformat(timespec="milliseconds")
        with self.db.transaction(immediate=True) as connection:
            active = self._active_manual_session(connection, account.alias)
            connection.execute(
                "DELETE FROM system_state WHERE key=?",
                ("manual_session:%s" % account.alias,),
            )
            connection.execute(
                """INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                ("live_until:%s" % account.alias, now_text, now_text),
            )
            expired = connection.execute(
                """UPDATE approvals SET expires_at=?
                   WHERE used_at IS NULL AND expires_at>?
                     AND preview_id IN (
                         SELECT preview_id FROM trade_previews WHERE account_alias=?
                     )""",
                (now_text, now_text, account.alias),
            ).rowcount
            connection.execute(
                """INSERT INTO audit_log(
                       occurred_at,actor,action,account_alias,object_id,details_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    now_text, "mcp", "REVOKE_MANUAL_SESSION", account.alias,
                    active["session_id"] if active else None,
                    json_text({
                        "reason": reason.strip(),
                        "expired_single_trade_approvals": expired,
                    }),
                ),
            )
        self._write_local_authorization(
            account, [], now + dt.timedelta(seconds=1), "mcp",
            new_id("manual_revoke"), "REVOKED",
        )
        return {
            "account_alias": account.alias,
            "revoked_session_id": active["session_id"] if active else None,
            "timed_session_active": False,
            "expired_single_trade_approvals": expired,
            "revoked_by": "mcp",
        }

    @staticmethod
    def _clock_minutes(value):
        if not isinstance(value, str):
            raise ValidationError("trading-window times must use HH:MM")
        try:
            parsed = dt.datetime.strptime(value, "%H:%M").time()
        except ValueError as exc:
            raise ValidationError("trading-window times must use HH:MM") from exc
        return parsed.hour * 60 + parsed.minute

    def _next_auto_generation(self, connection, account_alias):
        key = "auto_generation:%s" % account_alias
        raw = self._state(connection, key, "0")
        try:
            generation = int(raw) + 1
        except (TypeError, ValueError):
            generation = 1
        connection.execute(
            """INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, str(generation), iso_now()),
        )
        return generation

    def _active_auto_permit_row(self, connection, account_alias):
        row = connection.execute(
            """SELECT * FROM auto_permits WHERE account_alias=? AND status='ACTIVE'
               ORDER BY created_at DESC LIMIT 1""",
            (account_alias,),
        ).fetchone()
        if not row:
            return None
        try:
            current_generation = int(self._state(
                connection, "auto_generation:%s" % account_alias, "0"
            ))
            if (
                int(row["generation"]) != current_generation or
                parse_time(row["starts_at"]) > utc_now() or
                parse_time(row["expires_at"]) <= utc_now()
            ):
                return None
        except (TypeError, ValueError):
            return None
        return row

    def _auto_usage_snapshot(self, connection, permit_id):
        row = connection.execute(
            """SELECT COUNT(*) AS order_count,COALESCE(SUM(notional),0) AS notional,
                      MAX(reserved_at) AS last_reserved_at
               FROM auto_permit_usage WHERE permit_id=?""",
            (permit_id,),
        ).fetchone()
        return {
            "order_count": int(row["order_count"] or 0),
            "notional": float(row["notional"] or 0),
            "last_reserved_at": row["last_reserved_at"],
        }

    def _limited_auto_health_reasons(self, connection, account):
        reasons = []
        limits = self.config.risks(account.alias)
        if self._mode(connection) != "LIMITED_AUTO":
            reasons.append("AUTO_MODE_NOT_ENABLED")
        if self._halted(connection):
            reasons.append("AUTO_TRADING_HALTED")
        heartbeat = connection.execute(
            "SELECT * FROM heartbeats WHERE adapter_instance=?",
            (account.adapter_instance,),
        ).fetchone()
        if (
            not heartbeat or heartbeat["status"] != "READY" or
            self._age_seconds(heartbeat["received_at"]) > limits.auto_heartbeat_max_age_seconds
        ):
            reasons.append("AUTO_ADAPTER_NOT_READY")
        elif heartbeat:
            try:
                heartbeat_payload = json.loads(heartbeat["payload_json"])
            except (TypeError, ValueError):
                heartbeat_payload = {}
            if heartbeat_payload.get("qmt_mode") != "LIMITED_AUTO":
                reasons.append("AUTO_ADAPTER_MODE_MISMATCH")
            if heartbeat_payload.get("local_halt") is True:
                reasons.append("AUTO_ADAPTER_LOCALLY_HALTED")
            if heartbeat_payload.get("limited_auto_local_pause") is True:
                reasons.append("AUTO_ADAPTER_LOCALLY_PAUSED")
        account_row = self._latest_account_row(connection, account.alias)
        if (
            not account_row or
            self._age_seconds(account_row["captured_at"]) > limits.max_snapshot_age_seconds
        ):
            reasons.append("AUTO_ACCOUNT_SNAPSHOT_STALE")
        marker, _ = self._latest_positions(connection, account.alias)
        if (
            not marker or
            self._age_seconds(marker["captured_at"]) > limits.max_snapshot_age_seconds
        ):
            reasons.append("AUTO_POSITION_SNAPSHOT_STALE")
        depths = self.queue.depths(account.adapter_instance)
        if int(depths.get("dead_letter", 0) or 0) > 0:
            reasons.append("AUTO_DEAD_LETTER_PRESENT")
        if int(depths.get("commands", 0) or 0) > limits.auto_max_queue_depth:
            reasons.append("AUTO_QUEUE_BACKLOG")
        unresolved = connection.execute(
            "SELECT COUNT(*) AS n FROM trade_intents WHERE account_alias=? AND status='SUBMIT_UNKNOWN'",
            (account.alias,),
        ).fetchone()["n"]
        if unresolved:
            reasons.append("AUTO_SUBMIT_UNKNOWN_PRESENT")
        return sorted(set(reasons))

    @staticmethod
    def _auto_schedule_active(policy, now):
        local_now = now.astimezone(CHINA_TZ)
        if local_now.strftime("%Y%m%d") != policy.get("trading_day"):
            return False
        minute = local_now.hour * 60 + local_now.minute
        for window in policy.get("trading_windows", []):
            try:
                start_hour, start_minute = [int(item) for item in window["start"].split(":")]
                end_hour, end_minute = [int(item) for item in window["end"].split(":")]
            except (AttributeError, KeyError, TypeError, ValueError):
                return False
            if start_hour * 60 + start_minute <= minute < end_hour * 60 + end_minute:
                return True
        return False

    def _limited_auto_policy_reasons(
        self, connection, permit_row, request, current, include_health=True,
    ):
        reasons = []
        account = self.config.account(current["account_alias"])
        limits = self.config.risks(account.alias)
        try:
            policy = json.loads(permit_row["policy_json"])
        except (TypeError, ValueError):
            return ["AUTO_POLICY_INVALID"], None, None
        if hashlib.sha256(canonical_json(policy)).hexdigest() != permit_row["policy_hash"]:
            reasons.append("AUTO_POLICY_HASH_MISMATCH")
        now = utc_now()
        try:
            if parse_time(permit_row["starts_at"]) > now or parse_time(permit_row["expires_at"]) <= now:
                reasons.append("AUTO_PERMIT_EXPIRED")
        except (TypeError, ValueError):
            reasons.append("AUTO_POLICY_INVALID")
        if not self._auto_schedule_active(policy, now):
            reasons.append("AUTO_OUTSIDE_TRADING_WINDOW")
        symbol = current["instrument"]["qmt_symbol"]
        action = current["resolved_action"]
        order = current["resolved_order"]
        source = request.get("source") or {}
        if symbol not in policy.get("allowed_symbols", []):
            reasons.append("AUTO_SYMBOL_NOT_ALLOWED")
        if action not in policy.get("allowed_actions", []):
            reasons.append("AUTO_ACTION_NOT_ALLOWED")
        if request.get("sizing", {}).get("type") not in policy.get("allowed_sizing_types", []):
            reasons.append("AUTO_SIZING_NOT_ALLOWED")
        for field in ("type", "rule_set_id", "rule_version"):
            if source.get(field) != policy.get("source", {}).get(field):
                reasons.append("AUTO_STRATEGY_BINDING_MISMATCH")
                break
        volume = int(order["volume"])
        notional = float(order["notional"])
        if volume > int(policy.get("max_order_volume", 0)):
            reasons.append("AUTO_ORDER_VOLUME_EXCEEDED")
        if notional > float(policy.get("max_order_notional", 0)):
            reasons.append("AUTO_ORDER_NOTIONAL_EXCEEDED")
        if (
            int(policy.get("max_order_volume", 0)) > limits.max_order_volume or
            float(policy.get("max_order_notional", 0)) > limits.max_order_notional or
            float(policy.get("max_session_notional", 0)) > min(
                limits.max_auto_session_notional, limits.max_daily_notional
            ) or int(policy.get("max_orders", 0)) > limits.max_auto_orders or
            int(policy.get("min_order_interval_seconds", 0)) < limits.min_auto_order_interval_seconds or
            int(policy.get("max_concurrent_orders", 0)) > limits.max_auto_concurrent_orders or
            float(policy.get("max_symbol_position_notional", 0)) > limits.max_auto_symbol_position_notional or
            float(policy.get("max_account_drawdown", 0)) > limits.max_auto_account_drawdown
        ):
            reasons.append("AUTO_POLICY_EXCEEDS_CURRENT_CONFIG")
        if account.account_type == "CREDIT":
            if not account.limited_auto_credit_enabled:
                reasons.append("AUTO_CREDIT_NOT_ENABLED")
            if action in AUTO_NEW_DEBT_ACTIONS and not policy.get("allow_credit_new_debt", False):
                reasons.append("AUTO_CREDIT_NEW_DEBT_NOT_ALLOWED")
        usage = self._auto_usage_snapshot(connection, permit_row["permit_id"])
        if usage["order_count"] + 1 > int(policy.get("max_orders", 0)):
            reasons.append("AUTO_ORDER_COUNT_EXCEEDED")
        if usage["notional"] + notional > float(policy.get("max_session_notional", 0)):
            reasons.append("AUTO_SESSION_NOTIONAL_EXCEEDED")
        last_reserved = usage.get("last_reserved_at")
        if last_reserved:
            elapsed = (now - parse_time(last_reserved)).total_seconds()
            if elapsed < int(policy.get("min_order_interval_seconds", 0)):
                reasons.append("AUTO_ORDER_RATE_EXCEEDED")
        active_count = connection.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE account_alias=? AND status IN (%s)" %
            ",".join("?" for _ in ACTIVE_ORDER_STATES),
            [account.alias] + sorted(ACTIVE_ORDER_STATES),
        ).fetchone()["n"]
        if int(active_count) >= int(policy.get("max_concurrent_orders", 0)):
            reasons.append("AUTO_CONCURRENT_ORDER_LIMIT")
        current_volume = int(current.get("account_summary", {}).get("position_total_volume", 0) or 0)
        projected_volume = current_volume
        if action in BUY_LIKE:
            projected_volume += volume
        elif action in SELL_LIKE:
            projected_volume = max(0, current_volume - volume)
        current_position_notional = current_volume * float(order["limit_price"])
        projected_position_notional = projected_volume * float(order["limit_price"])
        if (
            projected_position_notional > current_position_notional and
            projected_position_notional > float(policy.get("max_symbol_position_notional", 0))
        ):
            reasons.append("AUTO_SYMBOL_POSITION_LIMIT")
        baseline = float(policy.get("baseline_total_asset", 0) or 0)
        current_asset = float(current.get("account_summary", {}).get("total_asset", 0) or 0)
        if baseline <= 0 or current_asset <= 0:
            reasons.append("AUTO_ACCOUNT_EQUITY_UNAVAILABLE")
        elif baseline - current_asset > float(policy.get("max_account_drawdown", 0)):
            reasons.append("AUTO_ACCOUNT_DRAWDOWN_EXCEEDED")
        failure_rows = connection.execute(
            """SELECT i.status FROM auto_permit_usage u
               JOIN trade_intents i ON i.intent_id=u.intent_id
               WHERE u.permit_id=? ORDER BY u.reserved_at DESC LIMIT 100""",
            (permit_row["permit_id"],),
        ).fetchall()
        consecutive = 0
        for failure_row in failure_rows:
            if failure_row["status"] in AUTO_FAILURE_STATES:
                consecutive += 1
            else:
                break
        if consecutive >= int(policy.get("max_consecutive_failures", 1)):
            reasons.append("AUTO_CONSECUTIVE_FAILURE_LIMIT")
        if include_health:
            reasons.extend(self._limited_auto_health_reasons(connection, account))
        summary = {
            "permit_id": permit_row["permit_id"],
            "policy_hash": permit_row["policy_hash"],
            "generation": permit_row["generation"],
            "expires_at": permit_row["expires_at"],
            "usage": usage,
            "remaining_orders": max(0, int(policy.get("max_orders", 0)) - usage["order_count"]),
            "remaining_notional": max(
                0.0, float(policy.get("max_session_notional", 0)) - usage["notional"]
            ),
            "consecutive_failures": consecutive,
        }
        return sorted(set(reasons)), policy, summary

    def _limited_auto_status(self, connection, account, include_health=True):
        row = connection.execute(
            "SELECT * FROM auto_permits WHERE account_alias=? ORDER BY created_at DESC LIMIT 1",
            (account.alias,),
        ).fetchone()
        if not row:
            return {
                "configured": False, "active": False, "status": "NONE",
                "permit_id": None, "health_reasons": (
                    self._limited_auto_health_reasons(connection, account) if include_health else []
                ),
            }
        try:
            policy = json.loads(row["policy_json"])
        except (TypeError, ValueError):
            return {
                "configured": True, "active": False, "status": "INVALID",
                "permit_id": row["permit_id"], "health_reasons": ["AUTO_POLICY_INVALID"],
            }
        usage = self._auto_usage_snapshot(connection, row["permit_id"])
        try:
            remaining = max(0, int((parse_time(row["expires_at"]) - utc_now()).total_seconds()))
        except (TypeError, ValueError):
            remaining = 0
        current_generation = int(self._state(
            connection, "auto_generation:%s" % account.alias, "0"
        ))
        active = bool(
            row["status"] == "ACTIVE" and remaining > 0 and
            int(row["generation"]) == current_generation and
            self._mode(connection) == "LIMITED_AUTO" and not self._halted(connection)
        )
        return {
            "configured": True,
            "active": active,
            "status": row["status"] if remaining else "EXPIRED",
            "permit_id": row["permit_id"],
            "policy_hash": row["policy_hash"],
            "generation": row["generation"],
            "starts_at": row["starts_at"],
            "expires_at": row["expires_at"],
            "remaining_seconds": remaining,
            "pause_reason": row["pause_reason"],
            "revoked_reason": row["revoked_reason"],
            "policy": policy,
            "usage": usage,
            "remaining_orders": max(0, int(policy["max_orders"]) - usage["order_count"]),
            "remaining_notional": max(
                0.0, float(policy["max_session_notional"]) - usage["notional"]
            ),
            "health_reasons": (
                self._limited_auto_health_reasons(connection, account) if include_health else []
            ),
        }

    def _write_auto_permit_authorization(self, account, permit_row):
        policy = json.loads(permit_row["policy_json"])
        expires = parse_time(permit_row["expires_at"])
        self._write_local_authorization(
            account, ["LIMITED_AUTO"], expires, "mcp", permit_row["permit_id"],
            "LIMITED_AUTO_PERMIT", {
                "permit_id": permit_row["permit_id"],
                "policy_hash": permit_row["policy_hash"],
                "generation": permit_row["generation"],
                "policy": policy,
            },
        )

    def check_limited_auto_readiness(self, account_alias):
        account = self.config.account(account_alias)
        try:
            self.ingester.scan_once()
            self.ingester.reconcile_terminal_evidence()
            scan_error = None
        except Exception as exc:
            scan_error = type(exc).__name__
        run_id = new_id("reconcile")
        started = iso_now()
        with self.db.transaction(immediate=True) as connection:
            reasons = self._limited_auto_health_reasons(connection, account)
            if scan_error:
                reasons.append("AUTO_RECONCILIATION_SCAN_FAILED")
            reasons = sorted(set(reasons))
            status = "SUCCESS" if not reasons else "FAILED"
            result = {
                "account_alias": account.alias,
                "ready": not reasons,
                "reasons": reasons,
                "scan_error_type": scan_error,
            }
            connection.execute(
                """INSERT INTO reconciliation_runs(
                       run_id,account_alias,status,started_at,completed_at,result_json
                   ) VALUES(?,?,?,?,?,?)""",
                (run_id, account.alias, status, started, iso_now(), json_text(result)),
            )
        self._pause_active_auto_permit(account.alias, reasons, actor="worker")
        return dict(result, reconciliation_run_id=run_id)

    def authorize_limited_auto(
        self, account_alias, symbols, actions, source_type, rule_set_id,
        rule_version, max_order_notional, max_order_volume,
        max_session_notional, max_orders, max_concurrent_orders,
        max_symbol_position_notional, max_account_drawdown, reason, confirm,
        minutes=480, min_order_interval_seconds=1,
        max_consecutive_failures=3, trading_windows=None,
        allowed_sizing_types=None, allow_credit_new_debt=False,
    ):
        if confirm != "AUTHORIZE-LIMITED-AUTO-P1":
            raise BridgeError(
                "LOCAL_CONFIRMATION_REQUIRED",
                "confirm must be AUTHORIZE-LIMITED-AUTO-P1",
            )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValidationError("reason is required and must be at most 500 characters")
        account = self.config.account(account_alias)
        limits = self.config.risks(account.alias)
        if account.account_type == "CREDIT" and not account.limited_auto_credit_enabled:
            raise BridgeError("AUTO_CREDIT_NOT_ENABLED", "limited-auto credit trading is disabled in bridge config")
        if not isinstance(symbols, list) or not symbols or len(symbols) > 100:
            raise ValidationError("symbols must contain 1-100 entries")
        try:
            normalized_symbols = sorted({normalize_symbol(item) for item in symbols})
        except (TypeError, ValueError) as exc:
            raise ValidationError(str(exc))
        if len(normalized_symbols) != len(symbols):
            raise ValidationError("symbols must not contain duplicates")
        if account.instrument_allowlist and not set(normalized_symbols).issubset(account.instrument_allowlist):
            raise BridgeError("INSTRUMENT_NOT_ALLOWED", "permit symbols exceed the account allowlist")
        supported_actions = {"BUY", "SELL"} if account.account_type == "STOCK" else CREDIT_EXPLICIT_ACTIONS
        if not isinstance(allow_credit_new_debt, bool):
            raise ValidationError("allow_credit_new_debt must be a boolean")
        if (
            not isinstance(actions, list) or not actions or
            any(not isinstance(action, str) for action in actions) or
            any(action not in supported_actions for action in actions) or
            len(set(actions)) != len(actions)
        ):
            raise ValidationError("actions must be a non-empty unique supported action array")
        if any(action in AUTO_NEW_DEBT_ACTIONS for action in actions) and allow_credit_new_debt is not True:
            raise BridgeError(
                "AUTO_CREDIT_NEW_DEBT_NOT_ALLOWED",
                "new credit debt requires allow_credit_new_debt=true",
            )
        for name, value in {
            "source_type": source_type, "rule_set_id": rule_set_id,
            "rule_version": rule_version,
        }.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise ValidationError("%s must be a non-empty string up to 100 characters" % name)
        integer_values = {
            "minutes": minutes, "max_order_volume": max_order_volume,
            "max_orders": max_orders, "max_concurrent_orders": max_concurrent_orders,
            "min_order_interval_seconds": min_order_interval_seconds,
            "max_consecutive_failures": max_consecutive_failures,
        }
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in integer_values.values()):
            raise ValidationError("limited-auto integer limits must be positive integers")
        if minutes > limits.max_auto_authorization_minutes:
            raise ValidationError("minutes exceeds max_auto_authorization_minutes")
        if max_order_volume > limits.max_order_volume:
            raise ValidationError("max_order_volume exceeds the account hard limit")
        if max_orders > limits.max_auto_orders:
            raise ValidationError("max_orders exceeds max_auto_orders")
        if max_concurrent_orders > limits.max_auto_concurrent_orders:
            raise ValidationError("max_concurrent_orders exceeds the configured hard limit")
        if min_order_interval_seconds < limits.min_auto_order_interval_seconds:
            raise ValidationError("min_order_interval_seconds is below the configured hard minimum")
        if max_consecutive_failures > 100:
            raise ValidationError("max_consecutive_failures must not exceed 100")
        numeric_values = {
            "max_order_notional": _number(max_order_notional, "max_order_notional", 0.000001),
            "max_session_notional": _number(max_session_notional, "max_session_notional", 0.000001),
            "max_symbol_position_notional": _number(
                max_symbol_position_notional, "max_symbol_position_notional", 0.000001
            ),
            "max_account_drawdown": _number(max_account_drawdown, "max_account_drawdown", 0.000001),
        }
        if numeric_values["max_order_notional"] > limits.max_order_notional:
            raise ValidationError("max_order_notional exceeds the account hard limit")
        if numeric_values["max_session_notional"] > min(
            limits.max_auto_session_notional, limits.max_daily_notional
        ):
            raise ValidationError("max_session_notional exceeds the configured automatic/day limit")
        if numeric_values["max_session_notional"] < numeric_values["max_order_notional"]:
            raise ValidationError("max_session_notional must cover at least one maximum order")
        if numeric_values["max_symbol_position_notional"] > limits.max_auto_symbol_position_notional:
            raise ValidationError("max_symbol_position_notional exceeds the configured hard limit")
        if numeric_values["max_account_drawdown"] > limits.max_auto_account_drawdown:
            raise ValidationError("max_account_drawdown exceeds the configured hard limit")
        windows = trading_windows
        if windows is None:
            windows = [
                {"start": "09:30", "end": "11:30"},
                {"start": "13:00", "end": "15:00"},
            ]
        if not isinstance(windows, list) or not windows or len(windows) > 8:
            raise ValidationError("trading_windows must contain 1-8 windows")
        normalized_windows = []
        previous_end = -1
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValidationError("each trading window must contain exactly start and end")
            start = self._clock_minutes(window["start"])
            end = self._clock_minutes(window["end"])
            if start >= end or start < previous_end or start < 9 * 60 + 15 or end > 15 * 60:
                raise ValidationError("trading windows must be ordered, non-overlapping and within 09:15-15:00")
            normalized_windows.append({"start": window["start"], "end": window["end"]})
            previous_end = end
        default_sizing = [
            "FIXED_VOLUME", "FIXED_NOTIONAL", "AVAILABLE_CASH_PERCENT",
            "TARGET_PORTFOLIO_PERCENT", "TARGET_POSITION",
        ]
        sizing_types = default_sizing if allowed_sizing_types is None else allowed_sizing_types
        if account.account_type == "CREDIT":
            default_sizing = ["FIXED_VOLUME", "FIXED_NOTIONAL", "AVAILABLE_CASH_PERCENT"]
            sizing_types = default_sizing if allowed_sizing_types is None else allowed_sizing_types
        if (
            not isinstance(sizing_types, list) or not sizing_types or
            len(set(sizing_types)) != len(sizing_types) or
            set(sizing_types) - set(default_sizing)
        ):
            raise ValidationError("allowed_sizing_types contains unsupported or duplicate values")
        local_now = utc_now().astimezone(CHINA_TZ)
        if local_now.weekday() >= 5:
            raise BridgeError("AUTO_OUTSIDE_TRADING_DAY", "limited-auto permits cannot start on a weekend")
        close_local = local_now.replace(hour=15, minute=0, second=0, microsecond=0)
        expires_local = min(local_now + dt.timedelta(minutes=minutes), close_local)
        if expires_local <= local_now:
            raise BridgeError("AUTO_OUTSIDE_TRADING_DAY", "the local trading day has ended")
        starts = utc_now()
        expires = expires_local.astimezone(dt.timezone.utc)
        with self.db.transaction(immediate=True) as connection:
            health_reasons = self._limited_auto_health_reasons(connection, account)
            health_reasons = [
                item for item in health_reasons if item != "AUTO_ADAPTER_LOCALLY_PAUSED"
            ]
            if health_reasons:
                raise BridgeError(
                    "AUTO_NOT_READY", "limited-auto readiness checks failed",
                    {"reasons": health_reasons},
                )
            account_row = self._latest_account_row(connection, account.alias)
            account_data = _payload(account_row) or {}
            baseline_total_asset = float(account_data.get("total_asset", 0) or 0)
            if baseline_total_asset <= 0:
                raise BridgeError("AUTO_ACCOUNT_EQUITY_UNAVAILABLE", "positive total_asset is required")
            generation = self._next_auto_generation(connection, account.alias)
            permit_id = new_id("auto_permit")
            created = iso_now()
            policy = {
                "policy_version": 1,
                "permit_id": permit_id,
                "generation": generation,
                "account_alias": account.alias,
                "account_type": account.account_type,
                "trading_day": local_now.strftime("%Y%m%d"),
                "timezone": "Asia/Shanghai",
                "trading_windows": normalized_windows,
                "allowed_symbols": normalized_symbols,
                "allowed_actions": sorted(actions),
                "allowed_sizing_types": sorted(sizing_types),
                "source": {
                    "type": source_type.strip(),
                    "rule_set_id": rule_set_id.strip(),
                    "rule_version": rule_version.strip(),
                },
                "max_order_notional": numeric_values["max_order_notional"],
                "max_order_volume": max_order_volume,
                "max_session_notional": numeric_values["max_session_notional"],
                "max_orders": max_orders,
                "min_order_interval_seconds": min_order_interval_seconds,
                "max_concurrent_orders": max_concurrent_orders,
                "max_symbol_position_notional": numeric_values["max_symbol_position_notional"],
                "max_account_drawdown": numeric_values["max_account_drawdown"],
                "max_consecutive_failures": max_consecutive_failures,
                "baseline_total_asset": baseline_total_asset,
                "allow_credit_new_debt": bool(allow_credit_new_debt),
                "starts_at": starts.isoformat(timespec="milliseconds"),
                "expires_at": expires.isoformat(timespec="milliseconds"),
            }
            policy_hash = hashlib.sha256(canonical_json(policy)).hexdigest()
            connection.execute(
                """UPDATE auto_permits SET status='REVOKED',revoked_at=?,
                          revoked_reason='superseded by a new permit'
                   WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')""",
                (created, account.alias),
            )
            connection.execute(
                """INSERT INTO auto_permits(
                       permit_id,account_alias,status,generation,policy_hash,policy_json,
                       actor,reason,created_at,starts_at,expires_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    permit_id, account.alias, "ACTIVE", generation, policy_hash,
                    json_text(policy), "mcp", reason.strip(), created,
                    policy["starts_at"], policy["expires_at"],
                ),
            )
            connection.execute(
                """INSERT INTO audit_log(
                       occurred_at,actor,action,account_alias,object_id,details_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    created, "mcp", "AUTHORIZE_LIMITED_AUTO", account.alias,
                    permit_id, json_text({
                        "reason": reason.strip(), "policy_hash": policy_hash,
                        "expires_at": policy["expires_at"], "policy": policy,
                    }),
                ),
            )
            permit_row = connection.execute(
                "SELECT * FROM auto_permits WHERE permit_id=?", (permit_id,)
            ).fetchone()
        self._write_auto_permit_authorization(account, permit_row)
        with self.db.connect() as connection:
            return self._limited_auto_status(connection, account)

    def get_limited_auto_status(self, account_alias):
        account = self.config.account(account_alias)
        with self.db.connect() as connection:
            return self._limited_auto_status(connection, account)

    def _pause_active_auto_permit(self, account_alias, reasons, actor="worker"):
        serious = sorted(set(reasons) & AUTO_PAUSE_REASONS)
        if not serious:
            return False
        account = self.config.account(account_alias)
        now = iso_now()
        with self.db.transaction(immediate=True) as connection:
            row = self._active_auto_permit_row(connection, account.alias)
            if not row:
                return False
            self._next_auto_generation(connection, account.alias)
            connection.execute(
                """UPDATE auto_permits SET status='PAUSED',paused_at=?,pause_reason=?
                   WHERE permit_id=? AND status='ACTIVE'""",
                (now, ",".join(serious), row["permit_id"]),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (now, actor, "PAUSE_LIMITED_AUTO", account.alias, row["permit_id"], json_text({"reasons": serious})),
            )
        self._write_local_authorization(
            account, [], utc_now() + dt.timedelta(seconds=1), actor,
            new_id("auto_pause"), "PAUSED",
        )
        return True

    def resume_limited_auto(self, account_alias, reason, confirm):
        if confirm != "RESUME-LIMITED-AUTO-P1":
            raise BridgeError(
                "LOCAL_CONFIRMATION_REQUIRED", "confirm must be RESUME-LIMITED-AUTO-P1"
            )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValidationError("reason is required and must be at most 500 characters")
        account = self.config.account(account_alias)
        now = iso_now()
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT * FROM auto_permits WHERE account_alias=? AND status='PAUSED'
                   ORDER BY created_at DESC LIMIT 1""",
                (account.alias,),
            ).fetchone()
            if not row:
                raise BridgeError("AUTO_PERMIT_NOT_FOUND", "no paused limited-auto permit exists")
            if parse_time(row["expires_at"]) <= utc_now():
                raise BridgeError("AUTO_PERMIT_EXPIRED", "the paused permit has expired")
            if "AUTO_CONSECUTIVE_FAILURE_LIMIT" in (row["pause_reason"] or ""):
                raise BridgeError(
                    "AUTO_STRATEGY_REAUTHORIZATION_REQUIRED",
                    "consecutive failures require a new versioned permit, not resume",
                )
            health_reasons = self._limited_auto_health_reasons(connection, account)
            health_reasons = [
                item for item in health_reasons if item != "AUTO_ADAPTER_LOCALLY_PAUSED"
            ]
            if health_reasons:
                raise BridgeError("AUTO_NOT_READY", "readiness checks still fail", {"reasons": health_reasons})
            policy = json.loads(row["policy_json"])
            account_row = self._latest_account_row(connection, account.alias)
            current_asset = float((_payload(account_row) or {}).get("total_asset", 0) or 0)
            if (
                current_asset <= 0 or
                float(policy["baseline_total_asset"]) - current_asset > float(policy["max_account_drawdown"])
            ):
                raise BridgeError("AUTO_ACCOUNT_DRAWDOWN_EXCEEDED", "account drawdown guard still fails")
            generation = self._next_auto_generation(connection, account.alias)
            policy["generation"] = generation
            policy["resumed_at"] = now
            policy_hash = hashlib.sha256(canonical_json(policy)).hexdigest()
            connection.execute(
                """UPDATE auto_permits SET status='ACTIVE',generation=?,policy_hash=?,
                          policy_json=?,paused_at=NULL,pause_reason=NULL
                   WHERE permit_id=?""",
                (generation, policy_hash, json_text(policy), row["permit_id"]),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (now, "mcp", "RESUME_LIMITED_AUTO", account.alias, row["permit_id"], json_text({"reason": reason.strip(), "generation": generation})),
            )
            updated = connection.execute(
                "SELECT * FROM auto_permits WHERE permit_id=?", (row["permit_id"],)
            ).fetchone()
        self._write_auto_permit_authorization(account, updated)
        with self.db.connect() as connection:
            return self._limited_auto_status(connection, account)

    def revoke_limited_auto(self, account_alias, reason, confirm):
        if confirm != "REVOKE-LIMITED-AUTO-P1":
            raise BridgeError(
                "LOCAL_CONFIRMATION_REQUIRED", "confirm must be REVOKE-LIMITED-AUTO-P1"
            )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValidationError("reason is required and must be at most 500 characters")
        account = self.config.account(account_alias)
        now = iso_now()
        with self.db.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT * FROM auto_permits WHERE account_alias=? AND status IN ('ACTIVE','PAUSED')
                   ORDER BY created_at DESC LIMIT 1""",
                (account.alias,),
            ).fetchone()
            self._next_auto_generation(connection, account.alias)
            if row:
                connection.execute(
                    """UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason=?
                       WHERE permit_id=?""",
                    (now, reason.strip(), row["permit_id"]),
                )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                (now, "mcp", "REVOKE_LIMITED_AUTO", account.alias, row["permit_id"] if row else None, json_text({"reason": reason.strip()})),
            )
        self._write_local_authorization(
            account, [], utc_now() + dt.timedelta(seconds=1), "mcp",
            new_id("auto_revoke"), "REVOKED",
        )
        return {
            "account_alias": account.alias,
            "revoked_permit_id": row["permit_id"] if row else None,
            "active": False,
        }

    def _check_submit_authorization(
        self, connection, preview, approval_context, request=None, current=None,
    ):
        mode = self._mode(connection)
        requested_mode = preview["execution_mode"]
        if self._halted(connection):
            raise BridgeError("LIVE_NOT_ENABLED", "trading is halted")
        if mode != requested_mode:
            raise BridgeError("LIVE_NOT_ENABLED", "requested execution mode is not locally enabled")
        if requested_mode == "OBSERVE_ONLY":
            return None
        if requested_mode == "SIM_SIGNAL":
            return None
        if requested_mode == "LIMITED_AUTO":
            permit = self._active_auto_permit_row(connection, preview["account_alias"])
            if not permit:
                raise BridgeError("LOCAL_APPROVAL_REQUIRED", "no active limited-auto permit is available")
            reasons, policy, summary = self._limited_auto_policy_reasons(
                connection, permit, request or {}, current or preview,
            )
            if reasons:
                raise BridgeError(
                    "AUTO_POLICY_REJECTED", "limited-auto policy rejected this order",
                    {"reasons": reasons},
                )
            return {
                "type": "LIMITED_AUTO",
                "permit_id": permit["permit_id"],
                "policy_hash": permit["policy_hash"],
                "generation": permit["generation"],
                "policy": policy,
                "summary": summary,
            }
        live_until = self._state(connection, "live_until:%s" % preview["account_alias"])
        if not live_until or parse_time(live_until) <= utc_now():
            raise BridgeError("LIVE_NOT_ENABLED", "local LIVE permission is not active")
        if isinstance(approval_context, dict):
            approval_id = approval_context.get("local_approval_id")
            if approval_id:
                row = connection.execute(
                    "SELECT * FROM approvals WHERE approval_id=? AND preview_id=?",
                    (approval_id, preview["preview_id"]),
                ).fetchone()
                if (
                    row and row["decision"] == "APPROVED" and
                    row["used_at"] is None and
                    parse_time(row["expires_at"]) > utc_now()
                ):
                    return {
                        "type": "SINGLE_TRADE",
                        "approval_id": approval_id,
                    }
        session = self._active_manual_session(
            connection, preview["account_alias"]
        )
        if session:
            return {
                "type": "TIMED_SESSION",
                "session_id": session["session_id"],
            }
        raise BridgeError(
            "LOCAL_APPROVAL_REQUIRED",
            "a one-time approval or active timed manual session is required",
        )

    def submit_trade_intent(self, preview_id, approval_context=None):
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM trade_previews WHERE preview_id=?", (preview_id,)).fetchone()
            if not row:
                raise BridgeError("PREVIEW_NOT_FOUND", "preview was not found")
            if row["consumed_intent_id"]:
                return self.get_trade_intent(row["consumed_intent_id"])
            if parse_time(row["expires_at"]) <= utc_now():
                raise BridgeError("PREVIEW_EXPIRED", "preview has expired")
            request = json.loads(row["request_json"])
            stored = json.loads(row["result_json"])
        current = self._build_preview(request)
        if current["snapshot_fingerprint"] != row["snapshot_fingerprint"]:
            raise BridgeError(
                "RISK_REJECTED",
                "risk-relevant state or resolved order changed; create a new preview",
                {"reason": "SNAPSHOT_CHANGED"},
            )
        if not current["risk"]["allowed"]:
            if current.get("execution_mode") == "LIMITED_AUTO":
                self._pause_active_auto_permit(
                    current["account_alias"], current["risk"]["reasons"], actor="worker",
                )
            raise BridgeError("RISK_REJECTED", "trade failed hard risk checks", {"reasons": current["risk"]["reasons"]})

        intent_id = new_id("intent")
        client_key = new_client_order_key()
        risk_id = new_id("risk")
        created = iso_now()
        command = {
            "type": "EXECUTE_ORDER",
            "intent_id": intent_id,
            "intent_revision": 1,
            "client_order_key": client_key,
            "account_alias": current["account_alias"],
            "account_type": current["account_type"],
            "asset_type": "STOCK",
            "qmt_symbol": current["instrument"]["qmt_symbol"],
            "action": current["resolved_action"],
            "semantic_action": current["action"],
            "resolved_order": current["resolved_order"],
            "execution_mode": current["execution_mode"],
            "risk_decision_id": risk_id,
            "source_signal_id": (request.get("source") or {}).get("signal_id"),
            "source": request.get("source") or {},
            "credit": request.get("credit") or None,
            "auto_permit": None,
        }
        limits = self.config.risks(current["account_alias"])
        account = self.config.account(current["account_alias"])
        authorization = None
        try:
            with self.db.transaction(immediate=True) as connection:
                fresh = connection.execute("SELECT * FROM trade_previews WHERE preview_id=?", (preview_id,)).fetchone()
                if fresh["consumed_intent_id"]:
                    return self.get_trade_intent(fresh["consumed_intent_id"])
                if parse_time(fresh["expires_at"]) <= utc_now():
                    raise BridgeError("PREVIEW_EXPIRED", "preview has expired")
                authorization = self._check_submit_authorization(
                    connection, stored, approval_context, request=request, current=current,
                )
                if authorization and authorization.get("type") == "LIMITED_AUTO":
                    command["auto_permit"] = {
                        "permit_id": authorization["permit_id"],
                        "policy_hash": authorization["policy_hash"],
                        "generation": authorization["generation"],
                    }
                envelope = make_envelope(
                    self.keyring, "TRADE_INTENT", command, limits.command_ttl_seconds,
                    "qmt-bridge-worker", intent_id,
                )
                connection.execute(
                    "INSERT INTO risk_decisions(risk_decision_id,preview_id,account_alias,allowed,reasons_json,snapshot_fingerprint,created_at) VALUES(?,?,?,?,?,?,?)",
                    (risk_id, preview_id, account.alias, 1, "[]", current["snapshot_fingerprint"], created),
                )
                connection.execute(
                    """INSERT INTO trade_intents(intent_id,intent_revision,preview_id,client_order_key,account_alias,status,
                       action,symbol,requested_volume,limit_price,execution_mode,source_signal_id,created_at,updated_at,payload_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent_id, 1, preview_id, client_key, account.alias, "QUEUED", current["action"],
                        current["instrument"]["qmt_symbol"], current["resolved_order"]["volume"],
                        current["resolved_order"]["limit_price"], current["execution_mode"],
                        command["source_signal_id"], created, created, json_text(command),
                    ),
                )
                connection.execute(
                    "INSERT INTO qmt_commands(message_id,intent_id,account_alias,command_type,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                    (envelope["message_id"], intent_id, account.alias, "EXECUTE_ORDER", "PENDING_DELIVERY", created, json_text(envelope)),
                )
                if authorization and authorization.get("type") == "LIMITED_AUTO":
                    connection.execute(
                        """INSERT INTO auto_permit_usage(
                               permit_id,intent_id,account_alias,symbol,action,volume,notional,reserved_at
                           ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            authorization["permit_id"], intent_id, account.alias,
                            current["instrument"]["qmt_symbol"], current["resolved_action"],
                            current["resolved_order"]["volume"], current["resolved_order"]["notional"],
                            created,
                        ),
                    )
                connection.execute(
                    "UPDATE trade_previews SET consumed_intent_id=? WHERE preview_id=? AND consumed_intent_id IS NULL",
                    (intent_id, preview_id),
                )
                approval_id = authorization.get("approval_id") if authorization else None
                if approval_id:
                    connection.execute("UPDATE approvals SET used_at=? WHERE approval_id=?", (created, approval_id))
                audit_details = {
                    "preview_id": preview_id,
                    "execution_mode": current["execution_mode"],
                }
                if authorization:
                    audit_details["authorization_type"] = authorization["type"]
                    audit_details["authorization_id"] = (
                        authorization.get("approval_id") or
                        authorization.get("session_id") or
                        authorization.get("permit_id")
                    )
                connection.execute(
                    "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
                    (created, "mcp", "SUBMIT_TRADE_INTENT", account.alias, intent_id, json_text(audit_details)),
                )
        except BridgeError as exc:
            if current.get("execution_mode") == "LIMITED_AUTO":
                self._pause_active_auto_permit(
                    current["account_alias"], exc.details.get("reasons", []), actor="worker",
                )
            raise
        except sqlite3.IntegrityError as exc:
            raise BridgeError("DUPLICATE_INTENT", "intent conflicts with an existing request") from exc
        self.dispatch_pending_commands(account.alias)
        return self.get_trade_intent(intent_id)

    def dispatch_pending_commands(self, account_alias=None):
        with self.db.connect() as connection:
            query = "SELECT * FROM qmt_commands WHERE status='PENDING_DELIVERY'"
            values = []
            if account_alias:
                query += " AND account_alias=?"
                values.append(account_alias)
            rows = connection.execute(query, values).fetchall()
        delivered = 0
        for row in rows:
            envelope = json.loads(row["payload_json"])
            account = self.config.account(row["account_alias"])
            self.queue.write(account.adapter_instance, "commands", envelope)
            with self.db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE qmt_commands SET status='DELIVERED' WHERE message_id=? AND status='PENDING_DELIVERY'",
                    (row["message_id"],),
                )
            delivered += 1
        return delivered

    def cancel_order(self, account_alias, order_id, reason):
        account = self.config.account(account_alias)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValidationError("reason is required and must be at most 200 characters")
        with self.db.connect() as connection:
            mode = self._mode(connection)
            if mode == "OBSERVE_ONLY":
                raise BridgeError("LIVE_NOT_ENABLED", "cancellation is disabled in OBSERVE_ONLY mode")
            row = connection.execute(
                "SELECT * FROM orders WHERE account_alias=? AND qmt_order_id=? ORDER BY trading_day DESC LIMIT 1",
                (account_alias, str(order_id)),
            ).fetchone()
            if not row or not row["client_order_key"]:
                raise BridgeError("ORDER_NOT_FOUND", "order is missing or does not belong to this bridge")
            if row["status"] not in ACTIVE_ORDER_STATES:
                raise BridgeError("ORDER_NOT_CANCELLABLE", "order is not active")
            prior = connection.execute(
                "SELECT message_id,status,payload_json FROM qmt_commands WHERE account_alias=? AND command_type='CANCEL_ORDER' ORDER BY created_at DESC LIMIT 50",
                (account_alias,),
            ).fetchall()
            for command_row in prior:
                previous = json.loads(command_row["payload_json"])["payload"]
                if previous.get("qmt_order_id") == str(order_id):
                    return {
                        "message_id": command_row["message_id"], "status": command_row["status"],
                        "command_type": "CANCEL_ORDER", "duplicate": True,
                    }
        payload = {
            "type": "CANCEL_ORDER", "account_alias": account_alias,
            "account_type": account.account_type, "qmt_order_id": str(order_id),
            "client_order_key": row["client_order_key"], "intent_id": row["intent_id"],
            "reason": reason.strip(),
        }
        return self._queue_control_command(account, payload, "CANCEL_ORDER", row["intent_id"])

    def request_sync(self, account_alias, scopes, symbols=None):
        account = self.config.account(account_alias)
        allowed = {"ACCOUNT", "POSITION", "ORDER", "DEAL", "QUOTE", "CREDIT_ACCOUNT", "CREDIT_DEBT", "CREDIT_ELIGIBILITY"}
        if not isinstance(scopes, list) or not scopes or set(scopes) - allowed:
            raise ValidationError("scopes contains unsupported values")
        normalized_symbols = []
        if "QUOTE" in scopes:
            if not isinstance(symbols, list) or not symbols or len(symbols) > 100:
                raise ValidationError("QUOTE sync requires 1-100 symbols")
            try:
                normalized_symbols = sorted({normalize_symbol(item) for item in symbols})
            except ValueError as exc:
                raise ValidationError(str(exc))
        elif symbols is not None:
            raise ValidationError("symbols are only allowed with QUOTE scope")
        payload = {
            "type": "REQUEST_SYNC", "account_alias": account_alias,
            "account_type": account.account_type, "scopes": sorted(set(scopes)),
            "symbols": normalized_symbols,
        }
        return self._queue_control_command(account, payload, "REQUEST_SYNC")

    def _queue_control_command(self, account, payload, command_type, intent_id=None):
        limits = self.config.risks(account.alias)
        envelope = make_envelope(self.keyring, command_type, payload, limits.command_ttl_seconds, "qmt-bridge-worker", intent_id)
        created = iso_now()
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO qmt_commands(message_id,intent_id,account_alias,command_type,status,created_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                (envelope["message_id"], intent_id, account.alias, command_type, "PENDING_DELIVERY", created, json_text(envelope)),
            )
        self.dispatch_pending_commands(account.alias)
        return {"message_id": envelope["message_id"], "status": "DELIVERED", "command_type": command_type}

    def request_credit_precheck(self, account_alias, requests):
        account = self.config.account(account_alias)
        if account.account_type != "CREDIT":
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "account is not CREDIT")
        if not isinstance(requests, list) or not requests:
            raise ValidationError("requests must be a non-empty array")
        normalized = []
        for item in requests:
            try:
                require_exact_keys(item, {"symbol", "credit_action", "price_type", "price"}, {"symbol", "credit_action", "price_type", "price"})
                action = item["credit_action"]
                if action not in CREDIT_EXPLICIT_ACTIONS:
                    raise ValidationError("invalid credit_action")
                if item["price_type"] != "LIMIT":
                    raise ValidationError("credit precheck only supports LIMIT")
                normalized.append({
                    "symbol": normalize_symbol(item["symbol"]), "credit_action": action,
                    "price_type": "LIMIT", "price": _number(item["price"], "price", 0.000001),
                })
            except BridgeError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(str(exc))
        actions = {item["credit_action"] for item in normalized}
        if len(actions) != 1:
            raise ValidationError("one credit precheck batch must use exactly one credit_action")
        limits = self.config.risks(account_alias)
        with self.db.transaction(immediate=True) as connection:
            inflight = connection.execute(
                "SELECT seq FROM credit_capacity_queries WHERE account_alias=? AND status='IN_PROGRESS' LIMIT 1",
                (account_alias,),
            ).fetchone()
            if inflight:
                raise BridgeError("CREDIT_QUERY_IN_PROGRESS", "a credit query is already in progress", {"seq": inflight["seq"]})
            latest = connection.execute(
                "SELECT requested_at FROM credit_capacity_queries WHERE account_alias=? ORDER BY requested_at DESC LIMIT 1",
                (account_alias,),
            ).fetchone()
            if latest:
                age = self._age_seconds(latest["requested_at"])
                if age < limits.credit_query_cooldown_seconds:
                    raise BridgeError("CREDIT_QUERY_RATE_LIMITED", "credit query is cooling down", {"retry_after_seconds": math.ceil(limits.credit_query_cooldown_seconds - age)})
            seq = new_id("creditseq")
            created = iso_now()
            request_body = {"requests": normalized}
            connection.execute(
                "INSERT INTO credit_capacity_queries(account_alias,seq,status,requested_at,request_json) VALUES(?,?,?,?,?)",
                (account_alias, seq, "IN_PROGRESS", created, json_text(request_body)),
            )
        payload = {
            "type": "CREDIT_PRECHECK", "account_alias": account_alias,
            "account_type": "CREDIT", "seq": seq, "requests": normalized,
        }
        queued = self._queue_control_command(account, payload, "CREDIT_PRECHECK")
        return {
            **queued, "seq": seq, "capacity_snapshot_id": "ccap_" + seq,
            "query_status": "IN_PROGRESS",
        }

    def halt_trading(self, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValidationError("reason is required and must be at most 500 characters")
        now = iso_now()
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM system_state WHERE key LIKE 'manual_session:%'"
            )
            connection.execute(
                "UPDATE system_state SET value=?,updated_at=? WHERE key LIKE 'live_until:%'",
                (now, now),
            )
            connection.execute(
                "UPDATE approvals SET expires_at=? WHERE used_at IS NULL AND expires_at>?",
                (now, now),
            )
            connection.execute(
                """UPDATE auto_permits SET status='REVOKED',revoked_at=?,
                          revoked_reason='global trading halt'
                   WHERE status IN ('ACTIVE','PAUSED')""",
                (now,),
            )
            for account in self.config.accounts.values():
                self._next_auto_generation(connection, account.alias)
            connection.execute(
                "INSERT INTO system_state(key,value,updated_at) VALUES('halted','true',?) ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
                (now,),
            )
            connection.execute(
                "INSERT INTO system_state(key,value,updated_at) VALUES('halt_reason',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (reason.strip(), now),
            )
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,details_json) VALUES(?,?,?,?)",
                (now, "mcp", "HALT_TRADING", json_text({"reason": reason.strip()})),
            )
        self.write_adapter_halt(True, reason.strip())
        for account in self.config.accounts.values():
            self._write_local_authorization(
                account, [], utc_now() + dt.timedelta(seconds=1), "mcp",
                new_id("halt_revoke"), "REVOKED_BY_HALT",
            )
        with self.db.connect() as connection:
            mode = self._mode(connection)
        return {
            "mode": mode,
            "halted": True,
            "reason": reason.strip(),
            "clearing_requires_local_console": True,
        }

    def write_adapter_halt(self, halted, reason):
        payload = {"halted": bool(halted), "reason": reason, "changed_at": iso_now()}
        # A halt document is durable and deliberately long lived. Clearing it is
        # only exposed by the local console.
        envelope = make_envelope(
            self.keyring, "LOCAL_HALT", payload, 10 * 365 * 24 * 3600,
            "qmt-bridge-control",
        )
        for account in self.config.accounts.values():
            path = os.path.join(
                self.config.data_dir, "qmt_runtime", account.adapter_instance, "local_halt.json"
            )
            atomic_write_json(path, envelope)
