import argparse
import base64
import json
import os
import secrets
import sys

from .config import load_config
from .db import Database
from .modes import LEGACY_MODE_MIGRATIONS
from .util import atomic_write_bytes, atomic_write_json, iso_now


def _create_private(path, data):
    if os.path.exists(path):
        return False
    atomic_write_bytes(path, data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def initialize(root, enabled_account_types=None):
    root = os.path.abspath(root)
    if enabled_account_types is None:
        enabled_account_types = {"STOCK"}
    else:
        enabled_account_types = {str(value).upper() for value in enabled_account_types}
        if not enabled_account_types or enabled_account_types - {"STOCK", "CREDIT"}:
            raise ValueError("enabled_account_types must contain STOCK and/or CREDIT")
    config_dir = os.path.join(root, "config")
    data_dir = os.path.join(root, "data")
    secrets_dir = os.path.join(data_dir, "secrets")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(secrets_dir, exist_ok=True)
    key_id = "bridge-local-01"
    key_value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    _create_private(
        os.path.join(secrets_dir, "message_keys.json"),
        json.dumps({"active_key_id": key_id, "keys": {key_id: key_value}}, indent=2).encode("utf-8"),
    )
    _create_private(os.path.join(secrets_dir, "worker.token"), secrets.token_urlsafe(32).encode("ascii"))
    config_path = os.path.join(config_dir, "bridge.json")
    legacy_default_mode = None
    if not os.path.exists(config_path):
        config = {
            "data_dir": "../data",
            "host": "127.0.0.1",
            "port": 17642,
            "default_mode": "OBSERVE_ONLY",
            "max_message_bytes": 65536,
            "key_file": "../data/secrets/message_keys.json",
            "worker_token_file": "../data/secrets/worker.token",
            "accounts": [
                {
                    "alias": "main_stock", "account_type": "STOCK", "adapter_instance": "qmt_stock_01",
                    "enabled": "STOCK" in enabled_account_types, "lot_size": 100, "instrument_allowlist": [],
                    "limited_auto_credit_enabled": False,
                    "order_volume_rules": {
                        "default": {"min_buy": 100, "min_sell": 100},
                        "prefix_overrides": [{
                            "prefixes": ["688", "689"],
                            "min_buy": 200, "min_sell": 200,
                        }],
                    },
                    "risk_limits": {
                        "max_order_notional": 200000, "max_order_volume": 500000,
                        "max_daily_notional": 2000000,
                        "max_snapshot_age_seconds": 90,
                        "max_quote_age_seconds": 30,
                        "max_credit_snapshot_age_seconds": 600,
                        "preview_ttl_seconds": 120,
                        "command_ttl_seconds": 60,
                        "max_auto_authorization_minutes": 720,
                        "max_auto_session_notional": 2000000,
                        "max_auto_orders": 1000,
                        "min_auto_order_interval_seconds": 1,
                        "max_auto_concurrent_orders": 20,
                        "max_auto_symbol_position_notional": 500000,
                        "max_auto_account_drawdown": 100000,
                        "auto_heartbeat_max_age_seconds": 15,
                        "auto_max_queue_depth": 20,
                    },
                },
                {
                    "alias": "main_credit", "account_type": "CREDIT", "adapter_instance": "qmt_credit_01",
                    "enabled": "CREDIT" in enabled_account_types, "lot_size": 100, "instrument_allowlist": [],
                    "limited_auto_credit_enabled": False,
                    "credit_action_mapping": {
                        "buy": "MARGIN_BUY",
                        "sell": "COLLATERAL_SELL",
                    },
                    "order_volume_rules": {
                        "default": {"min_buy": 100, "min_sell": 100},
                        "prefix_overrides": [{
                            "prefixes": ["688", "689"],
                            "min_buy": 200, "min_sell": 200,
                        }],
                    },
                    "risk_limits": {
                        "max_order_notional": 100000, "max_order_volume": 500000,
                        "max_daily_notional": 1000000,
                        "max_snapshot_age_seconds": 90,
                        "max_quote_age_seconds": 30,
                        "max_credit_snapshot_age_seconds": 600,
                        "preview_ttl_seconds": 120,
                        "command_ttl_seconds": 60,
                        "min_maintenance_ratio": 1.5, "credit_query_cooldown_seconds": 180,
                        "max_auto_authorization_minutes": 720,
                        "max_auto_session_notional": 1000000,
                        "max_auto_orders": 500,
                        "min_auto_order_interval_seconds": 1,
                        "max_auto_concurrent_orders": 10,
                        "max_auto_symbol_position_notional": 250000,
                        "max_auto_account_drawdown": 50000,
                        "auto_heartbeat_max_age_seconds": 15,
                        "auto_max_queue_depth": 20,
                    },
                },
            ],
        }
        atomic_write_json(config_path, config)
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as stream:
                existing = json.load(stream)
        except (OSError, ValueError):
            existing = None
        if isinstance(existing, dict) and existing.get("default_mode") in LEGACY_MODE_MIGRATIONS:
            legacy_default_mode = existing["default_mode"]
            existing["default_mode"] = LEGACY_MODE_MIGRATIONS[legacy_default_mode]
            atomic_write_json(config_path, existing)
    loaded = load_config(config_path)
    for account in loaded.accounts.values():
        for folder in (
            "commands", "command_acks", "events", "control", "control_acks",
            "archive", "dead_letter",
        ):
            os.makedirs(os.path.join(loaded.data_dir, "queue", account.adapter_instance, folder), exist_ok=True)
        os.makedirs(os.path.join(loaded.data_dir, "qmt_runtime", account.adapter_instance, "execution_journal"), exist_ok=True)
        os.makedirs(os.path.join(loaded.data_dir, "qmt_runtime", account.adapter_instance, "heartbeat"), exist_ok=True)
    database_path = os.path.join(loaded.data_dir, "state", "bridge.db")
    database_existed = os.path.exists(database_path)
    database = Database(database_path)
    database.initialize(loaded.default_mode)
    if legacy_default_mode == "HALTED" and not database_existed:
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE system_state SET value='true',updated_at=? WHERE key='halted'",
                (iso_now(),),
            )
    return config_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Initialize a WorkBuddy-QMT bridge runtime")
    parser.add_argument("--root", default="runtime", help="runtime root directory")
    args = parser.parse_args(argv)
    path = initialize(args.root)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
