"""Host Python integration only; passing does not certify restricted QMT permissions."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
PEER_SCRIPT = ROOT / "tools" / "qmt_probe_peer.py"
QMT_SCRIPT = ROOT / "tools" / "qmt_capability_probe.py"


@unittest.skipUnless(sys.platform == "win32", "Host-only Windows integration")
class HostProbeIntegrationTests(unittest.TestCase):
    def test_host_script_proves_duplex_and_both_mutex_directions_against_real_peer(self):
        spec = importlib.util.spec_from_file_location("qmt_probe_host_integration", QMT_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        name = "Local\\WBQMT_CAPABILITY_TEST_" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [sys.executable, "-u", str(PEER_SCRIPT), "--name", name, "--port", "0",
                 "--seconds", "15", "--output-dir", directory],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8")
            probe = None
            captured = io.StringIO()
            try:
                lines = queue.Queue()
                reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
                reader.start()
                line = lines.get(timeout=5)
                reader.join(1)
                self.assertTrue(line.startswith("[QMT-PROBE-PEER] READY "), line)
                ready = json.loads(line.split(" READY ", 1)[1])
                self.assertIsNotNone(ready["port"], ready["errors"])
                with contextlib.redirect_stdout(captured):
                    probe = module.CapabilityProbe(directory=directory, pair_name=name,
                                                   port=ready["port"], seconds=10)
                    probe.start()
                    # Historical QMT callbacks can arrive much faster than the
                    # peer's polling loop. Their count must not consume seconds.
                    for unused in range(1000):
                        probe.step()
                    self.assertNotEqual(probe.report.get("finish_reason"), "time budget exhausted",
                                        captured.getvalue())
                    deadline = time.monotonic() + 11
                    while not probe.finished and time.monotonic() < deadline:
                        time.sleep(0.03)
                        probe.step()
                self.assertTrue(probe.finished, captured.getvalue())
                checks = probe.report["checks"]
                for check in ("mmap.duplex", "network.local_connect",
                              "mutex.ctypes.cross_process", "mutex.ctypes.peer_excludes_qmt",
                              "mutex.ctypes.qmt_excludes_peer"):
                    self.assertEqual(checks[check]["status"], "PASS", (check, checks[check], captured.getvalue()))
                # Exercise pywin32 too when that optional backend is installed.
                if checks["mutex.win32event.local"]["status"] == "PASS":
                    for check in ("mutex.win32event.cross_process", "mutex.win32event.peer_excludes_qmt",
                                  "mutex.win32event.qmt_excludes_peer"):
                        self.assertEqual(checks[check]["status"], "PASS", (check, checks[check], captured.getvalue()))
                self.assertEqual(process.poll(), None, "DONE must leave helper available for another probe")
            finally:
                if probe is not None and not probe.finished:
                    with contextlib.redirect_stdout(captured):
                        probe.finish("host test cleanup")
                if process.poll() is None:
                    process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
