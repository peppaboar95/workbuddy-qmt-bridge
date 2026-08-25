import base64
import hashlib
import hmac
import json

from .errors import BridgeError
from .util import canonical_json, iso_now, new_id, parse_time, utc_now


class KeyRing:
    def __init__(self, keys, active_key_id):
        if active_key_id not in keys:
            raise BridgeError("CONFIG_ERROR", "active message key is missing")
        self.keys = dict(keys)
        self.active_key_id = active_key_id

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                raw = json.load(stream)
            keys = {
                key_id: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                for key_id, value in raw["keys"].items()
            }
            return cls(keys, raw["active_key_id"])
        except BridgeError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            raise BridgeError("CONFIG_ERROR", "cannot load message keys: %s" % exc)

    def sign(self, message):
        unsigned = dict(message)
        unsigned.pop("signature", None)
        key_id = unsigned.get("key_id", self.active_key_id)
        key = self.keys.get(key_id)
        if not key:
            raise BridgeError("SIGNATURE_INVALID", "unknown key_id")
        digest = hmac.new(key, canonical_json(unsigned), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify(self, message):
        if not isinstance(message, dict):
            raise BridgeError("SIGNATURE_INVALID", "message is not an object")
        supplied = message.get("signature")
        if not isinstance(supplied, str):
            raise BridgeError("SIGNATURE_INVALID", "signature is missing")
        expected = self.sign(message)
        if not hmac.compare_digest(expected, supplied):
            raise BridgeError("SIGNATURE_INVALID", "message signature does not match")
        return True


def make_envelope(keyring, message_type, payload, ttl_seconds, sender, correlation_id=None):
    from datetime import timedelta

    issued = utc_now()
    envelope = {
        "protocol_version": "1.0",
        "message_id": new_id("msg"),
        "correlation_id": correlation_id or new_id("corr"),
        "message_type": message_type,
        "issued_at": issued.isoformat(timespec="milliseconds"),
        "expires_at": (issued + timedelta(seconds=ttl_seconds)).isoformat(timespec="milliseconds"),
        "sender": sender,
        "key_id": keyring.active_key_id,
        "payload": payload,
    }
    envelope["signature"] = keyring.sign(envelope)
    return envelope


def validate_envelope(envelope, keyring, expected_types=None, max_clock_skew_seconds=5):
    required = {
        "protocol_version", "message_id", "correlation_id", "message_type",
        "issued_at", "expires_at", "sender", "key_id", "payload", "signature",
    }
    if set(envelope) != required:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "invalid envelope fields")
    if envelope["protocol_version"] != "1.0":
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "unsupported protocol version")
    if expected_types and envelope["message_type"] not in expected_types:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "unexpected message type")
    keyring.verify(envelope)
    try:
        issued = parse_time(envelope["issued_at"])
        expires = parse_time(envelope["expires_at"])
    except ValueError as exc:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", str(exc))
    now = utc_now()
    if expires <= now:
        raise BridgeError("MESSAGE_EXPIRED", "message has expired")
    if issued.timestamp() > now.timestamp() + max_clock_skew_seconds:
        raise BridgeError("CLOCK_SKEW", "message issued_at is in the future")
    if expires <= issued:
        raise BridgeError("MESSAGE_SCHEMA_INVALID", "expires_at must follow issued_at")
    return envelope

