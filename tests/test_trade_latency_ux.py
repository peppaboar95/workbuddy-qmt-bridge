import datetime as dt
import json
import os
import tempfile
import threading
import time
import unittest

from workbuddy_qmt.config import AccountConfig, BridgeConfig, RiskLimits
from workbuddy_qmt.core import BridgeCore
from workbuddy_qmt.db import Database
from workbuddy_qmt.errors import BridgeError
from workbuddy_qmt.file_queue import FileQueue
from workbuddy_qmt.mcp_server import TOOLS
from workbuddy_qmt.security import KeyRing
from workbuddy_qmt.worker import WORKER_QUEUE_INTERVAL_SECONDS


class DummyIngester:
    def scan_once(self):
        return {"processed": 0, "dead_lettered": 0}


class TradeLatencyUxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = AccountConfig(
            alias="main_stock", account_type="STOCK", adapter_instance="adapter_1"
        )
        self.config = BridgeConfig(
            data_dir=self.temp.name,
            default_mode="OBSERVE_ONLY",
            accounts={self.account.alias: self.account},
            risk_limits={self.account.alias: RiskLimits()},
        )
        self.database = Database(os.path.join(self.temp.name, "state", "bridge.db"))
        self.database.initialize("OBSERVE_ONLY")
        self.keyring = KeyRing({"test": b"x" * 32}, "test")
        self.queue = FileQueue(self.temp.name, self.keyring)
        self.queue.ensure_partition(self.account.adapter_instance)
        self.core = BridgeCore(
            self.config, self.database, self.keyring, self.queue, DummyIngester()
        )
        captured = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO account_snapshots(snapshot_id,account_alias,captured_at,received_at,payload_json) VALUES(?,?,?,?,?)",
                (
                    "acct_1", self.account.alias, captured, captured,
                    json.dumps({
                        "snapshot_id": "acct_1", "captured_at": captured,
                        "available_cash": 900000.0, "total_asset": 1000000.0,
                    }),
                ),
            )
            connection.execute(
                "INSERT INTO position_snapshot_runs(account_alias,snapshot_id,captured_at,received_at) VALUES(?,?,?,?)",
                (self.account.alias, "pos_1", captured, captured),
            )
            quote = {
                "snapshot_id": "quote_1", "captured_at": captured,
                "tick_at": captured, "symbol": "600000.SH", "last_price": 10.0,
                "bid_price": 9.99, "ask_price": 10.0, "price_tick": 0.01,
                "lower_limit": 8.0, "upper_limit": 12.0,
                "trading_phase": "CONTINUOUS_AUCTION",
                "dynamic_cage": {"applied": True, "buy_upper": 10.2, "sell_lower": 9.8},
            }
            connection.execute(
                "INSERT INTO quote_snapshots(account_alias,snapshot_id,symbol,captured_at,received_at,payload_json) VALUES(?,?,?,?,?,?)",
                (
                    self.account.alias, "quote_1", "600000.SH", captured,
                    captured, json.dumps(quote),
                ),
            )

    @staticmethod
    def _request(signal_id="signal_1"):
        return {
            "account_alias": "main_stock",
            "account_type": "STOCK",
            "asset_type": "STOCK",
            "instrument": {"canonical_symbol": "600000.SH"},
            "action": "BUY",
            "sizing": {"type": "FIXED_VOLUME", "value": 100},
            "price_policy": {"type": "FIXED_LIMIT", "limit_price": 10.0},
            "execution_mode": "OBSERVE_ONLY",
            "source": {"type": "manual", "signal_id": signal_id},
        }

    def _complete_next_sync(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.database.connect() as connection:
                row = connection.execute(
                    "SELECT message_id FROM qmt_commands WHERE command_type='REQUEST_SYNC' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
            if row:
                with self.database.transaction(immediate=True) as connection:
                    connection.execute(
                        "UPDATE qmt_commands SET status='SYNC_COMPLETED' WHERE message_id=?",
                        (row["message_id"],),
                    )
                return
            time.sleep(0.01)
        self.fail("prepare_trade did not queue a sync command")

    def test_prepare_trade_combines_sync_and_preview_without_submitting(self):
        updater = threading.Thread(target=self._complete_next_sync)
        updater.start()
        result = self.core.prepare_trade(self._request(), timeout_seconds=1)
        updater.join(timeout=2)

        self.assertEqual(result["sync"]["status"], "SYNC_COMPLETED")
        self.assertEqual(result["sync"]["symbols"], ["600000.SH"])
        self.assertEqual(
            result["sync"]["scopes"],
            ["ACCOUNT", "POSITION", "ORDER", "DEAL", "QUOTE"],
        )
        self.assertTrue(result["preview"]["risk"]["allowed"])
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS n FROM trade_previews").fetchone()["n"],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS n FROM trade_intents").fetchone()["n"],
                0,
            )

    def test_prepare_trade_times_out_without_creating_preview(self):
        with self.assertRaises(BridgeError) as captured:
            self.core.prepare_trade(self._request(), timeout_seconds=0.5)
        self.assertEqual(captured.exception.code, "SYNC_TIMEOUT")
        self.assertTrue(captured.exception.details["retry_safe"])
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS n FROM trade_previews").fetchone()["n"],
                0,
            )

    def test_wait_trade_intent_observes_one_status_change(self):
        preview = self.core.preview_trade(self._request())
        intent = self.core.submit_trade_intent(preview["preview_id"])

        def acknowledge():
            time.sleep(0.05)
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE trade_intents SET status='SUBMIT_CALLED' WHERE intent_id=?",
                    (intent["intent_id"],),
                )

        updater = threading.Thread(target=acknowledge)
        updater.start()
        result = self.core.wait_trade_intent(intent["intent_id"], timeout_seconds=1)
        updater.join(timeout=2)

        self.assertTrue(result["status_observed"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["intent"]["status"], "SUBMIT_CALLED")

    def test_wait_trade_intent_timeout_does_not_resubmit(self):
        preview = self.core.preview_trade(self._request())
        intent = self.core.submit_trade_intent(preview["preview_id"])
        result = self.core.wait_trade_intent(intent["intent_id"], timeout_seconds=0.1)

        self.assertTrue(result["timed_out"])
        self.assertEqual(result["intent"]["status"], "QUEUED")
        with self.database.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS n FROM trade_intents").fetchone()["n"],
                1,
            )

    def test_database_serializes_worker_writers(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        order = []

        def first_writer():
            with self.database.transaction(immediate=True):
                order.append("first")
                first_entered.set()
                release_first.wait(timeout=2)

        def second_writer():
            first_entered.wait(timeout=2)
            with self.database.transaction(immediate=True):
                order.append("second")

        first = threading.Thread(target=first_writer)
        second = threading.Thread(target=second_writer)
        first.start()
        second.start()
        self.assertTrue(first_entered.wait(timeout=2))
        time.sleep(0.05)
        self.assertEqual(order, ["first"])
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertEqual(order, ["first", "second"])

    def test_fast_poll_defaults_and_mcp_guidance(self):
        self.assertEqual(WORKER_QUEUE_INTERVAL_SECONDS, 0.25)
        tools = {tool["name"]: tool for tool in TOOLS}
        self.assertEqual(len(tools), 31)
        self.assertIn("prepare_trade", tools)
        self.assertIn("wait_trade_intent", tools)
        self.assertIn("Batch every target symbol", tools["request_sync"]["description"])
        self.assertIn("call wait_trade_intent once", tools["submit_trade_intent"]["description"])


if __name__ == "__main__":
    unittest.main()
