import ast
import base64
import builtins
import datetime as dt
import hashlib
import json
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class MmapAdapterTests(unittest.TestCase):
    def render(self):
        self.assertIsNotNone(importlib.util.find_spec('workbuddy_qmt.mmap_adapter'),
                             'mmap adapter renderer is not implemented')
        from workbuddy_qmt.mmap_adapter import render_mmap_adapter
        from workbuddy_qmt.config import AccountConfig, BridgeConfig
        from workbuddy_qmt.manager import _base_adapter_config
        account = AccountConfig('test', 'STOCK', 'instance')
        config = BridgeConfig(data_dir='runtime', key_file='keys')
        adapter = _base_adapter_config(config, account, 'test-account', 'profile')
        template = Path('src/workbuddy_qmt/assets/qmt_embedded_adapter.py').read_text(encoding='utf-8')
        keys = {'active_key_id': 'test', 'keys': {'test': base64.urlsafe_b64encode(b'x' * 32).decode('ascii')}}
        return render_mmap_adapter(template, adapter, keys, 'Local\\WBQMT_test', 1048576)

    def test_generated_script_has_python36_syntax_and_no_forbidden_imports(self):
        source = self.render()
        tree = ast.parse(source, feature_version=(3, 6))
        forbidden = {'os', 'uuid', 'sqlite3', 'socket', 'ctypes', 'multiprocessing', 'pathlib'}
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append((node.module or '').split('.')[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {'open', 'exec', 'eval', 'compile', '__import__'})
        self.assertFalse(set(imports) & forbidden)

    def test_script_initializes_without_file_io_or_os_import(self):
        source = self.render()
        real_import = builtins.__import__
        def restricted_import(name, *args, **kwargs):
            if name.split('.')[0] in {'os', 'uuid', 'socket', 'sqlite3', 'ctypes', 'multiprocessing'}:
                raise ImportError('forbidden: ' + name)
            return real_import(name, *args, **kwargs)
        scope = {'__builtins__': dict(vars(builtins), __import__=restricted_import,
                                     open=lambda *a, **k: self.fail('QMT file access'))}
        exec(compile(source, '<generated-qmt-mmap>', 'exec'), scope)
        context = mock.Mock()
        scope['MMAP_LOCK_FACTORY'] = lambda name: None
        scope['MmapChannel'] = lambda *a, **k: FakeChannel()
        scope['init'](context)
        self.assertEqual(scope['_STATUS'], 'RECOVERING')
        context.set_account.assert_called_once_with('test-account')
        self.assertFalse(scope['_link_live']())

    def test_missing_sync_hook_fails_closed_with_clear_error(self):
        scope = {}
        exec(compile(self.render(), '<generated-qmt-mmap>', 'exec'), scope)
        with self.assertRaisesRegex(RuntimeError, 'MMAP_LOCK_FACTORY'):
            scope['init'](mock.Mock())
        self.assertEqual(scope['_STATUS'], 'ERROR')

    def running(self):
        scope = {}
        exec(compile(self.render(), '<generated-qmt-mmap>', 'exec'), scope)
        channel = FakeChannel()
        scope['MMAP_LOCK_FACTORY'] = lambda name: None
        scope['MmapChannel'] = lambda *a, **k: channel
        scope['get_trade_detail_data'] = lambda *args: []
        scope['init'](mock.Mock())
        return scope, channel

    def control(self, scope, **changes):
        frame = {
            'account_alias': 'test', 'adapter_session': scope['_adapter_session'],
            'worker_session': 'worker-1',
            'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat(),
            'config': dict(scope['MMAP_BOOTSTRAP']['config']), 'profile': {},
            'runtime': {}, 'journals': [], 'commands': [], 'grants': [],
        }
        frame.update(changes)
        return frame

    def test_hello_keeps_events_until_authenticated_worker_handshake(self):
        scope, channel = self.running()
        scope['_write_event']({'type': 'ERROR_EVENT'})
        scope['poll_commands']()
        self.assertEqual(channel.sent[-1]['events'], [])
        self.assertEqual(len(scope['_pending_events']), 1)
        channel.incoming.append((1, self.control(scope)))
        scope['poll_commands']()
        self.assertEqual(scope['_STATUS'], 'READY')
        self.assertTrue(channel.sent[-1]['events'])

    def test_stale_adapter_control_cannot_renew_lease(self):
        scope, channel = self.running()
        channel.incoming.append((1, self.control(scope, adapter_session='previous-adapter')))
        scope['poll_commands']()
        self.assertFalse(scope['_link_live']())

    def test_invalid_mode_is_rejected_before_changing_control_state(self):
        scope, channel = self.running()
        frame = self.control(scope)
        frame['config']['qmt_mode'] = 'INVALID'
        with self.assertRaisesRegex(RuntimeError, 'invalid qmt_mode'):
            scope['_apply_control'](frame)
        self.assertEqual(scope['_CONFIG']['qmt_mode'], 'OBSERVE_ONLY')

    def test_local_pause_survives_control_until_new_authorization(self):
        scope, channel = self.running()
        scope['_atomic_json'](scope['_auto_pause_path'](), {
            'paused': True, 'permit_id': 'permit', 'generation': 1})
        channel.incoming.append((1, self.control(scope)))
        scope['poll_commands']()
        pause = scope['_load_json'](scope['_auto_pause_path']())
        self.assertTrue(pause['paused'])

    def test_older_worker_pause_cannot_replace_current_local_pause(self):
        scope, channel = self.running()
        scope['_atomic_json'](scope['_auto_pause_path'](), {
            'paused': True, 'permit_id': 'permit', 'generation': 2})
        channel.incoming.append((1, self.control(scope, runtime={
            'limited_auto_pause.json': {'paused': True, 'permit_id': 'permit', 'generation': 1}})))
        scope['poll_commands']()
        command = {'auto_permit': {'permit_id': 'permit', 'generation': 2}}
        self.assertIsNotNone(scope['_active_auto_pause'](command))

    def test_outbound_batch_shrinks_to_mailbox_capacity(self):
        scope, channel = self.running()
        channel.incoming.append((1, self.control(scope)))
        scope['poll_commands']()
        channel.max_payload_size = 32712
        for index in range(8):
            scope['_write_event']({'type': 'ERROR_EVENT', 'error_message': 'x' * 10000})
        scope['poll_commands']()
        self.assertLessEqual(len(scope['_canonical'](channel.sent[-1])), channel.max_payload_size)
        self.assertTrue(scope['_pending_events'])
        self.assertFalse(scope['_faulted'])

    def execute_fixture(self):
        scope, channel = self.running()
        channel.incoming.append((1, self.control(scope)))
        scope['poll_commands']()
        scope['_validate_execute'] = lambda command: None
        scope['_mapping_for'] = lambda command: (
            {'strategy_name': 'Test'}, {'op_type': 23, 'order_type': 1101, 'price_type': 11})
        scope['passorder'] = mock.Mock()
        command = {'client_order_key': 'client', 'intent_id': 'intent',
                   'execution_mode': 'SIM_SIGNAL', 'qmt_symbol': '600000.SH',
                   'resolved_order': {'volume': 100, 'limit_price': 10}}
        scope['_execute_order'](command, 'message')
        grant = {'client_order_key': 'client', 'intent_id': 'intent',
                 'worker_session': 'worker-1', 'adapter_session': scope['_adapter_session'],
                 'command_digest': hashlib.sha256(scope['_canonical'](command)).hexdigest(),
                 'state': 'PRE_SUBMIT',
                 'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat()}
        return scope, command, grant

    def test_order_waits_for_durable_grant_and_calls_native_once(self):
        scope, command, grant = self.execute_fixture()
        scope['passorder'].assert_not_called()
        self.assertEqual(scope['_pending_records'][-1]['kind'], 'PREPARE_EXECUTION')
        scope['_consume_grant'](grant)
        scope['_consume_grant'](grant)
        scope['_execute_order'](command, 'duplicate-message')
        scope['passorder'].assert_called_once()

    def test_expired_lease_or_wrong_grant_never_calls_native(self):
        for failure in ('lease', 'session', 'digest', 'expiry'):
            with self.subTest(failure=failure):
                scope, command, grant = self.execute_fixture()
                if failure == 'lease':
                    scope['_lease_deadline'] = 0
                elif failure == 'session':
                    grant['worker_session'] = 'old-worker'
                elif failure == 'digest':
                    grant['command_digest'] = 'changed-command'
                else:
                    grant['expires_at'] = '2000-01-01T00:00:00+00:00'
                scope['_consume_grant'](grant)
                scope['passorder'].assert_not_called()
                self.assertEqual(scope['_pending_events'][-1]['envelope']['payload']['status'], 'SUBMIT_UNKNOWN')

    def test_lease_expiring_during_risk_recheck_blocks_native_call(self):
        scope, command, grant = self.execute_fixture()
        scope['_validate_execute'] = lambda command: scope.update(_lease_deadline=0)
        scope['_consume_grant'](grant)
        scope['passorder'].assert_not_called()

    def test_denied_unreserved_order_does_not_emit_execution_journal(self):
        scope, command, grant = self.execute_fixture()
        before = len(scope['_pending_records'])
        grant.update(denied=True, state='SUBMIT_UNKNOWN', reason='MODE_MISMATCH')
        scope['_consume_grant'](grant)
        scope['passorder'].assert_not_called()
        self.assertFalse(any(record['kind'] == 'JOURNAL' for record in scope['_pending_records'][before:]))
        self.assertEqual(scope['_pending_events'][-1]['envelope']['payload']['status'], 'RISK_REJECTED')

    def test_worker_takeover_replacing_control_blocks_native_call(self):
        scope, command, grant = self.execute_fixture()
        scope['_channel'].guard_allowed = False
        scope['_consume_grant'](grant)
        scope['passorder'].assert_not_called()


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.incoming = []
        self.guard_allowed = True
    def send(self, value):
        self.sent.append(value)
        return True
    def peek(self):
        return self.incoming[0] if self.incoming else None
    def ack(self, sequence):
        self.incoming.pop(0)
        return True
    def close(self):
        pass
    def guard_incoming(self, sequence, callback):
        if not self.guard_allowed:
            return False
        callback()
        return True


if __name__ == '__main__':
    unittest.main()
