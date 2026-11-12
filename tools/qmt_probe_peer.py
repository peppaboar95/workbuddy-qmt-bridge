#!/usr/bin/env python
"""External, bounded capability-probe peer; no QMT or brokerage APIs.

Run this in ordinary Windows Python, then run qmt_capability_probe.py in QMT.
Only the dedicated capability-probe mapping/mutex and a loopback TCP listener
are used. The bridge transport and its configuration are never opened.
"""

import argparse
import json
import mmap
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))
from workbuddy_qmt.mmap_sync import WindowsNamedMutex


DEFAULT_NAME = "Local\\WBQMT_CAPABILITY_PROBE_V1"
SIZE = 4096
MARKER = b"WBQMT_PROBE_V1!!"
REQUEST_OFFSET = 128
RESPONSE_OFFSET = 1024
SLOT_CAPACITY = 768
DEFAULT_PORT = 17643
PREFIX = "[QMT-PROBE-PEER]"
_SAFE_NAME = re.compile(r"Local\\WBQMT_CAPABILITY_(?:PROBE|TEST)_[A-Za-z0-9_-]+\Z")
_NONCE = re.compile(r"[a-fA-F0-9]{32}\Z")
_OPERATIONS = {"PING", "MUTEX_HOLD", "MUTEX_RELEASE", "QMT_HOLDS", "DONE"}
_BACKENDS = {"ctypes", "win32event"}


def validate_name(name):
    """Prevent command-line overrides from selecting any bridge object."""
    if not isinstance(name, str) or len(name) > 200 or not _SAFE_NAME.fullmatch(name):
        raise ValueError("name must be a dedicated Local\\WBQMT_CAPABILITY_PROBE_* "
                         "or Local\\WBQMT_CAPABILITY_TEST_* ASCII name, at most 200 characters")
    return name


def _slot_bounds(mapping, offset, capacity):
    if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            or isinstance(capacity, bool) or not isinstance(capacity, int)
            or capacity < 5 or offset + capacity > len(mapping)):
        raise ValueError("slot must fit in mapping and have space for a length and payload")


def read_slot(mapping, offset, capacity=SLOT_CAPACITY):
    """Read an unchanged bounded slot twice; malformed/in-flight slots return None."""
    _slot_bounds(mapping, offset, capacity)
    first = mapping[offset:offset + capacity]
    length = struct.unpack(">I", first[:4])[0]
    if not 0 < length <= capacity - 4:
        return None
    if first != mapping[offset:offset + capacity]:
        return None
    try:
        value = json.loads(first[4:4 + length].decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def write_slot(mapping, offset, value, capacity=SLOT_CAPACITY):
    """Invalidate length, copy JSON, then publish length as the final write."""
    _slot_bounds(mapping, offset, capacity)
    if not isinstance(value, dict):
        raise ValueError("probe slot JSON must be an object")
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= capacity - 4:
        raise ValueError("probe slot JSON exceeds its bounded capacity")
    mapping[offset:offset + 4] = b"\0" * 4
    mapping[offset + 4:offset + capacity] = payload + b"\0" * (capacity - 4 - len(payload))
    mapping[offset:offset + 4] = struct.pack(">I", len(payload))


class ProbePeer:
    """Own probe resources and process all mutex operations on one loop thread."""

    def __init__(self, name=DEFAULT_NAME, port=DEFAULT_PORT):
        self.name = validate_name(name)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.peer_epoch = uuid.uuid4().hex
        self.port = None
        self.errors = []
        self.lock = None
        self.mapping = None
        self._listener = None
        self._owner = None
        self._owner_held = False
        self._thread_id = threading.get_ident()
        self._held = False
        self._nonce = None
        self._responses = {}
        self._closed = False
        try:
            # A separate lifetime lock prevents another helper process from
            # resetting this mapping while the probe mutex is intentionally free.
            self._owner = WindowsNamedMutex(self.name + "_owner", timeout_ms=0)
            try:
                self._owner.__enter__()
            except TimeoutError:
                raise RuntimeError("another capability probe peer already owns " + self.name)
            self._owner_held = True
            self.lock = WindowsNamedMutex(self.name + "_mutex", timeout_ms=0)
            # Initialization cannot overwrite a probe while another owner holds
            # the probe mutex. This namespace never names the bridge mapping.
            with self.lock:
                self.mapping = mmap.mmap(-1, SIZE, tagname=self.name, access=mmap.ACCESS_WRITE)
                self.mapping[:] = b"\0" * SIZE
                self.mapping[:16] = MARKER
                self.mapping[16:48] = self.peer_epoch.encode("ascii")
        except Exception:
            self.close()
            raise
        try:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.bind(("127.0.0.1", port))
            self._listener.listen(4)
            self._listener.setblocking(False)
            self.port = self._listener.getsockname()[1]
        except OSError as error:
            self.errors.append("TCP listener unavailable: " + str(error))
            if self._listener is not None:
                self._listener.close()
                self._listener = None

    def _check_thread(self):
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("probe step and close must run on their creating thread")

    def _release(self):
        if self._held:
            self.lock.__exit__(None, None, None)
            self._held = False

    def _respond(self, nonce, op, backend=None, error=None):
        response = {"nonce": nonce, "op": op, "peer_epoch": self.peer_epoch}
        if backend is not None:
            response["backend"] = backend
        if error is not None:
            response["error"] = error
        return response

    def _process_request(self, request):
        nonce, op, backend = request.get("nonce"), request.get("op"), request.get("backend")
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            return False
        if not isinstance(op, str) or op not in _OPERATIONS:
            return False
        if backend is not None and (not isinstance(backend, str) or backend not in _BACKENDS):
            return False
        if nonce != self._nonce:
            self._release()
            self._nonce = nonce
            self._responses = {}
        key = (nonce, op, backend)
        if key in self._responses:
            # Leave the published response in place for normal repeated polls.
            # Re-publish only when the caller intentionally reuses an old key.
            cached = self._responses[key]
            if read_slot(self.mapping, RESPONSE_OFFSET) != cached:
                write_slot(self.mapping, RESPONSE_OFFSET, cached)
            return False
        try:
            if op == "PING":
                response = self._respond(nonce, "PONG", backend)
            elif op == "MUTEX_HOLD":
                if not self._held:
                    self.lock.__enter__()
                    self._held = True
                response = self._respond(nonce, "MUTEX_HELD", backend)
            elif op == "MUTEX_RELEASE":
                self._release()
                response = self._respond(nonce, "MUTEX_RELEASED", backend)
            elif op == "QMT_HOLDS":
                if self._held:
                    response = self._respond(nonce, "ERROR", backend, "peer still owns the mutex")
                else:
                    try:
                        with self.lock:
                            pass
                    except TimeoutError:
                        response = self._respond(nonce, "PEER_EXCLUDED", backend)
                    else:
                        response = self._respond(nonce, "ERROR", backend,
                                                 "peer acquired mutex; exclusion was not observed")
            else:  # DONE ends a probe run, but keeps the external helper available.
                self._release()
                response = self._respond(nonce, "DONE", backend)
        except Exception as error:
            response = self._respond(nonce, "ERROR", backend, type(error).__name__)
        self._responses[key] = response
        write_slot(self.mapping, RESPONSE_OFFSET, response)
        print(PREFIX + " " + json.dumps(response, sort_keys=True), flush=True)
        return True

    def _serve_socket(self):
        if self._listener is None:
            return
        try:
            connection, unused = self._listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as error:
            self.errors.append("TCP accept unavailable: " + type(error).__name__)
            self._listener.close()
            self._listener = None
            return
        with connection:
            deadline = time.monotonic() + 0.2
            request = b""
            try:
                while len(request) < 44:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    connection.settimeout(remaining)
                    chunk = connection.recv(45 - len(request))
                    if not chunk:
                        return
                    request += chunk
                nonce = request[12:]
                if request[:12] == b"WBQMT-PROBE:" and re.fullmatch(b"[a-fA-F0-9]{32}", nonce):
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        connection.settimeout(remaining)
                        connection.sendall(b"ACK:" + nonce)
            except (OSError, ValueError):
                pass  # A stalled/broken client never stops the mmap probe.

    def step(self):
        self._check_thread()
        if self._closed:
            raise RuntimeError("probe peer is closed")
        request = read_slot(self.mapping, REQUEST_OFFSET)
        processed = self._process_request(request) if request is not None else False
        self._serve_socket()
        return processed

    def close(self):
        self._check_thread()
        if self._closed:
            return
        self._release()
        try:
            if self._listener is not None:
                self._listener.close()
        finally:
            try:
                if self.mapping is not None:
                    self.mapping.close()
            finally:
                try:
                    if self.lock is not None:
                        self.lock.close()
                finally:
                    try:
                        if self._owner_held:
                            self._owner.__exit__(None, None, None)
                            self._owner_held = False
                    finally:
                        if self._owner is not None:
                            self._owner.close()
                        self._closed = True


def _seconds(value):
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("seconds must be an integer from 1 to 600")
    if not 1 <= seconds <= 600:
        raise argparse.ArgumentTypeError("seconds must be an integer from 1 to 600")
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_NAME, type=validate_name)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="loopback-only TCP port (0 chooses a free port)")
    parser.add_argument("--seconds", type=_seconds, default=90,
                        help="bounded helper lifetime, 1 to 600 seconds (default: 90)")
    parser.add_argument("--output-dir", default=os.path.join(_PROJECT_ROOT, "probe-output"),
                        help="create only this directory for the separate QMT probe's reports")
    args = parser.parse_args(argv)
    instance = None
    try:
        output_dir = os.path.abspath(args.output_dir)
        if not os.path.isdir(output_dir):
            os.mkdir(output_dir)
        instance = ProbePeer(name=args.name, port=args.port)
        ready = {"name": instance.name, "port": instance.port,
                 "peer_epoch": instance.peer_epoch, "epoch": instance.peer_epoch,
                 "output_dir": output_dir, "seconds": args.seconds, "errors": instance.errors}
        print(PREFIX + " READY " + json.dumps(ready, sort_keys=True), flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            instance.step()
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(PREFIX + " ERROR " + type(error).__name__ + ": " + str(error), flush=True)
        return 1
    finally:
        if instance is not None:
            instance.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
