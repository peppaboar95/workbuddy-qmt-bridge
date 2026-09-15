#coding:gbk
"""QMT embedded Python adapter. This file intentionally uses stdlib only.

Copy it into the QMT strategy editor, update ADAPTER_CONFIG_PATH, and run one
strategy instance per account alias. The default configuration is fail-closed.
No xtquant import is used or allowed here.
"""

import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import time
import uuid

ADAPTER_CONFIG_PATH = r"D:\workbuddy-qmt-bridge\config\qmt_adapter.json"

_CONFIG = None
_CONFIG_SOURCE = None
_MODE_CONFIG_STAMP = None
_MODE_RELOAD_STATUS = None
_MODE_RELOAD_ERROR = None
_KEYS = None
_ACTIVE_KEY = None
_CONTEXT = None
_STATUS = "STARTING"
_LAST_SCAN = None
_LAST_ORDER_CALLBACK = None
_LAST_DEAL_CALLBACK = None
_LAST_SNAPSHOT = None
_CREDIT_QUERY_IN_FLIGHT = None
_LAST_CREDIT_QUERY_AT = 0.0
_LAST_CONSOLE_HEARTBEAT_AT = 0.0
_LAST_CONSOLE_STATUS = None
_LAST_CONSOLE_LOCAL_HALT = None
_LAST_ORDER_DEAL_RECONCILE_AT = 0.0
_ORDER_DEAL_RECONCILE_REQUESTED = False
_SCHEDULE_METHODS = {}
_P0_PROBE_ATTEMPTS = 0
_P0_PROBE_COMPLETE = False


def _config_int(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(name + " must be an integer")
    if value < minimum or value > maximum:
        raise RuntimeError("%s must be between %d and %d" % (name, minimum, maximum))
    return value


def _config_number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(name + " must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(name + " must be a finite number")
    if parsed < minimum or parsed > maximum:
        raise RuntimeError("%s must be between %s and %s" % (name, minimum, maximum))
    return parsed


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value=None):
    return (value or _now()).isoformat(timespec="milliseconds")


def _parse_time(value):
    text = value.replace("Z", "+0000")
    if len(text) >= 6 and text[-3] == ":" and text[-6] in "+-":
        text = text[:-3] + text[-2:]
    formats = ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
    for pattern in formats:
        try:
            return datetime.datetime.strptime(text, pattern).astimezone(datetime.timezone.utc)
        except ValueError:
            pass
    raise ValueError("invalid timestamp")


def _time_ns():
    # time.time_ns was added after the Python 3.6 runtime still used by many
    # QMT builds. UUID keeps collisions impossible within the same clock tick.
    return int(time.time() * 1000000000)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _atomic_json(path, value):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temp = path + ".tmp." + uuid.uuid4().hex
    stream = open(temp, "wb")
    try:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    finally:
        stream.close()
    os.replace(temp, path)


def _load_json(path):
    stream = open(path, "r", encoding="utf-8")
    try:
        return json.load(
            stream,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number is forbidden: " + value)
            ),
        )
    finally:
        stream.close()


def _load_config():
    global _CONFIG, _KEYS, _ACTIVE_KEY, _CONFIG_SOURCE
    global _MODE_CONFIG_STAMP, _MODE_RELOAD_STATUS, _MODE_RELOAD_ERROR
    config = _load_json(ADAPTER_CONFIG_PATH)
    source = dict(config)
    source.pop("qmt_mode", None)
    required = [
        "account_alias", "account_type", "adapter_instance", "qmt_account_id",
        "data_dir", "key_file", "mapping_profile", "qmt_mode", "strategy_name",
        "expected_profile_id", "expected_qmt_build", "expected_broker_build",
    ]
    for name in required:
        if name not in config:
            raise RuntimeError("missing adapter config field: " + name)
    for name in required:
        if name != "qmt_mode" and (not isinstance(config[name], str) or not config[name].strip()):
            raise RuntimeError("adapter config field must be a non-empty string: " + name)
    if config["account_type"] not in ("STOCK", "CREDIT"):
        raise RuntimeError("invalid account_type")
    if config["qmt_mode"] not in ("OBSERVE_ONLY", "SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO"):
        raise RuntimeError("invalid qmt_mode")
    config["console_heartbeat_seconds"] = _config_int(
        config.get("console_heartbeat_seconds", 30), "console_heartbeat_seconds", 5, 3600
    )
    config["price_guard_max_quote_age_seconds"] = _config_int(
        config.get("price_guard_max_quote_age_seconds", 30),
        "price_guard_max_quote_age_seconds", 1, 60,
    )
    config["max_batch"] = _config_int(config.get("max_batch", 10), "max_batch", 1, 100)
    config["command_poll_interval_ms"] = _config_int(
        config.get("command_poll_interval_ms", 500),
        "command_poll_interval_ms", 100, 5000,
    )
    config["order_deal_reconcile_seconds"] = _config_int(
        config.get("order_deal_reconcile_seconds", 30),
        "order_deal_reconcile_seconds", 10, 3600,
    )
    config["max_message_bytes"] = _config_int(
        config.get("max_message_bytes", 65536), "max_message_bytes", 1024, 16777216
    )
    default_adapter_notional = 100000 if config["account_type"] == "CREDIT" else 200000
    config["adapter_max_volume"] = _config_int(
        config.get("adapter_max_volume", 500000), "adapter_max_volume", 1, 2147483647
    )
    config["adapter_max_notional"] = _config_number(
        config.get("adapter_max_notional", default_adapter_notional),
        "adapter_max_notional", 0.000001, 1e15,
    )
    config["adapter_max_auto_session_notional"] = _config_number(
        config.get("adapter_max_auto_session_notional", 1000000),
        "adapter_max_auto_session_notional", 0.000001, 1e15,
    )
    config["adapter_max_auto_orders"] = _config_int(
        config.get("adapter_max_auto_orders", 1000),
        "adapter_max_auto_orders", 1, 100000,
    )
    config["adapter_min_auto_order_interval_seconds"] = _config_int(
        config.get("adapter_min_auto_order_interval_seconds", 1),
        "adapter_min_auto_order_interval_seconds", 1, 3600,
    )
    config["adapter_max_auto_concurrent_orders"] = _config_int(
        config.get("adapter_max_auto_concurrent_orders", 20),
        "adapter_max_auto_concurrent_orders", 1, 1000,
    )
    config["adapter_max_auto_symbol_position_notional"] = _config_number(
        config.get("adapter_max_auto_symbol_position_notional", 500000),
        "adapter_max_auto_symbol_position_notional", 0.000001, 1e15,
    )
    config["adapter_max_auto_account_drawdown"] = _config_number(
        config.get("adapter_max_auto_account_drawdown", 100000),
        "adapter_max_auto_account_drawdown", 0.000001, 1e15,
    )
    if not isinstance(config.get("p0_probe_enabled", False), bool):
        raise RuntimeError("p0_probe_enabled must be a boolean")
    config["p0_probe_enabled"] = config.get("p0_probe_enabled", False)
    if not isinstance(config.get("allow_cancel_while_halted", True), bool):
        raise RuntimeError("allow_cancel_while_halted must be a boolean")
    if not isinstance(config.get("limited_auto_credit_enabled", False), bool):
        raise RuntimeError("limited_auto_credit_enabled must be a boolean")
    config["limited_auto_credit_enabled"] = config.get("limited_auto_credit_enabled", False)
    for name in ("order_status_map", "field_map"):
        if not isinstance(config.get(name, {}), dict):
            raise RuntimeError(name + " must be an object")
    if config["account_type"] == "CREDIT":
        config["credit_query_cooldown_seconds"] = _config_int(
            config.get("credit_query_cooldown_seconds", 180),
            "credit_query_cooldown_seconds", 1, 86400,
        )
        config["credit_snapshot_max_age_seconds"] = _config_int(
            config.get("credit_snapshot_max_age_seconds", 600),
            "credit_snapshot_max_age_seconds", 1, 86400,
        )
        config["minimum_maintenance_ratio"] = _config_number(
            config.get("minimum_maintenance_ratio", 1.5),
            "minimum_maintenance_ratio", 0.000001, 100.0,
        )
        for name in ("credit_query_wrapper_verified", "credit_final_check_verified"):
            if not isinstance(config.get(name, False), bool):
                raise RuntimeError(name + " must be a boolean")
        for name in ("eligibility_allowed_values", "credit_field_map"):
            if not isinstance(config.get(name, {}), dict):
                raise RuntimeError(name + " must be an object")
    keys = _load_json(config["key_file"])
    decoded = {}
    for key_id, value in keys["keys"].items():
        decoded[key_id] = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if keys["active_key_id"] not in decoded:
        raise RuntimeError("active message key is missing")
    _CONFIG = config
    _KEYS = decoded
    _ACTIVE_KEY = keys["active_key_id"]
    _ensure_dirs()
    if config["qmt_mode"] != "OBSERVE_ONLY":
        _load_verified_profile()
    _CONFIG_SOURCE = source
    _MODE_CONFIG_STAMP = None
    _MODE_RELOAD_STATUS = None
    _MODE_RELOAD_ERROR = None


def _refresh_config_mode():
    global _MODE_CONFIG_STAMP, _MODE_RELOAD_STATUS, _MODE_RELOAD_ERROR, _STATUS
    try:
        info = os.stat(ADAPTER_CONFIG_PATH)
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == _MODE_CONFIG_STAMP and _MODE_RELOAD_ERROR is None:
            return True
        config = _load_json(ADAPTER_CONFIG_PATH)
        mode = config.get("qmt_mode")
        if mode not in ("OBSERVE_ONLY", "SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO"):
            raise RuntimeError("invalid qmt_mode")
        source = dict(config)
        source.pop("qmt_mode", None)
        if source != _CONFIG_SOURCE:
            raise RuntimeError("adapter settings changed; restart the QMT strategy")
        if mode != "OBSERVE_ONLY":
            _load_verified_profile()
        _CONFIG["qmt_mode"] = mode
        _MODE_CONFIG_STAMP = stamp
        if _MODE_RELOAD_STATUS is not None:
            _STATUS = _MODE_RELOAD_STATUS
        _MODE_RELOAD_STATUS = None
        _MODE_RELOAD_ERROR = None
        return True
    except Exception as exc:
        if _MODE_RELOAD_STATUS is None:
            _MODE_RELOAD_STATUS = _STATUS
        _STATUS = "ERROR"
        _CONFIG["qmt_mode"] = "OBSERVE_ONLY"
        error = str(exc)[:200]
        if error != _MODE_RELOAD_ERROR:
            _MODE_RELOAD_ERROR = error
            _write_event({
                "type": "ERROR_EVENT", "error_code": "ADAPTER_MODE_SYNC_FAILED",
                "error_message": error,
            })
        return False


def _partition():
    return os.path.join(_CONFIG["data_dir"], "queue", _CONFIG["adapter_instance"])


def _runtime():
    return os.path.join(_CONFIG["data_dir"], "qmt_runtime", _CONFIG["adapter_instance"])


def _ensure_dirs():
    for name in ("commands", "command_acks", "events", "control", "control_acks", "archive", "dead_letter"):
        path = os.path.join(_partition(), name)
        if not os.path.isdir(path):
            os.makedirs(path)
    journal = os.path.join(_runtime(), "execution_journal")
    heartbeat = os.path.join(_runtime(), "heartbeat")
    if not os.path.isdir(journal):
        os.makedirs(journal)
    if not os.path.isdir(heartbeat):
        os.makedirs(heartbeat)


def _signature(message):
    unsigned = dict(message)
    unsigned.pop("signature", None)
    key_id = unsigned.get("key_id")
    key = _KEYS.get(key_id)
    if key is None:
        raise RuntimeError("unknown key_id")
    digest = hmac.new(key, _canonical(unsigned), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _verify(message, allowed_message_types):
    required = set([
        "protocol_version", "message_id", "correlation_id", "message_type",
        "issued_at", "expires_at", "sender", "key_id", "payload", "signature",
    ])
    if set(message.keys()) != required:
        raise RuntimeError("invalid envelope fields")
    if message["protocol_version"] != "1.0" or message["message_type"] not in allowed_message_types:
        raise RuntimeError("invalid protocol or message type")
    if not hmac.compare_digest(_signature(message), message["signature"]):
        raise RuntimeError("invalid signature")
    if _parse_time(message["expires_at"]) <= _now():
        raise RuntimeError("expired message")
    if _parse_time(message["issued_at"]) > _now() + datetime.timedelta(seconds=5):
        raise RuntimeError("clock skew")


def _envelope(payload):
    issued = _now()
    message = {
        "protocol_version": "1.0",
        "message_id": "msg_" + uuid.uuid4().hex,
        "correlation_id": payload.get("intent_id") or "corr_" + uuid.uuid4().hex,
        "message_type": "QMT_EVENT",
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + datetime.timedelta(minutes=5)),
        "sender": "qmt-adapter-" + _CONFIG["adapter_instance"],
        "key_id": _ACTIVE_KEY,
        "payload": payload,
    }
    message["signature"] = _signature(message)
    return message


def _write_event(payload, folder="events"):
    payload = dict(payload)
    payload.setdefault("event_id", "evt_" + uuid.uuid4().hex)
    payload.setdefault("account_alias", _CONFIG["account_alias"])
    payload.setdefault("account_type", _CONFIG["account_type"])
    payload.setdefault("occurred_at", _iso())
    message = _envelope(payload)
    name = "%020d_%s.json" % (_time_ns(), message["message_id"])
    _atomic_json(os.path.join(_partition(), folder, name), message)
    return message["message_id"]


def _dead_letter(path):
    target = os.path.join(_partition(), "dead_letter", os.path.basename(path) + ".invalid")
    if os.path.exists(target):
        target += "." + str(_time_ns())
    os.replace(path, target)


def _archive(path):
    target = os.path.join(_partition(), "archive", os.path.basename(path) + ".processed")
    if os.path.exists(target):
        target += "." + str(_time_ns())
    os.replace(path, target)


def _journal_path(client_key):
    safe = "".join(c for c in client_key if c.isalnum() or c in "_-")
    return os.path.join(_runtime(), "execution_journal", safe + ".json")


def _write_journal(command, state, details=None):
    value = {
        "client_order_key": command["client_order_key"],
        "intent_id": command["intent_id"],
        "state": state,
        "created_at": _iso(),
        "details": details or {},
        "auto_permit": command.get("auto_permit"),
        "order_notional": (
            float(command.get("resolved_order", {}).get("limit_price", 0) or 0) *
            int(command.get("resolved_order", {}).get("volume", 0) or 0)
        ),
    }
    _atomic_json(_journal_path(command["client_order_key"]), value)


def _ack(command, status, message_id=None, details=None):
    _write_event({
        "type": "COMMAND_ACK", "message_id": message_id,
        "intent_id": command.get("intent_id"),
        "client_order_key": command.get("client_order_key"),
        "status": status, "details": details or {},
    }, "command_acks")


def _request_order_deal_reconcile():
    global _ORDER_DEAL_RECONCILE_REQUESTED
    _ORDER_DEAL_RECONCILE_REQUESTED = True


def _mark_order_deal_reconciled():
    global _LAST_ORDER_DEAL_RECONCILE_AT, _ORDER_DEAL_RECONCILE_REQUESTED
    _LAST_ORDER_DEAL_RECONCILE_AT = time.time()
    _ORDER_DEAL_RECONCILE_REQUESTED = False


def _local_authorized(mode, command=None):
    if mode not in ("OBSERVE_ONLY", "SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO"):
        return None
    if _CONFIG.get("qmt_mode") != mode:
        return None
    if mode == "OBSERVE_ONLY":
        return {}
    if mode == "SIM_SIGNAL":
        return {}
    path = os.path.join(_runtime(), "local_authorization.json")
    if not os.path.exists(path):
        return None
    try:
        message = _load_json(path)
        _verify(message, set(["LOCAL_AUTHORIZATION"]))
        payload = message["payload"]
        basic = (payload.get("account_alias") == _CONFIG["account_alias"] and
                 mode in payload.get("allowed_modes", []) and
                 _parse_time(payload["live_until"]) > _now())
        if not basic:
            return None
        if mode == "LIMITED_AUTO":
            return {"auto_policy": _validate_auto_authorization(payload, command)}
        return {}
    except Exception:
        return None


def _auto_schedule_active(policy):
    china_timezone = datetime.timezone(datetime.timedelta(hours=8))
    local_now = _now().astimezone(china_timezone)
    if local_now.strftime("%Y%m%d") != policy.get("trading_day"):
        return False
    minute = local_now.hour * 60 + local_now.minute
    for window in policy.get("trading_windows", []):
        try:
            start_hour, start_minute = [int(item) for item in window["start"].split(":")]
            end_hour, end_minute = [int(item) for item in window["end"].split(":")]
        except Exception:
            return False
        if start_hour * 60 + start_minute <= minute < end_hour * 60 + end_minute:
            return True
    return False


def _auto_journal_usage(permit_id):
    directory = os.path.join(_runtime(), "execution_journal")
    count = 0
    notional = 0.0
    last_created = None
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        value = _load_json(os.path.join(directory, name))
        permit = value.get("auto_permit") or {}
        if permit.get("permit_id") != permit_id:
            continue
        count += 1
        current_notional = value.get("order_notional")
        if isinstance(current_notional, bool) or not isinstance(current_notional, (int, float)):
            raise RuntimeError("invalid limited-auto execution journal")
        notional += float(current_notional)
        created = value.get("created_at")
        if created and (last_created is None or _parse_time(created) > _parse_time(last_created)):
            last_created = created
    return {"order_count": count, "notional": notional, "last_created_at": last_created}


def _auto_pause_path():
    return os.path.join(_runtime(), "limited_auto_pause.json")


def _pause_auto_locally(command, reason):
    permit = command.get("auto_permit") or {}
    if command.get("execution_mode") != "LIMITED_AUTO" or not permit.get("permit_id"):
        return
    _atomic_json(_auto_pause_path(), {
        "paused": True,
        "permit_id": permit.get("permit_id"),
        "generation": permit.get("generation"),
        "reason": str(reason)[:200],
        "paused_at": _iso(),
    })


def _active_auto_pause(command=None):
    path = _auto_pause_path()
    if not os.path.exists(path):
        return None
    pause = _load_json(path)
    if pause.get("paused") is not True:
        return None
    if command is not None:
        permit = command.get("auto_permit") or {}
    else:
        authorization_path = os.path.join(_runtime(), "local_authorization.json")
        if not os.path.exists(authorization_path):
            return pause
        authorization = _load_json(authorization_path)
        _verify(authorization, set(["LOCAL_AUTHORIZATION"]))
        permit = authorization.get("payload", {}).get("auto_permit") or {}
    if (
        pause.get("permit_id") == permit.get("permit_id") and
        pause.get("generation") == permit.get("generation")
    ):
        return pause
    return None


def _validate_auto_authorization(payload, command):
    if payload.get("authorization_type") != "LIMITED_AUTO_PERMIT":
        raise RuntimeError("limited-auto authorization type mismatch")
    if not isinstance(command, dict) or command.get("execution_mode") != "LIMITED_AUTO":
        raise RuntimeError("limited-auto command is missing")
    authorization = payload.get("auto_permit")
    command_permit = command.get("auto_permit")
    if not isinstance(authorization, dict) or not isinstance(command_permit, dict):
        raise RuntimeError("limited-auto permit binding is missing")
    if set(command_permit.keys()) != set(["permit_id", "policy_hash", "generation"]):
        raise RuntimeError("limited-auto command binding is invalid")
    policy = authorization.get("policy")
    if not isinstance(policy, dict):
        raise RuntimeError("limited-auto policy is missing")
    policy_hash = hashlib.sha256(_canonical(policy)).hexdigest()
    if policy_hash != authorization.get("policy_hash") or policy_hash != command_permit.get("policy_hash"):
        raise RuntimeError("limited-auto policy hash mismatch")
    if (
        authorization.get("permit_id") != command_permit.get("permit_id") or
        authorization.get("generation") != command_permit.get("generation") or
        policy.get("permit_id") != command_permit.get("permit_id") or
        policy.get("generation") != command_permit.get("generation")
    ):
        raise RuntimeError("limited-auto permit generation mismatch")
    if _active_auto_pause(command) is not None:
        raise RuntimeError("limited-auto is locally paused")
    if policy.get("account_alias") != _CONFIG["account_alias"] or policy.get("account_type") != _CONFIG["account_type"]:
        raise RuntimeError("limited-auto policy account mismatch")
    if (
        int(policy.get("max_order_volume", 0)) > int(_CONFIG.get("adapter_max_volume", 0)) or
        float(policy.get("max_order_notional", 0)) > float(_CONFIG.get("adapter_max_notional", 0)) or
        float(policy.get("max_session_notional", 0)) > float(_CONFIG.get("adapter_max_auto_session_notional", 0)) or
        int(policy.get("max_orders", 0)) > int(_CONFIG.get("adapter_max_auto_orders", 0)) or
        int(policy.get("min_order_interval_seconds", 0)) < int(_CONFIG.get("adapter_min_auto_order_interval_seconds", 1)) or
        int(policy.get("max_concurrent_orders", 0)) > int(_CONFIG.get("adapter_max_auto_concurrent_orders", 0)) or
        float(policy.get("max_symbol_position_notional", 0)) > float(_CONFIG.get("adapter_max_auto_symbol_position_notional", 0)) or
        float(policy.get("max_account_drawdown", 0)) > float(_CONFIG.get("adapter_max_auto_account_drawdown", 0))
    ):
        raise RuntimeError("limited-auto policy exceeds adapter hard limits")
    if _parse_time(policy["starts_at"]) > _now() or _parse_time(policy["expires_at"]) <= _now():
        raise RuntimeError("limited-auto policy is outside its lifetime")
    if policy.get("expires_at") != payload.get("live_until"):
        raise RuntimeError("limited-auto authorization lifetime mismatch")
    if not _auto_schedule_active(policy):
        raise RuntimeError("limited-auto order is outside the trading window")
    if command.get("qmt_symbol") not in policy.get("allowed_symbols", []):
        raise RuntimeError("limited-auto symbol is not allowed")
    if command.get("action") not in policy.get("allowed_actions", []):
        raise RuntimeError("limited-auto action is not allowed")
    source = command.get("source") or {}
    for name in ("type", "rule_set_id", "rule_version"):
        if source.get(name) != policy.get("source", {}).get(name):
            raise RuntimeError("limited-auto strategy binding mismatch")
    order = command["resolved_order"]
    volume = int(order["volume"])
    notional = volume * float(order["limit_price"])
    if volume > int(policy.get("max_order_volume", 0)):
        raise RuntimeError("limited-auto order volume exceeded")
    if notional > float(policy.get("max_order_notional", 0)):
        raise RuntimeError("limited-auto order notional exceeded")
    if _CONFIG["account_type"] == "CREDIT":
        if not _CONFIG.get("limited_auto_credit_enabled", False):
            raise RuntimeError("limited-auto credit trading is disabled")
        if command.get("action") in ("MARGIN_BUY", "SHORT_SELL") and not policy.get("allow_credit_new_debt", False):
            raise RuntimeError("limited-auto new credit debt is not allowed")
    usage = _auto_journal_usage(command_permit["permit_id"])
    if usage["order_count"] + 1 > int(policy.get("max_orders", 0)):
        raise RuntimeError("limited-auto order count exceeded")
    if usage["notional"] + notional > float(policy.get("max_session_notional", 0)):
        raise RuntimeError("limited-auto session notional exceeded")
    if usage["last_created_at"]:
        elapsed = (_now() - _parse_time(usage["last_created_at"])).total_seconds()
        if elapsed < int(policy.get("min_order_interval_seconds", 0)):
            raise RuntimeError("limited-auto order rate exceeded")
    return policy


def _local_halted():
    path = os.path.join(_runtime(), "local_halt.json")
    if not os.path.exists(path):
        return False
    try:
        message = _load_json(path)
        # Halt is intentionally durable, so only its signature is checked here.
        if not hmac.compare_digest(_signature(message), message.get("signature", "")):
            return True
        return bool(message.get("payload", {}).get("halted", True))
    except Exception:
        return True


def _load_verified_profile():
    profile = _load_json(_CONFIG["mapping_profile"])
    required = set([
        "protocol_version", "profile_id", "qmt_build", "broker_build",
        "account_type", "strategy_name", "adapter_binding", "verified",
        "key_id", "mappings", "signature",
    ])
    if not isinstance(profile, dict) or set(profile.keys()) != required:
        raise RuntimeError("mapping profile fields do not match the bound-profile schema")
    supplied = profile.get("signature")
    if not supplied or not hmac.compare_digest(_signature(profile), supplied):
        raise RuntimeError("mapping profile signature is invalid")
    if profile.get("protocol_version") != "1.0" or profile.get("verified") is not True:
        raise RuntimeError("mapping profile is not verified")
    if profile.get("account_type") != _CONFIG["account_type"]:
        raise RuntimeError("mapping profile account type mismatch")
    if profile.get("strategy_name") != _CONFIG["strategy_name"]:
        raise RuntimeError("mapping profile strategy name mismatch")
    expected_fields = {
        "profile_id": "expected_profile_id",
        "qmt_build": "expected_qmt_build",
        "broker_build": "expected_broker_build",
    }
    for profile_name, config_name in expected_fields.items():
        expected = _CONFIG.get(config_name)
        if (not isinstance(expected, str) or not expected.strip() or
                "REPLACE" in expected.upper() or profile.get(profile_name) != expected):
            raise RuntimeError("mapping profile %s does not match adapter binding" % profile_name)
    binding = {
        "account_alias": _CONFIG["account_alias"],
        "account_type": _CONFIG["account_type"],
        "adapter_instance": _CONFIG["adapter_instance"],
        "qmt_account_id": _CONFIG["qmt_account_id"],
        "strategy_name": _CONFIG["strategy_name"],
    }
    if profile.get("adapter_binding") != binding:
        raise RuntimeError("mapping profile adapter identity mismatch")
    if not isinstance(profile.get("mappings"), dict) or not profile["mappings"]:
        raise RuntimeError("mapping profile mappings are empty")
    return profile


def _mapping_entry(profile, key):
    mapping = profile.get("mappings", {}).get(key)
    if not isinstance(mapping, dict) or set(mapping.keys()) != set(["op_type", "order_type", "price_type"]):
        raise RuntimeError("unsupported QMT enum mapping")
    for name in ("op_type", "order_type", "price_type"):
        if isinstance(mapping.get(name), bool) or not isinstance(mapping.get(name), int):
            raise RuntimeError("invalid QMT enum mapping")
    return mapping


def _mapping_for(command):
    profile = _load_verified_profile()
    key = "%s:%s:%s:%s" % (
        command["account_type"], command["asset_type"], command["action"],
        command["resolved_order"]["price_type"],
    )
    mapping = _mapping_entry(profile, key)
    return profile, mapping


def _validate_execute(command):
    allowed = set([
        "type", "intent_id", "intent_revision", "client_order_key", "account_alias",
        "account_type", "asset_type", "qmt_symbol", "action", "semantic_action", "resolved_order",
        "execution_mode", "risk_decision_id", "source_signal_id", "source", "credit",
        "auto_permit",
    ])
    if set(command.keys()) != allowed:
        raise RuntimeError("invalid execute command fields")
    if command["account_alias"] != _CONFIG["account_alias"] or command["account_type"] != _CONFIG["account_type"]:
        raise RuntimeError("account partition mismatch")
    if command["asset_type"] != "STOCK" or command["resolved_order"].get("price_type") != "LIMIT":
        raise RuntimeError("unsupported asset or price type")
    volume = command["resolved_order"].get("volume")
    price = command["resolved_order"].get("limit_price")
    if isinstance(volume, bool) or not isinstance(volume, int) or volume <= 0:
        raise RuntimeError("zero or invalid order volume is forbidden")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(float(price)) or price <= 0:
        raise RuntimeError("invalid price")
    volume_rule = command["resolved_order"].get("volume_rule")
    if not isinstance(volume_rule, dict):
        raise RuntimeError("missing signed order volume rule")
    minimum = volume_rule.get("minimum_volume")
    if not isinstance(minimum, int) or minimum <= 0:
        raise RuntimeError("invalid minimum order volume")
    if volume < minimum:
        if command["action"] in ("BUY", "COLLATERAL_BUY", "MARGIN_BUY", "BUY_TO_REPAY"):
            raise RuntimeError("minimum buy volume not met")
        if not volume_rule.get("odd_lot_liquidation", False):
            raise RuntimeError("minimum sell volume not met")
    if volume > int(_CONFIG.get("adapter_max_volume", 500000)):
        raise RuntimeError("adapter volume limit exceeded")
    default_adapter_notional = 100000 if _CONFIG.get("account_type") == "CREDIT" else 200000
    if volume * price > float(_CONFIG.get("adapter_max_notional", default_adapter_notional)):
        raise RuntimeError("adapter notional limit exceeded")
    if _local_halted():
        raise RuntimeError("adapter is locally halted")
    authorization = _local_authorized(command["execution_mode"], command)
    if authorization is None:
        raise RuntimeError("execution mode is not locally authorized")
    if command["execution_mode"] != "OBSERVE_ONLY":
        _validate_live_price(command)
        _final_account_risk(command, authorization.get("auto_policy"))


def _same_symbol(left, right):
    return str(left).split(".")[0] == str(right).split(".")[0]


def _positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 and math.isfinite(parsed) else None


def _first_book_price(value):
    if isinstance(value, (list, tuple)):
        return _positive_number(value[0]) if value else None
    return _positive_number(value)


def _tick_datetime(tick):
    raw = _pick(tick, ["time", "timetag", "stime"])
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        stamp = float(raw)
        if stamp > 100000000000:
            stamp /= 1000.0
        if stamp > 1000000000:
            return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
    if raw not in (None, ""):
        text = str(raw).strip()
        for pattern in (
            "%Y%m%d %H:%M:%S.%f", "%Y%m%d %H:%M:%S", "%Y%m%d%H%M%S",
            "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
        ):
            try:
                local_value = datetime.datetime.strptime(text, pattern)
                return local_value.astimezone().astimezone(datetime.timezone.utc)
            except ValueError:
                pass
    raise RuntimeError("QMT quote timestamp is unavailable")


def _trading_phase(now=None):
    local_now = (now or datetime.datetime.now()).astimezone()
    if local_now.weekday() >= 5:
        return "CLOSED"
    value = local_now.time()
    if datetime.time(9, 30) <= value < datetime.time(11, 30) or datetime.time(13, 0) <= value < datetime.time(14, 57):
        return "CONTINUOUS_AUCTION"
    if datetime.time(9, 15) <= value <= datetime.time(9, 25):
        return "OPEN_CALL_AUCTION"
    if datetime.time(14, 57) <= value <= datetime.time(15, 0):
        return "CLOSE_CALL_AUCTION"
    return "CLOSED"


def _round_half_up_tick(value, tick):
    return round(math.floor(value / tick + 0.5 + 1e-10) * tick, 6)


def _dynamic_price_cage(symbol, bid_price, ask_price, last_price, previous_close, tick, phase):
    if phase != "CONTINUOUS_AUCTION":
        return {"applied": False, "rule": "NOT_CONTINUOUS_AUCTION", "buy_upper": None, "sell_lower": None}
    buy_base = ask_price or bid_price or last_price or previous_close
    sell_base = bid_price or ask_price or last_price or previous_close
    if buy_base is None or sell_base is None:
        raise RuntimeError("dynamic price-cage benchmark is unavailable")
    code = str(symbol).split(".")[0]
    market = str(symbol).split(".")[-1].upper() if "." in str(symbol) else ""
    if market == "BJ":
        ratio = 0.05
        widened_by_ticks = True
        rule = "BSE_5_PERCENT_OR_10_TICKS"
    elif market == "SH" and code.startswith(("688", "689")):
        ratio = 0.02
        widened_by_ticks = False
        rule = "SSE_STAR_2_PERCENT"
    else:
        ratio = 0.02
        widened_by_ticks = True
        rule = "SSE_SZSE_2_PERCENT_OR_10_TICKS"
    buy_value = buy_base * (1 + ratio)
    sell_value = sell_base * (1 - ratio)
    if widened_by_ticks:
        buy_value = max(buy_value, buy_base + 10 * tick)
        sell_value = min(sell_value, sell_base - 10 * tick)
    return {
        "applied": True, "rule": rule,
        "buy_benchmark": round(buy_base, 6), "sell_benchmark": round(sell_base, 6),
        "buy_upper": _round_half_up_tick(buy_value, tick),
        "sell_lower": _round_half_up_tick(sell_value, tick),
    }


def _market_quote(symbol):
    if _CONTEXT is None:
        raise RuntimeError("QMT context is unavailable for price guard")
    full_tick = getattr(_CONTEXT, "get_full_tick", None)
    instrument_detail = getattr(_CONTEXT, "get_instrumentdetail", None)
    if not callable(full_tick) or not callable(instrument_detail):
        raise RuntimeError("QMT quote or instrument-detail API is unavailable")
    ticks = full_tick([symbol])
    tick_data = ticks.get(symbol) if isinstance(ticks, dict) else None
    if tick_data is None and isinstance(ticks, dict):
        tick_data = ticks.get(str(symbol).split(".")[0])
    if not isinstance(tick_data, dict):
        raise RuntimeError("QMT quote is unavailable")
    tick_at = _tick_datetime(tick_data)
    quote_age = max(0.0, (_now() - tick_at).total_seconds())
    if quote_age > int(_CONFIG.get("price_guard_max_quote_age_seconds", 30)):
        raise RuntimeError("QMT quote is stale")
    detail = instrument_detail(symbol)
    if not isinstance(detail, dict):
        raise RuntimeError("QMT instrument detail is unavailable")
    price_tick = _positive_number(_pick(detail, ["PriceTick", "price_tick", "m_dPriceTick"]))
    if price_tick is None:
        raise RuntimeError("QMT instrument price tick is unavailable")
    last_price = _positive_number(_pick(tick_data, ["lastPrice", "last_price", "last"]))
    previous_close = _positive_number(_pick(tick_data, ["lastClose", "preClose", "previous_close"]))
    if previous_close is None:
        previous_close = _positive_number(_pick(detail, ["PreClose", "pre_close", "LastClose"]))
    bid_price = _first_book_price(_pick(tick_data, ["bidPrice", "bid_price", "bids"]))
    ask_price = _first_book_price(_pick(tick_data, ["askPrice", "ask_price", "asks"]))
    upper_raw = _pick(detail, ["UpStopPrice", "up_stop_price", "upper_limit"])
    lower_raw = _pick(detail, ["DownStopPrice", "down_stop_price", "lower_limit"])
    if upper_raw is None or lower_raw is None:
        raise RuntimeError("QMT daily price-limit fields are unavailable")
    try:
        upper_limit = float(upper_raw or 0)
        lower_limit = float(lower_raw or 0)
    except (TypeError, ValueError):
        raise RuntimeError("QMT daily price-limit fields are invalid")
    if upper_limit < 0 or lower_limit < 0 or bool(upper_limit) != bool(lower_limit):
        raise RuntimeError("QMT daily price-limit fields are invalid")
    phase = _trading_phase()
    cage = _dynamic_price_cage(
        symbol, bid_price, ask_price, last_price, previous_close, price_tick, phase
    )
    return {
        "symbol": symbol, "tick_at": _iso(tick_at), "quote_age_seconds": round(quote_age, 3),
        "last_price": last_price, "previous_close": previous_close,
        "bid_price": bid_price, "ask_price": ask_price,
        "upper_limit": upper_limit, "lower_limit": lower_limit,
        "price_tick": price_tick, "trading_phase": phase, "dynamic_cage": cage,
    }


def _validate_live_price(command):
    quote = _market_quote(command["qmt_symbol"])
    price = float(command["resolved_order"]["limit_price"])
    tick = float(quote["price_tick"])
    if abs(price / tick - round(price / tick)) > 1e-6:
        raise RuntimeError("order price is not aligned to QMT price tick")
    lower = float(quote.get("lower_limit") or 0)
    upper = float(quote.get("upper_limit") or 0)
    if lower and (price < lower - 1e-8 or price > upper + 1e-8):
        raise RuntimeError("order price is outside QMT daily price limit")
    cage = quote.get("dynamic_cage") or {}
    if cage.get("applied"):
        if command["action"] in ("BUY", "COLLATERAL_BUY", "MARGIN_BUY", "BUY_TO_REPAY"):
            if price > float(cage["buy_upper"]) + 1e-8:
                raise RuntimeError("buy price exceeds QMT dynamic price cage")
        elif command["action"] in ("SELL", "COLLATERAL_SELL", "SHORT_SELL", "SELL_TO_REPAY"):
            if price < float(cage["sell_lower"]) - 1e-8:
                raise RuntimeError("sell price is below QMT dynamic price cage")


def _runtime_snapshot(name):
    path = os.path.join(_runtime(), name)
    if not os.path.exists(path):
        return None
    value = _load_json(path)
    maximum = int(_CONFIG.get("credit_snapshot_max_age_seconds", 600))
    if time.time() - float(value.get("written_at_epoch", 0)) > maximum:
        return None
    return value


def _verify_odd_lot_final(command, available_volume):
    rule = command["resolved_order"]["volume_rule"]
    if rule.get("odd_lot_liquidation", False):
        volume = int(command["resolved_order"]["volume"])
        minimum = int(rule["minimum_volume"])
        if available_volume != volume or available_volume >= minimum:
            raise RuntimeError("odd-lot sell must liquidate the entire remaining position")


def _final_account_risk(command, auto_policy=None):
    accounts = _trade_details("ACCOUNT")
    if not accounts:
        raise RuntimeError("account snapshot unavailable at final check")
    order = command["resolved_order"]
    notional = float(order["limit_price"]) * int(order["volume"])
    if command["account_type"] == "STOCK":
        if command["action"] == "BUY":
            available = _pick(accounts[0], _field_names("available_cash", ["m_dAvailable", "available_cash"]))
            if available is None or float(available) < notional:
                raise RuntimeError("insufficient cash at final check")
        elif command["action"] == "SELL":
            available = 0
            for item in _trade_details("POSITION"):
                normalized = _normalize_position(item)
                if normalized.get("symbol") and _same_symbol(normalized["symbol"], command["qmt_symbol"]):
                    available += int(normalized.get("available_volume") or 0)
            _verify_odd_lot_final(command, available)
            if available < int(order["volume"]):
                raise RuntimeError("insufficient sellable volume at final check")
        if auto_policy is not None:
            _final_auto_dynamic_risk(command, auto_policy, accounts[0])
        return
    _final_credit_risk(command)
    if auto_policy is not None:
        _final_auto_dynamic_risk(command, auto_policy, accounts[0])


def _final_auto_dynamic_risk(command, policy, raw_account):
    total_asset = _pick(
        raw_account, _field_names("total_asset", ["m_dBalance", "total_asset"])
    )
    if total_asset is None or float(total_asset) <= 0:
        raise RuntimeError("account equity is unavailable at limited-auto final check")
    if float(policy.get("baseline_total_asset", 0)) - float(total_asset) > float(policy.get("max_account_drawdown", 0)):
        raise RuntimeError("limited-auto account drawdown exceeded at final check")
    current_volume = 0
    for raw_position in _trade_details("POSITION"):
        position = _normalize_position(raw_position)
        if position.get("symbol") and _same_symbol(position["symbol"], command["qmt_symbol"]):
            current_volume += int(position.get("total_volume") or 0)
    order_volume = int(command["resolved_order"]["volume"])
    if command["action"] in ("BUY", "COLLATERAL_BUY", "MARGIN_BUY", "BUY_TO_REPAY"):
        projected_volume = current_volume + order_volume
    else:
        projected_volume = max(0, current_volume - order_volume)
    current_notional = current_volume * float(command["resolved_order"]["limit_price"])
    projected_notional = projected_volume * float(command["resolved_order"]["limit_price"])
    if (
        projected_notional > current_notional and
        projected_notional > float(policy.get("max_symbol_position_notional", 0))
    ):
        raise RuntimeError("limited-auto symbol position limit exceeded at final check")
    active = 0
    for raw_order in _trade_details("ORDER"):
        status = _normalize_order_status(_pick(
            raw_order, _field_names("order_status", ["m_nOrderStatus", "order_status"])
        ))
        if status in (
            "QUEUED", "REPORTED", "ACCEPTED", "ORDER_ACCEPTED",
            "PARTIALLY_FILLED", "CANCEL_REQUESTED", "SUBMIT_CALLED",
        ):
            active += 1
    if active >= int(policy.get("max_concurrent_orders", 0)):
        raise RuntimeError("limited-auto concurrent-order limit exceeded at final check")


def _final_credit_risk(command):
    if not _CONFIG.get("credit_final_check_verified", False):
        raise RuntimeError("credit final check is not verified")
    account = _runtime_snapshot("latest_credit_account.json")
    eligibility = _runtime_snapshot("latest_credit_eligibility.json")
    debts = _runtime_snapshot("latest_credit_debts.json")
    if account is None or eligibility is None:
        raise RuntimeError("credit final-check snapshots are stale")
    symbol = command["qmt_symbol"]
    instrument = next((item for item in eligibility.get("instruments", []) if _same_symbol(item.get("symbol"), symbol)), None)
    flags = {
        "COLLATERAL_BUY": "collateral_buy_eligible", "COLLATERAL_SELL": "collateral_sell_eligible",
        "MARGIN_BUY": "margin_buy_eligible", "SHORT_SELL": "short_sell_eligible",
        "BUY_TO_REPAY": "buy_to_repay_eligible", "SELL_TO_REPAY": "sell_to_repay_eligible",
    }
    if instrument is None or not instrument.get(flags[command["action"]], False):
        raise RuntimeError("credit instrument is not eligible at final check")
    order = command["resolved_order"]
    if command["action"] == "COLLATERAL_BUY":
        available_cash = account.get("available_cash")
        if available_cash is None or float(available_cash) < float(order["limit_price"]) * int(order["volume"]):
            raise RuntimeError("insufficient credit-account cash at final check")
    if command["action"] in ("COLLATERAL_SELL", "SELL_TO_REPAY"):
        sellable = 0
        for raw_position in _trade_details("POSITION"):
            position = _normalize_position(raw_position)
            if position.get("symbol") and _same_symbol(position["symbol"], symbol):
                sellable += int(position.get("available_volume") or 0)
        _verify_odd_lot_final(command, sellable)
        if sellable < int(order["volume"]):
            raise RuntimeError("insufficient collateral volume at final check")
    if command["action"] == "SHORT_SELL" and int(instrument.get("short_available_volume", 0) or 0) < int(order["volume"]):
        raise RuntimeError("insufficient short inventory at final check")
    ratio = account.get("maintenance_ratio")
    if command["action"] in ("MARGIN_BUY", "SHORT_SELL"):
        if ratio is None or float(ratio) < float(_CONFIG.get("minimum_maintenance_ratio", 1.5)):
            raise RuntimeError("credit maintenance ratio below final limit")
    if command["action"] in ("MARGIN_BUY", "SHORT_SELL", "BUY_TO_REPAY", "SELL_TO_REPAY"):
        capacity_id = (command.get("credit") or {}).get("capacity_snapshot_id")
        if not capacity_id:
            raise RuntimeError("credit capacity reference missing")
        capacity = _runtime_snapshot(os.path.join("credit_capacity", capacity_id + ".json"))
        matched = None
        if capacity:
            matched = next((item for item in capacity.get("results", []) if
                            _same_symbol(item.get("symbol"), symbol) and
                            item.get("credit_action") == command["action"]), None)
        if matched is None or int(matched.get("max_volume", 0)) < int(command["resolved_order"]["volume"]):
            raise RuntimeError("credit capacity unavailable at final check")
    if command["action"] in ("BUY_TO_REPAY", "SELL_TO_REPAY"):
        debt_ref = (command.get("credit") or {}).get("debt_contract_ref")
        debt = None if debts is None else next((item for item in debts.get("debts", []) if item.get("debt_contract_ref") == debt_ref), None)
        if debt is None or not _same_symbol(debt.get("symbol"), symbol):
            raise RuntimeError("credit debt reference unavailable at final check")
        if (command["action"] == "BUY_TO_REPAY" and
                int(debt.get("outstanding_volume", 0) or 0) < int(command["resolved_order"]["volume"])):
            raise RuntimeError("repay volume exceeds outstanding short debt")


def _qmt_function(name):
    function = globals().get(name)
    if not callable(function):
        raise RuntimeError("QMT function is unavailable: " + name)
    return function


def _execute_order(command, message_id):
    journal = _journal_path(command["client_order_key"])
    if os.path.exists(journal):
        existing = _load_json(journal)
        if existing.get("state") in ("PRE_SUBMIT", "SUBMIT_CALLED"):
            _pause_auto_locally(command, "existing pre-submit journal")
            _ack(command, "SUBMIT_UNKNOWN", message_id, {"reason": "existing pre-submit journal"})
        else:
            _ack(command, existing.get("state", "ACKNOWLEDGED"), message_id, {"duplicate": True})
        return
    _validate_execute(command)
    if command["execution_mode"] == "OBSERVE_ONLY":
        _write_journal(command, "OBSERVE_ONLY_ACKNOWLEDGED")
        _ack(command, "OBSERVE_ONLY_ACKNOWLEDGED", message_id)
        return
    profile, mapping = _mapping_for(command)
    order = command["resolved_order"]
    _write_journal(command, "PRE_SUBMIT")
    try:
        _qmt_function("passorder")(
            mapping["op_type"], mapping["order_type"], _CONFIG["qmt_account_id"],
            command["qmt_symbol"], mapping["price_type"], order["limit_price"],
            order["volume"], profile.get("strategy_name", "WorkBuddyQMT"), 2,
            command["client_order_key"], _CONTEXT,
        )
    except Exception as exc:
        # The call may have crossed the native boundary. Never rewrite PRE_SUBMIT
        # to a retryable state and never call passorder automatically again.
        _pause_auto_locally(command, "passorder returned an uncertain result")
        _request_order_deal_reconcile()
        _ack(command, "SUBMIT_UNKNOWN", message_id, {"error": str(exc)[:200]})
        return
    _write_journal(command, "SUBMIT_CALLED")
    _ack(command, "SUBMIT_CALLED", message_id)


def _cancel_order(command, message_id):
    if command.get("account_alias") != _CONFIG["account_alias"]:
        raise RuntimeError("account partition mismatch")
    if _local_halted() and not _CONFIG.get("allow_cancel_while_halted", True):
        raise RuntimeError("cancel while halted is disabled")
    result = _qmt_function("cancel")(
        command["qmt_order_id"], _CONFIG["qmt_account_id"], _CONFIG["account_type"], _CONTEXT
    )
    _ack(command, "CANCEL_REQUESTED", message_id, {"qmt_signal_sent": bool(result)})


def _request_sync(command, message_id):
    scopes = command.get("scopes", [])
    _emit_snapshots(_CONTEXT, scopes, command.get("symbols", []))
    if set(["ORDER", "DEAL"]).issubset(set(scopes)):
        _mark_order_deal_reconciled()
    _ack(command, "SYNC_COMPLETED", message_id)


def _credit_precheck(command, message_id):
    global _CREDIT_QUERY_IN_FLIGHT, _LAST_CREDIT_QUERY_AT
    if _CONFIG["account_type"] != "CREDIT":
        raise RuntimeError("credit query on STOCK adapter")
    if _CREDIT_QUERY_IN_FLIGHT is not None:
        raise RuntimeError("credit query already in flight")
    requests = command.get("requests") or []
    actions = set([item.get("credit_action") for item in requests])
    if len(actions) != 1:
        raise RuntimeError("one credit query batch must contain one action")
    if not _CONFIG.get("credit_query_wrapper_verified", False):
        raise RuntimeError("credit query wrapper is not verified")
    profile = _load_verified_profile()
    action = list(actions)[0]
    mapping = _mapping_entry(profile, "CREDIT:STOCK:%s:LIMIT" % action)
    if time.time() - _LAST_CREDIT_QUERY_AT < int(_CONFIG.get("credit_query_cooldown_seconds", 180)):
        raise RuntimeError("credit query is cooling down")
    qmt_seq = int(time.time() * 1000) % 2147483647
    _CREDIT_QUERY_IN_FLIGHT = {
        "kind": "OPVOLUME", "bridge_seq": command["seq"], "qmt_seq": qmt_seq,
        "requests": requests, "action": action,
    }
    _LAST_CREDIT_QUERY_AT = time.time()
    symbols = [item["symbol"] for item in requests]
    prices = [item["price"] for item in requests]
    if len(symbols) == 1:
        symbols = symbols[0]
        prices = prices[0]
    try:
        _qmt_function("query_credit_opvolume")(
            _CONFIG["qmt_account_id"], symbols, mapping["op_type"], mapping["price_type"],
            prices, qmt_seq, _CONTEXT,
        )
    except Exception:
        _CREDIT_QUERY_IN_FLIGHT = None
        raise
    _ack(command, "ACKNOWLEDGED", message_id, {"credit_query_started": True, "qmt_seq": qmt_seq})


def _process_file(path):
    message = _load_json(path)
    _verify(message, set(["TRADE_INTENT", "CANCEL_ORDER", "REQUEST_SYNC", "CREDIT_PRECHECK"]))
    command = message["payload"]
    kind = command.get("type")
    if kind == "EXECUTE_ORDER":
        _execute_order(command, message["message_id"])
    elif kind == "CANCEL_ORDER":
        _cancel_order(command, message["message_id"])
    elif kind == "REQUEST_SYNC":
        _request_sync(command, message["message_id"])
    elif kind == "CREDIT_PRECHECK":
        _credit_precheck(command, message["message_id"])
    else:
        raise RuntimeError("unknown command type")


def poll_commands(C=None):
    global _LAST_SCAN
    if _CONFIG is None or not _refresh_config_mode():
        return
    directory = os.path.join(_partition(), "commands")
    names = sorted([name for name in os.listdir(directory) if name.endswith(".json")])[:int(_CONFIG.get("max_batch", 10))]
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.getsize(path) > int(_CONFIG.get("max_message_bytes", 65536)):
                raise RuntimeError("message too large")
            _process_file(path)
            _archive(path)
        except Exception as exc:
            try:
                message = _load_json(path)
                command = message.get("payload", {})
                _pause_auto_locally(command, "limited-auto command was rejected")
                _ack(command, "QMT_REJECTED", message.get("message_id"), {"error": str(exc)[:200]})
            except Exception:
                pass
            _dead_letter(path)
    _LAST_SCAN = _iso()


def _pick(value, names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _pick_nonempty(value, names, default=None):
    for name in names:
        if isinstance(value, dict) and name in value:
            item = value[name]
        elif hasattr(value, name):
            item = getattr(value, name)
        else:
            continue
        if item not in (None, ""):
            return item
    return default


def _field_names(logical, defaults):
    configured = (_CONFIG.get("field_map") or {}).get(logical)
    if configured:
        return configured if isinstance(configured, list) else [configured]
    return defaults


def _client_order_key(value, names):
    raw = _pick(value, names)
    if raw in (None, ""):
        return None
    candidate = str(raw).rsplit("_&&&_", 1)[-1]
    if not (candidate.startswith("WB") and 4 <= len(candidate) <= 64 and candidate.isalnum()):
        return None
    return candidate


def _normalize_account(value):
    return {
        "total_asset": _pick(value, _field_names("total_asset", ["m_dBalance", "total_asset"])),
        "available_cash": _pick(value, _field_names("available_cash", ["m_dAvailable", "available_cash"])),
        "market_value": _pick(value, _field_names("market_value", ["m_dInstrumentValue", "market_value"])),
        "connection_status": _pick(value, _field_names("connection_status", ["m_strStatus", "m_nStatus", "status"])),
    }


def _normalize_position(value):
    return {
        "symbol": _pick(value, _field_names("position_symbol", ["m_strInstrumentID", "stock_code", "symbol"])),
        "total_volume": _pick(value, _field_names("position_total", ["m_nVolume", "volume", "total_volume"]), 0),
        "available_volume": _pick(value, _field_names("position_available", ["m_nCanUseVolume", "can_use_volume", "available_volume"]), 0),
        "frozen_volume": _pick(value, _field_names("position_frozen", ["m_nFrozenVolume", "frozen_volume"]), 0),
        "cost_price": _pick(value, _field_names("position_cost", ["m_dOpenPrice", "cost_price"])),
        "last_price": _pick(value, _field_names("position_last", ["m_dLastPrice", "last_price"])),
        "market_value": _pick(value, _field_names("position_value", ["m_dInstrumentValue", "market_value"])),
    }


def _trade_details(kind):
    function = _qmt_function("get_trade_detail_data")
    result = function(_CONFIG["qmt_account_id"], _CONFIG["account_type"], kind, _CONFIG.get("strategy_name", "WorkBuddyQMT"))
    if result is None:
        return []
    return result if isinstance(result, (list, tuple)) else [result]


def _emit_snapshots(C=None, scopes=None, symbols=None):
    global _LAST_SNAPSHOT
    scopes = set(scopes or ["ACCOUNT", "POSITION", "ORDER", "DEAL"])
    captured = _iso()
    snapshot_id = "snap_" + uuid.uuid4().hex
    if "ACCOUNT" in scopes:
        values = _trade_details("ACCOUNT")
        if values:
            data = _normalize_account(values[0])
            data.update({"type": "ACCOUNT_SNAPSHOT", "snapshot_id": snapshot_id, "captured_at": captured})
            _write_event(data)
    if "POSITION" in scopes:
        positions = [_normalize_position(item) for item in _trade_details("POSITION")]
        positions = [item for item in positions if item.get("symbol")]
        _write_event({"type": "POSITION_SNAPSHOT", "snapshot_id": snapshot_id, "captured_at": captured, "positions": positions})
    if "ORDER" in scopes:
        for item in _trade_details("ORDER"):
            _write_order_event(item)
    if "DEAL" in scopes:
        for item in _trade_details("DEAL"):
            _write_trade_event(item)
    if "QUOTE" in scopes:
        wanted = list(dict.fromkeys(symbols or []))
        if not wanted or len(wanted) > 100:
            raise RuntimeError("QUOTE sync requires 1-100 symbols")
        quotes = [_market_quote(symbol) for symbol in wanted]
        _write_event({
            "type": "QUOTE_SNAPSHOT", "snapshot_id": "quote_" + uuid.uuid4().hex,
            "captured_at": captured, "quotes": quotes,
        })
    _LAST_SNAPSHOT = captured


def snapshot_task(C=None):
    try:
        _emit_snapshots(C, ["ACCOUNT", "POSITION"])
        _write_p0_probe(C)
    except Exception as exc:
        _write_event({"type": "ERROR_EVENT", "error_code": "SNAPSHOT_FAILED", "error_message": str(exc)[:200]})


def order_deal_reconcile_task(C=None):
    global _ORDER_DEAL_RECONCILE_REQUESTED
    if _CONFIG is None:
        return
    interval = int(_CONFIG.get("order_deal_reconcile_seconds", 30))
    if (not _ORDER_DEAL_RECONCILE_REQUESTED and
            time.time() - _LAST_ORDER_DEAL_RECONCILE_AT < interval):
        return
    try:
        _emit_snapshots(C, ["ORDER", "DEAL"])
        _mark_order_deal_reconciled()
    except Exception as exc:
        _ORDER_DEAL_RECONCILE_REQUESTED = True
        _write_event({
            "type": "ERROR_EVENT", "error_code": "ORDER_DEAL_RECONCILE_FAILED",
            "error_message": str(exc)[:200],
        })


def _p0_field_shape(value):
    shape = {"object_type": type(value).__name__, "fields": []}
    if isinstance(value, dict):
        names = sorted(value.keys(), key=lambda item: str(item))
    else:
        names = sorted([name for name in dir(value) if not str(name).startswith("_")])
    for name in names:
        try:
            item = value.get(name) if isinstance(value, dict) else getattr(value, name)
        except Exception:
            shape["fields"].append({"name": str(name), "readable": False})
            continue
        if callable(item):
            continue
        shape["fields"].append({
            "name": str(name),
            "type": type(item).__name__,
            "is_null": item is None,
            "readable": True,
        })
    return shape


def _write_p0_probe(C=None):
    global _P0_PROBE_ATTEMPTS, _P0_PROBE_COMPLETE
    if not _CONFIG.get("p0_probe_enabled", False):
        return
    if _P0_PROBE_COMPLETE or _P0_PROBE_ATTEMPTS >= 12:
        return
    _P0_PROBE_ATTEMPTS += 1
    scopes = {}
    for kind in ("ACCOUNT", "POSITION", "ORDER", "DEAL"):
        try:
            values = _trade_details(kind)
            scopes[kind] = {
                "sample_present": bool(values),
                "shape": _p0_field_shape(values[0]) if values else None,
            }
        except Exception as exc:
            scopes[kind] = {
                "sample_present": False,
                "shape": None,
                "error_type": type(exc).__name__,
            }
    required_samples_captured = bool(
        scopes.get("ACCOUNT", {}).get("sample_present") and
        scopes.get("POSITION", {}).get("sample_present")
    )
    timed_out = _P0_PROBE_ATTEMPTS >= 12 and not required_samples_captured
    result = {
        "probe_version": "1.0",
        "generated_at": _iso(),
        "account_alias": _CONFIG["account_alias"],
        "account_type": _CONFIG["account_type"],
        "adapter_instance": _CONFIG["adapter_instance"],
        "qmt_mode": _CONFIG.get("qmt_mode"),
        "attempt": _P0_PROBE_ATTEMPTS,
        "complete": required_samples_captured,
        "stopped_reason": (
            "required_samples_captured" if required_samples_captured else
            "timeout" if timed_out else "waiting_for_required_samples"
        ),
        "schedule_methods": dict(_SCHEDULE_METHODS),
        "scopes": scopes,
    }
    _atomic_json(os.path.join(_runtime(), "p0_probe.json"), result)
    methods = ",".join(["%s=%s" % (name, _SCHEDULE_METHODS[name]) for name in sorted(_SCHEDULE_METHODS)])
    print(
        "[WorkBuddy-QMT][P0_PROBE] attempt=%s complete=%s fields=names-types-null-only schedule=%s" % (
            _P0_PROBE_ATTEMPTS, "YES" if required_samples_captured else "NO", methods,
        ),
        flush=True,
    )
    if required_samples_captured or timed_out:
        _P0_PROBE_COMPLETE = True


def _console_heartbeat(local_halt):
    global _LAST_CONSOLE_HEARTBEAT_AT, _LAST_CONSOLE_STATUS, _LAST_CONSOLE_LOCAL_HALT
    current = time.time()
    interval = int(_CONFIG.get("console_heartbeat_seconds", 30))
    status_changed = _STATUS != _LAST_CONSOLE_STATUS
    halt_changed = local_halt != _LAST_CONSOLE_LOCAL_HALT
    if not status_changed and not halt_changed and current - _LAST_CONSOLE_HEARTBEAT_AT < interval:
        return
    print(
        "[WorkBuddy-QMT][HEARTBEAT] time=%s account=%s adapter=%s status=%s mode=%s local_halt=%s" % (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            _CONFIG["account_alias"],
            _CONFIG["adapter_instance"],
            _STATUS,
            _CONFIG.get("qmt_mode"),
            "YES" if local_halt else "NO",
        ),
        flush=True,
    )
    _LAST_CONSOLE_HEARTBEAT_AT = current
    _LAST_CONSOLE_STATUS = _STATUS
    _LAST_CONSOLE_LOCAL_HALT = local_halt


def heartbeat_task(C=None):
    age_ms = None
    if _LAST_SNAPSHOT:
        age_ms = int((_now() - _parse_time(_LAST_SNAPSHOT)).total_seconds() * 1000)
    local_halt = _local_halted()
    auto_pause = None
    try:
        auto_pause = _active_auto_pause()
    except Exception:
        auto_pause = {"reason": "limited-auto pause state is unreadable"}
    _write_event({
        "type": "HEARTBEAT", "status": _STATUS, "qmt_mode": _CONFIG.get("qmt_mode"),
        "adapter_instance": _CONFIG["adapter_instance"], "last_command_scan_at": _LAST_SCAN,
        "last_order_callback_at": _LAST_ORDER_CALLBACK, "last_deal_callback_at": _LAST_DEAL_CALLBACK,
        "snapshot_age_ms": age_ms, "local_halt": local_halt,
        "limited_auto_local_pause": bool(auto_pause),
        "limited_auto_pause_reason": auto_pause.get("reason") if auto_pause else None,
    })
    _console_heartbeat(local_halt)


def _schedule(C, function_name, interval):
    try:
        count = int(interval.split("n", 1)[0])
        if interval.endswith("MilliSecond"):
            delta = datetime.timedelta(milliseconds=count)
        elif interval.endswith("Second"):
            delta = datetime.timedelta(seconds=count)
        elif interval.endswith("Day"):
            delta = datetime.timedelta(days=count)
        else:
            raise ValueError("unsupported schedule interval")
        first = (datetime.datetime.now() + delta).strftime("%Y%m%d%H%M%S")
        C.schedule_run(globals()[function_name], first, -1, delta, function_name)
        _SCHEDULE_METHODS[function_name] = "schedule_run"
        return
    except Exception:
        pass
    C.run_time(function_name, interval, "2018-01-01 00:00:00")
    _SCHEDULE_METHODS[function_name] = "run_time"


def init(C):
    global _CONTEXT, _STATUS
    _CONTEXT = C
    try:
        _load_config()
        C.set_account(_CONFIG["qmt_account_id"])
        _STATUS = "RECOVERING"
        heartbeat_task(C)
        _schedule(
            C, "poll_commands",
            "%dnMilliSecond" % int(_CONFIG.get("command_poll_interval_ms", 500)),
        )
        _schedule(C, "snapshot_task", "5nSecond")
        _schedule(C, "order_deal_reconcile_task", "5nSecond")
        _schedule(C, "heartbeat_task", "5nSecond")
        if _CONFIG["account_type"] == "CREDIT":
            _schedule(C, "credit_reference_task", "180nSecond")
            _schedule(C, "credit_account_query_task", "30nSecond")
    except Exception:
        _STATUS = "ERROR"
        raise


def after_init(C):
    global _STATUS
    try:
        _emit_snapshots(C)
        _mark_order_deal_reconciled()
        _write_p0_probe(C)
        _STATUS = "READY"
        heartbeat_task(C)
    except Exception:
        _STATUS = "ERROR"
        raise


def handlebar(C):
    pass


def account_callback(C, accountInfo):
    data = _normalize_account(accountInfo)
    data.update({"type": "ACCOUNT_SNAPSHOT", "snapshot_id": "acct_" + uuid.uuid4().hex, "captured_at": _iso()})
    _write_event(data)


def position_callback(C, positionInfo):
    item = _normalize_position(positionInfo)
    if item.get("symbol"):
        _write_event({"type": "POSITION_EVENT", "snapshot_id": "pos_" + uuid.uuid4().hex, "captured_at": _iso(), "position": item})


def _write_order_event(orderInfo):
    global _LAST_ORDER_CALLBACK
    _LAST_ORDER_CALLBACK = _iso()
    _write_event({
        "type": "ORDER_EVENT",
        "qmt_order_id": str(_pick(orderInfo, _field_names("order_id", ["m_strOrderSysID", "order_id"]), "")),
        "client_order_key": _client_order_key(orderInfo, _field_names("order_remark", ["m_strRemark", "remark", "user_order_id", "strategyName"])),
        "symbol": _pick(orderInfo, _field_names("order_symbol", ["m_strInstrumentID", "stock_code", "symbol"])),
        "order_status_raw": str(_pick(orderInfo, _field_names("order_status", ["m_nOrderStatus", "order_status"]), "")),
        "order_status_normalized": _normalize_order_status(_pick(orderInfo, _field_names("order_status", ["m_nOrderStatus", "order_status"]))),
        "requested_volume": _pick(orderInfo, _field_names("order_volume", ["m_nVolumeTotalOriginal", "order_volume"])),
        "filled_volume": _pick(orderInfo, _field_names("order_filled", ["m_nVolumeTraded", "traded_volume"]), 0),
        "limit_price": _pick(orderInfo, _field_names("order_price", ["m_dLimitPrice", "price"])),
        "order_error_code": str(_pick(orderInfo, _field_names("order_error_code", ["m_nErrorID", "error_id"]), "")),
        "order_error_message": str(_pick_nonempty(orderInfo, _field_names(
            "order_error_message", ["m_strCancelInfo", "cancel_info", "m_strErrorMsg", "status_msg"]
        ), ""))[:200],
    })


def _normalize_order_status(raw):
    defaults = {
        "48": "QUEUED", "49": "QUEUED", "50": "REPORTED",
        "51": "CANCEL_REQUESTED", "52": "CANCEL_REQUESTED",
        "53": "PARTIALLY_CANCELLED", "54": "CANCELLED",
        "55": "PARTIALLY_FILLED", "56": "FILLED", "57": "REJECTED",
    }
    mapping = dict(defaults)
    mapping.update(_CONFIG.get("order_status_map", {}))
    return mapping.get(str(raw), "UNKNOWN")


def order_callback(C, orderInfo):
    _write_order_event(orderInfo)


def _write_trade_event(dealInfo):
    global _LAST_DEAL_CALLBACK
    _LAST_DEAL_CALLBACK = _iso()
    _write_event({
        "type": "TRADE_EVENT",
        "trade_id": str(_pick(dealInfo, _field_names("trade_id", ["m_strTradeID", "trade_id"]), "")),
        "qmt_order_id": str(_pick(dealInfo, _field_names("trade_order_id", ["m_strOrderSysID", "order_id"]), "")),
        "client_order_key": _client_order_key(dealInfo, _field_names("trade_remark", ["m_strRemark", "remark", "user_order_id", "strategyName"])),
        "symbol": _pick(dealInfo, _field_names("trade_symbol", ["m_strInstrumentID", "stock_code", "symbol"])),
        "volume": _pick(dealInfo, _field_names("trade_volume", ["m_nVolume", "traded_volume"]), 0),
        "price": _pick(dealInfo, _field_names("trade_price", ["m_dPrice", "traded_price"]), 0),
        "traded_at": _iso(),
    })


def deal_callback(C, dealInfo):
    _write_trade_event(dealInfo)


def orderError_callback(C, orderArgs, errMsg):
    _request_order_deal_reconcile()
    _write_event({
        "type": "ERROR_EVENT", "error_code": "QMT_ORDER_ERROR",
        "error_message": str(errMsg)[:200],
        "client_order_key": _client_order_key(
            orderArgs, ["m_strRemark", "remark", "user_order_id", "strategyName"]
        ),
    })


def credit_account_callback(C, seq, result):
    global _CREDIT_QUERY_IN_FLIGHT
    if _CONFIG["account_type"] != "CREDIT":
        return
    if (not isinstance(_CREDIT_QUERY_IN_FLIGHT, dict) or
            _CREDIT_QUERY_IN_FLIGHT.get("kind") != "ACCOUNT" or
            str(seq) != str(_CREDIT_QUERY_IN_FLIGHT.get("qmt_seq"))):
        _write_event({"type": "ERROR_EVENT", "error_code": "CREDIT_SEQ_MISMATCH", "error_message": "unmatched credit account callback seq"})
        return
    fields = _CONFIG.get("credit_field_map", {})
    data = {"type": "CREDIT_ACCOUNT_SNAPSHOT", "snapshot_id": "credit_" + uuid.uuid4().hex, "seq": str(seq), "captured_at": _iso()}
    for logical, names in fields.items():
        data[logical] = _pick(result, names if isinstance(names, list) else [names])
    _write_event(data)
    local = dict(data)
    local["written_at_epoch"] = time.time()
    _atomic_json(os.path.join(_runtime(), "latest_credit_account.json"), local)
    _CREDIT_QUERY_IN_FLIGHT = None


def credit_opvolume_callback(C, accid, seq, ret, result):
    global _CREDIT_QUERY_IN_FLIGHT
    if (not isinstance(_CREDIT_QUERY_IN_FLIGHT, dict) or
            _CREDIT_QUERY_IN_FLIGHT.get("kind") != "OPVOLUME" or
            str(seq) != str(_CREDIT_QUERY_IN_FLIGHT.get("qmt_seq"))):
        _write_event({"type": "ERROR_EVENT", "error_code": "CREDIT_SEQ_MISMATCH", "error_message": "unmatched credit callback seq"})
        return
    inflight = _CREDIT_QUERY_IN_FLIGHT
    if int(ret) != 1:
        _write_event({
            "type": "ERROR_EVENT", "error_code": "CREDIT_QUERY_FAILED",
            "error_message": "credit opvolume ret=%s" % ret,
            "bridge_seq": inflight["bridge_seq"], "qmt_seq": str(seq),
        })
        _CREDIT_QUERY_IN_FLIGHT = None
        return
    values = result if isinstance(result, (list, tuple)) else [result]
    normalized = []
    for index, item in enumerate(values):
        requested = inflight["requests"][min(index, len(inflight["requests"]) - 1)]
        normalized.append({
            "symbol": _pick(item, _field_names("credit_symbol", ["symbol", "stock_code"]), requested["symbol"]),
            "credit_action": requested["credit_action"],
            "max_volume": _pick(item, _field_names("credit_max_volume", ["m_nEnableAmount", "max_volume", "volume"]), 0),
        })
    _write_event({
        "type": "CREDIT_CAPACITY_SNAPSHOT", "seq": inflight["bridge_seq"],
        "snapshot_id": "ccap_" + inflight["bridge_seq"],
        "qmt_seq": str(seq), "captured_at": _iso(), "results": normalized,
    })
    capacity_value = {
        "snapshot_id": "ccap_" + inflight["bridge_seq"],
        "bridge_seq": inflight["bridge_seq"], "qmt_seq": str(seq),
        "results": normalized, "written_at_epoch": time.time(),
    }
    _atomic_json(
        os.path.join(_runtime(), "credit_capacity", capacity_value["snapshot_id"] + ".json"),
        capacity_value,
    )
    _CREDIT_QUERY_IN_FLIGHT = None


def credit_account_query_task(C=None):
    global _CREDIT_QUERY_IN_FLIGHT, _LAST_CREDIT_QUERY_AT
    if _CONFIG is None or _CONFIG.get("account_type") != "CREDIT":
        return
    if not _CONFIG.get("credit_query_wrapper_verified", False):
        return
    if _CREDIT_QUERY_IN_FLIGHT is not None:
        return
    cooldown = int(_CONFIG.get("credit_query_cooldown_seconds", 180))
    if time.time() - _LAST_CREDIT_QUERY_AT < cooldown:
        return
    qmt_seq = int(time.time() * 1000) % 2147483647
    _CREDIT_QUERY_IN_FLIGHT = {"kind": "ACCOUNT", "qmt_seq": qmt_seq}
    _LAST_CREDIT_QUERY_AT = time.time()
    try:
        _qmt_function("query_credit_account")(_CONFIG["qmt_account_id"], qmt_seq, _CONTEXT)
    except Exception as exc:
        _CREDIT_QUERY_IN_FLIGHT = None
        _write_event({"type": "ERROR_EVENT", "error_code": "CREDIT_ACCOUNT_QUERY_FAILED", "error_message": str(exc)[:200]})


def _credit_symbol(item):
    instrument = _pick(item, ["m_strInstrumentID", "symbol", "stock_code"])
    exchange = _pick(item, ["m_strExchangeID", "exchange"])
    if instrument and "." not in str(instrument) and exchange:
        return str(instrument) + "." + str(exchange).upper()
    return instrument


def _eligibility_flag(name, raw):
    values = (_CONFIG.get("eligibility_allowed_values") or {}).get(name, [])
    return raw in values or str(raw) in [str(item) for item in values]


def _debt_ref(item):
    fields = [
        _CONFIG["account_alias"], _pick(item, ["m_strInstrumentID"]),
        _pick(item, ["m_eCompactType"]), _pick(item, ["m_nOpenDate"]),
        _pick(item, ["m_nBusinessVol"]),
    ]
    digest = hmac.new(_KEYS[_ACTIVE_KEY], _canonical(fields), hashlib.sha256).hexdigest()
    return "debt_" + digest[:20]


def credit_reference_task(C=None):
    if _CONFIG is None or _CONFIG.get("account_type") != "CREDIT":
        return
    try:
        assure = _qmt_function("get_assure_contract")(_CONFIG["qmt_account_id"]) or []
        shortable = _qmt_function("get_enable_short_contract")(_CONFIG["qmt_account_id"]) or []
        instruments = {}
        for item in assure:
            symbol = _credit_symbol(item)
            if not symbol:
                continue
            assure_status = _pick(item, ["m_eAssureStatus"])
            fin_status = _pick(item, ["m_eFinStatus"])
            slo_status = _pick(item, ["m_eSloStatus"])
            instruments[symbol] = {
                "symbol": symbol,
                "collateral_buy_eligible": _eligibility_flag("collateral", assure_status),
                "collateral_sell_eligible": _eligibility_flag("collateral", assure_status),
                "margin_buy_eligible": _eligibility_flag("margin_buy", fin_status),
                "short_sell_eligible": _eligibility_flag("short_sell", slo_status),
                "buy_to_repay_eligible": _eligibility_flag("buy_to_repay", slo_status),
                "sell_to_repay_eligible": _eligibility_flag("sell_to_repay", assure_status),
                "collateral_ratio": _pick(item, ["m_dAssureRatio"]),
                "margin_ratio": _pick(item, ["m_dFinRatio"]),
                "short_ratio": _pick(item, ["m_dSloRatio"]),
                "short_available_volume": 0,
            }
        for item in shortable:
            symbol = _credit_symbol(item)
            if symbol not in instruments:
                instruments[symbol] = {
                    "symbol": symbol, "collateral_buy_eligible": False,
                    "collateral_sell_eligible": False, "margin_buy_eligible": False,
                    "short_sell_eligible": False, "buy_to_repay_eligible": False,
                    "sell_to_repay_eligible": False,
                }
            instruments[symbol]["short_available_volume"] = _pick(item, ["m_nEnableAmount"], 0)
        _write_event({
            "type": "CREDIT_ELIGIBILITY_SNAPSHOT", "snapshot_id": "elig_" + uuid.uuid4().hex,
            "captured_at": _iso(), "instruments": list(instruments.values()),
        })
        _atomic_json(os.path.join(_runtime(), "latest_credit_eligibility.json"), {
            "written_at_epoch": time.time(), "instruments": list(instruments.values()),
        })

        debts_raw = _qmt_function("get_unclosed_compacts")(_CONFIG["qmt_account_id"], "CREDIT") or []
        debts = []
        for item in debts_raw:
            debts.append({
                "debt_contract_ref": _debt_ref(item), "symbol": _credit_symbol(item),
                "debt_type": str(_pick(item, ["m_eCompactType"], "")),
                "original_volume": _pick(item, ["m_nBusinessVol"]),
                "outstanding_volume": _pick(item, ["m_nRealCompactVol"]),
                "outstanding_amount": _pick(item, ["m_dRealCompactBalance", "m_dRealCompactAmount"]),
                "interest_and_fees": _pick(item, ["m_dInterest", "m_dFee"]),
                "opened_on": _pick(item, ["m_nOpenDate"]),
                "due_on": _pick(item, ["m_nEndDate", "m_nDueDate"]), "status": "OPEN",
            })
        _write_event({
            "type": "CREDIT_DEBT_SNAPSHOT", "snapshot_id": "debt_" + uuid.uuid4().hex,
            "captured_at": _iso(), "debts": debts,
        })
        _atomic_json(os.path.join(_runtime(), "latest_credit_debts.json"), {
            "written_at_epoch": time.time(), "debts": debts,
        })
    except Exception as exc:
        _write_event({"type": "ERROR_EVENT", "error_code": "CREDIT_REFERENCE_QUERY_FAILED", "error_message": str(exc)[:200]})
