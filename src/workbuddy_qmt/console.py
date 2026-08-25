import argparse
import datetime as dt
import json
import os
import sys

from .config import load_config
from .db import Database
from .errors import BridgeError
from .modes import RUN_MODES
from .security import KeyRing, make_envelope
from .util import atomic_write_json, iso_now, json_text, new_id, parse_time, utc_now


def _db(config):
    database = Database(os.path.join(config.data_dir, "state", "bridge.db"))
    database.initialize(config.default_mode)
    return database


def _set_state(connection, key, value, now):
    connection.execute(
        "INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, value, now),
    )


def _audit(connection, action, actor, details, account_alias=None, object_id=None):
    connection.execute(
        "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
        (iso_now(), actor, action, account_alias, object_id, json_text(details)),
    )


def _invalidate_auto_permits(connection, config, now, reason):
    connection.execute(
        """UPDATE auto_permits SET status='REVOKED',revoked_at=?,revoked_reason=?
           WHERE status IN ('ACTIVE','PAUSED')""",
        (now, reason),
    )
    for account in config.accounts.values():
        key = "auto_generation:%s" % account.alias
        row = connection.execute(
            "SELECT value FROM system_state WHERE key=?", (key,)
        ).fetchone()
        try:
            generation = int(row["value"]) + 1 if row else 1
        except (TypeError, ValueError):
            generation = 1
        _set_state(connection, key, str(generation), now)


def _keyring(config):
    return KeyRing.load(config.key_file)


def _runtime_file(config, account, name):
    return os.path.join(config.data_dir, "qmt_runtime", account.adapter_instance, name)


def _write_authorization(config, account, modes, expires, actor):
    envelope = make_envelope(
        _keyring(config), "LOCAL_AUTHORIZATION",
        {
            "account_alias": account.alias, "allowed_modes": list(modes),
            "live_until": expires.isoformat(timespec="milliseconds"), "actor": actor,
        },
        max(1, int((expires - utc_now()).total_seconds())), "qmt-bridge-local-console",
    )
    atomic_write_json(_runtime_file(config, account, "local_authorization.json"), envelope)


def _validate_profile_binding(config, path, profile):
    allowed = {
        "protocol_version", "profile_id", "qmt_build", "broker_build",
        "account_type", "strategy_name", "adapter_binding", "verified",
        "key_id", "mappings", "signature",
    }
    if not isinstance(profile, dict) or set(profile) != allowed:
        raise BridgeError("INVALID_REQUEST", "profile fields do not match the v1 bound-profile schema")
    if profile.get("protocol_version") != "1.0":
        raise BridgeError("INVALID_REQUEST", "profile protocol_version must be 1.0")
    for name in ("profile_id", "qmt_build", "broker_build", "strategy_name"):
        value = profile.get(name)
        if not isinstance(value, str) or not value.strip() or "REPLACE" in value.upper():
            raise BridgeError("INVALID_REQUEST", "profile %s must be filled from P0 evidence" % name)
    if profile.get("verified") is not True or not isinstance(profile.get("mappings"), dict) or not profile["mappings"]:
        raise BridgeError("INVALID_REQUEST", "profile must be marked verified and contain mappings")
    for key, mapping in profile["mappings"].items():
        if not isinstance(key, str) or not isinstance(mapping, dict) or set(mapping) != {"op_type", "order_type", "price_type"}:
            raise BridgeError("INVALID_REQUEST", "each mapping must contain exactly op_type, order_type and price_type")
        if any(isinstance(mapping[name], bool) or not isinstance(mapping[name], int) for name in mapping):
            raise BridgeError("INVALID_REQUEST", "QMT mapping values must be integers")

    adapter_path = os.path.join(os.path.dirname(path), "qmt_adapter.json")
    try:
        with open(adapter_path, "r", encoding="utf-8") as stream:
            adapter = json.load(stream)
    except (OSError, ValueError) as exc:
        raise BridgeError("INVALID_REQUEST", "cannot read sibling qmt_adapter.json: %s" % exc)
    if not isinstance(adapter, dict):
        raise BridgeError("INVALID_REQUEST", "qmt_adapter.json must be an object")
    if os.path.abspath(str(adapter.get("mapping_profile", ""))) != path:
        raise BridgeError("INVALID_REQUEST", "qmt_adapter.json does not point to this profile")

    expected_profile = {
        "profile_id": adapter.get("expected_profile_id"),
        "qmt_build": adapter.get("expected_qmt_build"),
        "broker_build": adapter.get("expected_broker_build"),
    }
    for name, expected in expected_profile.items():
        if not isinstance(expected, str) or not expected.strip() or "REPLACE" in expected.upper():
            raise BridgeError("INVALID_REQUEST", "fill qmt_adapter.json expected_%s from P0 evidence first" % name)
        if profile.get(name) != expected:
            raise BridgeError("INVALID_REQUEST", "profile %s does not match qmt_adapter.json expected value" % name)

    binding = {
        "account_alias": adapter.get("account_alias"),
        "account_type": adapter.get("account_type"),
        "adapter_instance": adapter.get("adapter_instance"),
        "qmt_account_id": adapter.get("qmt_account_id"),
        "strategy_name": adapter.get("strategy_name"),
    }
    if profile.get("adapter_binding") != binding:
        raise BridgeError("INVALID_REQUEST", "profile adapter_binding does not match qmt_adapter.json")
    account = config.account(binding["account_alias"])
    if (binding["account_type"] != account.account_type or
            binding["adapter_instance"] != account.adapter_instance or
            profile.get("account_type") != account.account_type or
            profile.get("strategy_name") != binding["strategy_name"]):
        raise BridgeError("INVALID_REQUEST", "profile/adapter identity does not match bridge.json")
    return adapter


def _write_halt_files(config, halted, reason, actor):
    keyring = _keyring(config)
    for account in config.accounts.values():
        envelope = make_envelope(
            keyring, "LOCAL_HALT",
            {"halted": bool(halted), "reason": reason, "actor": actor, "changed_at": iso_now()},
            10 * 365 * 24 * 3600, "qmt-bridge-local-console",
        )
        atomic_write_json(_runtime_file(config, account, "local_halt.json"), envelope)


def run(args):
    config = load_config(args.config)
    database = _db(config)
    actor = os.environ.get("USERNAME") or os.environ.get("USER") or "local-user"
    if args.command == "status":
        with database.connect() as connection:
            state = {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM system_state")}
            approvals = connection.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE decision='APPROVED' AND used_at IS NULL AND expires_at>?",
                (iso_now(),),
            ).fetchone()["n"]
        return {"state": state, "active_approvals": approvals}

    now = iso_now()
    if args.command == "set-mode":
        if args.mode not in RUN_MODES:
            raise BridgeError("INVALID_REQUEST", "invalid run mode")
        if args.mode != "OBSERVE_ONLY" and args.confirm != args.mode:
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "pass --confirm %s" % args.mode)
        with database.transaction(immediate=True) as connection:
            halted = connection.execute("SELECT value FROM system_state WHERE key='halted'").fetchone()
            if halted and halted["value"] == "true":
                raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "clear the halt before changing mode")
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
            _invalidate_auto_permits(
                connection, config, now, "run mode changed to %s" % args.mode
            )
            _set_state(connection, "mode", args.mode, now)
            _audit(connection, "SET_MODE", actor, {"mode": args.mode})
        for account in config.accounts.values():
            _write_authorization(config, account, [], utc_now() + dt.timedelta(seconds=1), actor)
        return {"mode": args.mode}

    if args.command == "enable-live":
        config.account(args.account_alias)
        if args.confirm != "ENABLE-LIVE":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "pass --confirm ENABLE-LIVE")
        if args.minutes < 1 or args.minutes > 60:
            raise BridgeError("INVALID_REQUEST", "minutes must be between 1 and 60")
        expires = utc_now() + dt.timedelta(minutes=args.minutes)
        with database.transaction(immediate=True) as connection:
            _set_state(connection, "live_until:%s" % args.account_alias, expires.isoformat(timespec="milliseconds"), now)
            _audit(connection, "ENABLE_LIVE", actor, {"expires_at": expires.isoformat()}, args.account_alias)
        _write_authorization(config, config.account(args.account_alias), ["MANUAL_LIVE"], expires, actor)
        return {"account_alias": args.account_alias, "live_until": expires.isoformat(timespec="milliseconds")}

    if args.command == "approve-preview":
        with database.transaction(immediate=True) as connection:
            preview = connection.execute("SELECT * FROM trade_previews WHERE preview_id=?", (args.preview_id,)).fetchone()
            if not preview:
                raise BridgeError("PREVIEW_NOT_FOUND", "preview was not found")
            result = json.loads(preview["result_json"])
            if parse_time(preview["expires_at"]) <= utc_now():
                raise BridgeError("PREVIEW_EXPIRED", "preview has expired")
            if not result["risk"]["allowed"]:
                raise BridgeError("RISK_REJECTED", "cannot approve a rejected preview", {"reasons": result["risk"]["reasons"]})
            approval_id = new_id("approval")
            requested_expiry = utc_now() + dt.timedelta(seconds=args.ttl)
            expiry = min(requested_expiry, parse_time(preview["expires_at"]))
            connection.execute(
                "INSERT INTO approvals(approval_id,preview_id,decision,actor,reason,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (approval_id, args.preview_id, "APPROVED", actor, args.reason, now, expiry.isoformat(timespec="milliseconds")),
            )
            _audit(connection, "APPROVE_PREVIEW", actor, {"expires_at": expiry.isoformat()}, preview["account_alias"], args.preview_id)
        return {"approval_id": approval_id, "preview_id": args.preview_id, "expires_at": expiry.isoformat(timespec="milliseconds")}

    if args.command == "halt":
        with database.transaction(immediate=True) as connection:
            current_mode = connection.execute(
                "SELECT value FROM system_state WHERE key='mode'"
            ).fetchone()["value"]
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
            _invalidate_auto_permits(connection, config, now, "local trading halt")
            _set_state(connection, "halted", "true", now)
            _set_state(connection, "halt_reason", args.reason, now)
            _audit(connection, "HALT_TRADING", actor, {"reason": args.reason})
        _write_halt_files(config, True, args.reason, actor)
        return {"mode": current_mode, "halted": True, "reason": args.reason}

    if args.command == "clear-halt":
        if args.confirm != "CLEAR-HALT":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "pass --confirm CLEAR-HALT")
        with database.transaction(immediate=True) as connection:
            halted = connection.execute("SELECT value FROM system_state WHERE key='halted'").fetchone()
            if not halted or halted["value"] != "true":
                raise BridgeError("INVALID_REQUEST", "bridge is not halted")
            _set_state(connection, "halted", "false", now)
            _set_state(connection, "mode", "OBSERVE_ONLY", now)
            _set_state(connection, "halt_reason", "", now)
            _audit(connection, "CLEAR_HALT", actor, {"new_mode": "OBSERVE_ONLY", "reason": args.reason})
        _write_halt_files(config, False, args.reason, actor)
        for account in config.accounts.values():
            _write_authorization(config, account, [], utc_now() + dt.timedelta(seconds=1), actor)
        return {"mode": "OBSERVE_ONLY", "halted": False}

    if args.command == "sign-qmt-profile":
        if args.confirm != "VERIFIED-PROFILE":
            raise BridgeError("LOCAL_CONFIRMATION_REQUIRED", "pass --confirm VERIFIED-PROFILE")
        path = os.path.abspath(args.path)
        try:
            with open(path, "r", encoding="utf-8") as stream:
                profile = json.load(stream)
        except (OSError, ValueError) as exc:
            raise BridgeError("INVALID_REQUEST", "cannot read profile: %s" % exc)
        _validate_profile_binding(config, path, profile)
        ring = _keyring(config)
        profile["key_id"] = ring.active_key_id
        profile["signature"] = ring.sign(profile)
        atomic_write_json(path, profile)
        with database.transaction(immediate=True) as connection:
            _audit(connection, "SIGN_QMT_PROFILE", actor, {"path": path, "profile_id": profile.get("profile_id")})
        return {"path": path, "profile_id": profile.get("profile_id"), "signed": True}

    if args.command == "audit":
        with database.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (args.limit,)
            ).fetchall()]
    raise BridgeError("INVALID_REQUEST", "unknown console command")


def build_parser():
    parser = argparse.ArgumentParser(description="Local-only QMT bridge control console")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    set_mode = sub.add_parser("set-mode")
    set_mode.add_argument("mode", choices=RUN_MODES)
    set_mode.add_argument("--confirm")
    live = sub.add_parser("enable-live")
    live.add_argument("account_alias")
    live.add_argument("--minutes", type=int, default=10)
    live.add_argument("--confirm")
    approve = sub.add_parser("approve-preview")
    approve.add_argument("preview_id")
    approve.add_argument("--ttl", type=int, default=30)
    approve.add_argument("--reason", default="local operator approval")
    halt = sub.add_parser("halt")
    halt.add_argument("reason")
    clear = sub.add_parser("clear-halt")
    clear.add_argument("--reason", required=True)
    clear.add_argument("--confirm")
    audit = sub.add_parser("audit")
    audit.add_argument("--limit", type=int, default=50)
    sign_profile = sub.add_parser("sign-qmt-profile")
    sign_profile.add_argument("path")
    sign_profile.add_argument("--confirm")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
