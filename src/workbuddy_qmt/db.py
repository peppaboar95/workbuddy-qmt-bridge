import contextlib
import os
import sqlite3
import threading

from .modes import normalize_mode
from .util import iso_now

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL
);
INSERT INTO schema_meta(version)
SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_meta);

CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_events (
  message_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  event_type TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_requests (
  request_id TEXT PRIMARY KEY,
  method TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  ok INTEGER,
  error_code TEXT
);
CREATE TABLE IF NOT EXISTS market_signals (
  signal_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_previews (
  preview_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  snapshot_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  consumed_intent_id TEXT
);
CREATE TABLE IF NOT EXISTS trade_intents (
  intent_id TEXT NOT NULL,
  intent_revision INTEGER NOT NULL,
  preview_id TEXT NOT NULL,
  client_order_key TEXT NOT NULL UNIQUE,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL,
  action TEXT NOT NULL,
  symbol TEXT NOT NULL,
  requested_volume INTEGER NOT NULL,
  limit_price REAL NOT NULL,
  execution_mode TEXT NOT NULL,
  source_signal_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(intent_id, intent_revision),
  FOREIGN KEY(preview_id) REFERENCES trade_previews(preview_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_intents_signal_once
ON trade_intents(source_signal_id) WHERE source_signal_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS risk_decisions (
  risk_decision_id TEXT PRIMARY KEY,
  preview_id TEXT NOT NULL,
  account_alias TEXT NOT NULL,
  allowed INTEGER NOT NULL,
  reasons_json TEXT NOT NULL,
  snapshot_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qmt_commands (
  message_id TEXT PRIMARY KEY,
  intent_id TEXT,
  account_alias TEXT NOT NULL,
  command_type TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qmt_command_acks (
  event_id TEXT PRIMARY KEY,
  message_id TEXT,
  intent_id TEXT,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  account_alias TEXT NOT NULL,
  trading_day TEXT NOT NULL,
  qmt_order_id TEXT NOT NULL,
  client_order_key TEXT,
  intent_id TEXT,
  symbol TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_volume INTEGER,
  filled_volume INTEGER,
  limit_price REAL,
  updated_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(account_alias, trading_day, qmt_order_id)
);
CREATE TABLE IF NOT EXISTS order_events (
  event_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  qmt_order_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
  account_alias TEXT NOT NULL,
  trading_day TEXT NOT NULL,
  trade_id TEXT NOT NULL,
  qmt_order_id TEXT,
  intent_id TEXT,
  symbol TEXT NOT NULL,
  volume INTEGER NOT NULL,
  price REAL NOT NULL,
  traded_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(account_alias, trading_day, trade_id)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_latest
ON account_snapshots(account_alias, captured_at DESC);
CREATE TABLE IF NOT EXISTS quote_snapshots (
  account_alias TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(account_alias, snapshot_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_quote_snapshots_latest
ON quote_snapshots(account_alias, symbol, captured_at DESC);
CREATE TABLE IF NOT EXISTS position_snapshots (
  account_alias TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(account_alias, snapshot_id, symbol)
);
CREATE TABLE IF NOT EXISTS position_snapshot_runs (
  account_alias TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY(account_alias, snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_position_runs_latest
ON position_snapshot_runs(account_alias, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_latest
ON position_snapshots(account_alias, captured_at DESC);
CREATE TABLE IF NOT EXISTS credit_account_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  seq TEXT,
  captured_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_account_latest
ON credit_account_snapshots(account_alias, captured_at DESC);
CREATE TABLE IF NOT EXISTS credit_debt_snapshots (
  account_alias TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  debt_contract_ref TEXT NOT NULL,
  symbol TEXT NOT NULL,
  status TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(account_alias, snapshot_id, debt_contract_ref)
);
CREATE TABLE IF NOT EXISTS credit_eligibility_snapshots (
  account_alias TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY(account_alias, snapshot_id, symbol)
);
CREATE TABLE IF NOT EXISTS credit_capacity_queries (
  account_alias TEXT NOT NULL,
  seq TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  completed_at TEXT,
  request_json TEXT NOT NULL,
  result_json TEXT,
  PRIMARY KEY(account_alias, seq)
);
CREATE TABLE IF NOT EXISTS heartbeats (
  adapter_instance TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
  run_id TEXT PRIMARY KEY,
  account_alias TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS control_commands (
  command_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  account_alias TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  account_alias TEXT,
  object_id TEXT,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_permits (
  permit_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL,
  policy_hash TEXT NOT NULL,
  policy_json TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  starts_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  paused_at TEXT,
  pause_reason TEXT,
  revoked_at TEXT,
  revoked_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_auto_permits_account_status
ON auto_permits(account_alias, status, created_at DESC);
CREATE TABLE IF NOT EXISTS auto_permit_usage (
  permit_id TEXT NOT NULL,
  intent_id TEXT PRIMARY KEY,
  account_alias TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,
  volume INTEGER NOT NULL,
  notional REAL NOT NULL,
  reserved_at TEXT NOT NULL,
  FOREIGN KEY(permit_id) REFERENCES auto_permits(permit_id)
);
CREATE INDEX IF NOT EXISTS idx_auto_permit_usage_permit_time
ON auto_permit_usage(permit_id, reserved_at DESC);
"""


class ClosingConnection(sqlite3.Connection):
    """Make ``with database.connect()`` close handles on Windows as expected."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        # One Worker process owns one Database instance. Serializing its short
        # write transactions avoids SQLite busy waits between MCP request
        # threads and the background event ingester while WAL readers continue.
        self._write_lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, factory=ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self, default_mode="OBSERVE_ONLY"):
        normalized_default = normalize_mode(default_mode, allow_legacy=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            schema_version = connection.execute(
                "SELECT version FROM schema_meta LIMIT 1"
            ).fetchone()["version"]
            if schema_version > 2:
                raise RuntimeError("database schema is newer than this bridge")
            if schema_version < 2:
                connection.execute("UPDATE schema_meta SET version=2")
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key,value,updated_at) VALUES('mode',?,?)",
                (normalized_default, iso_now()),
            )
            current = connection.execute(
                "SELECT value FROM system_state WHERE key='mode'"
            ).fetchone()["value"]
            try:
                normalized_current = normalize_mode(current, allow_legacy=True)
            except ValueError:
                raise RuntimeError("invalid persisted run mode")
            legacy_halted = current == "HALTED"
            if normalized_current != current:
                connection.execute(
                    "UPDATE system_state SET value=?,updated_at=? WHERE key='mode'",
                    (normalized_current, iso_now()),
                )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key,value,updated_at) VALUES('halted','false',?)",
                (iso_now(),),
            )
            if legacy_halted:
                connection.execute(
                    "UPDATE system_state SET value='true',updated_at=? WHERE key='halted'",
                    (iso_now(),),
                )
            connection.execute(
                "INSERT OR IGNORE INTO system_state(key,value,updated_at) VALUES('halt_reason','',?)",
                (iso_now(),),
            )

    @contextlib.contextmanager
    def transaction(self, immediate=False):
        with self._write_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
