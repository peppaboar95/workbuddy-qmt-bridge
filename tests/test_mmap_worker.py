import threading
import io
import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

from workbuddy_qmt.worker import RuntimeLoop
from workbuddy_qmt import worker


class MmapWorkerTests(unittest.TestCase):
    def test_background_loop_pumps_configured_gateway(self):
        gateway = mock.Mock()
        core = SimpleNamespace(ingester=mock.Mock(), dispatch_pending_commands=mock.Mock(),
                               mmap_gateway=gateway)
        loop = RuntimeLoop(core)
        def pump():
            loop.stop_event.set()
            return {'errors': {}}
        gateway.poll_once.side_effect = pump
        # A finite fallback also makes an implementation without the pump fail
        # deterministically instead of leaving a test thread running forever.
        core.dispatch_pending_commands.side_effect = lambda: threading.Timer(0.05, loop.stop_event.set).start()
        loop.run()
        gateway.poll_once.assert_called_once()

    def test_bind_failure_does_not_take_over_existing_worker_mapping(self):
        config = SimpleNamespace(host='127.0.0.1', port=17642,
                                 worker_token_file='token', max_message_bytes=65536,
                                 accounts={'a': SimpleNamespace(enabled=True, adapter_transport='mmap')})
        core = SimpleNamespace(mmap_gateway=None)
        with mock.patch.object(worker, 'build_runtime', return_value=(config, mock.Mock(), core)), \
                mock.patch('builtins.open', mock.mock_open(read_data='x' * 32)), \
                mock.patch.object(worker, 'ThreadingHTTPServer', side_effect=OSError('address in use')), \
                mock.patch.object(worker, 'RuntimeLoop') as runtime_loop, \
                mock.patch('workbuddy_qmt.mmap_gateway.MmapGateway') as gateway:
            with self.assertRaisesRegex(OSError, 'address in use'):
                worker.main(['--config', 'bridge.json'])
            gateway.assert_not_called()
            runtime_loop.assert_not_called()

    def test_background_gateway_error_is_reported_without_private_message(self):
        gateway = mock.Mock()
        core = SimpleNamespace(ingester=mock.Mock(), dispatch_pending_commands=mock.Mock(),
                               mmap_gateway=gateway)
        loop = RuntimeLoop(core)
        def poll():
            loop.stop_event.set()
            return {'errors': {'account': {'code': 'MMAP_TEST_ERROR', 'message': 'private payload'}}}
        gateway.poll_once.side_effect = poll
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            loop.run()
        self.assertIn('MMAP_TEST_ERROR', output.getvalue())
        self.assertNotIn('private payload', output.getvalue())
