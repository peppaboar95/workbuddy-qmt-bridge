import argparse
import ipaddress
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config
from .core import BridgeCore
from .db import Database
from .errors import BridgeError
from .event_ingest import EventIngester
from .file_queue import FileQueue
from .security import KeyRing
from .util import iso_now, new_id

WORKER_QUEUE_INTERVAL_SECONDS = 0.25


def _make_mmap_gateway(core, config_path):
    if any(account.enabled and account.adapter_transport == 'mmap'
           for account in core.config.accounts.values()):
        from .mmap_gateway import MmapGateway
        resolved_path = config_path or os.environ.get('WORKBUDDY_QMT_CONFIG', 'config/bridge.json')
        return MmapGateway(core, resolved_path)
    return None


def build_runtime(config_path=None, start_mmap=True):
    config = load_config(config_path)
    if not ipaddress.ip_address(config.host).is_loopback:
        raise BridgeError("CONFIG_ERROR", "worker must bind to a loopback address")
    database = Database(os.path.join(config.data_dir, "state", "bridge.db"))
    database.initialize(config.default_mode)
    keyring = KeyRing.load(config.key_file)
    queue = FileQueue(config.data_dir, keyring, config.max_message_bytes)
    for account in config.accounts.values():
        queue.ensure_partition(account.adapter_instance)
    ingester = EventIngester(config, database, queue)
    ingester.reconcile_terminal_evidence()
    core = BridgeCore(config, database, keyring, queue, ingester)
    core.mmap_gateway = _make_mmap_gateway(core, config_path) if start_mmap else None
    return config, database, core


class RuntimeLoop(threading.Thread):
    def __init__(self, core, interval=WORKER_QUEUE_INTERVAL_SECONDS):
        super().__init__(name="qmt-queue-loop", daemon=True)
        self.core = core
        self.interval = interval
        self.stop_event = threading.Event()
        self._mmap_errors = {}

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.core.ingester.scan_once()
                self.core.dispatch_pending_commands()
                gateway = getattr(self.core, 'mmap_gateway', None)
                if gateway is not None:
                    errors = gateway.poll_once().get('errors', {})
                    for alias, error in errors.items():
                        code = error.get('code', 'MMAP_ERROR')
                        if self._mmap_errors.get(alias) != code:
                            print('mmap gateway error [%s]: %s' % (alias, code), file=sys.stderr, flush=True)
                    self._mmap_errors = {alias: error.get('code', 'MMAP_ERROR') for alias, error in errors.items()}
            except Exception as exc:
                print("queue loop error: %s" % exc, file=sys.stderr, flush=True)
            self.stop_event.wait(self.interval)


def make_handler(core, database, token, max_bytes):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WorkBuddyQMT/0.1"

        def log_message(self, format_string, *args):
            return

        def _send(self, status, value):
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):
            if self.path != "/rpc":
                self._send(404, {"ok": False})
                return
            supplied = self.headers.get("Authorization", "")
            if supplied != "Bearer " + token:
                self._send(401, {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "invalid local token", "details": {}}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_bytes:
                    raise BridgeError("INVALID_REQUEST", "invalid request size")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict) or set(body) - {"method", "params", "request_id"} or "method" not in body:
                    raise BridgeError("INVALID_REQUEST", "invalid RPC envelope")
                request_id = body.get("request_id") or new_id("req")
                method = body["method"]
                try:
                    data = core.call(method, body.get("params", {}))
                    result = {"ok": True, "request_id": request_id, "as_of": iso_now(), "data": data, "warnings": [], "error": None}
                    error_code = None
                except TypeError as exc:
                    raise BridgeError("INVALID_REQUEST", "invalid method parameters: %s" % exc)
                with database.transaction() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO mcp_requests(request_id,method,requested_at,ok,error_code) VALUES(?,?,?,?,?)",
                        (request_id, method, result["as_of"], 1, error_code),
                    )
                self._send(200, result)
            except BridgeError as exc:
                request_id = locals().get("request_id", new_id("req"))
                result = {
                    "ok": False, "request_id": request_id, "as_of": iso_now(), "data": None, "warnings": [],
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
                try:
                    with database.transaction() as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO mcp_requests(request_id,method,requested_at,ok,error_code) VALUES(?,?,?,?,?)",
                            (request_id, locals().get("method", "<invalid>"), result["as_of"], 0, exc.code),
                        )
                except Exception:
                    pass
                self._send(200, result)
            except Exception:
                self._send(500, {
                    "ok": False, "request_id": new_id("req"), "as_of": iso_now(), "data": None, "warnings": [],
                    "error": {"code": "INTERNAL_ERROR", "message": "worker failed to process request", "details": {}},
                })

    return Handler


def _install_console_signal_handlers():
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGBREAK"):
        console_signal = getattr(signal, name, None)
        if console_signal is None:
            continue
        previous[console_signal] = signal.getsignal(console_signal)
        signal.signal(console_signal, interrupt)
    return previous


def _restore_console_signal_handlers(previous):
    for console_signal, handler in previous.items():
        signal.signal(console_signal, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="WorkBuddy-QMT bridge worker")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    # Acquire the listening port before fencing any existing mmap session.
    # A second Worker which cannot bind must not invalidate the running one.
    config, database, core = build_runtime(args.config, start_mmap=False)
    try:
        with open(config.worker_token_file, "r", encoding="ascii") as stream:
            token = stream.read().strip()
    except OSError as exc:
        raise SystemExit("cannot read worker token: %s" % exc)
    if len(token) < 32:
        raise SystemExit("worker token is too short")
    server = ThreadingHTTPServer((config.host, config.port), make_handler(core, database, token, config.max_message_bytes))
    server.daemon_threads = True
    loop = None
    previous_handlers = {}
    try:
        core.mmap_gateway = _make_mmap_gateway(core, args.config)
        loop = RuntimeLoop(core)
        loop.start()
        previous_handlers = _install_console_signal_handlers()
        print("worker listening on http://%s:%d" % (config.host, config.port), file=sys.stderr, flush=True)
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if loop is not None:
            loop.stop_event.set()
        server.server_close()
        if loop is not None:
            # Do not close a mapping while a persistence/ACK operation owns it.
            loop.join()
        if core.mmap_gateway is not None:
            core.mmap_gateway.close()
        _restore_console_signal_handlers(previous_handlers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
