"""External Worker bridge between durable SQLite/file state and volatile mmap."""

import datetime as dt
import hashlib
import json
import os

from .config import effective_mmap_name
from .errors import BridgeError
from .mmap_journal import ExecutionJournal
from .security import validate_envelope
from .util import atomic_write_json, canonical_json, new_id, parse_time, utc_now


_RUNTIME_FILES = ("local_authorization.json", "local_halt.json", "limited_auto_pause.json")
_EVENT_FOLDERS = frozenset({"events", "command_acks", "control_acks"})
_COMMAND_TYPES = frozenset({"TRADE_INTENT", "CANCEL_ORDER", "REQUEST_SYNC", "CREDIT_PRECHECK"})


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise BridgeError("MMAP_PROTOCOL_INVALID", "%s must be a nonempty string" % name)
    return value


def _read_object(path):
    def reject_constant(value):
        raise ValueError("non-finite JSON number: " + value)

    with open(path, "r", encoding="utf-8") as stream:
        value = json.load(stream, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise BridgeError("MMAP_PROTOCOL_INVALID", "expected a JSON object")
    return value


class _Endpoint:
    def __init__(self, account, channel):
        self.account = account
        self.channel = channel
        self.adapter_session = None
        self.ready = False
        self.sent = {}
        self.command_keys = {}
        self.grants = []
        self.grant_records = {}
        self.sequence = None
        self.event_index = 0
        self.record_index = 0

    def reset_progress(self):
        self.sequence = None
        self.event_index = 0
        self.record_index = 0


class MmapGateway:
    def __init__(self, core, config_path, channel_factory=None, lock_factory=None):
        self.core = core
        self.config = core.config
        self.config_path = os.path.abspath(config_path)
        self.bundle_root = os.path.join(os.path.dirname(os.path.dirname(self.config_path)), "qmt_ready")
        self.worker_session = new_id("worker")
        self._endpoints = {}
        self._locks = []
        self._closed = False
        key = core.keyring.keys[core.keyring.active_key_id]
        try:
            for account in self.config.accounts.values():
                if not account.enabled or account.adapter_transport != "mmap":
                    continue
                if channel_factory is not None:
                    channel = channel_factory(account, key)
                else:
                    from .assets.mmap_channel import MmapChannel
                    if lock_factory is None:
                        from .mmap_sync import WindowsNamedMutex
                        lock_factory = WindowsNamedMutex
                    name = effective_mmap_name(self.config.data_dir, account)
                    lock = lock_factory(name + "_lock")
                    self._locks.append(lock)
                    channel = MmapChannel(name, account.mmap_size, key, "worker", lock)
                self._endpoints[account.alias] = _Endpoint(account, channel)
                # Fence an unconsumed old grant before doing recovery work.
                # The Adapter's final native boundary checks this same sequence
                # under the transport mutex, so startup cannot race an old call.
                if not channel.replace_outgoing({
                    "kind": "WORKER_RESET", "account_alias": account.alias,
                    "worker_session": self.worker_session, "adapter_session": None,
                }):
                    raise BridgeError("MMAP_RESET_FAILED", "Worker startup fence could not be published")
            self.journal = ExecutionJournal(core.db)
            for endpoint in self._endpoints.values():
                self.journal.recover(endpoint.account.alias, self.worker_session)
        except Exception:
            self.close()
            raise

    def poll_once(self):
        """Persist incoming work before ACK; isolate failures by account.

        Failed accounts do not receive a renewed control lease. Reported errors
        remain visible to the Worker while other account mailboxes can progress.
        """
        if self._closed:
            raise RuntimeError("mmap gateway is closed")
        result = {"received": 0, "sent": 0, "errors": {}}
        for alias, endpoint in self._endpoints.items():
            try:
                incoming = endpoint.channel.peek()
                if incoming is not None:
                    self._consume(endpoint, *incoming)
                    result["received"] += 1
                if endpoint.adapter_session is not None and self._send(endpoint):
                    result["sent"] += 1
            except Exception as exc:
                result["errors"][alias] = {
                    "code": getattr(exc, "code", type(exc).__name__),
                    "message": str(exc),
                }
        return result

    def _switch_session(self, endpoint, session):
        if session == endpoint.adapter_session:
            return
        self.journal.recover(endpoint.account.alias, self.worker_session, session)
        endpoint.adapter_session = session
        endpoint.ready = False
        endpoint.sent.clear()
        endpoint.command_keys.clear()
        endpoint.grants.clear()
        endpoint.grant_records.clear()
        endpoint.reset_progress()

    def _consume(self, endpoint, sequence, frame):
        account = endpoint.account
        if not isinstance(frame, dict) or frame.get("account_alias") != account.alias:
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "mmap frame account does not match endpoint")
        session = _text(frame.get("adapter_session"), "adapter_session")
        events = frame.get("events", [])
        records = frame.get("records", [])
        if not isinstance(events, list) or not isinstance(records, list):
            raise BridgeError("MMAP_PROTOCOL_INVALID", "mmap events and records must be arrays")
        worker = frame.get("worker_session")
        if worker is None:
            if events or records:
                raise BridgeError("MMAP_PROTOCOL_INVALID", "HELLO cannot contain events or records")
            self._switch_session(endpoint, session)
            endpoint.ready = True
        else:
            _text(worker, "worker_session")
            self._switch_session(endpoint, session)
            if worker != self.worker_session:
                # Learn the Adapter identity to send bootstrap control, but an
                # old Worker's events, requests and grants cannot cross sessions.
                self.journal.recover(account.alias)
                endpoint.sent.clear()
                endpoint.command_keys.clear()
                endpoint.grants.clear()
                endpoint.grant_records.clear()
                endpoint.ready = False
                self._events(endpoint, sequence, events)
                if records and endpoint.record_index == 0:
                    # Native facts have their signed event envelopes above.
                    # Old execution/control requests remain audit evidence only.
                    self._audit_quarantine(endpoint, "MMAP_STALE_SESSION_QUARANTINE", {
                        "worker_session": worker, "adapter_session": session,
                        "sequence": sequence, "records": records,
                    })
                    endpoint.record_index = len(records)
                if not endpoint.channel.ack(sequence):
                    raise BridgeError("MMAP_ACK_FAILED", "stale mmap frame could not be acknowledged")
                endpoint.reset_progress()
                return
            endpoint.ready = True

        self._events(endpoint, sequence, events)
        while endpoint.record_index < len(records):
            self._record(endpoint, records[endpoint.record_index])
            endpoint.record_index += 1
        if not endpoint.channel.ack(sequence):
            raise BridgeError("MMAP_ACK_FAILED", "persisted mmap frame could not be acknowledged")
        endpoint.reset_progress()

    def _events(self, endpoint, sequence, events):
        if endpoint.sequence != sequence:
            endpoint.sequence = sequence
            endpoint.event_index = 0
            endpoint.record_index = 0
        while endpoint.event_index < len(events):
            self._event(endpoint, events[endpoint.event_index])
            endpoint.event_index += 1

    def _event(self, endpoint, item):
        if not isinstance(item, dict) or item.get("folder") not in _EVENT_FOLDERS:
            raise BridgeError("MMAP_PROTOCOL_INVALID", "invalid mmap event folder")
        envelope = item.get("envelope")
        if not isinstance(envelope, dict):
            raise BridgeError("MMAP_PROTOCOL_INVALID", "mmap event envelope is missing")
        validate_envelope(envelope, self.core.keyring, {"QMT_EVENT"})
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or payload.get("account_alias") != endpoint.account.alias:
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "mmap event account does not match endpoint")
        self.core.ingester.ingest(envelope, endpoint.account)

    def _audit_quarantine(self, endpoint, action, details):
        with self.core.db.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO audit_log(occurred_at,actor,action,account_alias,object_id,details_json) "
                "VALUES(?,'mmap-gateway',?,?,?,?)",
                (utc_now().isoformat(timespec="milliseconds"), action, endpoint.account.alias,
                 endpoint.adapter_session, canonical_json(details).decode("utf-8")),
            )

    def _record(self, endpoint, record):
        if not isinstance(record, dict):
            raise BridgeError("MMAP_PROTOCOL_INVALID", "mmap record must be an object")
        kind = record.get("kind")
        account = endpoint.account
        if kind == "PREPARE_EXECUTION":
            self._prepare(endpoint, record)
        elif kind == "RECEIVED":
            self._receipt(endpoint, _text(record.get("message_id"), "message_id"))
        elif kind == "JOURNAL":
            value = record.get("value")
            if not isinstance(value, dict) or value.get("account_alias", account.alias) != account.alias:
                raise BridgeError("ACCOUNT_TYPE_MISMATCH", "journal account does not match endpoint")
            key = _text(value.get("client_order_key"), "client_order_key")
            existing = next((entry for entry in self.journal.list_entries(account.alias)
                             if entry["client_order_key"] == key), None)
            if existing is None or existing["intent_id"] != value.get("intent_id"):
                raise BridgeError("EXECUTION_UNCERTAIN", "journal record has no matching durable grant")
            try:
                authoritative = self.journal.record(account.alias, key, value.get("state"), value.get("details"))
            except BridgeError as exc:
                if exc.code != "EXECUTION_UNCERTAIN" or value.get("state") not in ("PRE_SUBMIT", "SUBMIT_CALLED", "SUBMIT_UNKNOWN"):
                    raise
                # A late transport status cannot undo expiry/recovery or broker
                # facts, but it must not wedge the mailbox holding those facts.
                self._audit_quarantine(endpoint, "MMAP_WEAK_JOURNAL_QUARANTINE", {
                    "worker_session": self.worker_session, "adapter_session": endpoint.adapter_session,
                    "sequence": endpoint.sequence, "records": [record], "preserved_state": existing["state"],
                })
                authoritative = existing
            atomic_write_json(self._journal_path(account, key), authoritative)
        elif kind == "PAUSE":
            value = self._scoped_value(account, record.get("value"))
            atomic_write_json(self._runtime_path(account, "limited_auto_pause.json"), value)
            if value.get("paused") is True:
                with self.core.db.connect() as connection:
                    permit = connection.execute(
                        "SELECT permit_id,generation FROM auto_permits WHERE account_alias=? AND status='ACTIVE'",
                        (account.alias,),
                    ).fetchone()
                if permit and permit["permit_id"] == value.get("permit_id") and permit["generation"] == value.get("generation"):
                    self.core._pause_active_auto_permit(account.alias, ["AUTO_ADAPTER_LOCALLY_PAUSED"], actor="mmap-adapter")
        elif kind == "P0_PROBE":
            atomic_write_json(self._runtime_path(account, "p0_probe.json"), self._scoped_value(account, record.get("value")))
        else:
            raise BridgeError("MMAP_PROTOCOL_INVALID", "unknown mmap record kind")

    @staticmethod
    def _scoped_value(account, value):
        if not isinstance(value, dict) or value.get("account_alias", account.alias) != account.alias:
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "mmap record value account does not match endpoint")
        return value

    def _prepare(self, endpoint, record):
        command = record.get("command")
        if not isinstance(command, dict):
            raise BridgeError("MMAP_PROTOCOL_INVALID", "execution request command is missing")
        _text(command.get("client_order_key"), "client_order_key")
        _text(command.get("intent_id"), "intent_id")
        try:
            # The same lock serializes mode/permit changes with journal.prepare;
            # its short FULL-synchronous transaction commits before this returns.
            with self.core.db._write_lock:
                self._check_execution(endpoint, record)
                grant = self.journal.prepare(endpoint.account.alias, command,
                                             self.worker_session, endpoint.adapter_session)
        except BridgeError as exc:
            if exc.code == "EXECUTION_JOURNAL_UNAVAILABLE":
                raise
            now = utc_now()
            grant = {
                "account_alias": endpoint.account.alias,
                "client_order_key": command["client_order_key"],
                "intent_id": command["intent_id"],
                "command_digest": hashlib.sha256(canonical_json(command)).hexdigest(),
                "worker_session": self.worker_session,
                "adapter_session": endpoint.adapter_session,
                "grant_id": new_id("denied"),
                "issued_at": now.isoformat(timespec="milliseconds"),
                "expires_at": (now + dt.timedelta(seconds=3)).isoformat(timespec="milliseconds"),
                "state": "SUBMIT_UNKNOWN", "denied": True, "reason": exc.code,
            }
        endpoint.grants.append(grant)
        if grant["state"] == "PRE_SUBMIT":
            endpoint.grant_records[grant["grant_id"]] = json.loads(canonical_json(record))

    def _check_execution(self, endpoint, record):
        account = endpoint.account
        command = record["command"]
        message_id = _text(record.get("message_id"), "message_id")
        if command.get("type") != "EXECUTE_ORDER" or command.get("account_alias") != account.alias:
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "execution request account or command type mismatch")
        with self.core.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM qmt_commands WHERE message_id=? AND account_alias=?",
                (message_id, account.alias),
            ).fetchone()
            if row is None:
                raise BridgeError("EXECUTION_UNCERTAIN", "execution request has no stored command")
            original = json.loads(row["payload_json"])
            validate_envelope(original, self.core.keyring, {"TRADE_INTENT"})
            if original["message_id"] != message_id or canonical_json(original["payload"]) != canonical_json(command):
                raise BridgeError("EXECUTION_UNCERTAIN", "execution request differs from the stored signed command")
            if row["intent_id"] != command["intent_id"] or row["command_type"] not in ("EXECUTE_ORDER", "TRADE_INTENT"):
                raise BridgeError("EXECUTION_UNCERTAIN", "execution request does not match stored intent")
            if row["status"] not in ("PENDING_DELIVERY", "DELIVERED", "QUEUED"):
                raise BridgeError("EXECUTION_UNCERTAIN", "stored command already has execution evidence")
            mode = command.get("execution_mode")
            if mode not in ("OBSERVE_ONLY", "SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO") or self.core._mode(connection) != mode:
                raise BridgeError("MODE_MISMATCH", "execution mode no longer matches Worker mode")
            if self.core._halted(connection):
                raise BridgeError("TRADING_HALTED", "Worker trading is halted")
            # A pre-mmap file journal is evidence, never a fresh permission.
            if os.path.exists(self._journal_path(account, command["client_order_key"])):
                raise BridgeError("EXECUTION_UNCERTAIN", "execution key has an external journal")
            runtime = self._runtime_values(account)
            halt = runtime.get("local_halt.json")
            if halt is not None:
                validate_envelope(halt, self.core.keyring, {"LOCAL_HALT"})
                if (halt.get("payload", {}).get("account_alias", account.alias) != account.alias or
                        halt.get("payload", {}).get("halted") is not False):
                    raise BridgeError("TRADING_HALTED", "Adapter local trading halt is active")
            if mode in ("MANUAL_LIVE", "LIMITED_AUTO"):
                authorization = runtime.get("local_authorization.json")
                if authorization is None:
                    raise BridgeError("LOCAL_AUTHORIZATION_REQUIRED", "local live authorization is missing")
                validate_envelope(authorization, self.core.keyring, {"LOCAL_AUTHORIZATION"})
                payload = authorization.get("payload")
                if (not isinstance(payload, dict) or payload.get("account_alias") != account.alias or
                        mode not in payload.get("allowed_modes", []) or
                        parse_time(payload.get("live_until")) <= utc_now()):
                    raise BridgeError("LOCAL_AUTHORIZATION_REQUIRED", "local live authorization is inactive")
                if mode == "LIMITED_AUTO":
                    self._check_permit(connection, account, command, payload, runtime)

    def _check_permit(self, connection, account, command, authorization, runtime):
        binding = command.get("auto_permit")
        authorized = authorization.get("auto_permit")
        if (authorization.get("authorization_type") != "LIMITED_AUTO_PERMIT" or
                not isinstance(binding, dict) or set(binding) != {"permit_id", "policy_hash", "generation"} or
                not isinstance(authorized, dict)):
            raise BridgeError("AUTO_PERMIT_INVALID", "limited-auto permit binding is missing")
        policy = authorized.get("policy")
        if not isinstance(policy, dict):
            raise BridgeError("AUTO_PERMIT_INVALID", "limited-auto policy is missing")
        digest = hashlib.sha256(canonical_json(policy)).hexdigest()
        if (any(binding.get(key) != authorized.get(key) for key in binding) or
                digest != binding["policy_hash"] or
                policy.get("permit_id") != binding["permit_id"] or policy.get("generation") != binding["generation"] or
                policy.get("account_alias") != account.alias or policy.get("account_type") != account.account_type):
            raise BridgeError("AUTO_PERMIT_INVALID", "limited-auto authorization binding mismatch")
        row = connection.execute(
            "SELECT * FROM auto_permits WHERE permit_id=? AND account_alias=? AND status='ACTIVE'",
            (binding["permit_id"], account.alias),
        ).fetchone()
        now = utc_now()
        if (row is None or row["generation"] != binding["generation"] or row["policy_hash"] != digest or
                canonical_json(json.loads(row["policy_json"])) != canonical_json(policy) or
                parse_time(row["starts_at"]) > now or parse_time(row["expires_at"]) <= now):
            raise BridgeError("AUTO_PERMIT_INVALID", "limited-auto database permit is inactive")
        pause = runtime.get("limited_auto_pause.json")
        if (pause and pause.get("paused") is True and pause.get("permit_id") == binding["permit_id"] and
                pause.get("generation") == binding["generation"]):
            raise BridgeError("AUTO_ADAPTER_LOCALLY_PAUSED", "limited-auto permit is locally paused")

    def _receipt(self, endpoint, message_id):
        path = endpoint.sent.get(message_id)
        if path is None:
            directory = os.path.join(self.core.queue.ensure_partition(endpoint.account.adapter_instance), "commands")
            for name in sorted(os.listdir(directory)):
                if name.endswith(".json"):
                    candidate = os.path.join(directory, name)
                    if _read_object(candidate).get("message_id") == message_id:
                        path = candidate
                        break
        if path is not None and os.path.exists(path):
            self.core.queue._move(path, endpoint.account.adapter_instance, "archive", ".processed")
        endpoint.sent.pop(message_id, None)
        endpoint.command_keys.pop(message_id, None)

    def _commands(self, endpoint):
        directory = os.path.join(self.core.queue.ensure_partition(endpoint.account.adapter_instance), "commands")
        result = []
        for name in sorted(os.listdir(directory)):
            if len(result) >= 8:
                break
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            if path in endpoint.sent.values():
                # Once delivered, the file is retained until RECEIVED even if
                # the signed command expires while a QMT callback is running.
                continue
            try:
                if os.path.getsize(path) > self.core.queue.max_message_bytes:
                    raise BridgeError("MESSAGE_TOO_LARGE", "queue command exceeds size limit")
                envelope = _read_object(path)
                validate_envelope(envelope, self.core.keyring, _COMMAND_TYPES)
                if envelope.get("payload", {}).get("account_alias") != endpoint.account.alias:
                    raise BridgeError("ACCOUNT_TYPE_MISMATCH", "queued command account does not match endpoint")
            except (BridgeError, ValueError):
                self.core.queue._move(path, endpoint.account.adapter_instance, "dead_letter", ".invalid")
                continue
            if envelope["message_id"] not in endpoint.sent:
                result.append((envelope, path))
        return result

    def _runtime_path(self, account, name):
        return os.path.join(self.config.data_dir, "qmt_runtime", account.adapter_instance, name)

    def _runtime_values(self, account):
        result = {}
        for name in _RUNTIME_FILES:
            path = self._runtime_path(account, name)
            if os.path.exists(path):
                result[name] = _read_object(path)
        return result

    def _journal_path(self, account, client_key):
        safe = "".join(character for character in client_key if character.isalnum() or character in "_-")
        if not safe:
            raise BridgeError("MMAP_PROTOCOL_INVALID", "execution journal key cannot form a filename")
        return self._runtime_path(account, os.path.join("execution_journal", safe + ".json"))

    def _journal_snapshot(self, endpoint, runtime, commands):
        needed = {grant["client_order_key"] for grant in endpoint.grants}
        needed.update(endpoint.command_keys.values())
        for envelope, path in commands:
            key = envelope.get("payload", {}).get("client_order_key")
            if isinstance(key, str):
                needed.add(key)
        authorization = runtime.get("local_authorization.json", {}).get("payload", {})
        permit = authorization.get("auto_permit") if isinstance(authorization, dict) else None
        permit_id = permit.get("permit_id") if isinstance(permit, dict) else None
        entries = []
        has_permit_history = False
        for entry in self.journal.list_entries(endpoint.account.alias):
            journal_permit = entry.get("auto_permit") or {}
            same_permit = permit_id is not None and journal_permit.get("permit_id") == permit_id
            if same_permit or entry["client_order_key"] in needed:
                entries.append(entry)
                has_permit_history = has_permit_history or same_permit
        return entries, permit_id if has_permit_history else None

    def _send(self, endpoint):
        # Recheck immediately before publishing, serialized with DB mode/halt
        # and permit changes. A prepared grant may have waited under backpressure.
        with self.core.db._write_lock:
            return self._send_locked(endpoint)

    def _recheck_grants(self, endpoint):
        for index, grant in enumerate(endpoint.grants):
            if grant["state"] != "PRE_SUBMIT":
                continue
            try:
                record = endpoint.grant_records.get(grant["grant_id"])
                if record is None or parse_time(grant["expires_at"]) <= utc_now():
                    raise BridgeError("EXECUTION_UNCERTAIN", "pending execution grant is no longer valid")
                self._check_execution(endpoint, record)
            except BridgeError as exc:
                if exc.code == "EXECUTION_JOURNAL_UNAVAILABLE":
                    raise
                # Quarantine first, then deliver a denial using the original
                # identity and digest. A storage failure publishes no control.
                self.journal.record(
                    endpoint.account.alias, grant["client_order_key"], "SUBMIT_UNKNOWN",
                    {"reason": "execution permission revoked before dispatch", "error_code": exc.code},
                )
                endpoint.grants[index] = dict(grant, state="SUBMIT_UNKNOWN", denied=True, reason=exc.code)

    def _send_locked(self, endpoint):
        account = endpoint.account
        bundle = os.path.join(self.bundle_root, account.alias)
        config = _read_object(os.path.join(bundle, "qmt_adapter.json"))
        if (config.get("account_alias") != account.alias or config.get("account_type") != account.account_type or
                config.get("adapter_instance") != account.adapter_instance):
            raise BridgeError("ACCOUNT_TYPE_MISMATCH", "external Adapter config does not match endpoint")
        if endpoint.ready:
            self._recheck_grants(endpoint)
        commands = self._commands(endpoint) if endpoint.ready else []
        grants = list(endpoint.grants[:8]) if endpoint.ready else []
        runtime = self._runtime_values(account)
        journals, quota_permit = self._journal_snapshot(endpoint, runtime, commands)
        frame = {
            "account_alias": account.alias,
            "adapter_session": endpoint.adapter_session,
            "worker_session": self.worker_session,
            "expires_at": (utc_now() + dt.timedelta(seconds=3)).isoformat(timespec="milliseconds"),
            "config": config,
            "profile": _read_object(os.path.join(bundle, "qmt_profile.json")),
            "runtime": runtime,
            "journals": journals,
            "commands": [],
            "grants": [],
        }
        capacity = getattr(endpoint.channel, "max_payload_size", account.mmap_size // 2 - 56)
        if len(canonical_json(frame)) > capacity:
            if quota_permit is not None:
                raise BridgeError("MMAP_FRAME_TOO_LARGE", "mmap control and current-permit quota history exceed mailbox capacity",
                                  {"permit_id": quota_permit})
            raise BridgeError("MMAP_FRAME_TOO_LARGE", "mmap control and journals exceed mailbox capacity")
        had_items = bool(commands or grants)
        frame["commands"] = [envelope for envelope, path in commands]
        frame["grants"] = grants
        # Grants have a short deadline, so make room for them before commands.
        # Remove only a suffix; unsent items remain pending for the next frame.
        while len(canonical_json(frame)) > capacity and commands:
            commands.pop()
            frame["commands"].pop()
        while len(canonical_json(frame)) > capacity and grants:
            grants.pop()
        if had_items and not commands and not grants:
            raise BridgeError("MMAP_FRAME_TOO_LARGE", "one mmap item exceeds remaining mailbox capacity")
        if not endpoint.channel.send(frame):
            return False
        for envelope, path in commands:
            endpoint.sent[envelope["message_id"]] = path
            key = envelope.get("payload", {}).get("client_order_key")
            if isinstance(key, str):
                endpoint.command_keys[envelope["message_id"]] = key
        for grant in grants:
            endpoint.grant_records.pop(grant["grant_id"], None)
        del endpoint.grants[:len(grants)]
        return True

    def close(self):
        if self._closed:
            return
        self._closed = True
        first_error = None
        for resource in [endpoint.channel for endpoint in self._endpoints.values()] + self._locks:
            try:
                close = getattr(resource, "close", None)
                if close is not None:
                    close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
