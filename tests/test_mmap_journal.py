import concurrent.futures
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

from workbuddy_qmt.db import Database
from workbuddy_qmt.errors import BridgeError
from workbuddy_qmt.mmap_journal import ExecutionJournal
from workbuddy_qmt.util import canonical_json, parse_time


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "journal.db")
        self.database = Database(self.path)
        self.journal = ExecutionJournal(self.database)
        self.command = {
            "type": "EXECUTE_ORDER",
            "account_alias": "stock",
            "client_order_key": "WB-test-1",
            "intent_id": "intent-1",
            "execution_mode": "LIMITED_AUTO",
            "resolved_order": {"symbol": "600000.SH", "volume": 100, "limit_price": 10.5},
            "auto_permit": {"permit_id": "permit-1", "generation": 1},
        }

    def prepare(self, command=None, **kwargs):
        return self.journal.prepare(
            "stock", command or self.command,
            kwargs.get("worker_session", "worker-1"),
            kwargs.get("adapter_session", "adapter-1"),
        )

    def assert_uncertain(self, action):
        with self.assertRaises(BridgeError) as caught:
            action()
        self.assertEqual("EXECUTION_UNCERTAIN", caught.exception.code)
        return caught.exception

    def test_prepare_is_durable_before_return_and_never_regrants_after_restart(self):
        grant = self.prepare()
        self.assertEqual("PRE_SUBMIT", grant["state"])
        self.assertEqual("intent-1", grant["intent_id"])
        self.assertEqual("worker-1", grant["worker_session"])
        self.assertEqual("adapter-1", grant["adapter_session"])
        self.assertTrue(grant["grant_id"])
        self.assertEqual(hashlib.sha256(canonical_json(self.command)).hexdigest(), grant["command_digest"])
        reopened = ExecutionJournal(Database(self.path))
        persisted = reopened.list_entries("stock")
        self.assertEqual(grant["grant_id"], persisted[0]["grant_id"])
        self.assertEqual("PRE_SUBMIT", persisted[0]["state"])
        self.assertEqual(1050.0, persisted[0]["order_notional"])
        self.assertEqual(self.command["auto_permit"], persisted[0]["auto_permit"])
        self.assertEqual({}, persisted[0]["details"])
        self.assertIn("created_at", persisted[0])
        self.assert_uncertain(lambda: reopened.prepare("stock", self.command, "worker-2", "adapter-2"))
        self.assert_uncertain(self.prepare)
        self.assertEqual(1, len(reopened.list_entries("stock")))

    def test_canonical_duplicate_and_changed_payload_both_fail_closed(self):
        self.prepare()
        reordered = dict(reversed(list(self.command.items())))
        same = self.assert_uncertain(lambda: self.prepare(reordered))
        self.assertTrue(same.details["command_digest_matches"])
        changed = json.loads(json.dumps(self.command))
        changed["resolved_order"]["volume"] = 200
        mismatch = self.assert_uncertain(lambda: self.prepare(changed))
        self.assertFalse(mismatch.details["command_digest_matches"])
        self.assertEqual(1050.0, self.journal.list_entries("stock")[0]["order_notional"])

    def test_client_key_and_recovery_are_scoped_to_account(self):
        self.prepare()
        other = dict(self.command, account_alias="credit")
        second = self.journal.prepare("credit", other, "worker-1", "adapter-1")
        self.assertNotEqual(second["grant_id"], self.journal.list_entries("stock")[0]["grant_id"])
        self.journal.recover("stock")
        self.assertEqual("SUBMIT_UNKNOWN", self.journal.list_entries("stock")[0]["state"])
        self.assertEqual("PRE_SUBMIT", self.journal.list_entries("credit")[0]["state"])
        self.assertEqual([], self.journal.list_entries("missing"))

    def test_expired_grant_never_becomes_a_retry_permission(self):
        now = dt.datetime(2026, 11, 12, 1, 0, tzinfo=dt.timezone.utc)
        with mock.patch("workbuddy_qmt.mmap_journal.utc_now", return_value=now):
            grant = self.prepare()
        duration = (parse_time(grant["expires_at"]) - parse_time(grant["issued_at"])).total_seconds()
        self.assertGreater(duration, 0)
        self.assertLessEqual(duration, 5)
        with mock.patch("workbuddy_qmt.mmap_journal.utc_now", return_value=now + dt.timedelta(seconds=20)):
            self.assert_uncertain(self.prepare)
            recovered = self.journal.recover("stock", "worker-1", "adapter-1")
        self.assertEqual("SUBMIT_UNKNOWN", recovered[0]["state"])
        self.assert_uncertain(self.prepare)

    def test_concurrent_connections_grant_the_same_key_only_once(self):
        journals = [ExecutionJournal(Database(self.path)) for _ in range(4)]

        def attempt(journal):
            try:
                journal.prepare("stock", self.command, "worker-1", "adapter-1")
                return "granted"
            except BridgeError as exc:
                return exc.code

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, journals))
        self.assertEqual(1, results.count("granted"))
        self.assertEqual(3, results.count("EXECUTION_UNCERTAIN"))

    def test_commit_failure_returns_no_grant_and_rolls_back(self):
        class CommitFailure(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("simulated storage failure")

        def failing_connect():
            connection = sqlite3.connect(self.path, isolation_level=None, factory=CommitFailure)
            connection.row_factory = sqlite3.Row
            return connection

        with mock.patch.object(self.database, "connect", side_effect=failing_connect):
            with self.assertRaises(BridgeError) as caught:
                self.prepare()
        self.assertEqual("EXECUTION_JOURNAL_UNAVAILABLE", caught.exception.code)
        self.assertEqual([], self.journal.list_entries("stock"))
        self.assertEqual("PRE_SUBMIT", self.prepare()["state"])

    def test_ambiguous_commit_failure_cannot_produce_a_second_grant(self):
        class ReplyFailure(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("commit completed but caller lost the result")

        def ambiguous_connect():
            connection = sqlite3.connect(self.path, isolation_level=None, factory=ReplyFailure)
            connection.row_factory = sqlite3.Row
            return connection

        with mock.patch.object(self.database, "connect", side_effect=ambiguous_connect):
            with self.assertRaises(BridgeError) as caught:
                self.prepare()
        self.assertEqual("EXECUTION_JOURNAL_UNAVAILABLE", caught.exception.code)
        reopened = ExecutionJournal(Database(self.path))
        self.assertEqual("PRE_SUBMIT", reopened.list_entries("stock")[0]["state"])
        self.assert_uncertain(lambda: reopened.prepare("stock", self.command, "worker-2", "adapter-2"))

    def test_journal_transactions_require_full_synchronous_without_weakening_extra(self):
        original_connect = self.database.connect
        settings = []

        def connect_with_level(level):
            connection = original_connect()
            connection.execute("PRAGMA synchronous=%s" % level)
            connection.create_function("capture_sync", 0, lambda: settings.append(connection.execute("PRAGMA synchronous").fetchone()[0]) or 1)
            return connection

        with self.database.connect() as connection:
            connection.execute("CREATE TRIGGER capture_durability BEFORE INSERT ON mmap_execution_journal BEGIN SELECT capture_sync(); END")
        for level in ("NORMAL", "EXTRA"):
            command = dict(self.command, client_order_key="key-" + level)
            with mock.patch.object(self.database, "connect", side_effect=lambda level=level: connect_with_level(level)):
                self.prepare(command)
        self.assertEqual([2, 3], settings)

    def test_record_survives_restart_and_rejects_regressions(self):
        self.prepare()
        recorded = self.journal.record("stock", "WB-test-1", "SUBMIT_CALLED", {"native_result": 0})
        self.assertEqual("SUBMIT_CALLED", recorded["state"])
        reopened = ExecutionJournal(Database(self.path))
        self.assertEqual({"native_result": 0}, reopened.list_entries("stock")[0]["details"])
        self.assert_uncertain(lambda: reopened.record("stock", "WB-test-1", "PRE_SUBMIT"))
        reopened.record("stock", "WB-test-1", "SUBMIT_UNKNOWN", {"reason": "session lost"})
        self.assert_uncertain(lambda: reopened.record("stock", "WB-test-1", "SUBMIT_CALLED"))
        reopened.record("stock", "WB-test-1", "ORDER_ACCEPTED", {"qmt_order_id": "123"})
        self.assert_uncertain(lambda: reopened.record("stock", "WB-test-1", "SUBMIT_UNKNOWN"))
        terminal = reopened.record("stock", "WB-test-1", "FILLED", {"volume": 100})
        self.assertEqual(terminal, reopened.record("stock", "WB-test-1", "FILLED", {"volume": 1}))
        self.assert_uncertain(lambda: reopened.record("stock", "WB-test-1", "CANCELLED"))
        self.assert_uncertain(lambda: reopened.record("stock", "WB-test-1", "SUBMIT_CALLED"))

    def test_observe_only_ack_is_durable_terminal_and_record_cannot_create_a_grant(self):
        self.assert_uncertain(lambda: self.journal.record("stock", "missing", "SUBMIT_CALLED"))
        self.prepare()
        self.journal.record("stock", "WB-test-1", "OBSERVE_ONLY_ACKNOWLEDGED")
        restarted = ExecutionJournal(Database(self.path))
        self.assertEqual([], restarted.recover("stock"))
        self.assertEqual("OBSERVE_ONLY_ACKNOWLEDGED", restarted.list_entries("stock")[0]["state"])
        self.assert_uncertain(lambda: restarted.prepare("stock", self.command, "worker-2", "adapter-2"))

    def test_recover_marks_only_stale_sessions_and_returns_unknown_entries(self):
        first = self.prepare()
        second = dict(self.command, client_order_key="WB-test-2", intent_id="intent-2")
        self.prepare(second, adapter_session="adapter-2")
        self.journal.record("stock", "WB-test-2", "SUBMIT_CALLED")
        unknown = self.journal.recover("stock", "worker-1", "adapter-1")
        self.assertEqual(["WB-test-2"], [entry["client_order_key"] for entry in unknown])
        self.assertEqual(first["grant_id"], self.journal.list_entries("stock")[0]["grant_id"])
        self.assertEqual("PRE_SUBMIT", self.journal.list_entries("stock")[0]["state"])
        restarted = ExecutionJournal(Database(self.path))
        recovered = restarted.recover("stock", "worker-2")
        self.assertEqual({"WB-test-1", "WB-test-2"}, {entry["client_order_key"] for entry in recovered})
        self.assertTrue(all(entry["state"] == "SUBMIT_UNKNOWN" for entry in recovered))
        self.assert_uncertain(self.prepare)

    def test_recovery_updates_uncertain_intents_without_overwriting_known_evidence(self):
        self.database.initialize()
        with self.database.connect() as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            connection.execute(
                "INSERT INTO trade_previews(preview_id,account_alias,request_json,result_json,snapshot_fingerprint,created_at,expires_at) "
                "VALUES('preview','stock','{}','{}','snapshot','before','later')"
            )

        def add_intent(command, account, status):
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO trade_intents(intent_id,intent_revision,preview_id,client_order_key,account_alias,status,"
                    "action,symbol,requested_volume,limit_price,execution_mode,created_at,updated_at,payload_json) "
                    "VALUES(?,1,'preview',?,?,?,'BUY','600000.SH',100,10.5,'LIMITED_AUTO','before','before','{}')",
                    (command["intent_id"], command["client_order_key"], account, status),
                )

        statuses = ("QUEUED", "SUBMIT_CALLED", "ORDER_ACCEPTED", "PARTIALLY_FILLED", "FILLED", "QMT_REJECTED")
        for index, status in enumerate(statuses):
            command = dict(self.command, client_order_key="key-%d" % index, intent_id="intent-%d" % index)
            self.prepare(command)
            add_intent(command, "stock", status)
        # Matching identifiers on another account must not be updated.
        crossed = dict(self.command, client_order_key="crossed-key", intent_id="crossed-intent")
        self.prepare(crossed)
        add_intent(crossed, "credit", "QUEUED")
        restarted = ExecutionJournal(Database(self.path))
        restarted.recover("stock")
        with self.database.connect() as connection:
            recovered = connection.execute("SELECT status FROM trade_intents WHERE account_alias='stock' ORDER BY intent_id").fetchall()
            untouched = connection.execute("SELECT status,updated_at FROM trade_intents WHERE account_alias='credit'").fetchone()
        self.assertEqual(["SUBMIT_UNKNOWN", "SUBMIT_UNKNOWN"] + list(statuses[2:]), [row["status"] for row in recovered])
        self.assertEqual(("QUEUED", "before"), tuple(untouched))


if __name__ == "__main__":
    unittest.main()
