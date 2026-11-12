import builtins
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from workbuddy_qmt.assets.mmap_channel import MmapChannel
from workbuddy_qmt.bootstrap import initialize
from workbuddy_qmt.config import effective_mmap_name, load_config
from workbuddy_qmt.manager import generate_qmt_bundle
from workbuddy_qmt.mmap_adapter import render_mmap_adapter
from workbuddy_qmt.security import KeyRing
from workbuddy_qmt.util import atomic_write_json, iso_now
from workbuddy_qmt.worker import build_runtime


class _QmtContext:
    def __init__(self):
        self.account_id = None
        self.schedules = {}

    def set_account(self, account_id):
        self.account_id = account_id

    def schedule_run(self, callback, first, repeat, interval, name):
        self.schedules[name] = callback


@unittest.skipUnless(os.name == "nt", "requires Windows named mmap")
class MmapIntegrationTests(unittest.TestCase):
    """Real named transport and SQLite, with only QMT native calls simulated."""

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("workbuddy_qmt.mmap_gateway"),
                             "Worker mmap gateway is not implemented")
        from workbuddy_qmt.mmap_gateway import MmapGateway
        self.gateway_class = MmapGateway
        # Keep FileQueue's atomic filename below legacy Windows MAX_PATH even
        # when the host's default sandbox Temp directory has a long prefix.
        self.temp = tempfile.TemporaryDirectory(prefix=".mmap-i-", dir=os.getcwd())
        self.addCleanup(self.temp.cleanup)
        self.config_path = initialize(self.temp.name, {"STOCK"})
        raw = self.read_json(self.config_path)
        raw["accounts"][0].update(adapter_transport="mmap", mmap_size=131072)
        atomic_write_json(self.config_path, raw)
        self.config = load_config(self.config_path)
        self.account = self.config.account("main_stock")
        self.bundle = generate_qmt_bundle(self.config_path, self.account.alias, "sim-stock-001")
        adapter = self.read_json(self.bundle["adapter_config"])
        profile = self.read_json(self.bundle["mapping_profile"])
        profile.update(profile_id="verified-integration-profile", qmt_build="test-qmt",
                       broker_build="test-broker", verified=True,
                       mappings={"STOCK:STOCK:BUY:LIMIT": {
                           "op_type": 23, "order_type": 1101, "price_type": 11,
                       }})
        for name in ("profile_id", "qmt_build", "broker_build"):
            adapter["expected_" + name] = profile[name]
        keyring = KeyRing.load(self.config.key_file)
        profile["signature"] = keyring.sign(profile)
        atomic_write_json(self.bundle["adapter_config"], adapter)
        atomic_write_json(self.bundle["mapping_profile"], profile)
        template = Path("src/workbuddy_qmt/assets/qmt_embedded_adapter.py").read_text(encoding="utf-8")
        self.source = render_mmap_adapter(
            template, adapter, self.read_json(self.config.key_file),
            effective_mmap_name(self.config.data_dir, self.account), self.account.mmap_size,
        )
        self.lock = threading.RLock()
        self.worker_channels = []
        self.gateways = []
        self.adapter_channels = []
        self.addCleanup(self.close_channels)
        self.submission_evidence = []
        self.passorder = mock.Mock(side_effect=self.broker_submit)
        self.start_worker()
        self.start_qmt()

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def close_channels(self):
        for gateway in self.gateways:
            gateway.close()
        for channel in self.adapter_channels:
            channel.close()

    def start_worker(self):
        self.config, self.database, self.core = build_runtime(self.config_path, start_mmap=False)

        def worker_channel(account, key):
            channel = MmapChannel(
                effective_mmap_name(self.config.data_dir, account), account.mmap_size,
                key, "worker", self.lock,
            )
            self.worker_channels.append(channel)
            return channel

        self.gateway = self.gateway_class(self.core, self.config_path, channel_factory=worker_channel)
        self.gateways.append(self.gateway)

    def start_qmt(self):
        real_import = builtins.__import__
        forbidden = {"os", "uuid", "sqlite3", "socket", "ctypes", "multiprocessing", "pathlib"}

        def restricted_import(name, *args, **kwargs):
            if name.split(".")[0] in forbidden:
                raise AssertionError("QMT imported forbidden module: " + name)
            return real_import(name, *args, **kwargs)

        def forbidden_open(*args, **kwargs):
            self.fail("QMT attempted filesystem access")

        self.scope = {
            "__builtins__": dict(vars(builtins), __import__=restricted_import, open=forbidden_open),
            "get_trade_detail_data": self.trade_details,
            "passorder": self.passorder,
        }
        exec(compile(self.source, "<restricted-qmt-integration>", "exec"), self.scope)
        channel_class = self.scope['MmapChannel']
        def adapter_channel(*args, **kwargs):
            channel = channel_class(*args, **kwargs)
            self.adapter_channels.append(channel)
            return channel
        self.scope['MmapChannel'] = adapter_channel
        self.scope['MMAP_LOCK_FACTORY'] = lambda name: self.lock
        self.context = _QmtContext()
        self.scope["init"](self.context)

    def trade_details(self, account_id, account_type, kind, strategy):
        self.assertEqual((account_id, account_type, strategy),
                         ("sim-stock-001", "STOCK", "WorkBuddyQMT"))
        if kind == "ACCOUNT":
            return [{"m_dBalance": 1000000, "m_dAvailable": 900000,
                     "m_dInstrumentValue": 100000, "m_strStatus": "CONNECTED"}]
        return []

    def broker_submit(self, *args):
        client_key = args[-2]
        # A separate connection must observe the committed row before native QMT
        # receives the order, not merely an uncommitted write on the gateway.
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT state,entry_json FROM mmap_execution_journal "
                "WHERE account_alias=? AND client_order_key=?",
                (self.account.alias, client_key),
            ).fetchone()
        self.assertIsNotNone(row, "passorder ran without durable execution evidence")
        self.assertEqual(row["state"], "PRE_SUBMIT")
        self.submission_evidence.append(json.loads(row["entry_json"]))
        return 1

    def pump(self, count=8):
        for _ in range(count):
            self.assertEqual(self.gateway.poll_once()["errors"], {})
            self.scope["poll_commands"](self.context)
            self.assertEqual(self.gateway.poll_once()["errors"], {})

    def enable_simulation(self):
        adapter = self.read_json(self.bundle["adapter_config"])
        adapter["qmt_mode"] = "SIM_SIGNAL"
        atomic_write_json(self.bundle["adapter_config"], adapter)
        with self.database.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value='SIM_SIGNAL' WHERE key='mode'")
        # Market clock and quote restrictions are independent of transport;
        # cash, profile, signature, mode and durable grant checks remain real.
        self.scope["_validate_live_price"] = lambda command: None
        self.pump()
        self.assertEqual(self.scope["_CONFIG"]["qmt_mode"], "SIM_SIGNAL")

    def execute_payload(self, client_key="WB-mmap-integration-001"):
        return {
            "type": "EXECUTE_ORDER", "intent_id": "intent-mmap-integration-001",
            "intent_revision": 1, "client_order_key": client_key,
            "account_alias": self.account.alias, "account_type": "STOCK",
            "asset_type": "STOCK", "qmt_symbol": "600000.SH", "action": "BUY",
            "semantic_action": "BUY", "resolved_order": {
                "price_type": "LIMIT", "limit_price": 10.0, "volume": 100,
                "volume_rule": {"minimum_volume": 100, "odd_lot_liquidation": False},
            },
            "execution_mode": "SIM_SIGNAL", "risk_decision_id": "risk-integration-001",
            "source_signal_id": None, "source": {"source_type": "manual"},
            "credit": None, "auto_permit": None,
        }

    def enqueue_execute(self, payload=None):
        payload = payload or self.execute_payload()
        return self.core._queue_control_command(
            self.account, payload, "TRADE_INTENT", payload["intent_id"],
        )

    def wait_for_command_frame(self, message_id):
        for _ in range(12):
            self.assertEqual(self.gateway.poll_once()["errors"], {})
            pending = self.scope["_channel"].peek()
            if pending and any(item["message_id"] == message_id for item in pending[1].get("commands", [])):
                return pending
            self.scope["poll_commands"](self.context)
        self.fail("queued command never reached the named mmap")

    def test_handshake_initializes_restricted_qmt_and_persists_snapshots(self):
        self.assertEqual(self.scope["_STATUS"], "RECOVERING")
        self.assertFalse(self.scope["_link_live"]())
        self.pump()
        self.assertEqual(self.scope["_STATUS"], "READY")
        self.assertTrue(self.scope["_link_live"]())
        self.assertEqual(self.context.account_id, "sim-stock-001")
        self.assertIn("poll_commands", self.context.schedules)
        with self.database.connect() as connection:
            row = connection.execute("SELECT payload_json FROM account_snapshots").fetchone()
            positions = connection.execute("SELECT COUNT(*) FROM position_snapshot_runs").fetchone()[0]
        self.assertEqual(json.loads(row["payload_json"])["available_cash"], 900000)
        self.assertGreater(positions, 0)
        self.passorder.assert_not_called()

    def test_request_sync_crosses_named_mapping_and_completes_in_sqlite(self):
        self.pump()
        with self.database.connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
        request = self.core.request_sync(self.account.alias, ["ACCOUNT", "POSITION"])
        self.pump()
        with self.database.connect() as connection:
            status = connection.execute("SELECT status FROM qmt_commands WHERE message_id=?",
                                        (request["message_id"],)).fetchone()[0]
            ack = connection.execute("SELECT status FROM qmt_command_acks WHERE message_id=?",
                                     (request["message_id"],)).fetchone()[0]
            after = connection.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
        self.assertEqual(status, "SYNC_COMPLETED")
        self.assertEqual(ack, "SYNC_COMPLETED")
        self.assertGreater(after, before)
        self.passorder.assert_not_called()

    def test_failed_event_commit_keeps_authenticated_mailbox_for_retry(self):
        self.pump()
        message_id = self.scope["_write_event"]({
            "type": "ACCOUNT_SNAPSHOT", "snapshot_id": "retry-snapshot-001",
            "captured_at": iso_now(), "total_asset": 123456, "available_cash": 123000,
            "market_value": 456, "connection_status": "CONNECTED",
        })
        self.scope["poll_commands"](self.context)
        channel = self.worker_channels[-1]
        pending = channel.peek()
        self.assertIsNotNone(pending)
        self.assertTrue(any(event["envelope"]["message_id"] == message_id
                            for event in pending[1]["events"]))
        with mock.patch.object(self.core.ingester, "ingest", side_effect=OSError("injected disk outage")):
            report = self.gateway.poll_once()
        self.assertIn(self.account.alias, report["errors"])
        self.assertEqual(channel.peek()[0], pending[0])
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute("SELECT message_id FROM inbound_events WHERE message_id=?",
                                                 (message_id,)).fetchone())
        self.assertEqual(self.gateway.poll_once()["errors"], {})
        self.assertIsNone(channel.peek())
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM inbound_events WHERE message_id=?",
                                                (message_id,)).fetchone()[0], 1)

    def test_passorder_requires_committed_pre_submit_and_duplicate_key_never_reexecutes(self):
        self.enable_simulation()
        self.enqueue_execute()
        self.pump()
        self.assertEqual(self.passorder.call_count, 1)
        self.assertEqual(len(self.submission_evidence), 1)
        self.enqueue_execute()
        self.pump()
        self.assertEqual(self.passorder.call_count, 1)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mmap_execution_journal").fetchone()[0], 1)

    def test_worker_and_qmt_restart_changes_sessions_without_regranting_stable_key(self):
        self.enable_simulation()
        payload = self.execute_payload()
        self.enqueue_execute(payload)
        self.pump()
        self.assertEqual(self.passorder.call_count, 1)
        old_worker_session = self.gateway.worker_session
        old_adapter_session = self.scope["_adapter_session"]
        self.gateway.close()
        self.scope["_channel"].close()
        self.start_worker()
        self.start_qmt()
        self.scope["_validate_live_price"] = lambda command: None
        self.pump()
        self.assertNotEqual(self.gateway.worker_session, old_worker_session)
        self.assertNotEqual(self.scope["_adapter_session"], old_adapter_session)
        self.enqueue_execute(payload)
        self.pump()
        self.assertEqual(self.passorder.call_count, 1)
        with self.database.connect() as connection:
            row = connection.execute("SELECT state,entry_json FROM mmap_execution_journal").fetchone()
        self.assertIn(row["state"], {"SUBMIT_CALLED", "SUBMIT_UNKNOWN"})
        self.assertEqual(json.loads(row["entry_json"])["worker_session"], old_worker_session)

    def test_old_unconsumed_grant_is_not_executable_after_worker_restart(self):
        self.enable_simulation()
        request = self.enqueue_execute()
        self.wait_for_command_frame(request["message_id"])
        self.scope["poll_commands"](self.context)
        self.assertEqual(self.gateway.poll_once()["errors"], {})
        pending = self.scope["_channel"].peek()
        self.assertIsNotNone(pending)
        old_grants = copy.deepcopy(pending[1].get("grants"))
        self.assertTrue(old_grants, "Worker did not return the durable execution grant")
        self.assertEqual(old_grants[0]["state"], "PRE_SUBMIT")
        self.passorder.assert_not_called()
        old_sequence = pending[0]
        old_worker_session = self.gateway.worker_session
        self.gateway.close()
        self.start_worker()
        barrier = self.scope["_channel"].peek()
        self.assertIsNotNone(barrier)
        self.assertGreater(barrier[0], old_sequence)
        self.assertEqual(barrier[1]["kind"], "WORKER_RESET")
        self.assertEqual(barrier[1]["worker_session"], self.gateway.worker_session)
        self.assertEqual(barrier[1]["account_alias"], self.account.alias)
        self.assertNotIn("grants", barrier[1])
        # QMT runs before this new Worker has polled or sent fresh controls.
        # Startup itself must have fenced the still-unconsumed native grant.
        self.scope["poll_commands"](self.context)
        self.passorder.assert_not_called()
        self.assertEqual(self.scope["_STATUS"], "RECOVERING")
        self.assertFalse(self.scope["_link_live"]())
        self.assertIsNone(self.scope["_worker_session"])
        self.assertFalse(self.scope["_pending_execute"])
        self.pump()
        self.assertNotEqual(self.gateway.worker_session, old_worker_session)
        self.assertEqual(self.scope["_worker_session"], self.gateway.worker_session)
        self.assertFalse(self.scope["_pending_execute"])
        # Replay the old grant through a fresh, authenticated transport frame.
        # The live control session cannot make the stale grant valid again.
        pending = self.scope["_channel"].peek()
        self.assertIsNotNone(pending)
        replay = copy.deepcopy(pending[1])
        replay["commands"] = []
        replay["grants"] = old_grants
        self.scope["_channel"].ack(pending[0])
        self.assertTrue(self.worker_channels[-1].send(replay))
        self.scope["poll_commands"](self.context)
        self.assertEqual(self.gateway.poll_once()["errors"], {})
        self.enqueue_execute()
        self.pump()
        self.passorder.assert_not_called()
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT state FROM mmap_execution_journal").fetchone()[0],
                             "SUBMIT_UNKNOWN")

    def test_mode_change_before_prepare_denies_without_wedging_event_ingestion(self):
        self.enable_simulation()
        request = self.enqueue_execute()
        self.wait_for_command_frame(request["message_id"])
        self.scope["poll_commands"](self.context)
        self.assertTrue(self.scope["_pending_execute"])
        self.passorder.assert_not_called()
        with self.database.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value='OBSERVE_ONLY' WHERE key='mode'")
        self.assertEqual(self.gateway.poll_once()["errors"], {})
        pending = self.scope["_channel"].peek()
        self.assertIsNotNone(pending)
        denied = pending[1]["grants"]
        self.assertEqual(len(denied), 1)
        self.assertTrue(denied[0]["denied"])
        self.assertEqual(denied[0]["reason"], "MODE_MISMATCH")
        self.scope["poll_commands"](self.context)
        self.pump()
        self.passorder.assert_not_called()
        self.assertFalse(self.scope["_pending_execute"])
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mmap_execution_journal").fetchone()[0], 0)
            status = connection.execute("SELECT status FROM qmt_commands WHERE message_id=?",
                                        (request["message_id"],)).fetchone()[0]
        self.assertEqual(status, "RISK_REJECTED")
        # A denial without PRE_SUBMIT must not emit a JOURNAL record that blocks
        # this account's future traffic waiting for nonexistent durable evidence.
        request = self.core.request_sync(self.account.alias, ["ACCOUNT", "POSITION"])
        self.pump()
        with self.database.connect() as connection:
            status = connection.execute("SELECT status FROM qmt_commands WHERE message_id=?",
                                        (request["message_id"],)).fetchone()[0]
        self.assertEqual(status, "SYNC_COMPLETED")

    def test_expired_control_lease_rejects_a_delivered_execute_command(self):
        self.enable_simulation()
        request = self.enqueue_execute()
        self.wait_for_command_frame(request["message_id"])
        clock = self.scope["time"].monotonic()
        ticks = iter([clock] + [clock + 20] * 30)
        with mock.patch.object(self.scope["time"], "monotonic", side_effect=lambda: next(ticks)):
            self.scope["poll_commands"](self.context)
        self.assertFalse(self.scope["_pending_execute"])
        self.passorder.assert_not_called()
        self.assertEqual(self.gateway.poll_once()["errors"], {})
        with self.database.connect() as connection:
            state = connection.execute("SELECT status FROM qmt_commands WHERE message_id=?",
                                       (request["message_id"],)).fetchone()[0]
            grants = connection.execute("SELECT COUNT(*) FROM mmap_execution_journal").fetchone()[0]
        self.assertEqual(state, "QMT_REJECTED")
        self.assertEqual(grants, 0)


if __name__ == "__main__":
    unittest.main()
