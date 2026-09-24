import datetime as dt
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbuddy_qmt.assets import qmt_embedded_adapter as embedded_adapter
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
                "dynamic_cage": {
                    "applied": True,
                    "rule": "SSE_SZSE_2_PERCENT_OR_10_TICKS",
                    "buy_upper": 10.2,
                    "sell_lower": 9.8,
                },
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

    def test_submit_ignores_moving_cage_benchmarks_and_subcent_cash_noise(self):
        preview = self.core.preview_trade(self._request())
        with self.database.transaction(immediate=True) as connection:
            account_row = connection.execute(
                "SELECT payload_json FROM account_snapshots WHERE snapshot_id='acct_1'"
            ).fetchone()
            account = json.loads(account_row["payload_json"])
            account["available_cash"] = 900000.000000125
            connection.execute(
                "UPDATE account_snapshots SET payload_json=? WHERE snapshot_id='acct_1'",
                (json.dumps(account),),
            )

            quote_row = connection.execute(
                "SELECT payload_json FROM quote_snapshots WHERE snapshot_id='quote_1'"
            ).fetchone()
            quote = json.loads(quote_row["payload_json"])
            quote.update({"last_price": 10.03, "bid_price": 10.02, "ask_price": 10.03})
            quote["dynamic_cage"] = {
                "applied": True,
                "rule": "SSE_SZSE_2_PERCENT_OR_10_TICKS",
                "buy_benchmark": 10.03,
                "sell_benchmark": 10.02,
                "buy_upper": 10.23,
                "sell_lower": 9.82,
            }
            connection.execute(
                "UPDATE quote_snapshots SET payload_json=? WHERE snapshot_id='quote_1'",
                (json.dumps(quote),),
            )

        intent = self.core.submit_trade_intent(preview["preview_id"])
        self.assertEqual(intent["status"], "QUEUED")

    def test_submit_rejects_cage_change_that_changes_resolved_order(self):
        request = self._request()
        request["price_policy"]["limit_price"] = 10.5
        preview = self.core.preview_trade(request)
        self.assertEqual(preview["resolved_order"]["limit_price"], 10.2)

        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload_json FROM quote_snapshots WHERE snapshot_id='quote_1'"
            ).fetchone()
            quote = json.loads(row["payload_json"])
            quote["dynamic_cage"]["buy_upper"] = 10.21
            connection.execute(
                "UPDATE quote_snapshots SET payload_json=? WHERE snapshot_id='quote_1'",
                (json.dumps(quote),),
            )

        with self.assertRaises(BridgeError) as captured:
            self.core.submit_trade_intent(preview["preview_id"])
        self.assertEqual(captured.exception.code, "RISK_REJECTED")
        self.assertEqual(captured.exception.details["reason"], "SNAPSHOT_CHANGED")

    def test_submit_rejects_material_cash_change(self):
        preview = self.core.preview_trade(self._request())
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload_json FROM account_snapshots WHERE snapshot_id='acct_1'"
            ).fetchone()
            account = json.loads(row["payload_json"])
            account["available_cash"] = 899999.99
            connection.execute(
                "UPDATE account_snapshots SET payload_json=? WHERE snapshot_id='acct_1'",
                (json.dumps(account),),
            )

        with self.assertRaises(BridgeError) as captured:
            self.core.submit_trade_intent(preview["preview_id"])
        self.assertEqual(captured.exception.code, "RISK_REJECTED")
        self.assertEqual(captured.exception.details["reason"], "SNAPSHOT_CHANGED")

    def test_future_quote_times_are_fresh_but_stale_signal_evidence_is_rejected(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat(
            timespec="milliseconds"
        )
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT payload_json FROM quote_snapshots WHERE snapshot_id='quote_1'"
            ).fetchone()
            quote = json.loads(row["payload_json"])
            quote["tick_at"] = future
            connection.execute(
                "UPDATE quote_snapshots SET payload_json=? WHERE snapshot_id='quote_1'",
                (json.dumps(quote),),
            )

        fresh_request = self._request("future_quote")
        fresh_request["signal_evidence"] = {"quote_at": future}
        fresh = self.core.preview_trade(fresh_request)
        self.assertNotIn("MARKET_DATA_STALE", fresh["risk"]["reasons"])
        self.assertLess(fresh["price_guard"]["quote_age_seconds"], 1)

        stale_request = self._request("stale_signal")
        stale_request["signal_evidence"] = {
            "quote_at": (
                dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=31)
            ).isoformat(timespec="milliseconds")
        }
        stale = self.core.preview_trade(stale_request)
        self.assertIn("MARKET_DATA_STALE", stale["risk"]["reasons"])
        self.assertLess(stale["price_guard"]["quote_age_seconds"], 1)

    def test_health_exposes_worker_adapter_mode_mismatch(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        heartbeat = {
            "type": "HEARTBEAT",
            "status": "READY",
            "qmt_mode": "SIM_SIGNAL",
            "adapter_instance": self.account.adapter_instance,
        }
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO heartbeats(adapter_instance,account_alias,status,occurred_at,received_at,payload_json) VALUES(?,?,?,?,?,?)",
                (
                    self.account.adapter_instance,
                    self.account.alias,
                    "READY",
                    now,
                    now,
                    json.dumps(heartbeat),
                ),
            )

        account = self.core.qmt_health()["accounts"][0]
        self.assertEqual(account["adapter_mode"], "SIM_SIGNAL")
        self.assertFalse(account["mode_matches"])
        self.assertFalse(account["ready"])

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

    def test_adapter_uses_callbacks_with_bounded_full_reconciliation(self):
        original = {
            "config": embedded_adapter._CONFIG,
            "last": embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT,
            "requested": embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED,
        }

        def restore():
            embedded_adapter._CONFIG = original["config"]
            embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT = original["last"]
            embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED = original["requested"]

        self.addCleanup(restore)
        embedded_adapter._CONFIG = {"order_deal_reconcile_seconds": 30}
        embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT = time.time()
        embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED = False

        with (
            mock.patch.object(embedded_adapter, "_emit_snapshots") as emit,
            mock.patch.object(embedded_adapter, "_write_p0_probe"),
        ):
            embedded_adapter.snapshot_task("context")
            emit.assert_called_once_with("context", ["ACCOUNT", "POSITION"])

        with mock.patch.object(embedded_adapter, "_emit_snapshots") as emit:
            embedded_adapter.order_deal_reconcile_task("context")
            emit.assert_not_called()
            embedded_adapter._request_order_deal_reconcile()
            embedded_adapter.order_deal_reconcile_task("context")
            emit.assert_called_once_with("context", ["ORDER", "DEAL"])
            self.assertFalse(embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED)

        with mock.patch.object(embedded_adapter, "_write_event"):
            embedded_adapter.orderError_callback(None, {}, "broker rejected")
        self.assertTrue(embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED)

    def test_explicit_order_deal_sync_resets_reconcile_deadline(self):
        original = {
            "last": embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT,
            "requested": embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED,
        }

        def restore():
            embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT = original["last"]
            embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED = original["requested"]

        self.addCleanup(restore)
        embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT = 0.0
        embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED = True
        with (
            mock.patch.object(embedded_adapter, "_emit_snapshots") as emit,
            mock.patch.object(embedded_adapter, "_ack") as ack,
        ):
            embedded_adapter._request_sync(
                {"scopes": ["ACCOUNT", "POSITION", "ORDER", "DEAL"], "symbols": []},
                "msg_sync",
            )
        emit.assert_called_once()
        ack.assert_called_once()
        self.assertGreater(embedded_adapter._LAST_ORDER_DEAL_RECONCILE_AT, 0)
        self.assertFalse(embedded_adapter._ORDER_DEAL_RECONCILE_REQUESTED)


if __name__ == "__main__":
    unittest.main()
