import json
import ipaddress
import math
import os
import re
from dataclasses import dataclass, field

from .errors import BridgeError
from .modes import RUN_MODES, normalize_mode
from .util import normalize_symbol


ACCOUNT_TYPES = {"STOCK", "CREDIT"}
DEFAULT_SPECIAL_PREFIXES = ("688", "689")
CREDIT_BUY_ACTIONS = {"MARGIN_BUY", "COLLATERAL_BUY"}
CREDIT_SELL_ACTIONS = {"COLLATERAL_SELL", "SHORT_SELL"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

TOP_LEVEL_FIELDS = {
    "data_dir", "host", "port", "default_mode", "max_message_bytes",
    "key_file", "worker_token_file", "accounts",
}
ACCOUNT_FIELDS = {
    "alias", "account_type", "adapter_instance", "enabled", "lot_size",
    "odd_lot_sell_allowed", "instrument_allowlist", "credit_action_mapping",
    "credit_action_priority", "order_volume_rules", "risk_limits",
    "limited_auto_credit_enabled",
}
RISK_INTEGER_RANGES = {
    "max_order_volume": (1, 2147483647),
    "max_snapshot_age_seconds": (1, 3600),
    "max_quote_age_seconds": (1, 60),
    "max_credit_snapshot_age_seconds": (1, 86400),
    "preview_ttl_seconds": (1, 3600),
    "command_ttl_seconds": (1, 3600),
    "credit_query_cooldown_seconds": (1, 86400),
    "max_auto_authorization_minutes": (1, 720),
    "max_auto_orders": (1, 100000),
    "min_auto_order_interval_seconds": (1, 3600),
    "max_auto_concurrent_orders": (1, 1000),
    "auto_heartbeat_max_age_seconds": (5, 60),
    "auto_max_queue_depth": (1, 10000),
}
RISK_NUMBER_RANGES = {
    "max_order_notional": (0.000001, 1e15),
    "max_daily_notional": (0.000001, 1e15),
    "min_maintenance_ratio": (0.000001, 100.0),
    "max_auto_session_notional": (0.000001, 1e15),
    "max_auto_symbol_position_notional": (0.000001, 1e15),
    "max_auto_account_drawdown": (0.000001, 1e15),
}


@dataclass(frozen=True)
class OrderVolumeRule:
    prefixes: tuple
    min_buy: int
    min_sell: int


@dataclass(frozen=True)
class AccountConfig:
    alias: str
    account_type: str
    adapter_instance: str
    enabled: bool = True
    lot_size: int = 100
    odd_lot_sell_allowed: bool = True
    instrument_allowlist: tuple = ()
    default_min_buy_volume: int = 100
    default_min_sell_volume: int = 100
    order_volume_overrides: tuple = (
        OrderVolumeRule(DEFAULT_SPECIAL_PREFIXES, 200, 200),
    )
    credit_buy_action: str = "MARGIN_BUY"
    credit_sell_action: str = "COLLATERAL_SELL"
    limited_auto_credit_enabled: bool = False

    def minimum_order_volume(self, symbol, side):
        code = symbol.split(".", 1)[0]
        for rule in self.order_volume_overrides:
            if any(code.startswith(prefix) for prefix in rule.prefixes):
                return rule.min_buy if side == "BUY" else rule.min_sell
        return self.default_min_buy_volume if side == "BUY" else self.default_min_sell_volume

    def configured_credit_action(self, action):
        if self.account_type != "CREDIT":
            return action
        if action == "BUY":
            return self.credit_buy_action
        if action == "SELL":
            return self.credit_sell_action
        return action


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 200000.0
    max_order_volume: int = 500000
    max_daily_notional: float = 2000000.0
    min_maintenance_ratio: float = 1.5
    max_snapshot_age_seconds: int = 90
    max_quote_age_seconds: int = 30
    max_credit_snapshot_age_seconds: int = 600
    preview_ttl_seconds: int = 120
    command_ttl_seconds: int = 60
    credit_query_cooldown_seconds: int = 180
    max_auto_authorization_minutes: int = 720
    max_auto_session_notional: float = 2000000.0
    max_auto_orders: int = 1000
    min_auto_order_interval_seconds: int = 1
    max_auto_concurrent_orders: int = 20
    max_auto_symbol_position_notional: float = 500000.0
    max_auto_account_drawdown: float = 100000.0
    auto_heartbeat_max_age_seconds: int = 15
    auto_max_queue_depth: int = 20


@dataclass(frozen=True)
class BridgeConfig:
    data_dir: str
    host: str = "127.0.0.1"
    port: int = 17642
    default_mode: str = "OBSERVE_ONLY"
    max_message_bytes: int = 65536
    accounts: dict = field(default_factory=dict)
    risk_limits: dict = field(default_factory=dict)
    key_file: str = ""
    worker_token_file: str = ""

    def account(self, alias):
        account = self.accounts.get(alias)
        if not account or not account.enabled:
            raise BridgeError("ACCOUNT_NOT_FOUND", "unknown or disabled account alias")
        return account

    def risks(self, alias):
        return self.risk_limits.get(alias, RiskLimits())


def _absolute(base, value):
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join(base, value))


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BridgeError("CONFIG_ERROR", "%s must be a positive integer" % name)
    return value


def _bounded_int(value, name, minimum, maximum):
    parsed = _positive_int(value, name)
    if parsed < minimum or parsed > maximum:
        raise BridgeError(
            "CONFIG_ERROR", "%s must be between %d and %d" % (name, minimum, maximum)
        )
    return parsed


def _bounded_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError("CONFIG_ERROR", "%s must be a finite number" % name)
    parsed = float(value)
    if not math.isfinite(parsed):
        raise BridgeError("CONFIG_ERROR", "%s must be a finite number" % name)
    if parsed < minimum or parsed > maximum:
        raise BridgeError(
            "CONFIG_ERROR", "%s must be between %s and %s" % (name, minimum, maximum)
        )
    return parsed


def _strict_boolean(value, name):
    if not isinstance(value, bool):
        raise BridgeError("CONFIG_ERROR", "%s must be a boolean" % name)
    return value


def _nonempty_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("CONFIG_ERROR", "%s must be a non-empty string" % name)
    return value


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _risk_limits(raw, account_alias):
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_ERROR", "risk_limits must be an object")
    known = set(RiskLimits.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise BridgeError("CONFIG_ERROR", "unknown risk fields: %s" % sorted(unknown))
    defaults = RiskLimits()
    values = {}
    for name in known:
        value = raw.get(name, getattr(defaults, name))
        if name in RISK_INTEGER_RANGES:
            values[name] = _bounded_int(
                value, "%s.risk_limits.%s" % (account_alias, name),
                *RISK_INTEGER_RANGES[name]
            )
        else:
            values[name] = _bounded_number(
                value, "%s.risk_limits.%s" % (account_alias, name),
                *RISK_NUMBER_RANGES[name]
            )
    return RiskLimits(**values)


def _order_volume_config(item):
    raw = item.get("order_volume_rules", {})
    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_ERROR", "order_volume_rules must be an object")
    unknown = set(raw) - {"default", "prefix_overrides"}
    if unknown:
        raise BridgeError("CONFIG_ERROR", "unknown order_volume_rules fields: %s" % sorted(unknown))
    default = raw.get("default", {})
    if not isinstance(default, dict) or set(default) - {"min_buy", "min_sell"}:
        raise BridgeError("CONFIG_ERROR", "invalid default order volume rule")
    min_buy = _positive_int(default.get("min_buy", 100), "default min_buy")
    min_sell = _positive_int(default.get("min_sell", 100), "default min_sell")
    overrides_raw = raw.get("prefix_overrides", [{
        "prefixes": list(DEFAULT_SPECIAL_PREFIXES), "min_buy": 200, "min_sell": 200,
    }])
    if not isinstance(overrides_raw, list):
        raise BridgeError("CONFIG_ERROR", "prefix_overrides must be an array")
    overrides = []
    seen = set()
    for entry in overrides_raw:
        if not isinstance(entry, dict) or set(entry) != {"prefixes", "min_buy", "min_sell"}:
            raise BridgeError("CONFIG_ERROR", "invalid prefix order volume rule")
        prefixes = entry["prefixes"]
        if not isinstance(prefixes, list) or not prefixes:
            raise BridgeError("CONFIG_ERROR", "volume rule prefixes must be a non-empty array")
        normalized = []
        for prefix in prefixes:
            if not isinstance(prefix, str) or not prefix.isdigit() or len(prefix) > 6 or prefix in seen:
                raise BridgeError("CONFIG_ERROR", "invalid or duplicate volume rule prefix")
            seen.add(prefix)
            normalized.append(prefix)
        overrides.append(OrderVolumeRule(
            tuple(normalized),
            _positive_int(entry["min_buy"], "override min_buy"),
            _positive_int(entry["min_sell"], "override min_sell"),
        ))
    return min_buy, min_sell, tuple(overrides)


def _credit_actions(item, account_type):
    has_mapping = "credit_action_mapping" in item
    has_legacy_priority = "credit_action_priority" in item
    if account_type != "CREDIT":
        if has_mapping or has_legacy_priority:
            raise BridgeError("CONFIG_ERROR", "credit action configuration is only valid for CREDIT accounts")
        return "MARGIN_BUY", "COLLATERAL_SELL"
    if has_mapping and has_legacy_priority:
        raise BridgeError("CONFIG_ERROR", "use credit_action_mapping only; do not combine it with credit_action_priority")
    if has_mapping:
        raw = item["credit_action_mapping"]
        if not isinstance(raw, dict) or set(raw) != {"buy", "sell"}:
            raise BridgeError("CONFIG_ERROR", "credit_action_mapping must contain exactly buy and sell")
        buy = raw["buy"]
        sell = raw["sell"]
    elif has_legacy_priority:
        raw = item["credit_action_priority"]
        if not isinstance(raw, dict) or set(raw) - {"buy", "sell"}:
            raise BridgeError("CONFIG_ERROR", "invalid legacy credit_action_priority")
        buy_values = raw.get("buy", ["MARGIN_BUY"])
        sell_values = raw.get("sell", ["COLLATERAL_SELL"])
        if (not isinstance(buy_values, list) or not buy_values or
                any(not isinstance(value, str) for value in buy_values) or
                set(buy_values) - CREDIT_BUY_ACTIONS):
            raise BridgeError("CONFIG_ERROR", "legacy credit buy priority contains invalid actions")
        if (not isinstance(sell_values, list) or not sell_values or
                any(not isinstance(value, str) for value in sell_values) or
                set(sell_values) - CREDIT_SELL_ACTIONS):
            raise BridgeError("CONFIG_ERROR", "legacy credit sell priority contains invalid actions")
        # Backward compatibility is deliberately fail-closed: only the first
        # configured legacy action is used; later entries are never attempted.
        buy = buy_values[0]
        sell = sell_values[0]
    else:
        buy = "MARGIN_BUY"
        sell = "COLLATERAL_SELL"
    if not isinstance(buy, str) or buy not in CREDIT_BUY_ACTIONS:
        raise BridgeError("CONFIG_ERROR", "credit buy action must be MARGIN_BUY or COLLATERAL_BUY")
    if not isinstance(sell, str) or sell not in CREDIT_SELL_ACTIONS:
        raise BridgeError("CONFIG_ERROR", "credit sell action must be COLLATERAL_SELL or SHORT_SELL")
    return buy, sell


def load_config(path=None):
    path = path or os.environ.get("WORKBUDDY_QMT_CONFIG", "config/bridge.json")
    path = os.path.abspath(path)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = json.load(stream, parse_constant=_reject_json_constant)
    except (OSError, ValueError) as exc:
        raise BridgeError("CONFIG_ERROR", "cannot load bridge config: %s" % exc)

    if not isinstance(raw, dict):
        raise BridgeError("CONFIG_ERROR", "bridge config must be a JSON object")
    unknown = set(raw) - TOP_LEVEL_FIELDS
    if unknown:
        raise BridgeError("CONFIG_ERROR", "unknown bridge config fields: %s" % sorted(unknown))
    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise BridgeError("CONFIG_ERROR", "accounts must be a non-empty array")

    base = os.path.dirname(path)
    data_dir = _absolute(base, _nonempty_string(raw.get("data_dir", "../data"), "data_dir"))
    mode = raw.get("default_mode", "OBSERVE_ONLY")
    try:
        mode = normalize_mode(mode, allow_legacy=True)
    except ValueError:
        raise BridgeError("CONFIG_ERROR", "invalid default_mode")

    host = _nonempty_string(raw.get("host", "127.0.0.1"), "host")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("not loopback")
    except ValueError:
        raise BridgeError("CONFIG_ERROR", "host must be a numeric loopback address")
    port = _bounded_int(raw.get("port", 17642), "port", 1, 65535)
    max_message_bytes = _bounded_int(
        raw.get("max_message_bytes", 65536), "max_message_bytes", 1024, 16777216
    )
    key_file = _absolute(base, _nonempty_string(
        raw.get("key_file", "../data/secrets/message_keys.json"), "key_file"
    ))
    worker_token_file = _absolute(base, _nonempty_string(
        raw.get("worker_token_file", "../data/secrets/worker.token"), "worker_token_file"
    ))

    accounts = {}
    risks = {}
    adapter_instances = set()
    for item in accounts_raw:
        if not isinstance(item, dict):
            raise BridgeError("CONFIG_ERROR", "each account must be an object")
        unknown = set(item) - ACCOUNT_FIELDS
        if unknown:
            raise BridgeError("CONFIG_ERROR", "unknown account fields: %s" % sorted(unknown))
        alias = _nonempty_string(item.get("alias"), "account alias")
        adapter_instance = _nonempty_string(item.get("adapter_instance"), "adapter_instance")
        if not SAFE_NAME_RE.fullmatch(alias) or alias in {".", ".."}:
            raise BridgeError("CONFIG_ERROR", "account alias contains unsafe characters")
        if not SAFE_NAME_RE.fullmatch(adapter_instance) or adapter_instance in {".", ".."}:
            raise BridgeError("CONFIG_ERROR", "adapter_instance contains unsafe characters")
        account_type = item.get("account_type")
        if account_type not in ACCOUNT_TYPES:
            raise BridgeError("CONFIG_ERROR", "invalid account_type")
        min_buy, min_sell, volume_overrides = _order_volume_config(item)
        credit_buy, credit_sell = _credit_actions(item, account_type)
        lot_size = _positive_int(item.get("lot_size", 100), "lot_size")
        allowlist = item.get("instrument_allowlist", [])
        if not isinstance(allowlist, list) or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}(\.(SH|SZ|BJ))?", value.upper())
                for value in allowlist):
            raise BridgeError("CONFIG_ERROR", "instrument_allowlist must contain valid stock symbols")
        normalized_allowlist = tuple(normalize_symbol(value) for value in allowlist)
        if len(set(normalized_allowlist)) != len(normalized_allowlist):
            raise BridgeError("CONFIG_ERROR", "instrument_allowlist contains duplicate symbols")
        account = AccountConfig(
            alias=alias,
            account_type=account_type,
            adapter_instance=adapter_instance,
            enabled=_strict_boolean(item.get("enabled", True), "enabled"),
            lot_size=lot_size,
            odd_lot_sell_allowed=_strict_boolean(
                item.get("odd_lot_sell_allowed", True), "odd_lot_sell_allowed"
            ),
            instrument_allowlist=normalized_allowlist,
            default_min_buy_volume=min_buy,
            default_min_sell_volume=min_sell,
            order_volume_overrides=volume_overrides,
            credit_buy_action=credit_buy,
            credit_sell_action=credit_sell,
            limited_auto_credit_enabled=_strict_boolean(
                item.get("limited_auto_credit_enabled", False),
                "limited_auto_credit_enabled",
            ),
        )
        if account.alias in accounts:
            raise BridgeError("CONFIG_ERROR", "duplicate account alias")
        if account.adapter_instance in adapter_instances:
            raise BridgeError("CONFIG_ERROR", "duplicate adapter_instance")
        accounts[account.alias] = account
        adapter_instances.add(account.adapter_instance)
        risks[account.alias] = _risk_limits(item.get("risk_limits", {}), account.alias)

    if not accounts:
        raise BridgeError("CONFIG_ERROR", "at least one account is required")
    return BridgeConfig(
        data_dir=data_dir,
        host=host,
        port=port,
        default_mode=mode,
        max_message_bytes=max_message_bytes,
        accounts=accounts,
        risk_limits=risks,
        key_file=key_file,
        worker_token_file=worker_token_file,
    )
