import ast
import builtins
import hashlib
import hmac
import multiprocessing
import pathlib
import struct
import sys
import threading
import unittest
import uuid

try:
    from workbuddy_qmt.assets import mmap_channel
except ImportError:
    mmap_channel = None


# Independent wire encoder used to exercise the untrusted-memory boundary.
_HEADER = struct.Struct(">8sBBBBQI32s")
_KEY = b"0123456789abcdef0123456789abcdef"
_NAME = "workbuddy-mmap-test"
_SIZE = 1024


def _receive_in_another_process(name, lock):
    channel = mmap_channel.MmapChannel(name, _SIZE, _KEY, "adapter", lock)
    try:
        if channel.peek() != (1, {"command": "cross-process"}):
            raise AssertionError("child did not receive the pending command")
        if not channel.ack(1):
            raise AssertionError("child did not acknowledge the command")
        if not channel.send({"event": "received"}):
            raise AssertionError("child could not publish its response")
    finally:
        channel.close()


def _replace_in_another_process(name, lock):
    channel = mmap_channel.MmapChannel(name, _SIZE, _KEY, "worker", lock)
    try:
        if not channel.replace_outgoing({"worker_session": "new-worker"}):
            raise AssertionError("new Worker did not publish its takeover barrier")
    finally:
        channel.close()


class CheckedMapping(bytearray):
    def __init__(self, size, lock):
        super().__init__(size)
        self.lock = lock

    def __getitem__(self, item):
        if not self.lock.locked():
            raise AssertionError("shared memory read without the supplied lock")
        return super().__getitem__(item)

    def __setitem__(self, item, value):
        if not self.lock.locked():
            raise AssertionError("shared memory write without the supplied lock")
        return super().__setitem__(item, value)


class MmapChannelTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(mmap_channel, "the mmap transport is not implemented")
        self.mapping = bytearray(_SIZE)
        self.lock = threading.Lock()
        self.worker = self.channel("worker")
        self.adapter = self.channel("adapter")
        self.addCleanup(self.worker.close)
        self.addCleanup(self.adapter.close)

    def channel(self, role, **overrides):
        arguments = dict(name=_NAME, size=_SIZE, key=_KEY, role=role,
                         lock=self.lock, mapping=self.mapping)
        arguments.update(overrides)
        return mmap_channel.MmapChannel(**arguments)

    def write_raw(self, payload, direction=1, sequence=1, state=1):
        prefix = b"WQMT-MMAP\0" + _NAME.encode("utf-8") + b"\0" + struct.pack(">Q", _SIZE)
        unsigned = _HEADER.pack(b"WQMMAP01", 1, direction, state, 0,
                                sequence, len(payload), bytes(32))
        signature = hmac.new(_KEY, prefix + unsigned + payload, hashlib.sha256).digest()
        frame = _HEADER.pack(b"WQMMAP01", 1, direction, state, 0,
                             sequence, len(payload), signature) + payload
        start = 0 if direction == 1 else _SIZE // 2
        self.mapping[start:start + _SIZE // 2] = frame + bytes(_SIZE // 2 - len(frame))

    def test_peek_retries_without_consuming_until_explicit_ack(self):
        self.assertIsNone(self.adapter.peek())
        self.assertTrue(self.worker.send({"command": "query", "account": "证券"}))
        expected = (1, {"command": "query", "account": "证券"})
        self.assertEqual(self.adapter.peek(), expected)
        self.assertEqual(self.adapter.peek(), expected)
        self.assertFalse(self.worker.send({"command": "second"}))
        self.assertTrue(self.adapter.ack(1))
        self.assertIsNone(self.adapter.peek())
        self.assertFalse(self.adapter.ack(1))
        self.assertTrue(self.worker.send({"command": "second"}))
        self.assertEqual(self.adapter.peek(), (2, {"command": "second"}))

    def test_each_direction_has_independent_backpressure(self):
        self.assertTrue(self.worker.send({"command": 1}))
        self.assertTrue(self.adapter.send({"event": 1}))
        self.assertFalse(self.worker.send({"command": 2}))
        self.assertFalse(self.adapter.send({"event": 2}))
        self.assertEqual(self.worker.peek(), (1, {"event": 1}))
        self.assertTrue(self.worker.ack(1))
        self.assertTrue(self.adapter.send({"event": 2}))
        self.assertEqual(self.worker.peek(), (2, {"event": 2}))
        self.assertFalse(self.worker.send({"command": 2}))
        self.assertEqual(self.adapter.peek(), (1, {"command": 1}))

    def test_startup_takeover_replaces_unacknowledged_outgoing_at_next_sequence(self):
        self.assertTrue(callable(getattr(self.worker, "replace_outgoing", None)),
                        "sender startup takeover is not implemented")
        self.worker.send({"worker_session": "old-worker"})
        self.adapter.send({"event": "retained"})
        self.assertTrue(self.worker.replace_outgoing({"worker_session": "new-worker"}))
        self.assertEqual(self.adapter.peek(), (2, {"worker_session": "new-worker"}))
        self.assertFalse(self.adapter.ack(1))
        self.assertFalse(self.worker.send({"command": "still-full"}))
        self.assertEqual(self.worker.peek(), (1, {"event": "retained"}))

    def test_guard_invokes_callback_only_for_current_pending_sequence_under_lock(self):
        calls = []

        def callback():
            self.assertTrue(self.lock.locked())
            calls.append("called")

        self.assertFalse(self.adapter.guard_incoming(1, callback))
        self.worker.send({"command": 1})
        for sequence in (0, 2, -1, True, 1.0, "1", None):
            self.assertFalse(self.adapter.guard_incoming(sequence, callback))
        self.assertEqual(calls, [])
        before = bytes(self.mapping)
        self.assertTrue(self.adapter.guard_incoming(1, callback))
        self.assertEqual(calls, ["called"])
        self.assertEqual(bytes(self.mapping), before)
        # Guards do not consume a frame or supply application-level once-only
        # execution: callers must latch their own execution keys.
        self.assertTrue(self.adapter.guard_incoming(1, callback))
        self.assertEqual(calls, ["called", "called"])
        self.adapter.ack(1)
        self.assertFalse(self.adapter.guard_incoming(1, callback))

    def test_replacement_invalidates_a_previously_peeked_sequence_guard(self):
        self.worker.send({"worker_session": "old-worker", "grant": "old"})
        sequence, unused = self.adapter.peek()
        self.worker.replace_outgoing({"worker_session": "new-worker"})
        calls = []
        self.assertFalse(self.adapter.guard_incoming(sequence, lambda: calls.append("unsafe")))
        self.assertEqual(calls, [])
        self.assertTrue(self.adapter.guard_incoming(2, lambda: calls.append("current")))
        self.assertEqual(calls, ["current"])

    def test_guard_releases_the_lock_and_propagates_a_callback_failure(self):
        self.worker.send({"command": 1})

        def fail():
            raise RuntimeError("submission failed")

        with self.assertRaisesRegex(RuntimeError, "submission failed"):
            self.adapter.guard_incoming(1, fail)
        self.assertFalse(self.lock.locked())
        self.assertTrue(self.worker.replace_outgoing({"worker_session": "replacement"}))

    def test_takeover_cannot_replace_memory_during_a_guarded_callback(self):
        self.worker.send({"command": 1})
        started = threading.Event()
        replaced = threading.Event()
        errors = []

        def replace():
            started.set()
            try:
                self.worker.replace_outgoing({"worker_session": "new-worker"})
                replaced.set()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=replace)

        def callback():
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(replaced.wait(0.05))

        try:
            self.assertTrue(self.adapter.guard_incoming(1, callback))
        finally:
            if thread.ident is not None:
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(replaced.is_set())
        self.assertEqual(self.adapter.peek(), (2, {"worker_session": "new-worker"}))

    def test_startup_replacement_rejects_bad_json_and_size_without_discarding_pending(self):
        self.worker.send({"command": "retained"})
        before = bytes(self.mapping)
        for message in (None, {"value": float("nan")}, {"value": "x" * _SIZE}):
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    self.worker.replace_outgoing(message)
                self.assertEqual(bytes(self.mapping), before)

    def test_takeover_and_guard_reject_tampering_without_invoking_callback(self):
        self.worker.send({"command": 1})
        self.mapping[_HEADER.size] ^= 1
        before = bytes(self.mapping)
        calls = []
        with self.assertRaises(ValueError):
            self.worker.replace_outgoing({"worker_session": "replacement"})
        with self.assertRaises(ValueError):
            self.adapter.guard_incoming(1, lambda: calls.append("unsafe"))
        self.assertEqual(calls, [])
        self.assertEqual(bytes(self.mapping), before)

    def test_wrong_ack_leaves_message_pending(self):
        self.worker.send({"command": 1})
        before = bytes(self.mapping)
        for sequence in (0, 2, -1, True, 1.0, "1", None):
            with self.subTest(sequence=sequence):
                self.assertFalse(self.adapter.ack(sequence))
                self.assertEqual(bytes(self.mapping), before)
        self.assertEqual(self.adapter.peek(), (1, {"command": 1}))
        self.assertFalse(self.worker.send({"command": 2}))

    def test_opening_peer_preserves_pending_message_and_sequence(self):
        self.worker.send({"command": 1})
        replacement = self.channel("adapter")
        self.addCleanup(replacement.close)
        self.assertEqual(replacement.peek(), (1, {"command": 1}))
        self.assertTrue(replacement.ack(1))
        new_sender = self.channel("worker")
        self.addCleanup(new_sender.close)
        self.assertTrue(new_sender.send({"command": 2}))
        self.assertEqual(replacement.peek(), (2, {"command": 2}))

    def test_forged_ack_does_not_release_backpressure(self):
        self.worker.send({"command": 1})
        self.mapping[10] = 0  # Change pending to acknowledged without its HMAC.
        self.mapping[20:24] = bytes(4)
        before = bytes(self.mapping)
        with self.assertRaises(ValueError):
            self.worker.send({"command": 2})
        self.assertEqual(bytes(self.mapping), before)

    def test_replayed_ack_does_not_release_a_later_message(self):
        self.worker.send({"command": 1})
        self.adapter.ack(1)
        old_ack = bytes(self.mapping[:_SIZE // 2])
        self.worker.send({"command": 2})
        self.mapping[:_SIZE // 2] = old_ack
        with self.assertRaises(ValueError):
            self.worker.send({"command": 3})

    def test_replayed_message_after_ack_is_rejected(self):
        self.worker.send({"command": 1})
        frame = bytes(self.mapping[:_SIZE // 2])
        self.adapter.peek()
        self.adapter.ack(1)
        self.mapping[:_SIZE // 2] = frame
        with self.assertRaises(ValueError):
            self.adapter.peek()
        with self.assertRaises(ValueError):
            self.adapter.ack(1)

    def test_payload_sequence_and_mac_tampering_are_rejected(self):
        for index in (_HEADER.size, 19, 24):
            with self.subTest(index=index):
                self.write_raw(b'{"command":1}')
                self.mapping[index] ^= 1
                with self.assertRaises(ValueError):
                    self.adapter.peek()
                with self.assertRaises(ValueError):
                    self.adapter.ack(1)

    def test_cross_direction_replay_is_rejected(self):
        self.worker.send({"command": 1})
        self.mapping[_SIZE // 2:] = self.mapping[:_SIZE // 2]
        with self.assertRaises(ValueError):
            self.worker.peek()

    def test_invalid_magic_version_state_and_reserved_byte_are_rejected(self):
        for index in (0, 8, 10, 11):
            with self.subTest(index=index):
                self.write_raw(b'{}')
                self.mapping[index] = 254
                with self.assertRaises(ValueError):
                    self.adapter.peek()

    def test_forged_or_zero_pending_length_is_rejected(self):
        for length in (0, self.worker.max_payload_size + 1, 0xffffffff):
            with self.subTest(length=length):
                self.write_raw(b'{}')
                self.mapping[20:24] = struct.pack(">I", length)
                with self.assertRaises(ValueError):
                    self.adapter.peek()

    def test_pending_zero_sequence_is_rejected_even_with_valid_hmac(self):
        self.write_raw(b'{}', sequence=0)
        with self.assertRaises(ValueError):
            self.adapter.peek()

    def test_sequence_exhaustion_does_not_wrap_or_overwrite_memory(self):
        self.write_raw(b"", sequence=(1 << 64) - 1, state=0)
        before = bytes(self.mapping)
        for publish in (self.worker.send, self.worker.replace_outgoing):
            with self.assertRaises(ValueError):
                publish({"command": 1})
        self.assertEqual(bytes(self.mapping), before)

    def test_same_sequence_cannot_be_reused_for_a_changed_payload(self):
        self.write_raw(b'{"command":1}')
        self.assertEqual(self.adapter.peek(), (1, {"command": 1}))
        self.write_raw(b'{"command":2}')
        with self.assertRaises(ValueError):
            self.adapter.peek()

    def test_valid_hmac_does_not_allow_invalid_json(self):
        for payload in (b'{', b'[]', b'null', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":-Infinity}', b'{"nested":[1e999]}', b'{"x":1,"x":2}',
                        b'{"x":"\xff"}'):
            with self.subTest(payload=payload):
                self.write_raw(payload)
                with self.assertRaises(ValueError):
                    self.adapter.peek()
                with self.assertRaises(ValueError):
                    self.adapter.ack(1)

    def test_send_rejects_invalid_payload_without_changing_mailbox(self):
        for payload in ([], None, {"x": float("nan")}, {"x": float("inf")},
                        {"nested": [float("-inf")]}, {"x": object()},
                        {1: "ambiguous key"}, {"nested": {None: 1}}):
            with self.subTest(payload=payload):
                before = bytes(self.mapping)
                with self.assertRaises((TypeError, ValueError)):
                    self.worker.send(payload)
                self.assertEqual(bytes(self.mapping), before)
        self.assertTrue(self.worker.send({"nested": [None, True, False, 1, 2.5]}))
        self.assertEqual(self.adapter.peek(), (1, {"nested": [None, True, False, 1, 2.5]}))

    def test_exact_capacity_is_accepted_and_oversize_does_not_consume_sequence(self):
        capacity = self.worker.max_payload_size
        self.assertEqual(capacity, _SIZE // 2 - _HEADER.size)
        with self.assertRaises(ValueError):
            self.worker.send({"x": "a" * (capacity - 7)})
        self.assertIsNone(self.adapter.peek())
        self.assertTrue(self.worker.send({"x": "a" * (capacity - 8)}))
        self.assertEqual(self.adapter.peek(), (1, {"x": "a" * (capacity - 8)}))

    def test_wrong_key_or_mapping_identity_cannot_open_existing_mailboxes(self):
        self.worker.send({"command": 1})
        for overrides in ({"key": b"different secret"}, {"name": "other-account"}):
            with self.subTest(overrides=overrides):
                before = bytes(self.mapping)
                with self.assertRaises(ValueError):
                    self.channel("adapter", **overrides)
                self.assertEqual(bytes(self.mapping), before)

    def test_partial_initialization_is_not_silently_reset(self):
        mapping = bytearray(_SIZE)
        mapping[-1] = 1
        before = bytes(mapping)
        with self.assertRaises(ValueError):
            self.channel("worker", mapping=mapping)
        self.assertEqual(bytes(mapping), before)

    def test_mapping_configuration_is_validated_before_writing(self):
        cases = ({"role": "other"}, {"name": ""}, {"name": "a\0b"},
                 {"key": b""}, {"key": None}, {"lock": None},
                 {"size": 100}, {"size": True}, {"size": _SIZE - 1})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                before = bytes(self.mapping)
                with self.assertRaises((TypeError, ValueError)):
                    self.channel("worker", **overrides)
                self.assertEqual(bytes(self.mapping), before)

    def test_all_shared_memory_access_uses_the_supplied_lock(self):
        lock = threading.Lock()
        mapping = CheckedMapping(_SIZE, lock)
        worker = self.channel("worker", mapping=mapping, lock=lock)
        adapter = self.channel("adapter", mapping=mapping, lock=lock)
        self.addCleanup(worker.close)
        self.addCleanup(adapter.close)
        self.assertTrue(worker.send({"command": 1}))
        self.assertEqual(adapter.peek(), (1, {"command": 1}))
        self.assertTrue(adapter.ack(1))
        self.assertTrue(worker.send({"command": 2}))

    def test_close_does_not_close_borrowed_mapping(self):
        import mmap
        mapping = mmap.mmap(-1, _SIZE)
        self.addCleanup(mapping.close)
        channel = self.channel("worker", mapping=mapping)
        channel.close()
        channel.close()
        self.assertFalse(mapping.closed)
        for operation in (lambda: channel.send({}), channel.peek, lambda: channel.ack(1),
                          lambda: channel.replace_outgoing({}),
                          lambda: channel.guard_incoming(1, lambda: None)):
            with self.assertRaises(RuntimeError):
                operation()

    @unittest.skipUnless(sys.platform == "win32", "Windows named mmap")
    def test_named_mapping_is_shared_and_owned_mapping_is_closed(self):
        name = "Local\\workbuddy-test-" + uuid.uuid4().hex
        lock = threading.Lock()
        worker = mmap_channel.MmapChannel(name, _SIZE, _KEY, "worker", lock)
        adapter = mmap_channel.MmapChannel(name, _SIZE, _KEY, "adapter", lock)
        self.addCleanup(worker.close)
        self.addCleanup(adapter.close)
        self.assertTrue(worker.send({"command": 1}))
        self.assertEqual(adapter.peek(), (1, {"command": 1}))
        owned_mapping = worker._mapping
        worker.close()
        self.assertTrue(owned_mapping.closed)
        self.assertTrue(adapter.ack(1))

    @unittest.skipUnless(sys.platform == "win32", "Windows named mmap")
    def test_named_mapping_exchanges_messages_between_processes(self):
        context = multiprocessing.get_context("spawn")
        lock = context.Lock()
        name = "Local\\workbuddy-process-test-" + uuid.uuid4().hex
        worker = mmap_channel.MmapChannel(name, _SIZE, _KEY, "worker", lock)
        self.addCleanup(worker.close)
        self.assertTrue(worker.send({"command": "cross-process"}))
        process = context.Process(target=_receive_in_another_process, args=(name, lock))
        process.start()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("child process did not complete the mailbox exchange")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(worker.peek(), (1, {"event": "received"}))
        self.assertTrue(worker.ack(1))
        self.assertTrue(worker.send({"command": "after-child-ack"}))

    @unittest.skipUnless(sys.platform == "win32", "Windows named mmap")
    def test_spawned_worker_takeover_invalidates_the_old_qmt_sequence_guard(self):
        context = multiprocessing.get_context("spawn")
        lock = context.Lock()
        name = "Local\\workbuddy-takeover-test-" + uuid.uuid4().hex
        worker = mmap_channel.MmapChannel(name, _SIZE, _KEY, "worker", lock)
        adapter = mmap_channel.MmapChannel(name, _SIZE, _KEY, "adapter", lock)
        self.addCleanup(worker.close)
        self.addCleanup(adapter.close)
        self.assertTrue(worker.send({"worker_session": "old-worker"}))
        sequence, unused = adapter.peek()
        process = context.Process(target=_replace_in_another_process, args=(name, lock))
        process.start()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("new Worker did not complete its takeover")
        self.assertEqual(process.exitcode, 0)
        calls = []
        self.assertFalse(adapter.guard_incoming(sequence, lambda: calls.append("unsafe")))
        self.assertEqual(calls, [])
        self.assertEqual(adapter.peek(), (2, {"worker_session": "new-worker"}))

    def test_embedded_source_runs_with_restricted_imports_and_python36_syntax(self):
        source = pathlib.Path(mmap_channel.__file__).read_text(encoding="utf-8-sig")
        ast.parse(source, feature_version=(3, 6))
        real_import = builtins.__import__
        allowed = {"mmap", "struct", "json", "hashlib", "hmac"}

        def restricted_import(name, *args, **kwargs):
            if name not in allowed:
                raise AssertionError("forbidden embedded import: " + name)
            return real_import(name, *args, **kwargs)

        namespace = {"__builtins__": dict(vars(builtins), __import__=restricted_import)}
        exec(compile(source, "mmap_channel.py", "exec"), namespace)
        lock = threading.Lock()
        mapping = bytearray(_SIZE)
        worker = namespace["MmapChannel"](_NAME, _SIZE, _KEY, "worker", lock, mapping)
        adapter = namespace["MmapChannel"](_NAME, _SIZE, _KEY, "adapter", lock, mapping)
        self.addCleanup(worker.close)
        self.addCleanup(adapter.close)
        self.assertTrue(worker.send({"command": "restricted"}))
        self.assertEqual(adapter.peek(), (1, {"command": "restricted"}))
        self.assertTrue(adapter.ack(1))


if __name__ == "__main__":
    unittest.main()
