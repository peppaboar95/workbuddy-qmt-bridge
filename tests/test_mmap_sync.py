import ctypes
import multiprocessing
import os
import sys
import threading
import time
import unittest
import uuid

try:
    from workbuddy_qmt.mmap_sync import AbandonedMutexError, WindowsNamedMutex
except ImportError:
    AbandonedMutexError = None
    WindowsNamedMutex = None


def _increment_in_process(name, counter, start):
    mutex = WindowsNamedMutex(name, timeout_ms=5000)
    try:
        if not start.wait(5):
            raise AssertionError("process did not receive its start signal")
        for unused in range(40):
            with mutex:
                previous = counter.value
                time.sleep(0.0005)
                counter.value = previous + 1
    finally:
        mutex.close()


def _expect_process_timeout(name, result):
    mutex = WindowsNamedMutex(name, timeout_ms=40)
    try:
        started = time.monotonic()
        try:
            with mutex:
                result.put(("unexpected-acquisition", 0))
        except TimeoutError:
            result.put(("timed-out", time.monotonic() - started))
    finally:
        mutex.close()


def _abandon_in_process(name):
    mutex = WindowsNamedMutex(name)
    mutex.__enter__()
    os._exit(0)


@unittest.skipUnless(sys.platform == "win32", "Win32 named mutex")
class WindowsNamedMutexTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(WindowsNamedMutex, "external Windows synchronization is not implemented")
        self.name = "Local\\WBQMT_sync_test_" + uuid.uuid4().hex
        self.mutex = WindowsNamedMutex(self.name)
        self.addCleanup(self.mutex.close)

    def join_process(self, process):
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("mutex test process did not finish")
        self.assertEqual(process.exitcode, 0)

    def test_independently_spawned_processes_exclude_each_other(self):
        context = multiprocessing.get_context("spawn")
        counter = context.Value("i", 0, lock=False)
        start = context.Event()
        processes = [context.Process(target=_increment_in_process,
                                     args=(self.name, counter, start)) for unused in range(2)]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            self.join_process(process)
        self.assertEqual(counter.value, 80)

    def test_process_wait_timeout_is_bounded(self):
        context = multiprocessing.get_context("spawn")
        result = context.Queue()
        process = context.Process(target=_expect_process_timeout, args=(self.name, result))
        try:
            with self.mutex:
                process.start()
                self.join_process(process)
            status, elapsed = result.get(timeout=1)
            self.assertEqual(status, "timed-out")
            self.assertGreaterEqual(elapsed, 0.025)
            self.assertLess(elapsed, 1.5)
        finally:
            result.close()
            result.join_thread()

    def test_different_threads_using_one_object_cannot_enter_together(self):
        acquired = []

        def attempt():
            try:
                with self.mutex:
                    acquired.append(True)
            except TimeoutError:
                acquired.append(False)

        with self.mutex:
            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(acquired, [False])
        with self.mutex:
            pass

    def test_same_thread_reentry_releases_once_per_context(self):
        competitor = WindowsNamedMutex(self.name, timeout_ms=0)
        self.addCleanup(competitor.close)
        with self.mutex:
            with self.mutex:
                pass
            outcome = []

            def attempt():
                try:
                    with competitor:
                        outcome.append(True)
                except TimeoutError:
                    outcome.append(False)

            thread = threading.Thread(target=attempt)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [False])
        with competitor:
            pass

    def test_close_is_idempotent_and_future_entry_fails(self):
        self.mutex.close()
        self.mutex.close()
        with self.assertRaises(RuntimeError):
            with self.mutex:
                pass

    def test_closing_an_acquired_mutex_is_rejected_until_release(self):
        with self.mutex:
            with self.assertRaises(RuntimeError):
                self.mutex.close()
        self.mutex.close()

    def test_exit_without_owning_context_does_not_release_some_other_handle(self):
        other = WindowsNamedMutex(self.name, timeout_ms=0)
        self.addCleanup(other.close)
        with self.mutex:
            with self.assertRaises(RuntimeError):
                other.__exit__(None, None, None)
        with other:
            pass

    def test_body_exception_releases_mutex_and_is_not_suppressed(self):
        with self.assertRaisesRegex(ValueError, "body failed"):
            with self.mutex:
                raise ValueError("body failed")
        other = WindowsNamedMutex(self.name, timeout_ms=0)
        self.addCleanup(other.close)
        with other:
            pass

    def test_abandoned_owner_fails_closed_and_releases_recovered_mutex(self):
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=_abandon_in_process, args=(self.name,))
        process.start()
        self.join_process(process)
        with self.assertRaises(AbandonedMutexError):
            with self.mutex:
                self.fail("an abandoned mutex must not allow the protected operation")
        # Retrying this object must not silently clear its corruption signal.
        with self.assertRaises(AbandonedMutexError):
            with self.mutex:
                self.fail("abandoned mutex object must remain faulted")
        other = WindowsNamedMutex(self.name, timeout_ms=0)
        self.addCleanup(other.close)
        with other:
            pass

    def test_invalid_native_handle_reports_wait_failure(self):
        mutex = WindowsNamedMutex(self.name + "_invalid")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self.assertTrue(kernel32.CloseHandle(mutex._handle))
        with self.assertRaises(OSError) as raised:
            with mutex:
                pass
        self.assertNotEqual(raised.exception.winerror, 0)
        with self.assertRaises(OSError):
            mutex.close()

    def test_invalid_names_are_rejected_before_opening_a_handle(self):
        for name in (None, "", "Global\\unsafe", "plain", "Local\\", "Local\\a/b",
                     "Local\\a\\b", "Local\\a\0b", "Local\\中文", "Local\\" + "a" * 250):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    WindowsNamedMutex(name)

    def test_infinite_and_noninteger_timeouts_are_rejected(self):
        for timeout_ms in (-1, True, 0.5, None, 0xffffffff, 0x100000000):
            with self.subTest(timeout_ms=timeout_ms):
                with self.assertRaises(ValueError):
                    WindowsNamedMutex(self.name, timeout_ms=timeout_ms)


if __name__ == "__main__":
    unittest.main()
