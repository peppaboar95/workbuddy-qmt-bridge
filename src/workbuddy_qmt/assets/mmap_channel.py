"""Python 3.6-compatible authenticated, explicitly acknowledged mmap mailboxes.

The caller must supply the SAME reliable interprocess lock to both peers. A GIL,
thread-only lock, mmap.flush(), or slice assignment is not that synchronization.
The mapping is volatile transport; application sessions and durable processing
must be handled above this layer. HMAC authenticates messages, not secrecy.

Wire layout: two equal slots (worker -> adapter, then adapter -> worker). Each
slot starts with >8sBBBBQI32s: magic, version, direction, state, reserved=0,
sequence, payload length, SHA-256 HMAC. Remaining bytes hold UTF-8 JSON followed
by zero padding. ACK records retain the sequence and have zero payload length.
The HMAC covers the mapping identity, header with a zero MAC, and payload,
including state so a forged ACK cannot release a pending message.
"""

import mmap
import struct
import json
import hashlib
import hmac


MAGIC = b"WQMMAP01"
VERSION = 1
WORKER_TO_ADAPTER = 1
ADAPTER_TO_WORKER = 2
ACKNOWLEDGED = 0
PENDING = 1
_HEADER = struct.Struct(">8sBBBBQI32s")
HEADER_SIZE = _HEADER.size
MIN_SIZE = 2 * (HEADER_SIZE + 2)
MAX_SEQUENCE = (1 << 64) - 1
_ZERO_MAC = bytes(32)
_INFINITY = float("inf")


class MmapProtocolError(ValueError):
    """Untrusted shared memory does not contain a valid current frame."""


def _validate_json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value == _INFINITY or value == -_INFINITY:
            raise ValueError("non-finite JSON number")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _validate_json_value(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    raise ValueError("unsupported JSON value")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


class MmapChannel(object):
    """One sender and one receiver per role, with one outstanding message each.

    ``send(dict)`` returns False while the outgoing mailbox is unacknowledged.
    ``peek()`` repeatedly returns (sequence, dict) until ``ack(sequence)``.
    ``ack`` returns False for an absent message or a different sequence.
    ``replace_outgoing(dict)`` publishes a sender startup/session barrier even
    when the old outgoing frame is pending. ``guard_incoming(seq, callback)``
    serializes a final application action with such a takeover barrier.
    Malformed/authentication/replay failures raise MmapProtocolError (ValueError)
    without clearing memory. Invalid outgoing values raise ValueError.

    ``key`` is nonempty bytes or a UTF-8 string; deploy a high-entropy secret.
    ``mapping`` may inject a shared bytearray/mmap; it remains caller-owned.
    An omitted mapping opens a Windows named mmap and is owned by this object.
    Reopening preserves sequences, but replay protection across a complete
    process restart also requires application session identities.
    """

    def __init__(self, name, size, key, role, lock, mapping=None):
        if not isinstance(name, str) or not name or "\0" in name:
            raise ValueError("mapping name must be a nonempty string without NUL")
        if isinstance(size, bool) or not isinstance(size, int) or size < MIN_SIZE:
            raise ValueError("mapping size is too small or invalid")
        if size // 2 - HEADER_SIZE > 0xffffffff:
            raise ValueError("mapping payload exceeds the protocol length limit")
        if role not in ("worker", "adapter"):
            raise ValueError("role must be worker or adapter")
        if isinstance(key, str):
            key = key.encode("utf-8")
        if not isinstance(key, bytes) or not key:
            raise ValueError("a nonempty bytes or string HMAC key is required")
        if not callable(getattr(lock, "__enter__", None)) or not callable(
                getattr(lock, "__exit__", None)):
            raise ValueError("a reliable interprocess context-manager lock is required")
        if mapping is not None and len(mapping) != size:
            raise ValueError("mapping length does not match its configured size")

        self.name = name
        self.size = size
        self.role = role
        self.max_payload_size = size // 2 - HEADER_SIZE
        self._slot_size = size // 2
        self._key = key
        self._lock = lock
        self._outgoing = WORKER_TO_ADAPTER if role == "worker" else ADAPTER_TO_WORKER
        self._incoming = ADAPTER_TO_WORKER if role == "worker" else WORKER_TO_ADAPTER
        self._prefix = b"WQMT-MMAP\0" + name.encode("utf-8") + b"\0" + struct.pack(">Q", size)
        self._seen = {}
        self._acknowledged = {}
        self._pending_mac = {}
        self._closed = False
        self._owned = mapping is None
        self._mapping = mmap.mmap(-1, size, tagname=name) if self._owned else mapping
        try:
            with self._lock:
                if bytes(self._mapping[:size]) == bytes(size):
                    self._write_slot(WORKER_TO_ADAPTER, ACKNOWLEDGED, 0, b"")
                    self._write_slot(ADAPTER_TO_WORKER, ACKNOWLEDGED, 0, b"")
                else:
                    self._read_slot(WORKER_TO_ADAPTER)
                    self._read_slot(ADAPTER_TO_WORKER)
        except BaseException:
            if self._owned:
                self._mapping.close()
            self._closed = True
            raise

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("mmap channel is closed")

    def _offset(self, direction):
        return 0 if direction == WORKER_TO_ADAPTER else self._slot_size

    def _mac(self, direction, state, sequence, payload):
        header = _HEADER.pack(MAGIC, VERSION, direction, state, 0,
                              sequence, len(payload), _ZERO_MAC)
        return hmac.new(self._key, self._prefix + header + payload, hashlib.sha256).digest()

    def _write_slot(self, direction, state, sequence, payload):
        signature = self._mac(direction, state, sequence, payload)
        header = _HEADER.pack(MAGIC, VERSION, direction, state, 0,
                              sequence, len(payload), signature)
        frame = header + payload
        start = self._offset(direction)
        self._mapping[start:start + self._slot_size] = frame + bytes(self._slot_size - len(frame))
        self._seen[direction] = sequence
        if state == ACKNOWLEDGED:
            self._acknowledged[direction] = sequence
            self._pending_mac.pop(direction, None)
        else:
            self._pending_mac[direction] = signature

    def _read_slot(self, direction):
        start = self._offset(direction)
        header = bytes(self._mapping[start:start + HEADER_SIZE])
        magic, version, actual_direction, state, reserved, sequence, length, signature = _HEADER.unpack(header)
        if (magic != MAGIC or version != VERSION or actual_direction != direction
                or reserved != 0 or state not in (ACKNOWLEDGED, PENDING)):
            raise MmapProtocolError("invalid mmap frame header")
        if (length > self.max_payload_size or (state == ACKNOWLEDGED and length != 0)
                or (state == PENDING and (length < 2 or sequence == 0))):
            raise MmapProtocolError("invalid mmap frame length or sequence")
        payload = bytes(self._mapping[start + HEADER_SIZE:start + HEADER_SIZE + length])
        if len(payload) != length or not hmac.compare_digest(
                signature, self._mac(direction, state, sequence, payload)):
            raise MmapProtocolError("mmap frame authentication failed")
        high = self._seen.get(direction, -1)
        if sequence < high or (state == PENDING and sequence <= self._acknowledged.get(direction, -1)):
            raise MmapProtocolError("replayed mmap frame")
        if (state == PENDING and sequence == high and direction in self._pending_mac
                and not hmac.compare_digest(signature, self._pending_mac[direction])):
            raise MmapProtocolError("mmap payload changed without a new sequence")
        message = None
        if state == PENDING:
            try:
                message = json.loads(payload.decode("utf-8"), object_pairs_hook=_json_object,
                                     parse_constant=_reject_constant)
                if not isinstance(message, dict):
                    raise ValueError("mmap message must be a JSON object")
                _validate_json_value(message)
            except (ValueError, TypeError, RecursionError) as exc:
                raise MmapProtocolError("invalid mmap JSON payload: " + str(exc))
        self._seen[direction] = sequence
        if state == ACKNOWLEDGED:
            self._acknowledged[direction] = sequence
            self._pending_mac.pop(direction, None)
        else:
            self._pending_mac[direction] = signature
        return state, sequence, message

    def _publish(self, message, replace):
        self._ensure_open()
        try:
            if not isinstance(message, dict):
                raise ValueError("mmap message must be a dict")
            _validate_json_value(message)
            payload = json.dumps(message, ensure_ascii=False, allow_nan=False,
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("invalid outgoing mmap JSON: " + str(exc))
        if len(payload) > self.max_payload_size:
            raise ValueError("mmap message exceeds mailbox capacity")
        with self._lock:
            self._ensure_open()
            state, sequence, unused = self._read_slot(self._outgoing)
            if state == PENDING and not replace:
                return False
            if sequence == MAX_SEQUENCE:
                raise MmapProtocolError("mmap sequence exhausted")
            self._write_slot(self._outgoing, PENDING, sequence + 1, payload)
            return True

    def send(self, message):
        return self._publish(message, False)

    def replace_outgoing(self, message):
        """Publish a startup/session barrier, superseding our pending frame.

        Call only when taking over a sender session, never to bypass normal
        message backpressure. The caller must reconcile superseded work. The
        old authenticated sequence is incremented, never reset or reused.
        """
        return self._publish(message, True)

    def guard_incoming(self, sequence, callback):
        """Run callback under the shared lock if this frame is still pending.

        Return False without invoking callback when the sequence is no longer
        current. Do not call channel operations inside callback: the supplied
        lock need not be reentrant. Exceptions propagate after releasing it.
        This does not acknowledge or consume the message; application code
        must supply its own once-only execution latch.
        """
        if not callable(callback):
            raise TypeError("mmap guard callback must be callable")
        with self._lock:
            self._ensure_open()
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                return False
            state, current, unused = self._read_slot(self._incoming)
            if state != PENDING or sequence != current:
                return False
            callback()
            return True

    def peek(self):
        with self._lock:
            self._ensure_open()
            state, sequence, message = self._read_slot(self._incoming)
            return (sequence, message) if state == PENDING else None

    def ack(self, sequence):
        with self._lock:
            self._ensure_open()
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                return False
            state, current, unused = self._read_slot(self._incoming)
            if state != PENDING or sequence != current:
                return False
            self._write_slot(self._incoming, ACKNOWLEDGED, current, b"")
            return True

    def close(self):
        with self._lock:
            if not self._closed:
                if self._owned:
                    self._mapping.close()
                self._closed = True
