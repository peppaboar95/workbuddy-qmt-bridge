import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbuddy_qmt.bootstrap import initialize
from workbuddy_qmt.security import make_envelope
from workbuddy_qmt.util import iso_now
from workbuddy_qmt.worker import build_runtime


class EventAckRecoveryTests(unittest.TestCase):
    def test_uncertain_execution_ack_preserves_known_broker_state(self):
        with tempfile.TemporaryDirectory() as root:
            config, database, core = build_runtime(initialize(root, {'STOCK'}))
            account = config.account('main_stock')
            now = iso_now()
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    'INSERT INTO trade_previews(preview_id,account_alias,request_json,result_json,snapshot_fingerprint,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                    ('p', account.alias, '{}', '{}', 'fingerprint', now, now))
                connection.execute(
                    'INSERT INTO trade_intents(intent_id,intent_revision,preview_id,client_order_key,account_alias,status,action,symbol,requested_volume,limit_price,execution_mode,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('i', 1, 'p', 'key', account.alias, 'ORDER_ACCEPTED', 'BUY', '600000.SH', 100, 10, 'SIM_SIGNAL', now, now, '{}'))
            for current in ('ORDER_ACCEPTED', 'PARTIALLY_FILLED', 'FILLED', 'SUBMIT_UNKNOWN'):
                for incoming in ('SUBMIT_UNKNOWN', 'SUBMIT_CALLED'):
                    with self.subTest(current=current, incoming=incoming):
                        with database.transaction(immediate=True) as connection:
                            connection.execute('UPDATE trade_intents SET status=? WHERE intent_id=?', (current, 'i'))
                        payload = {'type': 'COMMAND_ACK', 'account_alias': account.alias,
                                   'intent_id': 'i', 'status': incoming}
                        envelope = make_envelope(core.keyring, 'QMT_EVENT', payload, 60, 'test')
                        core.ingester.ingest(envelope, account)
                        self.assertEqual(core.get_trade_intent('i')['status'], current)
