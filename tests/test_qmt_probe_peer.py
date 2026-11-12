"""Exercise only fresh capability-test objects, never the bridge transport."""

import importlib.util
import json
import mmap
import pathlib
import queue
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "qmt_probe_peer.py"
peer = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location("qmt_probe_peer", SCRIPT)
    peer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(peer)


class ProbeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(peer, "external capability probe peer is missing")
        self.mapping = mmap.mmap(-1, 4096)
        self.addCleanup(self.mapping.close)

    def test_slot_round_trip_uses_bounded_utf8_json(self):
        payload = {"nonce": "a" * 32, "op": "PING", "text": "测试"}
        peer.write_slot(self.mapping, 128, payload)
        self.assertEqual(peer.read_slot(self.mapping, 128), payload)
        length = struct.unpack(">I", self.mapping[128:132])[0]
        self.assertLessEqual(length, 764)
        self.assertEqual(json.loads(self.mapping[132:132 + length].decode("utf-8")), payload)

    def test_invalid_lengths_and_json_are_ignored(self):
        for header, payload in ((0, b""), (765, b"{}"), (0xffffffff, b"{}"),
                                (2, b"\xff\xff"), (2, b"[]"), (2, b"{!")):
            with self.subTest(header=header, payload=payload):
                self.mapping[128:132] = struct.pack(">I", header)
                self.mapping[132:134] = payload[:2].ljust(2, b"\0")
                self.assertIsNone(peer.read_slot(self.mapping, 128))

    def test_oversize_writer_preserves_existing_slot(self):
        original = {"nonce": "a" * 32, "op": "PING"}
        peer.write_slot(self.mapping, 128, original)
        with self.assertRaises(ValueError):
            peer.write_slot(self.mapping, 128, {"too_large": "x" * 768})
        self.assertEqual(peer.read_slot(self.mapping, 128), original)

    def test_names_cannot_select_bridge_or_global_objects(self):
        for name in ("Local\\bridge", "Global\\WBQMT_CAPABILITY_PROBE_V1",
                     "Local\\WBQMT_CAPABILITY_TEST_bad/path", "Local\\" + "x" * 200,
                     None, "Local\\WBQMT_CAPABILITY_TEST_"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    peer.validate_name(name)
        name = "Local\\WBQMT_CAPABILITY_TEST_" + uuid.uuid4().hex
        self.assertEqual(peer.validate_name(name), name)


@unittest.skipUnless(sys.platform == "win32", "Windows named mapping and mutex")
class ProbePeerWindowsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(peer, "external capability probe peer is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)

    def start_peer(self, seconds=20, port=0):
        name = "Local\\WBQMT_CAPABILITY_TEST_" + uuid.uuid4().hex
        output_dir = pathlib.Path(self.temporary.name) / uuid.uuid4().hex
        process = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT), "--name", name, "--port", str(port),
             "--seconds", str(seconds), "--output-dir", str(output_dir)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8")
        self.processes.append(process)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        line = lines.get(timeout=5)
        self.assertTrue(line.startswith("[QMT-PROBE-PEER] READY "), line)
        ready = json.loads(line.split(" READY ", 1)[1])
        self.assertEqual(ready["name"], name)
        self.assertEqual(pathlib.Path(ready["output_dir"]), output_dir)
        self.assertTrue(output_dir.is_dir())
        reader.join(1)
        mapping = mmap.mmap(-1, 4096, tagname=name)
        self.addCleanup(mapping.close)
        self.assertEqual(mapping[:16], b"WBQMT_PROBE_V1!!")
        self.assertEqual(mapping[16:48].decode("ascii"), ready["peer_epoch"])
        return process, mapping, ready

    def exchange(self, mapping, ready, nonce, op, backend=None):
        request = {"nonce": nonce, "op": op}
        if backend is not None:
            request["backend"] = backend
        peer.write_slot(mapping, 128, request)
        deadline = time.monotonic() + 2
        response_ops = {"PING": "PONG", "MUTEX_HOLD": "MUTEX_HELD",
                        "MUTEX_RELEASE": "MUTEX_RELEASED", "QMT_HOLDS": "PEER_EXCLUDED",
                        "DONE": "DONE"}
        while time.monotonic() < deadline:
            response = peer.read_slot(mapping, 1024)
            if (response and response.get("nonce") == nonce
                    and response.get("backend") == backend
                    and response.get("op") in (response_ops[op], "ERROR")):
                self.assertEqual(response["peer_epoch"], ready["peer_epoch"])
                return response
            time.sleep(0.01)
        self.fail("peer did not answer " + op)

    def test_spawned_peer_mmap_socket_and_repeated_hold_release(self):
        unused, mapping, ready = self.start_peer()
        nonce = uuid.uuid4().hex
        self.assertEqual(self.exchange(mapping, ready, nonce, "PING")["op"], "PONG")
        with socket.create_connection(("127.0.0.1", ready["port"]), timeout=1) as connection:
            connection.sendall(b"WBQMT-PROBE:" + nonce.encode("ascii"))
            self.assertEqual(connection.recv(128), b"ACK:" + nonce.encode("ascii"))
        mutex = peer.WindowsNamedMutex(ready["name"] + "_mutex", timeout_ms=0)
        self.addCleanup(mutex.close)
        for backend in ("ctypes", "win32event"):
            self.assertEqual(self.exchange(mapping, ready, nonce, "MUTEX_HOLD", backend)["op"],
                             "MUTEX_HELD")
            time.sleep(0.1)  # Re-reading the same request must not acquire recursively.
            with self.assertRaises(TimeoutError):
                with mutex:
                    pass
            self.assertEqual(self.exchange(mapping, ready, nonce, "MUTEX_RELEASE", backend)["op"],
                             "MUTEX_RELEASED")
            with mutex:
                pass
        self.assertEqual(self.exchange(mapping, ready, nonce, "DONE")["op"], "DONE")
        self.assertEqual(self.exchange(mapping, ready, uuid.uuid4().hex, "PING")["op"], "PONG")

    def test_inverse_mutex_contention_is_observed_from_other_process(self):
        unused, mapping, ready = self.start_peer()
        mutex = peer.WindowsNamedMutex(ready["name"] + "_mutex", timeout_ms=0)
        self.addCleanup(mutex.close)
        nonce = uuid.uuid4().hex
        with mutex:
            response = self.exchange(mapping, ready, nonce, "QMT_HOLDS", "ctypes")
        self.assertEqual(response["op"], "PEER_EXCLUDED")
        # An unowned mutex must produce ERROR, never a false contention result.
        response = self.exchange(mapping, ready, nonce, "QMT_HOLDS", "win32event")
        self.assertEqual(response["op"], "ERROR")

    def test_new_nonce_releases_old_hold_and_invalid_length_does_not_crash(self):
        unused, mapping, ready = self.start_peer()
        self.exchange(mapping, ready, uuid.uuid4().hex, "MUTEX_HOLD", "ctypes")
        mapping[128:132] = struct.pack(">I", 0xffffffff)
        time.sleep(0.05)
        response = self.exchange(mapping, ready, uuid.uuid4().hex, "PING")
        self.assertEqual(response["op"], "PONG")
        mutex = peer.WindowsNamedMutex(ready["name"] + "_mutex", timeout_ms=0)
        try:
            with mutex:
                pass
        finally:
            mutex.close()

    def test_busy_port_preserves_listener_and_mmap_service(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            unused, mapping, ready = self.start_peer(port=listener.getsockname()[1])
            self.assertIsNone(ready["port"])
            self.assertTrue(ready["errors"])
            self.assertEqual(self.exchange(mapping, ready, uuid.uuid4().hex, "PING")["op"], "PONG")
            listener.getsockname()  # The existing socket is still owned by this test.

    def test_cli_duration_releases_held_mutex_on_clean_exit(self):
        started = time.monotonic()
        process, mapping, ready = self.start_peer(seconds=1)
        self.exchange(mapping, ready, uuid.uuid4().hex, "MUTEX_HOLD", "ctypes")
        mutex = peer.WindowsNamedMutex(ready["name"] + "_mutex", timeout_ms=0)
        try:
            output, unused = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 0, output)
            self.assertLess(time.monotonic() - started, 5)
            with mutex:
                pass
        finally:
            mutex.close()

    def test_duplicate_process_cannot_reset_active_peer_mapping(self):
        unused, mapping, ready = self.start_peer()
        original_epoch = bytes(mapping[16:48])
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--name", ready["name"], "--port", "0",
             "--seconds", "1", "--output-dir", ready["output_dir"]],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("[QMT-PROBE-PEER] ERROR", result.stdout)
        self.assertNotIn(" READY ", result.stdout)
        self.assertEqual(bytes(mapping[16:48]), original_epoch)
        self.assertEqual(self.exchange(mapping, ready, uuid.uuid4().hex, "PING")["op"], "PONG")

    def test_cli_rejects_unbounded_lifetime_before_creating_directory(self):
        output_dir = pathlib.Path(self.temporary.name) / "must-not-exist"
        for seconds in ("0", "601", "nan"):
            with self.subTest(seconds=seconds):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "--seconds", seconds,
                     "--output-dir", str(output_dir)],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
