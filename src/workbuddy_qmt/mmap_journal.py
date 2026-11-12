"""Worker-owned durable, one-shot execution grants for the mmap adapter.

The adapter receives a grant only after PRE_SUBMIT has committed. Losing either
process or a reply never makes that client key eligible for another grant.
"""

import contextlib
import datetime as dt
import hashlib
import json
import math
import sqlite3

from .errors import BridgeError, ValidationError
from .util import canonical_json, new_id, parse_time, utc_now


GRANT_TTL_SECONDS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mmap_execution_journal (
    account_alias TEXT NOT NULL,
    client_order_key TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    PRIMARY KEY(account_alias, client_order_key)
)
"""

_PROGRESS = {
    "PRE_SUBMIT": 0,
    "SUBMIT_CALLED": 1,
    "SUBMIT_UNKNOWN": 2,
    "ACKNOWLEDGED": 3,
    "REPORTED": 4,
    "ACCEPTED": 5,
    "ORDER_ACCEPTED": 5,
    "PARTIALLY_FILLED": 6,
    "CANCEL_REQUESTED": 7,
}
_TERMINAL = frozenset({
    "PREVIEW_EXPIRED", "APPROVAL_REJECTED", "RISK_REJECTED", "QMT_REJECTED",
    "BROKER_REJECTED", "REJECTED", "EXPIRED", "CANCELLED", "PARTIALLY_CANCELLED",
    "FAILED", "FILLED", "OBSERVE_ONLY_ACKNOWLEDGED",
})


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a nonempty string" % name)
    return value


def _json_text(value):
    return canonical_json(value).decode("utf-8")


class ExecutionJournal:
    """Persist execution uncertainty in the external Worker's SQLite database."""

    def __init__(self, database):
        self.db = database
        with self._transaction() as connection:
            connection.execute(_SCHEMA)

    @contextlib.contextmanager
    def _transaction(self):
        # Database.transaction starts BEGIN before yielding. SQLite cannot change
        # synchronous inside a transaction, so configure this connection first.
        with self.db._write_lock:
            connection = None
            try:
                connection = self.db.connect()
                if connection.execute("PRAGMA synchronous").fetchone()[0] < 2:
                    connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception as exc:
                if connection is not None:
                    with contextlib.suppress(Exception):
                        connection.rollback()
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise BridgeError(
                        "EXECUTION_JOURNAL_UNAVAILABLE",
                        "execution journal could not be durably committed",
                        {"reason": type(exc).__name__},
                    ) from exc
                raise
            finally:
                if connection is not None:
                    connection.close()

    def prepare(self, account_alias, command, worker_session, adapter_session):
        """Commit PRE_SUBMIT and return the only grant this account/key can get.

        An identical repeat is uncertain too: the first reply may already have
        reached the adapter. Expiration limits use of a grant, never its memory.
        """
        _required_text(account_alias, "account_alias")
        _required_text(worker_session, "worker_session")
        _required_text(adapter_session, "adapter_session")
        if not isinstance(command, dict) or command.get("type") != "EXECUTE_ORDER":
            raise ValidationError("execution journal requires an EXECUTE_ORDER payload")
        try:
            encoded = canonical_json(command)
            payload = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValidationError("execution command must contain finite JSON values") from exc
        if payload.get("account_alias") != account_alias:
            raise ValidationError("execution command account does not match journal account")
        client_key = _required_text(payload.get("client_order_key"), "client_order_key")
        intent_id = _required_text(payload.get("intent_id"), "intent_id")
        digest = hashlib.sha256(encoded).hexdigest()
        order = payload.get("resolved_order") or {}
        try:
            notional = float(order.get("limit_price", 0) or 0) * int(order.get("volume", 0) or 0)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("execution command has invalid order notional") from exc
        if not math.isfinite(notional) or notional < 0:
            raise ValidationError("execution command has invalid order notional")

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT command_digest,state FROM mmap_execution_journal "
                "WHERE account_alias=? AND client_order_key=?",
                (account_alias, client_key),
            ).fetchone()
            if existing is not None:
                matches = existing["command_digest"] == digest
                raise BridgeError(
                    "EXECUTION_UNCERTAIN",
                    "execution key already has a grant" if matches else
                    "execution key was previously used for a different command",
                    {"client_order_key": client_key, "state": existing["state"],
                     "command_digest_matches": matches},
                )
            now = utc_now()
            issued = now.isoformat(timespec="milliseconds")
            entry = {
                "account_alias": account_alias,
                "client_order_key": client_key,
                "intent_id": intent_id,
                "command_digest": digest,
                "worker_session": worker_session,
                "adapter_session": adapter_session,
                "grant_id": new_id("grant"),
                "issued_at": issued,
                "expires_at": (now + dt.timedelta(seconds=GRANT_TTL_SECONDS)).isoformat(timespec="milliseconds"),
                "state": "PRE_SUBMIT",
                "created_at": issued,
                "updated_at": issued,
                "details": {},
                "auto_permit": payload.get("auto_permit"),
                "order_notional": notional,
            }
            connection.execute(
                "INSERT INTO mmap_execution_journal"
                "(account_alias,client_order_key,command_digest,state,entry_json) VALUES(?,?,?,?,?)",
                (account_alias, client_key, digest, "PRE_SUBMIT", _json_text(entry)),
            )
        return entry

    def record(self, account_alias, client_order_key, state, details=None):
        """Record forward progress; replays cannot erase uncertainty or facts."""
        _required_text(account_alias, "account_alias")
        _required_text(client_order_key, "client_order_key")
        if state not in _PROGRESS and state not in _TERMINAL:
            raise ValidationError("unknown execution journal state")
        if details is not None and not isinstance(details, dict):
            raise ValidationError("execution journal details must be an object")
        try:
            recorded_details = json.loads(_json_text(details or {}))
        except (TypeError, ValueError) as exc:
            raise ValidationError("execution journal details must contain finite JSON values") from exc
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT entry_json FROM mmap_execution_journal "
                "WHERE account_alias=? AND client_order_key=?",
                (account_alias, client_order_key),
            ).fetchone()
            if row is None:
                raise BridgeError("EXECUTION_UNCERTAIN", "execution key has no durable grant")
            entry = json.loads(row["entry_json"])
            previous = entry["state"]
            if state != previous:
                if previous in _TERMINAL or (
                    state not in _TERMINAL and _PROGRESS[state] <= _PROGRESS[previous]
                ):
                    raise BridgeError(
                        "EXECUTION_UNCERTAIN", "execution journal state cannot regress or rewrite terminal facts",
                        {"client_order_key": client_order_key, "state": previous, "requested_state": state},
                    )
                entry["state"] = state
                entry["details"] = recorded_details
                entry["updated_at"] = utc_now().isoformat(timespec="milliseconds")
                self._update(connection, entry)
        return entry

    def list_entries(self, account_alias):
        """Return persisted journal values used by limited-auto usage checks."""
        _required_text(account_alias, "account_alias")
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT entry_json FROM mmap_execution_journal WHERE account_alias=? ORDER BY client_order_key",
                (account_alias,),
            ).fetchall()
        return [json.loads(row["entry_json"]) for row in rows]

    def recover(self, account_alias, worker_session=None, adapter_session=None):
        """Quarantine stale in-flight grants and return all unresolved entries.

        Omitting both sessions means recovery after loss of the prior sessions.
        With current session IDs, only mismatched sessions or expired PRE_SUBMIT
        grants are quarantined. No recovery path grants permission to retry.
        """
        _required_text(account_alias, "account_alias")
        if worker_session is not None:
            _required_text(worker_session, "worker_session")
        if adapter_session is not None:
            _required_text(adapter_session, "adapter_session")
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT entry_json FROM mmap_execution_journal WHERE account_alias=? ORDER BY client_order_key",
                (account_alias,),
            ).fetchall()
            has_intents = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_intents'"
            ).fetchone() is not None
            now = utc_now()
            recovered_at = now.isoformat(timespec="milliseconds")
            unknown = []
            for row in rows:
                entry = json.loads(row["entry_json"])
                stale = (
                    (worker_session is None and adapter_session is None) or
                    (worker_session is not None and worker_session != entry["worker_session"]) or
                    (adapter_session is not None and adapter_session != entry["adapter_session"])
                )
                expired = entry["state"] == "PRE_SUBMIT" and parse_time(entry["expires_at"]) <= now
                if entry["state"] in ("PRE_SUBMIT", "SUBMIT_CALLED") and (stale or expired):
                    entry["state"] = "SUBMIT_UNKNOWN"
                    entry["updated_at"] = recovered_at
                    entry["details"] = dict(entry.get("details") or {},
                                            recovery_reason="expired grant" if expired else "execution session changed",
                                            recovered_at=recovered_at)
                    self._update(connection, entry)
                if entry["state"] == "SUBMIT_UNKNOWN":
                    if has_intents:
                        # Only states without known broker acceptance can become
                        # unknown. A late journal recovery cannot erase fills.
                        connection.execute(
                            "UPDATE trade_intents SET status='SUBMIT_UNKNOWN',updated_at=? "
                            "WHERE account_alias=? AND client_order_key=? AND intent_id=? "
                            "AND status IN ('QUEUED','PRE_SUBMIT','SUBMIT_CALLED','ACKNOWLEDGED')",
                            (recovered_at, account_alias, entry["client_order_key"], entry["intent_id"]),
                        )
                    unknown.append(entry)
        return unknown

    @staticmethod
    def _update(connection, entry):
        connection.execute(
            "UPDATE mmap_execution_journal SET state=?,entry_json=? "
            "WHERE account_alias=? AND client_order_key=?",
            (entry["state"], _json_text(entry), entry["account_alias"], entry["client_order_key"]),
        )
