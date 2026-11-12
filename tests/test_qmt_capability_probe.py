import ast
import builtins
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock


PROBE = Path(__file__).resolve().parents[1] / 'tools' / 'qmt_capability_probe.py'


class CapabilityProbeTests(unittest.TestCase):
    def load(self):
        self.assertTrue(PROBE.exists(), 'standalone QMT capability probe is missing')
        spec = importlib.util.spec_from_file_location('qmt_probe_test', str(PROBE))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_python36_syntax_and_no_trading_calls_or_import_time_execution(self):
        module = self.load()
        tree = ast.parse(PROBE.read_text(encoding='utf-8'), feature_version=(3, 6))
        forbidden = {'passorder', 'cancel', 'order_stock', 'get_trade_detail_data', 'set_account'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                self.assertNotIn(name, forbidden)
        self.assertIsNone(module.RUNTIME)
        for node in tree.body:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                self.assertFalse(any(isinstance(child, ast.While) for child in ast.walk(node)),
                                 'QMT may set __name__=__main__; never run a standalone loop at load time')

    def test_existing_database_is_not_used_when_os_is_denied(self):
        module = self.load()
        original = module._module
        def no_os(name):
            if name == 'os':
                raise ImportError('os denied')
            return original(name)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            unrelated = Path(directory) / 'probe.sqlite'
            unrelated.write_bytes(b'USER DATA')
            with mock.patch.object(module, '_module', side_effect=no_os):
                probe = module.CapabilityProbe(directory=directory)
                probe.start()
                probe.finish('test')
            self.assertEqual(probe.report['checks']['sqlite.file']['status'], 'PASS')
            self.assertEqual(unrelated.read_bytes(), b'USER DATA')

    def test_exclusive_create_collision_never_deletes_unowned_file(self):
        module = self.load()
        original = module._module
        class OperatingProxy:
            collision = None
            def __getattr__(self, name):
                return getattr(os, name)
            def makedirs(self, path):
                os.makedirs(path)
                self.collision = Path(path) / 'bytes.bin'
                self.collision.write_bytes(b'keep existing')
        operating = OperatingProxy()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(module, '_module', side_effect=lambda name: operating if name == 'os' else original(name)):
                probe = module.CapabilityProbe(directory=directory)
                probe.start()
                probe.finish('test')
            self.assertTrue(operating.collision.exists())
            self.assertEqual(operating.collision.read_bytes(), b'keep existing')

    def test_denied_clock_json_sys_still_produces_complete_stdout_report(self):
        module = self.load()
        original = module._module
        def blocked(name):
            if name in {'time', 'json', 'sys'}:
                raise ImportError(name + ' denied')
            return original(name)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output):
            with mock.patch.object(module, '_module', side_effect=blocked):
                probe = module.CapabilityProbe(directory=directory)
                probe.start()
        self.assertTrue(probe.finished)
        self.assertIn('REPORT_END', output.getvalue())

    def test_concurrent_stop_defers_cleanup_to_mutex_owner_callback(self):
        module = self.load()
        entered = threading.Event()
        class OwnerMutex:
            owner = None
            wrong_thread = False
            def acquire(self):
                self.owner = threading.get_ident()
                entered.set()
                return True
            def release(self):
                if self.owner is not None and self.owner != threading.get_ident():
                    self.wrong_thread = True
                    raise RuntimeError('wrong-thread release')
                self.owner = None
            def close(self):
                self.release()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            probe = module.CapabilityProbe(directory=directory,
                        pair_name='Local\\WBQMT_CAPABILITY_TEST_overlap')
            probe.start()
            probe.mapping[:16] = module._MARKER
            probe.mapping[16:48] = b'e' * 32
            probe.epoch = 'e' * 32
            probe.backend = 'ctypes'
            probe.phase = 'RELEASE'
            lock = OwnerMutex()
            probe.mutex = lock
            responses = [{'op': 'MUTEX_RELEASED'}]
            probe._read_response = lambda: responses.pop(0) if responses else None
            thread = threading.Thread(target=probe.step)
            thread.start()
            self.assertTrue(entered.wait(1))
            probe.finish('concurrent stop')
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(lock.wrong_thread)
            self.assertIsNone(lock.owner)

    def test_actual_file_sqlite_and_local_mutex_checks_preserve_other_files(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            sentinel = Path(directory) / 'user-file.txt'
            sentinel.write_text('keep', encoding='utf-8')
            probe = module.CapabilityProbe(directory=directory, pair_name='Local\\WBQMT_CAPABILITY_TEST_basic')
            probe.start()
            report = probe.finish('test complete')
            checks = report['checks']
            for name in ('file.write_read', 'file.json', 'os.listdir', 'os.stat',
                         'os.fsync', 'os.replace', 'sqlite.memory', 'sqlite.file', 'mmap.local'):
                self.assertEqual(checks[name]['status'], 'PASS', (name, checks[name]))
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
            self.assertFalse(Path(probe.scratch_directory).exists())
            self.assertTrue(Path(probe.report_path).exists())

    def test_denied_modules_and_open_do_not_abort_remaining_diagnostics(self):
        module = self.load()
        real_import = builtins.__import__
        denied = {'os', 'socket', 'sqlite3', 'mmap', 'ctypes', 'win32event', 'win32api',
                  'tempfile', 'msvcrt', 'multiprocessing', 'uuid', '_winapi'}
        def restricted_import(name, *args, **kwargs):
            if name.split('.')[0] in denied:
                raise ImportError('Forbidden: ' + name)
            return real_import(name, *args, **kwargs)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output):
            with mock.patch('builtins.__import__', side_effect=restricted_import), \
                    mock.patch('builtins.open', side_effect=PermissionError('file operations forbidden')):
                probe = module.CapabilityProbe(directory=directory)
                probe.start()
                report = probe.finish('test complete')
        self.assertEqual(report['checks']['import.os']['status'], 'FAIL')
        self.assertEqual(report['checks']['file.write_read']['status'], 'FAIL')
        self.assertEqual(report['checks']['import.math']['status'], 'PASS')
        self.assertIn('REPORT_BEGIN', output.getvalue())
        self.assertIn('REPORT_END', output.getvalue())

    def test_file_io_is_tested_even_when_os_import_is_denied(self):
        module = self.load()
        real_import = builtins.__import__
        def no_os(name, *args, **kwargs):
            if name == 'os':
                raise ImportError('os denied')
            return real_import(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            with mock.patch('builtins.__import__', side_effect=no_os):
                probe = module.CapabilityProbe(directory=directory)
                probe.start()
                report = probe.finish('test complete')
        self.assertEqual(report['checks']['import.os']['status'], 'FAIL')
        self.assertEqual(report['checks']['file.write_read']['status'], 'PASS')

    def test_missing_peer_is_inconclusive_and_time_budget_closes_probe(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            probe = module.CapabilityProbe(directory=directory,
                    pair_name='Local\\WBQMT_CAPABILITY_TEST_absent', seconds=1, port=0)
            probe.start()
            probe.deadline = 0
            probe.step()
        self.assertTrue(probe.finished)
        self.assertEqual(probe.report['checks']['mmap.duplex']['status'], 'SKIP')
        self.assertNotIn('WAIT', [row['status'] for row in probe.report['checks'].values()])

    def test_fast_qmt_callbacks_do_not_exhaust_a_wall_clock_time_budget(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            probe = module.CapabilityProbe(directory=directory,
                    pair_name='Local\\WBQMT_CAPABILITY_TEST_fast_callbacks', seconds=60, port=0)
            probe.start()
            try:
                # Simulate historical handlebar/timer callbacks at the same
                # monotonic instant. Callback count is not elapsed time.
                instant = probe.now()
                with mock.patch.object(probe, 'now', return_value=instant):
                    for unused in range(1000):
                        probe.step()
                self.assertFalse(probe.finished)
                self.assertGreater(probe.steps, 120)
                probe.deadline = 0
                probe.step()
                self.assertTrue(probe.finished)
            finally:
                probe.finish('test cleanup')

    def test_probe_refuses_real_bridge_namespace_and_nonlocal_network(self):
        module = self.load()
        for name in ('Local\\WBQMT_real_bridge', 'Global\\WBQMT_CAPABILITY_PROBE_V1'):
            with self.assertRaises(ValueError):
                module.CapabilityProbe(pair_name=name)
