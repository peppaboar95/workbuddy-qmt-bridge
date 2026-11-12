# -*- coding: utf-8 -*-
"""Paste this entire standalone Python 3.6-compatible script into a QMT model.

Start qmt_probe_peer.py first for cross-process checks. This script does not
access accounts, market data, orders, or broker endpoints. All optional imports
are isolated so a denied module cannot hide the remaining diagnostic results.
"""

PROBE_DIRECTORY = r"F:\workbuddy-qmt-bridge-main\probe-output"
PAIR_NAME = r"Local\WBQMT_CAPABILITY_PROBE_V1"
PEER_PORT = 17643
PROBE_SECONDS = 60
RUNTIME = None
_MARKER = b'WBQMT_PROBE_V1!!'
_SIZE = 4096
_REQUEST = 128
_RESPONSE = 1024
_CAPACITY = 768


def _module(name):
    return __import__(name, fromlist=['*'])


def _expect(condition, message):
    if not condition:
        raise RuntimeError(message)


class _Inconclusive(Exception):
    pass


def _safe_name(name):
    prefixes = ('Local\\WBQMT_CAPABILITY_PROBE', 'Local\\WBQMT_CAPABILITY_TEST_')
    suffix = name[6:] if isinstance(name, str) else ''
    return (isinstance(name, str) and name.startswith(prefixes) and len(name) <= 200
            and all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in suffix))


class _CtypesMutex:
    def __init__(self, module, name):
        self.c = module
        self.api = module.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = [module.c_void_p, module.c_int, module.c_wchar_p]
        self.api.CreateMutexW.restype = module.c_void_p
        self.api.WaitForSingleObject.argtypes = [module.c_void_p, module.c_uint32]
        self.api.WaitForSingleObject.restype = module.c_uint32
        self.api.ReleaseMutex.argtypes = [module.c_void_p]
        self.api.ReleaseMutex.restype = module.c_int
        self.api.CloseHandle.argtypes = [module.c_void_p]
        self.api.CloseHandle.restype = module.c_int
        self.handle = self.api.CreateMutexW(None, False, name)
        self.owned = False
        _expect(bool(self.handle), 'CreateMutexW failed: ' + str(module.get_last_error()))

    def acquire(self):
        status = self.api.WaitForSingleObject(self.handle, 0)
        if status == 258:
            return False
        if status == 128:
            self.api.ReleaseMutex(self.handle)
            raise RuntimeError('WAIT_ABANDONED: prior owner exited; not a successful lock test')
        _expect(status == 0, 'WaitForSingleObject failed: ' + str(status))
        self.owned = True
        return True

    def release(self):
        if self.owned:
            _expect(bool(self.api.ReleaseMutex(self.handle)), 'ReleaseMutex failed')
            self.owned = False

    def close(self):
        self.release()
        if self.handle:
            _expect(bool(self.api.CloseHandle(self.handle)), 'CloseHandle failed')
            self.handle = None


class _Pywin32Mutex:
    def __init__(self, module, name):
        self.api = module
        self.handle = module.CreateMutex(None, False, name)
        self.owned = False

    def acquire(self):
        status = self.api.WaitForSingleObject(self.handle, 0)
        if status == 258:
            return False
        if status == 128:
            self.api.ReleaseMutex(self.handle)
            raise RuntimeError('WAIT_ABANDONED: prior owner exited')
        _expect(status == 0, 'WaitForSingleObject failed: ' + str(status))
        self.owned = True
        return True

    def release(self):
        if self.owned:
            self.api.ReleaseMutex(self.handle)
            self.owned = False

    def close(self):
        self.release()
        if self.handle is not None:
            self.handle.Close()
            self.handle = None


class CapabilityProbe:
    def __init__(self, directory=PROBE_DIRECTORY, pair_name=PAIR_NAME,
                 seconds=PROBE_SECONDS, port=PEER_PORT):
        if not _safe_name(pair_name):
            raise ValueError('use only the dedicated Local\\WBQMT_CAPABILITY_PROBE/TEST namespace')
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 120:
            raise ValueError('probe duration must be 1..120 seconds')
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError('invalid local peer port')
        self.directory = str(directory).rstrip('/\\')
        self.pair_name = pair_name
        self.seconds = seconds
        self.port = port
        self.modules = {}
        self.report = {'probe_version': 2, 'role': 'qmt', 'checks': {},
                       'directory': self.directory, 'mapping_name': pair_name,
                       'network_scope': '127.0.0.1 only',
                       'pair_diagnostics': {'phase': 'NOT_STARTED', 'wait_reason': None}}
        self.finished = False
        self.deadline = 0
        self.started_at = 0.0
        self.steps = 0
        self.mapping = None
        self.epoch = None
        self.phase = 'PING'
        self.backends = []
        self.backend = None
        self.mutex = None
        self.created = []
        self.scratch_directory = ''
        self.report_path = None
        self.nonce = '%032x' % id(self)
        self.guard = None
        self.stop_requested = None

    def record(self, name, status, detail):
        row = {'status': status, 'detail': str(detail)[:500]}
        if self.report['checks'].get(name) != row:
            self.report['checks'][name] = row
            print('[QMT-PROBE] %s %s: %s' % (status, name, row['detail']), flush=True)
        return status == 'PASS'

    def check(self, name, operation, prerequisite=True):
        if not prerequisite:
            return self.record(name, 'SKIP', 'prerequisite unavailable; this is not proof of prohibition')
        try:
            detail = operation()
            return self.record(name, 'PASS', detail if detail is not None else 'operation completed')
        except FileNotFoundError as exc:
            return self.record(name, 'SKIP', 'probe directory or prerequisite missing: ' + str(exc))
        except Exception as exc:
            return self.record(name, 'FAIL', type(exc).__name__ + ': ' + str(exc))

    def now(self):
        clock = self.modules.get('time')
        return clock.monotonic() if clock else 0.0

    def start(self, context=None):
        names = ('sys', 'time', 'json', 'os', 'tempfile', 'sqlite3', 'socket', 'mmap',
                 'ctypes', 'win32event', 'win32api', 'msvcrt', 'hashlib', 'hmac',
                 'base64', 'math', 'uuid', '_winapi', 'threading', '_thread')
        for name in names:
            try:
                self.modules[name] = _module(name)
                self.record('import.' + name, 'PASS', 'import succeeded; operations tested separately')
            except Exception as exc:
                self.record('import.' + name, 'FAIL', type(exc).__name__ + ': ' + str(exc))
        system = self.modules.get('sys')
        if system:
            self.report['python_version'] = system.version
            self.report['pointer_bits'] = 64 if system.maxsize > 2 ** 32 else 32
            if system.version_info >= (3, 8):
                self.check('import.multiprocessing.shared_memory', lambda: _module('multiprocessing.shared_memory') and 'available')
            else:
                self.record('import.multiprocessing.shared_memory', 'SKIP', 'Python <3.8; API absent by version')
        if self.modules.get('time'):
            try:
                self.nonce = '%016x%016x' % (int(self.modules['time'].time() * 1000000), id(self))
                self.modules['time'].monotonic()
                self.record('time.clock', 'PASS', 'time and monotonic clock callable')
            except Exception as exc:
                self.record('time.clock', 'FAIL', type(exc).__name__ + ': ' + str(exc))
                self.modules['time'] = None
        try:
            threading_api = self.modules.get('_thread') or self.modules.get('threading')
            if threading_api is None:
                raise RuntimeError('no callback serialization API; paired test cannot run safely')
            self.guard = threading_api.RLock()
            self.record('probe.callback_guard', 'PASS', 'callbacks serialize; stop defers to current mutex owner')
        except Exception as exc:
            self.record('probe.callback_guard', 'SKIP', str(exc))
        self.report['run_id'] = self.nonce
        self.started_at = self.now()
        self.deadline = self.started_at + self.seconds
        self.check('os.getpid', lambda: self.modules['os'].getpid(), self.modules.get('os'))
        self.check('uuid.generate', lambda: self.modules['uuid'].uuid4().hex, self.modules.get('uuid'))
        def crypto():
            digest = self.modules['hmac'].new(b'probe-only-key', self.nonce.encode('ascii'),
                                            self.modules['hashlib'].sha256).digest()
            _expect(self.modules['hmac'].compare_digest(digest, digest), 'HMAC comparison failed')
            return 'HMAC-SHA256 and compare_digest callable'
        self.check('crypto.hmac_sha256', crypto, self.modules.get('hmac') and self.modules.get('hashlib'))
        self._files()
        self.check('sqlite.memory', lambda: self._database(':memory:', False), self.modules.get('sqlite3'))
        database = self._path(self._prefix + 'probe.sqlite')
        self.check('sqlite.file', lambda: self._database(database, True), self.modules.get('sqlite3'))
        self.check('mmap.local', self._local_mapping, self.modules.get('mmap'))
        for backend, factory in (('ctypes', _CtypesMutex), ('win32event', _Pywin32Mutex)):
            if self.check('mutex.' + backend + '.local',
                          lambda b=backend, f=factory: self._local_mutex(b, f), self.modules.get(backend)):
                self.backends.append(backend)
        self._network()
        self.record('mmap.duplex', 'WAIT', 'start external qmt_probe_peer.py, then wait for nonce response')
        for backend in self.backends:
            self.record('mutex.' + backend + '.cross_process', 'WAIT', 'requires live external peer')
        self.step()
        if context is not None and not self.finished:
            self._schedule(context)
        return self.report

    def _path(self, name):
        return (self.scratch_directory or self.directory) + '/' + name

    def _files(self):
        operating = self.modules.get('os')
        self.scratch_directory = self.directory + '/qmt-probe-' + self.nonce
        if operating and self.check('os.makedirs', lambda: operating.makedirs(self.scratch_directory)):
            pass
        else:
            # A denied os import must not prevent testing builtins.open against
            # the directory created by the external helper.
            self.scratch_directory = ''
            if not operating:
                self.record('os.makedirs', 'SKIP', 'os unavailable; using pre-existing probe directory')
        self._prefix = '' if self.scratch_directory else 'qmt-probe-' + self.nonce + '-'
        data_path = self._path(self._prefix + 'bytes.bin')
        def binary_roundtrip():
            data = ('WBQMT-PROBE:' + self.nonce).encode('ascii')
            with open(data_path, 'x+b') as stream:
                self.created.append(data_path)
                stream.write(data)
                stream.flush()
                stream.seek(0)
                _expect(stream.read() == data, 'file bytes did not round trip')
            return 'exclusive create, write, flush, seek and read succeeded'
        wrote = self.check('file.write_read', binary_roundtrip)
        json_path = self._path(self._prefix + 'payload.json')
        def json_roundtrip():
            value = {'probe': self.nonce, 'number': 1}
            with open(json_path, 'x', encoding='utf-8') as stream:
                self.created.append(json_path)
                self.modules['json'].dump(value, stream)
            with open(json_path, 'r', encoding='utf-8') as stream:
                _expect(self.modules['json'].load(stream) == value, 'JSON mismatch')
        self.check('file.json', json_roundtrip, self.modules.get('json'))
        self.check('os.listdir', lambda: operating.listdir(self.scratch_directory or self.directory) and 'directory enumerated', operating)
        self.check('os.stat', lambda: operating.stat(data_path).st_size, operating and wrote)
        def fsync():
            with open(data_path, 'r+b') as stream:
                stream.flush()
                operating.fsync(stream.fileno())
        self.check('os.fsync', fsync, operating and wrote)
        def replace():
            target = data_path + '.renamed'
            _expect(not operating.path.exists(target), 'rename target already exists; refusing overwrite')
            operating.replace(data_path, target)
            self.created.append(target)
            operating.replace(target, data_path)
        self.check('os.replace', replace, operating and wrote)
        def file_lock():
            path = self._path(self._prefix + 'lock.bin')
            api = self.modules['msvcrt']
            with open(path, 'x+b') as stream:
                self.created.append(path)
                stream.write(b'0')
                stream.flush()
                stream.seek(0)
                api.locking(stream.fileno(), api.LK_NBLCK, 1)
                try:
                    return 'file lock/unlock works locally; not a named mutex test'
                finally:
                    stream.seek(0)
                    api.locking(stream.fileno(), api.LK_UNLCK, 1)
        self.check('msvcrt.file_lock', file_lock, self.modules.get('msvcrt'))
        def temporary_file():
            descriptor, path = self.modules['tempfile'].mkstemp(
                prefix='qmt-probe-' + self.nonce + '-', suffix='.tmp',
                dir=self.scratch_directory or self.directory)
            self.created.append(path)
            operating.close(descriptor)
        self.check('tempfile.mkstemp', temporary_file, operating and self.modules.get('tempfile'))

    def _database(self, path, reopen):
        api = self.modules['sqlite3']
        if reopen:
            # sqlite.connect opens existing databases. Claim a new empty file
            # exclusively first; never inspect or modify a pre-existing DB.
            with open(path, 'xb'):
                self.created.append(path)
        connection = api.connect(path, timeout=0.2)
        try:
            connection.execute('CREATE TABLE probe(value TEXT)')
            connection.execute('INSERT INTO probe VALUES(?)', (self.nonce,))
            connection.commit()
            _expect(connection.execute('SELECT value FROM probe').fetchone()[0] == self.nonce, 'SQLite mismatch')
        finally:
            connection.close()
        if reopen:
            connection = api.connect(path, timeout=0.2)
            try:
                _expect(connection.execute('SELECT value FROM probe').fetchone()[0] == self.nonce, 'SQLite reopen mismatch')
            finally:
                connection.close()
        return 'create/insert/commit/read' + ('/close/reopen/read' if reopen else '')

    def _local_mapping(self):
        mapping = self.modules['mmap'].mmap(-1, 4096, tagname=self.pair_name + '_local_' + self.nonce)
        try:
            mapping[:16] = self.nonce[:16].encode('ascii')
            _expect(bytes(mapping[:16]) == self.nonce[:16].encode('ascii'), 'mmap local mismatch')
        finally:
            mapping.close()
        return 'named pagefile-backed mapping created, written and read'

    def _local_mutex(self, backend, factory):
        lock = factory(self.modules[backend], self.pair_name + '_local_mutex_' + self.nonce)
        try:
            _expect(lock.acquire(), 'new local mutex was unexpectedly busy')
            lock.release()
        finally:
            lock.close()
        return 'named Win32 mutex create/acquire/release/close succeeded'

    def _network(self):
        api = self.modules.get('socket')
        def listener():
            stream = api.socket(api.AF_INET, api.SOCK_STREAM)
            try:
                stream.settimeout(0.2)
                stream.bind(('127.0.0.1', 0))
                stream.listen(1)
                return 'loopback-only bind/listen succeeded'
            finally:
                stream.close()
        self.check('network.local_listen', listener, api)
        if not api or not self.port:
            self.record('network.local_connect', 'SKIP', 'socket unavailable or peer port disabled')
            return
        stream = None
        try:
            deadline = self.now() + 0.5 if self.modules.get('time') else None
            stream = api.socket(api.AF_INET, api.SOCK_STREAM)
            stream.settimeout(0.25)
            stream.connect(('127.0.0.1', self.port))
            stream.sendall(('WBQMT-PROBE:' + self.nonce).encode('ascii'))
            expected = ('ACK:' + self.nonce).encode('ascii')
            reply = b''
            while len(reply) < len(expected):
                if deadline is not None:
                    remaining = deadline - self.now()
                    if remaining <= 0:
                        raise TimeoutError('overall loopback probe deadline expired')
                    stream.settimeout(min(0.25, remaining))
                part = stream.recv(len(expected) - len(reply))
                if not part:
                    break
                reply += part
                if deadline is None:
                    break
            _expect(reply == expected, 'peer did not return the matching nonce')
            self.record('network.local_connect', 'PASS', '127.0.0.1 peer returned matching nonce')
        except (ConnectionRefusedError, TimeoutError, api.timeout) as exc:
            self.record('network.local_connect', 'SKIP', 'peer absent/unreachable; not proof of prohibition: ' + str(exc))
        except Exception as exc:
            self.record('network.local_connect', 'FAIL', type(exc).__name__ + ': ' + str(exc))
        finally:
            if stream is not None:
                stream.close()

    def _write_request(self, op, backend=None):
        value = {'nonce': self.nonce, 'op': op}
        if backend:
            value['backend'] = backend
        data = self.modules['json'].dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
        _expect(len(data) <= _CAPACITY - 4, 'request exceeds probe slot')
        self.mapping[_REQUEST:_REQUEST + _CAPACITY] = len(data).to_bytes(4, 'big') + data + bytes(_CAPACITY - 4 - len(data))

    def _read_response(self):
        diagnostics = self.report['pair_diagnostics']
        first = bytes(self.mapping[_RESPONSE:_RESPONSE + _CAPACITY])
        if first != bytes(self.mapping[_RESPONSE:_RESPONSE + _CAPACITY]):
            diagnostics['wait_reason'] = 'response_is_being_written'
            return None
        length = int.from_bytes(first[:4], 'big')
        if not 0 < length <= _CAPACITY - 4:
            diagnostics['wait_reason'] = 'empty_response' if length == 0 else 'invalid_response_length'
            return None
        try:
            value = self.modules['json'].loads(first[4:4 + length].decode('utf-8'))
        except Exception:
            diagnostics['wait_reason'] = 'invalid_response_json'
            return None
        if not isinstance(value, dict):
            diagnostics['wait_reason'] = 'response_is_not_an_object'
            return None
        for field, expected in (('nonce', self.nonce), ('peer_epoch', self.epoch), ('backend', self.backend)):
            if value.get(field) != expected:
                diagnostics['wait_reason'] = 'response_' + field + '_mismatch'
                diagnostics['last_response_op'] = value.get('op')
                return None
        diagnostics['wait_reason'] = None
        diagnostics['last_response_op'] = value.get('op')
        return value

    def step(self):
        if self.guard is None:
            if not self.finished:
                self.finish('paired checks require a callback serialization API')
            return
        if not self.guard.acquire(False):
            return
        try:
            self._step()
        finally:
            try:
                if self.stop_requested and not self.finished:
                    self._finish(self.stop_requested)
            finally:
                self.guard.release()

    def _step(self):
        if self.finished:
            return
        self.steps += 1
        diagnostics = self.report['pair_diagnostics']
        diagnostics['callback_steps'] = self.steps
        diagnostics['phase'] = self.phase
        diagnostics['backend'] = self.backend
        # Historical QMT handlebar/timer callbacks may run hundreds of times
        # within milliseconds. Their count is never an elapsed-time budget.
        if self.modules.get('time') and self.now() >= self.deadline:
            self.finish('time budget exhausted')
            return
        if not self.modules.get('mmap') or not self.modules.get('json') or not self.modules.get('time'):
            self.finish('paired checks require mmap, json and time')
            return
        try:
            if self.mapping is None:
                self.mapping = self.modules['mmap'].mmap(-1, _SIZE, tagname=self.pair_name)
            diagnostics['header_hex'] = bytes(self.mapping[:16]).hex()
            if bytes(self.mapping[:16]) != _MARKER:
                diagnostics['wait_reason'] = 'peer_header_not_visible'
                return
            epoch = bytes(self.mapping[16:48]).decode('ascii')
            diagnostics['peer_epoch'] = epoch
            if self.epoch is None:
                self.epoch = epoch
                self._write_request('PING')
            elif epoch != self.epoch:
                raise RuntimeError('external peer restarted; rerun probe rather than accepting stale phases')
            response = self._read_response()
            if not response:
                return
            operation = response.get('op')
            if operation == 'ERROR':
                raise RuntimeError('external peer: ' + str(response.get('error', response.get('detail', 'operation failed'))))
            if self.phase == 'PING' and operation == 'PONG':
                self.record('mmap.duplex', 'PASS', 'QMT nonce crossed to peer and matching response returned')
                self._next_backend()
            elif self.phase == 'HOLD' and operation == 'MUTEX_HELD':
                _expect(not self.mutex.acquire(), 'QMT acquired mutex while external peer claims ownership')
                self.record('mutex.' + self.backend + '.peer_excludes_qmt', 'PASS', 'QMT Wait(0) timed out while peer owns mutex')
                self.phase = 'RELEASE'
                self._write_request('MUTEX_RELEASE', self.backend)
            elif self.phase == 'RELEASE' and operation == 'MUTEX_RELEASED':
                _expect(self.mutex.acquire(), 'mutex unavailable after peer release')
                try:
                    self._write_request('QMT_HOLDS', self.backend)
                    deadline = self.now() + 0.3
                    confirmed = False
                    while self.now() < deadline:
                        response = self._read_response()
                        if response and response.get('op') == 'PEER_EXCLUDED':
                            confirmed = True
                            break
                        if response and response.get('op') == 'ERROR':
                            raise RuntimeError(str(response.get('error', response.get('detail'))))
                        self.modules['time'].sleep(0.01)
                    if not confirmed:
                        raise _Inconclusive('peer did not confirm exclusion within 300ms; rerun with helper active')
                finally:
                    # Acquisition and release always occur in this same callback.
                    self.mutex.release()
                self.record('mutex.' + self.backend + '.qmt_excludes_peer', 'PASS', 'external Wait(0) timed out while QMT owns mutex')
                self.record('mutex.' + self.backend + '.cross_process', 'PASS', 'mutual exclusion proved in both directions')
                self._next_backend()
        except _Inconclusive as exc:
            self.record('mutex.' + self.backend + '.cross_process', 'SKIP', str(exc))
            self.finish('paired timing inconclusive')
        except Exception as exc:
            self.record('pair.' + (self.backend or 'mmap'), 'FAIL', type(exc).__name__ + ': ' + str(exc))
            if self.backend:
                self.record('mutex.' + self.backend + '.cross_process', 'FAIL', type(exc).__name__ + ': ' + str(exc))
            self.finish('paired operation failed')

    def _next_backend(self):
        if self.mutex:
            self.mutex.close()
            self.mutex = None
        if not self.backends:
            self._write_request('DONE', self.backend)
            self.finish('all available paired backends checked')
            return
        self.backend = self.backends.pop(0)
        factory = _CtypesMutex if self.backend == 'ctypes' else _Pywin32Mutex
        self.mutex = factory(self.modules[self.backend], self.pair_name + '_mutex')
        self.phase = 'HOLD'
        self._write_request('MUTEX_HOLD', self.backend)

    def _schedule(self, context):
        try:
            context.run_time('qmt_probe_timer', '1nSecond', '2018-01-01 00:00:00')
            self.record('qmt.timer', 'PASS', 'run_time registration accepted; callbacks will confirm execution')
        except Exception as exc:
            self.record('qmt.timer', 'FAIL', type(exc).__name__ + ': ' + str(exc))
            print('[QMT-PROBE] Call qmt_probe_timer(ContextInfo) to continue; handlebar also polls.', flush=True)

    def _cleanup_files(self):
        operating = self.modules.get('os')
        if not operating:
            self.record('file.cleanup', 'SKIP', 'os unavailable; unique probe files may remain in probe directory')
            return
        def cleanup():
            root = operating.path.abspath(self.directory)
            for path in self.created:
                target = operating.path.abspath(path)
                _expect(operating.path.commonpath([root, target]) == root, 'cleanup escaped probe directory')
                if operating.path.exists(target):
                    operating.remove(target)
            if self.scratch_directory:
                target = operating.path.abspath(self.scratch_directory)
                _expect(operating.path.commonpath([root, target]) == root and target != root, 'invalid scratch directory')
                operating.rmdir(target)  # Empty own directory only; never recursive.
        self.check('file.cleanup', cleanup)

    def finish(self, reason='stopped'):
        if self.guard is None:
            return self._finish(reason)
        if not self.guard.acquire(False):
            self.stop_requested = reason
            return self.report
        try:
            return self._finish(reason)
        finally:
            self.guard.release()

    def _finish(self, reason):
        if self.finished:
            return self.report
        self.finished = True
        self.report['finished'] = True
        self.report['finish_reason'] = reason
        self.report['pair_diagnostics']['callback_steps'] = self.steps
        self.report['pair_diagnostics']['elapsed_seconds'] = (
            max(0.0, self.now() - self.started_at) if self.modules.get('time') else None)
        if self.mutex:
            self.check('mutex.close', self.mutex.close)
            self.mutex = None
        if self.mapping:
            # Request release if the peer currently owns the mutex. No waiting.
            self.check('pair.release_request', lambda: self._write_request('DONE', self.backend))
            self.check('mmap.close', self.mapping.close)
            self.mapping = None
        for name, row in list(self.report['checks'].items()):
            if row['status'] == 'WAIT':
                self.record(name, 'SKIP', 'peer/phase not verified before stop; no permission conclusion')
        self._cleanup_files()
        api = self.modules.get('json')
        if api:
            path = self.directory + '/QMT-probe-' + self.nonce + '.json'
            try:
                with open(path, 'x', encoding='utf-8') as stream:
                    api.dump(self.report, stream, ensure_ascii=True, indent=2, allow_nan=False)
                self.report_path = path
                print('[QMT-PROBE] REPORT_FILE ' + path, flush=True)
            except Exception as exc:
                print('[QMT-PROBE] REPORT_FILE_UNAVAILABLE ' + type(exc).__name__ + ': ' + str(exc), flush=True)
        print('[QMT-PROBE] REPORT_BEGIN', flush=True)
        try:
            encoded = api.dumps(self.report, ensure_ascii=True, sort_keys=True) if api else repr(self.report)
        except Exception:
            encoded = repr(self.report)
        print(encoded, flush=True)
        print('[QMT-PROBE] REPORT_END', flush=True)
        return self.report


def init(ContextInfo):
    global RUNTIME
    if RUNTIME is not None:
        RUNTIME.finish('probe restarted')
        if not RUNTIME.finished:
            print('[QMT-PROBE] Previous callback is stopping; start again after REPORT_END.', flush=True)
            return
    RUNTIME = CapabilityProbe()
    RUNTIME.start(ContextInfo)


def qmt_probe_timer(ContextInfo):
    if RUNTIME is not None and not RUNTIME.finished:
        RUNTIME.record('qmt.timer_callback', 'PASS', 'QMT called the scheduled probe callback')
        RUNTIME.step()


def handlebar(ContextInfo):
    if RUNTIME is not None:
        RUNTIME.step()


def stop(ContextInfo):
    if RUNTIME is not None:
        RUNTIME.finish('QMT model stopped')
