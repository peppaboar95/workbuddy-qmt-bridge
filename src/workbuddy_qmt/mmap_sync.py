"""External Worker synchronization for the Windows mmap transport.

Never import or bundle this module into QMT. Restricted QMT strategies must
supply their own verified MMAP_LOCK_FACTORY using the same Win32 mutex name.
"""

import ctypes
from ctypes import wintypes
import re
import sys
import threading


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xffffffff
_SAFE_NAME = re.compile(r"Local\\[A-Za-z0-9_-]{1,240}")


class AbandonedMutexError(RuntimeError):
    """An owner died; protected state must be reconciled before proceeding."""


def _windows_api():
    if sys.platform != "win32":
        raise OSError("WindowsNamedMutex requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    # HANDLE is pointer-sized. Defaults would truncate handles on 64-bit Python.
    api.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.ReleaseMutex.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


class WindowsNamedMutex:
    """A bounded-wait Win32 mutex context manager with explicit ownership.

    Names must be safe ``Local\\`` ASCII names. Worker callers use the mapping
    name plus ``_lock``. Win32 provides thread/process exclusion and same-thread
    reentry; every successful context entry has one matching ReleaseMutex.

    close() is idempotent and refuses to close a handle while acquired or being
    waited on. It never releases mutex ownership on another thread's behalf.
    An abandoned owner causes immediate release and a persistent fault on this
    instance. Callers must stop and reconcile the protected application state.
    """

    def __init__(self, name, timeout_ms=100):
        if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
            raise ValueError("mutex name must be a safe Local\\ ASCII name")
        if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
                or timeout_ms < 0 or timeout_ms >= 0xffffffff):
            raise ValueError("mutex timeout must be a finite nonnegative DWORD millisecond count")
        self.name = name
        self.timeout_ms = timeout_ms
        self._api = _windows_api()
        self._state_lock = threading.Lock()
        self._active = 0
        self._owners = {}
        self._abandoned = False
        self._handle = self._api.CreateMutexW(None, False, name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self):
        # Count a pending native wait so close() cannot invalidate its HANDLE.
        # Do not hold a Python lock during the wait: that could block the owning
        # thread from reaching __exit__ and manufacture a spurious timeout.
        with self._state_lock:
            if self._handle is None:
                raise RuntimeError("named mutex is closed")
            if self._abandoned:
                raise AbandonedMutexError("named mutex was abandoned; reconcile protected state")
            self._active += 1
            handle = self._handle
        entered = False
        try:
            status = self._api.WaitForSingleObject(handle, self.timeout_ms)
            if status == WAIT_TIMEOUT:
                raise TimeoutError("timed out waiting for named mutex " + self.name)
            if status == WAIT_FAILED:
                raise ctypes.WinError(ctypes.get_last_error())
            if status == WAIT_ABANDONED:
                # WAIT_ABANDONED grants ownership, even though the state is
                # unsafe. Release that ownership before surfacing the fault.
                with self._state_lock:
                    self._abandoned = True
                    self._active -= sum(self._owners.values())
                    self._owners.clear()
                if not self._api.ReleaseMutex(handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                raise AbandonedMutexError("named mutex owner died; reconcile protected state")
            if status != WAIT_OBJECT_0:
                raise OSError("unexpected Win32 mutex wait result: " + str(status))
            with self._state_lock:
                if self._abandoned:
                    if not self._api.ReleaseMutex(handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                    raise AbandonedMutexError("named mutex was abandoned; reconcile protected state")
                thread_id = threading.get_ident()
                self._owners[thread_id] = self._owners.get(thread_id, 0) + 1
                entered = True
            return self
        finally:
            if not entered:
                with self._state_lock:
                    self._active -= 1

    def __exit__(self, exc_type, exc_value, traceback):
        with self._state_lock:
            thread_id = threading.get_ident()
            depth = self._owners.get(thread_id, 0)
            if not depth:
                raise RuntimeError("current thread has no acquired mutex context")
            if not self._api.ReleaseMutex(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            if depth == 1:
                del self._owners[thread_id]
            else:
                self._owners[thread_id] = depth - 1
            self._active -= 1
        return False

    def close(self):
        with self._state_lock:
            if self._handle is None:
                return
            if self._active:
                raise RuntimeError("cannot close a mutex with active contexts or waits")
            if not self._api.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None
