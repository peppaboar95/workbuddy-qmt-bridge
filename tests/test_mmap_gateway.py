import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from workbuddy_qmt.config import AccountConfig, BridgeConfig
from workbuddy_qmt.core import BridgeCore
from workbuddy_qmt.db import Database
from workbuddy_qmt.errors import BridgeError
from workbuddy_qmt.event_ingest import EventIngester
from workbuddy_qmt.file_queue import FileQueue
from workbuddy_qmt.mmap_gateway import MmapGateway
from workbuddy_qmt.security import KeyRing, make_envelope
from workbuddy_qmt.util import atomic_write_json, canonical_json, iso_now, utc_now


class FakeChannel:
    def __init__(self):
        self.incoming = None
        self.sequence = 0
        self.sent = []
        self.resets = []
        self.acks = []
        self.blocked = False
        self.closed = False
        self.max_payload_size = 2 * 1024 * 1024

    def push(self, value):
        assert self.incoming is None
        self.sequence += 1
        self.incoming = (self.sequence, value)

    def peek(self):
        return self.incoming

    def ack(self, sequence):
        if self.incoming is None or sequence != self.incoming[0]:
            return False
        self.acks.append(sequence)
        self.incoming = None
        return True

    def send(self, value):
        if len(canonical_json(value)) > self.max_payload_size:
            raise ValueError("frame exceeds fake mailbox capacity")
        if self.blocked:
            return False
        self.sent.append(json.loads(json.dumps(value)))
        return True

    def replace_outgoing(self, value):
        self.resets.append(json.loads(json.dumps(value)))
        return True

    def close(self):
        self.closed = True


class MmapGatewayTests(unittest.TestCase):
    def setUp(self):
        # The sandbox TEMP prefix plus FileQueue's atomic filename can exceed
        # Windows MAX_PATH. Keep these transient test files in a short root.
        self.temp = tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1]))
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.config_path = os.path.join(self.root, "config", "bridge.json")
        self.account = AccountConfig("stock", "STOCK", "adapter-1", adapter_transport="mmap")
        self.config = BridgeConfig(data_dir=os.path.join(self.root, "data"), accounts={"stock": self.account})
        self.db = Database(os.path.join(self.root, "bridge.db"))
        self.db.initialize()
        self.keyring = KeyRing({"key": b"k" * 32}, "key")
        self.queue = FileQueue(self.config.data_dir, self.keyring)
        self.ingester = EventIngester(self.config, self.db, self.queue)
        self.core = BridgeCore(self.config, self.db, self.keyring, self.queue, self.ingester)
        self.bundle = os.path.join(self.root, "qmt_ready", "stock")
        self.runtime = os.path.join(self.config.data_dir, "qmt_runtime", self.account.adapter_instance)
        atomic_write_json(os.path.join(self.bundle, "qmt_adapter.json"), {
            "account_alias": "stock", "account_type": "STOCK", "adapter_instance": "adapter-1",
            "qmt_mode": "OBSERVE_ONLY", "mapping_profile": os.path.join(self.bundle, "qmt_profile.json"),
        })
        atomic_write_json(os.path.join(self.bundle, "qmt_profile.json"), {"profile_id": "profile-test"})
        self.channel = FakeChannel()
        self.gateway = self.make_gateway()
        self.command = {
            "type": "EXECUTE_ORDER", "account_alias": "stock", "account_type": "STOCK",
            "intent_id": "intent-1", "client_order_key": "WB-key-1", "execution_mode": "OBSERVE_ONLY",
            "resolved_order": {"limit_price": 10.0, "volume": 100}, "auto_permit": None,
        }

    def make_gateway(self):
        gateway = MmapGateway(self.core, self.config_path, channel_factory=lambda account, key: self.channel)
        self.addCleanup(gateway.close)
        return gateway

    def frame(self, records=None, events=None, worker=None, session="session-1"):
        return {"account_alias": "stock", "adapter_session": session,
                "worker_session": self.gateway.worker_session if worker is None else worker,
                "records": records or [], "events": events or []}

    def hello(self):
        frame = self.frame()
        frame["worker_session"] = None
        self.channel.push(frame)
        self.assertFalse(self.gateway.poll_once()["errors"])

    def store(self, command=None, ttl=60):
        command = command or self.command
        envelope = make_envelope(self.keyring, "TRADE_INTENT", command, ttl, "test-worker")
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO qmt_commands(message_id,intent_id,account_alias,command_type,status,created_at,payload_json) "
                "VALUES(?,?,?,'EXECUTE_ORDER','DELIVERED',?,?)",
                (envelope["message_id"], command["intent_id"], command["account_alias"], iso_now(), json.dumps(envelope)),
            )
        path = self.queue.write(self.account.adapter_instance, "commands", envelope)
        return envelope, path

    def request(self, envelope, command=None):
        self.channel.push(self.frame(records=[{
            "kind": "PREPARE_EXECUTION", "message_id": envelope["message_id"],
            "command": command or envelope["payload"],
        }]))
        return self.gateway.poll_once()

    def set_mode(self, mode):
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value=? WHERE key='mode'", (mode,))
        path = os.path.join(self.bundle, "qmt_adapter.json")
        with open(path, encoding="utf-8") as stream:
            config = json.load(stream)
        config["qmt_mode"] = mode
        atomic_write_json(path, config)
        self.command["execution_mode"] = mode

    def authorize(self, mode, **payload):
        value = {"account_alias": "stock", "allowed_modes": [mode],
                 "live_until": (utc_now() + dt.timedelta(minutes=5)).isoformat()}
        value.update(payload)
        authorization = make_envelope(self.keyring, "LOCAL_AUTHORIZATION", value, 300, "test")
        atomic_write_json(os.path.join(self.runtime, "local_authorization.json"), authorization)
        return authorization

    def test_hello_sends_fresh_config_profile_and_only_allowlisted_runtime(self):
        self.authorize("MANUAL_LIVE")
        atomic_write_json(os.path.join(self.runtime, "secret.json"), {"secret": "private"})
        self.hello()
        frame = self.channel.sent[-1]
        self.assertEqual("session-1", frame["adapter_session"])
        self.assertEqual(self.gateway.worker_session, frame["worker_session"])
        self.assertEqual("profile-test", frame["profile"]["profile_id"])
        self.assertEqual({"local_authorization.json"}, set(frame["runtime"]))
        self.assertEqual([], frame["grants"])
        self.assertEqual([1], self.channel.acks)

    def test_startup_publishes_reset_before_recovering_durable_grants(self):
        self.assertEqual(1, len(self.channel.resets))
        self.assertEqual({"kind": "WORKER_RESET", "account_alias": "stock",
                          "adapter_session": None, "worker_session": self.gateway.worker_session}, self.channel.resets[0])
        order = []
        channel = FakeChannel()
        original_reset = channel.replace_outgoing

        def reset(value):
            order.append("reset")
            return original_reset(value)

        with mock.patch.object(channel, "replace_outgoing", side_effect=reset), mock.patch(
            "workbuddy_qmt.mmap_gateway.ExecutionJournal.recover", side_effect=lambda *args: order.append("recover")
        ):
            gateway = MmapGateway(self.core, self.config_path, channel_factory=lambda account, key: channel)
            self.addCleanup(gateway.close)
        self.assertEqual(["reset", "recover"], order)

    def test_prepare_grant_is_committed_before_incoming_ack(self):
        self.hello()
        envelope, _ = self.store()
        original_ack = self.channel.ack

        def assert_persisted(sequence):
            self.assertEqual("PRE_SUBMIT", self.gateway.journal.list_entries("stock")[0]["state"])
            return original_ack(sequence)

        with mock.patch.object(self.channel, "ack", side_effect=assert_persisted):
            self.assertFalse(self.request(envelope)["errors"])
        self.assertEqual("PRE_SUBMIT", self.channel.sent[-1]["grants"][0]["state"])
        self.assertFalse(self.request(envelope)["errors"])
        self.assertEqual("SUBMIT_UNKNOWN", self.channel.sent[-1]["grants"][0]["state"])
        self.assertEqual(1, len(self.gateway.journal.list_entries("stock")))

    def test_worker_restart_discards_old_frames_and_never_reissues_a_grant(self):
        self.hello()
        envelope, _ = self.store()
        self.request(envelope)
        old_session = self.gateway.worker_session
        self.gateway.close()
        self.channel = FakeChannel()
        self.gateway = self.make_gateway()
        stale = self.frame(records=[{"kind": "PREPARE_EXECUTION", "message_id": envelope["message_id"], "command": self.command}], worker=old_session)
        self.channel.push(stale)
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual([], self.channel.sent[-1]["commands"])
        self.assertEqual([], self.channel.sent[-1]["grants"])
        self.assertEqual("SUBMIT_UNKNOWN", self.gateway.journal.list_entries("stock")[0]["state"])
        self.request(envelope)
        self.assertEqual("SUBMIT_UNKNOWN", self.channel.sent[-1]["grants"][0]["state"])

    def test_auth_halt_expiry_and_payload_mismatch_deny_without_reserving(self):
        self.set_mode("MANUAL_LIVE")
        self.hello()
        envelope, _ = self.store()
        self.request(envelope)
        self.assertEqual("SUBMIT_UNKNOWN", self.channel.sent[-1]["grants"][0]["state"])
        self.assertEqual([], self.gateway.journal.list_entries("stock"))
        self.authorize("MANUAL_LIVE")
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value='true' WHERE key='halted'")
        self.request(envelope)
        self.assertEqual([], self.gateway.journal.list_entries("stock"))
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value='false' WHERE key='halted'")
        changed = dict(self.command, resolved_order={"limit_price": 20.0, "volume": 100})
        self.request(envelope, changed)
        self.assertEqual([], self.gateway.journal.list_entries("stock"))
        # Expire the stored original while preserving its signature.
        envelope["issued_at"] = (utc_now() - dt.timedelta(minutes=2)).isoformat()
        envelope["expires_at"] = (utc_now() - dt.timedelta(minutes=1)).isoformat()
        envelope["signature"] = self.keyring.sign(envelope)
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE qmt_commands SET payload_json=? WHERE message_id=?", (json.dumps(envelope), envelope["message_id"]))
        self.request(envelope)
        self.assertEqual([], self.gateway.journal.list_entries("stock"))

    def test_manual_live_valid_authorization_grants(self):
        self.set_mode("MANUAL_LIVE")
        self.authorize("MANUAL_LIVE")
        self.hello()
        envelope, _ = self.store()
        self.assertFalse(self.request(envelope)["errors"])
        self.assertEqual("PRE_SUBMIT", self.channel.sent[-1]["grants"][0]["state"])

    def test_core_global_halt_clear_document_allows_a_fresh_grant(self):
        self.core.write_adapter_halt(False, "test clear")
        self.hello()
        envelope, _ = self.store()
        self.assertFalse(self.request(envelope)["errors"])
        self.assertEqual("PRE_SUBMIT", self.channel.sent[-1]["grants"][0]["state"])

    def test_limited_auto_requires_current_database_permit_binding(self):
        self.set_mode("LIMITED_AUTO")
        now = utc_now()
        policy = {"permit_id": "permit-1", "generation": 1, "account_alias": "stock", "account_type": "STOCK"}
        digest = hashlib.sha256(canonical_json(policy)).hexdigest()
        binding = {"permit_id": "permit-1", "generation": 1, "policy_hash": digest}
        self.command["auto_permit"] = binding
        self.authorize("LIMITED_AUTO", authorization_type="LIMITED_AUTO_PERMIT", auto_permit=dict(binding, policy=policy))
        with self.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO auto_permits(permit_id,account_alias,status,generation,policy_hash,policy_json,actor,reason,created_at,starts_at,expires_at) "
                "VALUES('permit-1','stock','ACTIVE',1,?,?,'test','test',?,?,?)",
                (digest, json.dumps(policy), iso_now(), (now - dt.timedelta(minutes=1)).isoformat(), (now + dt.timedelta(minutes=5)).isoformat()),
            )
        self.hello()
        envelope, _ = self.store()
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE auto_permits SET generation=2")
        self.request(envelope)
        self.assertEqual([], self.gateway.journal.list_entries("stock"))
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE auto_permits SET generation=1")
        self.request(envelope)
        self.assertEqual("PRE_SUBMIT", self.channel.sent[-1]["grants"][0]["state"])

    def test_failed_persistence_leaves_frame_unacked_and_does_not_renew_controls(self):
        self.hello()
        envelope, _ = self.store()
        sent_before = len(self.channel.sent)
        acked_before = len(self.channel.acks)
        with mock.patch.object(self.gateway.journal, "prepare", side_effect=BridgeError("EXECUTION_JOURNAL_UNAVAILABLE", "disk failed")):
            result = self.request(envelope)
        self.assertIn("stock", result["errors"])
        self.assertEqual(acked_before, len(self.channel.acks))
        self.assertEqual(sent_before, len(self.channel.sent))
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual("PRE_SUBMIT", self.channel.sent[-1]["grants"][0]["state"])

    def test_backpressure_preserves_grants_and_commands_and_receipt_archives(self):
        self.hello()
        envelope, path = self.store()
        self.channel.blocked = True
        self.request(envelope)
        self.assertTrue(os.path.isfile(path))
        self.channel.blocked = False
        self.gateway.poll_once()
        output = self.channel.sent[-1]
        self.assertEqual(envelope["message_id"], output["commands"][0]["message_id"])
        self.assertEqual("PRE_SUBMIT", output["grants"][0]["state"])
        self.gateway.poll_once()
        self.assertEqual([], self.channel.sent[-1]["commands"])
        self.assertEqual([], self.channel.sent[-1]["grants"])
        self.channel.push(self.frame(records=[{"kind": "RECEIVED", "message_id": envelope["message_id"]}]))
        self.gateway.poll_once()
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.listdir(os.path.join(self.queue.root, "adapter-1", "archive")))

    def test_sent_command_remains_until_receipt_even_after_expiration(self):
        self.hello()
        envelope, path = self.store()
        self.gateway.poll_once()
        envelope["issued_at"] = (utc_now() - dt.timedelta(minutes=2)).isoformat()
        envelope["expires_at"] = (utc_now() - dt.timedelta(minutes=1)).isoformat()
        envelope["signature"] = self.keyring.sign(envelope)
        atomic_write_json(path, envelope)
        self.gateway.poll_once()
        self.assertTrue(os.path.exists(path))
        self.channel.push(self.frame(records=[{"kind": "RECEIVED", "message_id": envelope["message_id"]}]))
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.listdir(os.path.join(self.queue.root, "adapter-1", "archive")))

    def test_pending_grant_is_revoked_if_database_halts_before_dispatch(self):
        self.hello()
        envelope, _ = self.store()
        self.channel.blocked = True
        self.assertFalse(self.request(envelope)["errors"])
        original = self.gateway.journal.list_entries("stock")[0]
        self.assertEqual("PRE_SUBMIT", original["state"])
        # Simulate a successful halt transaction followed by a failed external
        # halt-file write. The control runtime still has no halt document.
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE system_state SET value='true' WHERE key='halted'")
        self.channel.blocked = False
        self.assertFalse(self.gateway.poll_once()["errors"])
        denied = self.channel.sent[-1]["grants"][0]
        self.assertEqual("SUBMIT_UNKNOWN", denied["state"])
        self.assertTrue(denied["denied"])
        self.assertEqual(original["grant_id"], denied["grant_id"])
        self.assertEqual(original["command_digest"], denied["command_digest"])
        self.assertEqual("SUBMIT_UNKNOWN", self.gateway.journal.list_entries("stock")[0]["state"])

    def test_small_mailbox_sends_fitting_command_batches_without_losing_unsent_commands(self):
        self.hello()
        envelopes = []
        for index in range(8):
            command = dict(self.command, client_order_key="WB-budget-%d" % index,
                           intent_id="intent-budget-%d" % index, padding="预算" * 80)
            envelope, _ = self.store(command)
            envelopes.append(envelope)
        fitting = dict(self.channel.sent[-1], commands=envelopes[:2])
        self.channel.max_payload_size = len(canonical_json(fitting))
        received = []
        for _ in range(4):
            self.assertFalse(self.gateway.poll_once()["errors"])
            outgoing = self.channel.sent[-1]
            self.assertLessEqual(len(canonical_json(outgoing)), self.channel.max_payload_size)
            self.assertEqual(2, len(outgoing["commands"]))
            received.extend(envelope["message_id"] for envelope in outgoing["commands"])
        self.assertEqual([envelope["message_id"] for envelope in envelopes], received)

    def test_small_mailbox_retains_grants_that_do_not_fit_in_the_current_batch(self):
        self.hello()
        self.channel.blocked = True
        for index in range(2):
            command = dict(self.command, client_order_key="WB-grants-%d" % index, intent_id="intent-grants-%d" % index)
            envelope, _ = self.store(command)
            self.assertFalse(self.request(envelope)["errors"])
        entries = self.gateway.journal.list_entries("stock")
        fitting = dict(self.channel.sent[-1], commands=[], journals=entries, grants=entries[:1])
        self.channel.max_payload_size = len(canonical_json(fitting))
        self.channel.blocked = False
        received = []
        for _ in range(2):
            self.assertFalse(self.gateway.poll_once()["errors"])
            outgoing = self.channel.sent[-1]
            self.assertEqual(1, len(outgoing["grants"]))
            received.append(outgoing["grants"][0]["grant_id"])
        self.assertEqual([entry["grant_id"] for entry in entries], received)

    def test_oversize_base_control_fails_closed_without_sending(self):
        self.hello()
        sent_before = len(self.channel.sent)
        self.channel.max_payload_size = len(canonical_json(self.channel.sent[-1])) - 1
        self.assertIn("stock", self.gateway.poll_once()["errors"])
        self.assertEqual(sent_before, len(self.channel.sent))

    def test_journal_snapshot_omits_lifetime_history_and_keeps_current_permit_all_generations(self):
        self.hello()
        original_size = len(canonical_json(self.channel.sent[-1]))
        with self.db.transaction(immediate=True) as connection:
            for index in range(253):
                key = "history-%d" % index
                permit = "current-permit" if index >= 250 else "unrelated-permit"
                entry = {"account_alias": "stock", "client_order_key": key, "intent_id": "intent-" + key,
                         "state": "FILLED", "created_at": iso_now(), "details": {}, "order_notional": 1000.0,
                         "auto_permit": {"permit_id": permit, "generation": index - 249}, "command_digest": key}
                connection.execute(
                    "INSERT INTO mmap_execution_journal(account_alias,client_order_key,command_digest,state,entry_json) VALUES(?,?,?,'FILLED',?)",
                    ("stock", key, key, json.dumps(entry)),
                )
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual([], self.channel.sent[-1]["journals"])
        self.assertEqual(original_size, len(canonical_json(self.channel.sent[-1])))
        self.authorize("LIMITED_AUTO", authorization_type="LIMITED_AUTO_PERMIT",
                       auto_permit={"permit_id": "current-permit", "generation": 3, "policy_hash": "hash"})
        self.assertFalse(self.gateway.poll_once()["errors"])
        included = self.channel.sent[-1]["journals"]
        self.assertEqual(3, len(included))
        self.assertEqual({1, 2, 3}, {entry["auto_permit"]["generation"] for entry in included})
        self.assertEqual(3000.0, sum(entry["order_notional"] for entry in included))
        self.assertEqual(253, len(self.gateway.journal.list_entries("stock")))
        self.channel.max_payload_size = len(canonical_json(self.channel.sent[-1])) - 1
        sent_before = len(self.channel.sent)
        error = self.gateway.poll_once()["errors"]["stock"]
        self.assertIn("quota history", error["message"])
        self.assertEqual(sent_before, len(self.channel.sent))

    def test_stale_worker_signed_events_are_persisted_and_records_quarantined_before_ack(self):
        event = make_envelope(self.keyring, "QMT_EVENT", {
            "type": "HEARTBEAT", "account_alias": "stock", "account_type": "STOCK",
            "adapter_instance": "adapter-1", "status": "READY", "qmt_mode": "OBSERVE_ONLY",
        }, 60, "adapter")
        self.channel.push(self.frame(worker="old-worker", events=[{"folder": "events", "envelope": event}], records=[
            {"kind": "PREPARE_EXECUTION", "message_id": "old-command", "command": self.command},
        ]))
        with mock.patch.object(self.ingester, "ingest", side_effect=sqlite3.OperationalError("disk failed")):
            self.assertIn("stock", self.gateway.poll_once()["errors"])
        self.assertEqual([], self.channel.acks)
        self.assertEqual([], self.channel.sent)
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual([1], self.channel.acks)
        self.assertEqual([], self.channel.sent[-1]["grants"])
        self.assertEqual([], self.channel.sent[-1]["commands"])
        self.assertEqual([], self.gateway.journal.list_entries("stock"))
        with self.db.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0])
            quarantine = connection.execute("SELECT details_json FROM audit_log WHERE action='MMAP_STALE_SESSION_QUARANTINE'").fetchone()
        self.assertIsNotNone(quarantine)
        self.assertEqual("old-command", json.loads(quarantine["details_json"])["records"][0]["message_id"])

    def test_legacy_journal_or_submitted_database_command_cannot_receive_fresh_grant(self):
        self.hello()
        envelope, _ = self.store()
        legacy = os.path.join(self.runtime, "execution_journal", "WB-key-1.json")
        atomic_write_json(legacy, {"client_order_key": "WB-key-1", "state": "PRE_SUBMIT"})
        self.request(envelope)
        self.assertEqual("SUBMIT_UNKNOWN", self.channel.sent[-1]["grants"][0]["state"])
        self.assertEqual([], self.gateway.journal.list_entries("stock"))
        os.unlink(legacy)
        with self.db.transaction(immediate=True) as connection:
            connection.execute("UPDATE qmt_commands SET status='SUBMIT_CALLED'")
        self.request(envelope)
        self.assertEqual([], self.gateway.journal.list_entries("stock"))

    def test_accounts_progress_independently_and_disabled_or_file_accounts_are_ignored(self):
        self.gateway.close()
        self.config.accounts["credit"] = AccountConfig("credit", "CREDIT", "adapter-2", adapter_transport="mmap")
        self.config.accounts["file"] = AccountConfig("file", "STOCK", "adapter-3")
        self.config.accounts["disabled"] = AccountConfig("disabled", "STOCK", "adapter-4", enabled=False, adapter_transport="mmap")
        credit_bundle = os.path.join(self.root, "qmt_ready", "credit")
        atomic_write_json(os.path.join(credit_bundle, "qmt_adapter.json"), {
            "account_alias": "credit", "account_type": "CREDIT", "adapter_instance": "adapter-2", "qmt_mode": "OBSERVE_ONLY",
        })
        atomic_write_json(os.path.join(credit_bundle, "qmt_profile.json"), {"profile_id": "credit-profile"})
        channels = {}

        def factory(account, key):
            self.assertEqual(b"k" * 32, key)
            channels[account.alias] = FakeChannel()
            return channels[account.alias]

        gateway = MmapGateway(self.core, self.config_path, channel_factory=factory)
        self.addCleanup(gateway.close)
        self.assertEqual({"stock", "credit"}, set(channels))
        for alias, channel in channels.items():
            channel.push({"account_alias": alias, "adapter_session": "session", "worker_session": None, "events": [], "records": []})
        os.unlink(os.path.join(self.bundle, "qmt_profile.json"))
        result = gateway.poll_once()
        self.assertEqual({"stock"}, set(result["errors"]))
        self.assertEqual([], channels["stock"].sent)
        self.assertEqual(1, len(channels["credit"].sent))
        gateway.close()
        self.assertTrue(all(channel.closed for channel in channels.values()))

    def test_event_ingestion_commits_before_ack_and_is_retryable(self):
        self.hello()
        event = make_envelope(self.keyring, "QMT_EVENT", {
            "type": "HEARTBEAT", "account_alias": "stock", "account_type": "STOCK",
            "adapter_instance": "adapter-1", "status": "READY", "qmt_mode": "OBSERVE_ONLY",
        }, 60, "adapter")
        self.channel.push(self.frame(events=[{"folder": "events", "envelope": event}]))
        with mock.patch.object(self.ingester, "ingest", side_effect=sqlite3.OperationalError("disk unavailable")):
            self.assertIn("stock", self.gateway.poll_once()["errors"])
        self.assertEqual([1], self.channel.acks)
        self.assertFalse(self.gateway.poll_once()["errors"])
        with self.db.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM inbound_events").fetchone()[0])
            self.assertEqual("READY", connection.execute("SELECT status FROM heartbeats").fetchone()[0])
        self.assertEqual([1, 2], self.channel.acks)

    def test_partial_frame_retry_keeps_successful_prepare_grant_once(self):
        self.hello()
        envelope, _ = self.store()
        self.channel.push(self.frame(records=[
            {"kind": "PREPARE_EXECUTION", "message_id": envelope["message_id"], "command": self.command},
            {"kind": "P0_PROBE", "value": {"account_alias": "stock", "complete": True}},
        ]))
        with mock.patch("workbuddy_qmt.mmap_gateway.atomic_write_json", side_effect=OSError("disk unavailable")):
            self.assertIn("stock", self.gateway.poll_once()["errors"])
        self.assertEqual([1], self.channel.acks)
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual(1, len(self.channel.sent[-1]["grants"]))
        self.assertEqual("PRE_SUBMIT", self.channel.sent[-1]["grants"][0]["state"])

    def test_journal_copy_uses_authoritative_permit_and_notional(self):
        self.hello()
        envelope, _ = self.store()
        self.request(envelope)
        value = {"client_order_key": "WB-key-1", "intent_id": "intent-1", "state": "SUBMIT_CALLED",
                 "details": {"native_result": 0}, "order_notional": 0, "auto_permit": {"permit_id": "forged"}}
        self.channel.push(self.frame(records=[{"kind": "JOURNAL", "value": value}]))
        self.assertFalse(self.gateway.poll_once()["errors"])
        with open(os.path.join(self.runtime, "execution_journal", "WB-key-1.json"), encoding="utf-8") as stream:
            mirrored = json.load(stream)
        self.assertEqual(1000.0, mirrored["order_notional"])
        self.assertIsNone(mirrored["auto_permit"])
        self.assertEqual("SUBMIT_CALLED", mirrored["state"])

    def test_late_weaker_journal_is_quarantined_before_ack_and_preserves_unknown_or_terminal(self):
        self.hello()
        envelope, _ = self.store()
        self.request(envelope)
        self.gateway.journal.record("stock", "WB-key-1", "SUBMIT_UNKNOWN", {"reason": "grant expired"})
        late = {"kind": "JOURNAL", "value": {"client_order_key": "WB-key-1", "intent_id": "intent-1",
                                                  "state": "SUBMIT_CALLED", "details": {"late": True}}}
        self.channel.push(self.frame(records=[late]))
        with mock.patch.object(self.db, "transaction", side_effect=sqlite3.OperationalError("audit disk failed")):
            self.assertIn("stock", self.gateway.poll_once()["errors"])
        self.assertEqual([1, 2], self.channel.acks)
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual([1, 2, 3], self.channel.acks)
        self.assertEqual("SUBMIT_UNKNOWN", self.gateway.journal.list_entries("stock")[0]["state"])
        self.gateway.journal.record("stock", "WB-key-1", "FILLED", {"volume": 100})
        late["value"]["state"] = "SUBMIT_UNKNOWN"
        self.channel.push(self.frame(records=[late]))
        self.assertFalse(self.gateway.poll_once()["errors"])
        self.assertEqual("FILLED", self.gateway.journal.list_entries("stock")[0]["state"])
        with self.db.connect() as connection:
            rows = connection.execute("SELECT details_json FROM audit_log WHERE action='MMAP_WEAK_JOURNAL_QUARANTINE' ORDER BY seq").fetchall()
        self.assertEqual(["SUBMIT_UNKNOWN", "FILLED"], [json.loads(row[0])["preserved_state"] for row in rows])

    def test_invalid_hello_or_cross_account_event_stays_unacked(self):
        bad = self.frame(records=[{"kind": "RECEIVED", "message_id": "x"}])
        bad["worker_session"] = None
        self.channel.push(bad)
        self.assertIn("stock", self.gateway.poll_once()["errors"])
        self.assertEqual([], self.channel.acks)
        self.assertEqual([], self.channel.sent)


if __name__ == "__main__":
    unittest.main()
