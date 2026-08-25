import datetime as dt
import json

from .errors import BridgeError
from .util import iso_now, json_text, new_id, normalize_symbol


EVENT_TYPES = {
    "HEARTBEAT", "COMMAND_ACK", "ORDER_EVENT", "TRADE_EVENT", "POSITION_EVENT",
    "ACCOUNT_SNAPSHOT", "POSITION_SNAPSHOT", "QUOTE_SNAPSHOT", "CREDIT_ACCOUNT_SNAPSHOT",
    "CREDIT_DEBT_SNAPSHOT", "CREDIT_ELIGIBILITY_SNAPSHOT",
    "CREDIT_CAPACITY_SNAPSHOT", "ERROR_EVENT",
}

TERMINAL_INTENT_STATES = {
    "PREVIEW_EXPIRED", "APPROVAL_REJECTED", "RISK_REJECTED", "QMT_REJECTED",
    "BROKER_REJECTED", "EXPIRED", "CANCELLED", "PARTIALLY_CANCELLED", "FAILED",
    "FILLED", "OBSERVE_ONLY_ACKNOWLEDGED",
}


class EventIngester:
    def __init__(self, config, database, file_queue):
        self.config = config
        self.db = database
        self.queue = file_queue

    def scan_once(self, limit_per_folder=100):
        totals = {"processed": 0, "dead_lettered": 0}
        for account in self.config.accounts.values():
            for folder in ("events", "command_acks", "control_acks"):
                result = self.queue.consume(
                    account.adapter_instance,
                    folder,
                    lambda envelope, a=account: self.ingest(envelope, a),
                    expected_types={"QMT_EVENT"},
                    limit=limit_per_folder,
                )
                for key in totals:
                    totals[key] += result[key]
        return totals

    def reconcile_terminal_evidence(self):
        reconciled = 0
        with self.db.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT occurred_at,details_json FROM audit_log "
                "WHERE actor='qmt-adapter' AND action='ERROR_EVENT' ORDER BY seq"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["details_json"])
                except (TypeError, ValueError):
                    continue
                client_order_key = payload.get("client_order_key")
                if payload.get("error_code") != "QMT_ORDER_ERROR" or not client_order_key:
                    continue
                cursor = connection.execute(
                    "UPDATE trade_intents SET status='QMT_REJECTED',updated_at=? "
                    "WHERE client_order_key=? AND status NOT IN (%s)" %
                    ",".join("?" for _ in TERMINAL_INTENT_STATES),
                    [row["occurred_at"], client_order_key] + sorted(TERMINAL_INTENT_STATES),
                )
                reconciled += cursor.rowcount
        return reconciled

    def ingest(self, envelope, account):
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or payload.get("type") not in EVENT_TYPES:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "unknown QMT event type")
        if payload.get("account_alias") != account.alias:
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "event account does not match queue partition")
        if payload.get("account_type", account.account_type) != account.account_type:
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "event account type does not match configuration")
        event_type = payload["type"]
        received = iso_now()
        with self.db.transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbound_events(message_id,account_alias,event_type,received_at,payload_json) VALUES(?,?,?,?,?)",
                (envelope["message_id"], account.alias, event_type, received, json_text(payload)),
            )
            if cursor.rowcount == 0:
                return False
            handler = getattr(self, "_on_%s" % event_type.lower(), None)
            if handler:
                handler(connection, account, payload, received, envelope)
        return True

    def _on_heartbeat(self, connection, account, payload, received, envelope):
        status = payload.get("status")
        if status not in {"STARTING", "RECOVERING", "READY", "HALTED", "ERROR"}:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid heartbeat status")
        occurred = payload.get("occurred_at", envelope["issued_at"])
        connection.execute(
            """INSERT INTO heartbeats(adapter_instance,account_alias,status,occurred_at,received_at,payload_json)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(adapter_instance) DO UPDATE SET status=excluded.status,
               occurred_at=excluded.occurred_at,received_at=excluded.received_at,payload_json=excluded.payload_json""",
            (account.adapter_instance, account.alias, status, occurred, received, json_text(payload)),
        )

    def _on_command_ack(self, connection, account, payload, received, envelope):
        event_id = payload.get("event_id") or envelope["message_id"]
        status = payload.get("status")
        allowed = {
            "ACKNOWLEDGED", "OBSERVE_ONLY_ACKNOWLEDGED", "SUBMIT_CALLED", "SUBMIT_UNKNOWN",
            "QMT_REJECTED", "RISK_REJECTED", "CANCEL_REQUESTED", "SYNC_COMPLETED",
        }
        if status not in allowed:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid command ack status")
        occurred = payload.get("occurred_at", envelope["issued_at"])
        connection.execute(
            "INSERT OR IGNORE INTO qmt_command_acks(event_id,message_id,intent_id,account_alias,status,occurred_at,payload_json) VALUES(?,?,?,?,?,?,?)",
            (event_id, payload.get("message_id"), payload.get("intent_id"), account.alias, status, occurred, json_text(payload)),
        )
        if payload.get("message_id"):
            connection.execute(
                "UPDATE qmt_commands SET status=? WHERE message_id=?",
                (status, payload["message_id"]),
            )
        if payload.get("intent_id"):
            if status in TERMINAL_INTENT_STATES:
                connection.execute(
                    "UPDATE trade_intents SET status=?,updated_at=? WHERE intent_id=?",
                    (status, received, payload["intent_id"]),
                )
            else:
                connection.execute(
                    "UPDATE trade_intents SET status=?,updated_at=? WHERE intent_id=? AND status NOT IN (%s)" %
                    ",".join("?" for _ in TERMINAL_INTENT_STATES),
                    [status, received, payload["intent_id"]] + sorted(TERMINAL_INTENT_STATES),
                )

    def _on_order_event(self, connection, account, payload, received, envelope):
        required = ("qmt_order_id", "symbol", "order_status_normalized")
        if any(payload.get(name) in (None, "") for name in required):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "order event is missing required fields")
        event_id = payload.get("event_id") or envelope["message_id"]
        symbol = normalize_symbol(payload["symbol"])
        occurred = payload.get("occurred_at", envelope["issued_at"])
        trading_day = payload.get("trading_day") or occurred[:10].replace("-", "")
        intent_id = payload.get("intent_id")
        if not intent_id and payload.get("client_order_key"):
            linked = connection.execute(
                "SELECT intent_id FROM trade_intents WHERE client_order_key=?",
                (payload["client_order_key"],),
            ).fetchone()
            intent_id = linked["intent_id"] if linked else None
        connection.execute(
            "INSERT OR IGNORE INTO order_events(event_id,account_alias,qmt_order_id,occurred_at,payload_json) VALUES(?,?,?,?,?)",
            (event_id, account.alias, str(payload["qmt_order_id"]), occurred, json_text(payload)),
        )
        connection.execute(
            """INSERT INTO orders(account_alias,trading_day,qmt_order_id,client_order_key,intent_id,symbol,status,
               requested_volume,filled_volume,limit_price,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_alias,trading_day,qmt_order_id) DO UPDATE SET
               client_order_key=excluded.client_order_key,intent_id=excluded.intent_id,symbol=excluded.symbol,
               status=excluded.status,requested_volume=excluded.requested_volume,filled_volume=excluded.filled_volume,
               limit_price=excluded.limit_price,updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
            (
                account.alias, trading_day, str(payload["qmt_order_id"]), payload.get("client_order_key"),
                intent_id, symbol, payload["order_status_normalized"],
                payload.get("requested_volume"), payload.get("filled_volume"), payload.get("limit_price"),
                occurred, json_text(payload),
            ),
        )
        if intent_id:
            mapping = {
                "ACCEPTED": "ORDER_ACCEPTED", "PARTIALLY_FILLED": "PARTIALLY_FILLED",
                "FILLED": "FILLED", "CANCELLED": "CANCELLED",
                "PARTIALLY_CANCELLED": "PARTIALLY_CANCELLED", "REJECTED": "BROKER_REJECTED",
            }
            state = mapping.get(payload["order_status_normalized"], "ORDER_ACCEPTED")
            connection.execute(
                "UPDATE trade_intents SET status=?,updated_at=? WHERE intent_id=?",
                (state, occurred, intent_id),
            )

    def _on_trade_event(self, connection, account, payload, received, envelope):
        required = ("trade_id", "symbol", "volume", "price", "traded_at")
        if any(payload.get(name) in (None, "") for name in required):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "trade event is missing required fields")
        trading_day = payload.get("trading_day") or payload["traded_at"][:10].replace("-", "")
        intent_id = payload.get("intent_id")
        if not intent_id and payload.get("client_order_key"):
            linked = connection.execute(
                "SELECT intent_id FROM trade_intents WHERE client_order_key=?",
                (payload["client_order_key"],),
            ).fetchone()
            intent_id = linked["intent_id"] if linked else None
        connection.execute(
            """INSERT OR IGNORE INTO trades(account_alias,trading_day,trade_id,qmt_order_id,intent_id,symbol,
               volume,price,traded_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                account.alias, trading_day, str(payload["trade_id"]), payload.get("qmt_order_id"),
                intent_id, normalize_symbol(payload["symbol"]), int(payload["volume"]),
                float(payload["price"]), payload["traded_at"], json_text(payload),
            ),
        )

    def _on_position_event(self, connection, account, payload, received, envelope):
        item = payload.get("position")
        if not isinstance(item, dict) or not item.get("symbol"):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "position event is invalid")
        symbol = normalize_symbol(item["symbol"])
        captured = payload.get("captured_at", envelope["issued_at"])
        snapshot_id = payload.get("snapshot_id") or new_id("posinc")
        latest = connection.execute(
            "SELECT snapshot_id FROM position_snapshot_runs WHERE account_alias=? ORDER BY captured_at DESC LIMIT 1",
            (account.alias,),
        ).fetchone()
        connection.execute(
            "INSERT INTO position_snapshot_runs(account_alias,snapshot_id,captured_at,received_at) VALUES(?,?,?,?)",
            (account.alias, snapshot_id, captured, received),
        )
        if latest:
            connection.execute(
                """INSERT INTO position_snapshots(account_alias,snapshot_id,symbol,captured_at,payload_json)
                   SELECT account_alias,?,symbol,?,payload_json FROM position_snapshots
                   WHERE account_alias=? AND snapshot_id=? AND symbol<>?""",
                (snapshot_id, captured, account.alias, latest["snapshot_id"], symbol),
            )
        normalized = dict(item, symbol=symbol)
        connection.execute(
            "INSERT OR REPLACE INTO position_snapshots(account_alias,snapshot_id,symbol,captured_at,payload_json) VALUES(?,?,?,?,?)",
            (account.alias, snapshot_id, symbol, captured, json_text(normalized)),
        )

    def _on_account_snapshot(self, connection, account, payload, received, envelope):
        snapshot_id = payload.get("snapshot_id") or new_id("acct")
        captured = payload.get("captured_at", envelope["issued_at"])
        connection.execute(
            "INSERT OR IGNORE INTO account_snapshots(snapshot_id,account_alias,captured_at,received_at,payload_json) VALUES(?,?,?,?,?)",
            (snapshot_id, account.alias, captured, received, json_text(payload)),
        )

    def _on_position_snapshot(self, connection, account, payload, received, envelope):
        positions = payload.get("positions")
        if not isinstance(positions, list):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "positions must be an array")
        snapshot_id = payload.get("snapshot_id") or new_id("pos")
        captured = payload.get("captured_at", envelope["issued_at"])
        connection.execute(
            "INSERT OR IGNORE INTO position_snapshot_runs(account_alias,snapshot_id,captured_at,received_at) VALUES(?,?,?,?)",
            (account.alias, snapshot_id, captured, received),
        )
        for item in positions:
            if not isinstance(item, dict) or "symbol" not in item:
                raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid position entry")
            symbol = normalize_symbol(item["symbol"])
            normalized = dict(item, symbol=symbol)
            connection.execute(
                "INSERT OR IGNORE INTO position_snapshots(account_alias,snapshot_id,symbol,captured_at,payload_json) VALUES(?,?,?,?,?)",
                (account.alias, snapshot_id, symbol, captured, json_text(normalized)),
            )

    def _on_quote_snapshot(self, connection, account, payload, received, envelope):
        quotes = payload.get("quotes")
        if not isinstance(quotes, list) or not quotes:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "quotes must be a non-empty array")
        snapshot_id = payload.get("snapshot_id") or new_id("quote")
        captured = payload.get("captured_at", envelope["issued_at"])
        for item in quotes:
            if not isinstance(item, dict) or not item.get("symbol"):
                raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid quote entry")
            symbol = normalize_symbol(item["symbol"])
            normalized = dict(item, symbol=symbol)
            connection.execute(
                "INSERT OR IGNORE INTO quote_snapshots(account_alias,snapshot_id,symbol,captured_at,received_at,payload_json) VALUES(?,?,?,?,?,?)",
                (account.alias, snapshot_id, symbol, captured, received, json_text(normalized)),
            )

    def _on_credit_account_snapshot(self, connection, account, payload, received, envelope):
        self._require_credit(account)
        snapshot_id = payload.get("snapshot_id") or new_id("credit")
        captured = payload.get("captured_at", envelope["issued_at"])
        connection.execute(
            "INSERT OR IGNORE INTO credit_account_snapshots(snapshot_id,account_alias,seq,captured_at,received_at,payload_json) VALUES(?,?,?,?,?,?)",
            (snapshot_id, account.alias, payload.get("seq"), captured, received, json_text(payload)),
        )

    def _on_credit_debt_snapshot(self, connection, account, payload, received, envelope):
        self._require_credit(account)
        debts = payload.get("debts")
        if not isinstance(debts, list):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "debts must be an array")
        snapshot_id = payload.get("snapshot_id") or new_id("debt")
        captured = payload.get("captured_at", envelope["issued_at"])
        for item in debts:
            if not item.get("debt_contract_ref") or not item.get("symbol"):
                raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid debt entry")
            normalized = dict(item, symbol=normalize_symbol(item["symbol"]))
            connection.execute(
                "INSERT OR IGNORE INTO credit_debt_snapshots(account_alias,snapshot_id,debt_contract_ref,symbol,status,captured_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                (account.alias, snapshot_id, item["debt_contract_ref"], normalized["symbol"], item.get("status", "OPEN"), captured, json_text(normalized)),
            )

    def _on_credit_eligibility_snapshot(self, connection, account, payload, received, envelope):
        self._require_credit(account)
        instruments = payload.get("instruments")
        if not isinstance(instruments, list):
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "instruments must be an array")
        snapshot_id = payload.get("snapshot_id") or new_id("elig")
        captured = payload.get("captured_at", envelope["issued_at"])
        for item in instruments:
            normalized = dict(item, symbol=normalize_symbol(item["symbol"]))
            connection.execute(
                "INSERT OR IGNORE INTO credit_eligibility_snapshots(account_alias,snapshot_id,symbol,captured_at,payload_json) VALUES(?,?,?,?,?)",
                (account.alias, snapshot_id, normalized["symbol"], captured, json_text(normalized)),
            )

    def _on_credit_capacity_snapshot(self, connection, account, payload, received, envelope):
        self._require_credit(account)
        seq = str(payload.get("seq", ""))
        if not seq:
            raise BridgeError("MESSAGE_SCHEMA_INVALID", "credit capacity result requires seq")
        cursor = connection.execute(
            "UPDATE credit_capacity_queries SET status='COMPLETED',completed_at=?,result_json=? WHERE account_alias=? AND seq=? AND status='IN_PROGRESS'",
            (received, json_text(payload), account.alias, seq),
        )
        if cursor.rowcount != 1:
            raise BridgeError("CREDIT_CAPACITY_UNAVAILABLE", "capacity result seq has no matching request")

    def _on_error_event(self, connection, account, payload, received, envelope):
        intent_id = payload.get("intent_id")
        if not intent_id and payload.get("client_order_key"):
            linked = connection.execute(
                "SELECT intent_id FROM trade_intents WHERE client_order_key=?",
                (payload["client_order_key"],),
            ).fetchone()
            intent_id = linked["intent_id"] if linked else None
        if payload.get("error_code") == "QMT_ORDER_ERROR" and intent_id:
            connection.execute(
                "UPDATE trade_intents SET status='QMT_REJECTED',updated_at=? WHERE intent_id=?",
                (received, intent_id),
            )
        connection.execute(
            "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) VALUES(?,?,?,?,?,?)",
            (received, "qmt-adapter", "ERROR_EVENT", account.alias, intent_id or payload.get("event_id"), json_text(payload)),
        )

    @staticmethod
    def _require_credit(account):
        if account.account_type != "CREDIT":
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "credit event received on STOCK partition")
