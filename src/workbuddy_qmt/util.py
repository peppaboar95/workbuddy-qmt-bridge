import base64
import datetime as dt
import json
import os
import re
import uuid

UTC = dt.timezone.utc
SYMBOL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def utc_now():
    return dt.datetime.now(UTC)


def iso_now():
    return utc_now().isoformat(timespec="milliseconds")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must contain a timezone")
    return parsed.astimezone(UTC)


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def new_client_order_key():
    compact = base64.b32encode(uuid.uuid4().bytes).decode("ascii").rstrip("=")
    return "WB" + compact[:20]


def canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_symbol(value):
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()
    if re.fullmatch(r"\d{6}", symbol):
        if symbol.startswith(("5", "6", "9")):
            symbol += ".SH"
        elif symbol.startswith(("0", "1", "2", "3")):
            symbol += ".SZ"
        elif symbol.startswith(("4", "8")):
            symbol += ".BJ"
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("unsupported stock symbol: %s" % value)
    return symbol


def atomic_write_bytes(path, data):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temp = path + ".tmp.%s" % uuid.uuid4().hex
    with open(temp, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def atomic_write_json(path, value):
    atomic_write_bytes(path, canonical_json(value))


def require_exact_keys(value, allowed, required=()):
    if not isinstance(value, dict):
        raise ValueError("value must be an object")
    unknown = sorted(set(value) - set(allowed))
    missing = sorted(set(required) - set(value))
    if unknown:
        raise ValueError("unknown fields: %s" % ", ".join(unknown))
    if missing:
        raise ValueError("missing fields: %s" % ", ".join(missing))
