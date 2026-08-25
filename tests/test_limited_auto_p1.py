import datetime as dt
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

import workbuddy_qmt
from workbuddy_qmt.config import AccountConfig, BridgeConfig, RiskLimits
from workbuddy_qmt.core import BridgeCore
from workbuddy_qmt.db import Database
from workbuddy_qmt.file_queue import FileQueue
from workbuddy_qmt.security import KeyRing


class DummyIngester:
    def scan_once(self):
        return {"processed": 0, "dead_lettered": 0}

    def reconcile_terminal_evidence(self):
        return None


class LimitedAutoP1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixed_now = dt.datetime(2026, 8, 24, 2, 0, tzinfo=dt.timezone.utc)
        self.account = AccountConfig(
            alias="main_stock", account_type="STOCK", adapter_instance="adapter_1"
        )
        self.limits = RiskLimits(
            max_order_notional=200000.0,
            max_order_volume=500000,
            max_daily_notional=2000000.0,
            max_auto_authorization_minutes=720,
            max_auto_session_notional=1000000.0,
            max_auto_orders=100,
            max_auto_concurrent_orders=10,
            max_auto_symbol_position_notional=500000.0,
            max_auto_account_drawdown=100000.0,
        )
        self.config = BridgeConfig(
            data_dir=self.temp.name,
            default_mode="OBSERVE_ONLY",
            accounts={self.account.alias: self.account},
            risk_limits={self.account.alias: self.limits},
        )
        self.database = Database(os.path.join(self.temp.name, "state", "bridge.db"))
        self.database.initialize("OBSERVE_ONLY")
        self.keyring = KeyRing({"test": b"x" * 32}, "test")
        self.queue = FileQueue(self.temp.name, self.keyring)
        self.queue.ensure_partition(self.account.adapter_instance)
        self.core = BridgeCore(
            self.config, self.database, self.keyring, self.queue, DummyIngester()
        )
        self.now_patch = mock.patch("workbuddy_qmt.core.utc_now", return_value=self.fixed_now)
        self.now_patch.start()
        captured = self.fixed_now.isoformat(timespec="milliseconds")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE system_state SET value='LIMITED_AUTO' WHERE key='mode'"
            )
            connection.execute(
                """INSERT INTO account_snapshots(
                       snapshot_id,account_alias,captured_at,received_at,payload_json
                   ) VALUES(?,?,?,?,?)""",
                (
                    "acct_1", self.account.alias, captured, captured,
                    json.dumps({
                        "snapshot_id": "acct_1", "captured_at": captured,
                        "available_cash": 900000.0, "total_asset": 1000000.0,
                        "market_value": 100000.0,
                    }),
                ),
            )
            connection.execute(
                "INSERT INTO position_snapshot_runs(account_alias,snapshot_id,captured_at,received_at) VALUES(?,?,?,?)",
                (self.account.alias, "pos_1", captured, captured),
            )
            quote = {
                "snapshot_id": "quote_1", "captured_at": captured, "tick_at": captured,
                "symbol": "600000.SH", "last_price": 10.0, "bid_price": 9.99,
                "ask_price": 10.0, "price_tick": 0.01, "lower_limit": 8.0,
                "upper_limit": 12.0, "trading_phase": "CONTINUOUS_AUCTION",
                "dynamic_cage": {"applied": True, "buy_upper": 10.2, "sell_lower": 9.8},
            }
            connection.execute(
                """INSERT INTO quote_snapshots(
                       account_alias,snapshot_id,symbol,captured_at,received_at,payload_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    self.account.alias, "quote_1", "600000.SH", captured, captured,
                    json.dumps(quote),
                ),
            )
            heartbeat = {
                "status": "READY", "qmt_mode": "LIMITED_AUTO",
                "adapter_instance": self.account.adapter_instance, "local_halt": False,
            }
            connection.execute(
                """INSERT INTO heartbeats(
                       adapter_instance,account_alias,status,occurred_at,received_at,payload_json
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    self.account.adapter_instance, self.account.alias, "READY",
                    captured, captured, json.dumps(heartbeat),
                ),
            )

    def tearDown(self):
        self.now_patch.stop()
        self.temp.cleanup()

    def _authorize(self, max_orders=2):
        return self.core.authorize_limited_auto(
            account_alias=self.account.alias,
            symbols=["600000.SH"],
            actions=["BUY", "SELL"],
            source_type="monitor",
            rule_set_id="breakout",
            rule_version="v1",
            minutes=300,
            max_order_notional=20000.0,
            max_order_volume=10000,
            max_session_notional=50000.0,
            max_orders=max_orders,
            min_order_interval_seconds=1,
            max_concurrent_orders=2,
            max_symbol_position_notional=100000.0,
            max_account_drawdown=10000.0,
            max_consecutive_failures=2,
            reason="P1 unit test",
            confirm="AUTHORIZE-LIMITED-AUTO-P1",
        )

    @staticmethod
    def _request(signal_id="signal_1", rule_version="v1"):
        return {
            "account_alias": "main_stock",
            "account_type": "STOCK",
            "asset_type": "STOCK",
            "instrument": {"canonical_symbol": "600000.SH"},
            "action": "BUY",
            "sizing": {"type": "FIXED_VOLUME", "value": 100},
            "price_policy": {"type": "FIXED_LIMIT", "limit_price": 10.0},
            "execution_mode": "LIMITED_AUTO",
            "source": {
                "type": "monitor", "signal_id": signal_id,
                "rule_set_id": "breakout", "rule_version": rule_version,
            },
        }

    def test_schema_migrates_and_permit_is_structured(self):
        status = self._authorize()
        self.assertTrue(status["active"])
        self.assertEqual(status["policy"]["allowed_symbols"], ["600000.SH"])
        self.assertEqual(status["remaining_orders"], 2)
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT version FROM schema_meta").fetchone()["version"], 2
            )

    def test_preview_submit_reserves_budget_and_binds_command(self):
        permit = self._authorize(max_orders=1)
        preview = self.core.preview_trade(self._request())
        self.assertTrue(preview["risk"]["allowed"], preview["risk"]["reasons"])
        intent = self.core.submit_trade_intent(preview["preview_id"])
        binding = intent["payload"]["auto_permit"]
        self.assertEqual(binding["permit_id"], permit["permit_id"])
        self.assertEqual(binding["policy_hash"], permit["policy_hash"])
        status = self.core.get_limited_auto_status(self.account.alias)
        self.assertEqual(status["usage"]["order_count"], 1)
        self.assertEqual(status["remaining_orders"], 0)
        rejected = self.core.preview_trade(self._request("signal_2"))
        self.assertFalse(rejected["risk"]["allowed"])
        self.assertIn("AUTO_ORDER_COUNT_EXCEEDED", rejected["risk"]["reasons"])

    def test_strategy_version_is_enforced(self):
        self._authorize()
        preview = self.core.preview_trade(self._request(rule_version="v2"))
        self.assertFalse(preview["risk"]["allowed"])
        self.assertIn("AUTO_STRATEGY_BINDING_MISMATCH", preview["risk"]["reasons"])

    def test_pause_and_resume_rotate_generation(self):
        original = self._authorize()
        self.assertTrue(self.core._pause_active_auto_permit(
            self.account.alias, ["AUTO_SUBMIT_UNKNOWN_PRESENT"]
        ))
        paused = self.core.get_limited_auto_status(self.account.alias)
        self.assertEqual(paused["status"], "PAUSED")
        resumed = self.core.resume_limited_auto(
            self.account.alias, "fault resolved", "RESUME-LIMITED-AUTO-P1"
        )
        self.assertTrue(resumed["active"])
        self.assertGreater(resumed["generation"], original["generation"])
        self.assertNotEqual(resumed["policy_hash"], original["policy_hash"])

    def test_readiness_dead_letter_pauses_active_permit(self):
        self._authorize()
        dead_letter = os.path.join(
            self.temp.name, "queue", self.account.adapter_instance,
            "dead_letter", "fault.json",
        )
        with open(dead_letter, "w", encoding="utf-8") as stream:
            stream.write("{}")
        result = self.core.check_limited_auto_readiness(self.account.alias)
        self.assertFalse(result["ready"])
        self.assertIn("AUTO_DEAD_LETTER_PRESENT", result["reasons"])
        self.assertEqual(
            self.core.get_limited_auto_status(self.account.alias)["status"], "PAUSED"
        )

    def test_global_halt_revokes_permit(self):
        status = self._authorize()
        result = self.core.halt_trading("P1 test halt")
        self.assertTrue(result["halted"])
        revoked = self.core.get_limited_auto_status(self.account.alias)
        self.assertEqual(revoked["permit_id"], status["permit_id"])
        self.assertEqual(revoked["status"], "REVOKED")

    def test_adapter_rejects_tampered_strategy_binding(self):
        status = self._authorize()
        adapter_path = os.path.join(
            os.path.dirname(workbuddy_qmt.__file__), "assets",
            "qmt_embedded_adapter.py",
        )
        spec = importlib.util.spec_from_file_location("p1_adapter", adapter_path)
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        adapter._CONFIG = {
            "account_alias": self.account.alias,
            "account_type": "STOCK",
            "adapter_instance": self.account.adapter_instance,
            "data_dir": self.temp.name,
            "qmt_mode": "LIMITED_AUTO",
            "limited_auto_credit_enabled": False,
            "adapter_max_volume": 500000,
            "adapter_max_notional": 200000.0,
            "adapter_max_auto_session_notional": 1000000.0,
            "adapter_max_auto_orders": 100,
            "adapter_min_auto_order_interval_seconds": 1,
            "adapter_max_auto_concurrent_orders": 10,
            "adapter_max_auto_symbol_position_notional": 500000.0,
            "adapter_max_auto_account_drawdown": 100000.0,
        }
        adapter._KEYS = {"test": b"x" * 32}
        adapter._ACTIVE_KEY = "test"
        os.makedirs(
            os.path.join(
                self.temp.name, "qmt_runtime", self.account.adapter_instance,
                "execution_journal",
            ),
            exist_ok=True,
        )
        authorization_path = os.path.join(
            self.temp.name, "qmt_runtime", self.account.adapter_instance,
            "local_authorization.json",
        )
        with open(authorization_path, "r", encoding="utf-8") as stream:
            authorization = json.load(stream)
        command = {
            "execution_mode": "LIMITED_AUTO", "qmt_symbol": "600000.SH",
            "action": "BUY", "resolved_order": {"volume": 100, "limit_price": 10.0},
            "source": {"type": "monitor", "rule_set_id": "breakout", "rule_version": "v1"},
            "auto_permit": {
                "permit_id": status["permit_id"], "policy_hash": status["policy_hash"],
                "generation": status["generation"],
            },
        }
        with mock.patch.object(adapter, "_now", return_value=self.fixed_now):
            policy = adapter._validate_auto_authorization(authorization["payload"], command)
            self.assertEqual(policy["permit_id"], status["permit_id"])
            adapter._pause_auto_locally(command, "uncertain submit")
            with self.assertRaises(RuntimeError):
                adapter._validate_auto_authorization(authorization["payload"], command)
            os.remove(adapter._auto_pause_path())
            command["source"]["rule_version"] = "tampered"
            with self.assertRaises(RuntimeError):
                adapter._validate_auto_authorization(authorization["payload"], command)


if __name__ == "__main__":
    unittest.main()
