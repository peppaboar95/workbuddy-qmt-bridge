"""Restricted QMT mmap bindings. Bundled after the common trading engine."""


class _MemoryState:
    def __init__(self):
        self.values = {}

    @staticmethod
    def join(*parts):
        return '/'.join(str(p).replace('\\', '/').strip('/') for p in parts)

    def exists(self, path):
        return self.join(path) in self.values

    def listdir(self, path):
        prefix = self.join(path) + '/'
        return [key[len(prefix):] for key in self.values
                if key.startswith(prefix) and '/' not in key[len(prefix):]]


_memory = _MemoryState()
_channel = None
_worker_session = None
_adapter_session = None
_lease_deadline = 0.0
_id_counter = 0
_pending_events = []
_pending_records = []
_pending_execute = {}
_executed_keys = set()
_faulted = False
_initialized_snapshots = False
_VALIDATING_CLIENT_KEY = None
_local_pause_latch = None
_control_sequence = None


def _new_id():
    global _id_counter
    _id_counter += 1
    raw = repr((time.time(), time.perf_counter(), id(_memory), _id_counter))
    return hashlib.sha256(raw.encode('ascii')).hexdigest()[:32]


def _load_json(path):
    # Copy values to preserve the old read-from-storage ownership boundary.
    return json.loads(json.dumps(_memory.values[_memory.join(path)], allow_nan=False))


def _ensure_dirs():
    pass


def _atomic_json(path, value):
    global _local_pause_latch
    path = _memory.join(path)
    _memory.values[path] = json.loads(_canonical(value).decode('utf-8'))
    if '/execution_journal/' in path:
        _queue_record({'kind': 'JOURNAL', 'value': value})
    elif path.endswith('/limited_auto_pause.json'):
        _local_pause_latch = dict(value)
        _queue_record({'kind': 'PAUSE', 'value': value})
    elif path.endswith('/p0_probe.json'):
        _queue_record({'kind': 'P0_PROBE', 'value': value})
    # Credit query caches remain local to this QMT session and are recollected
    # after restart. Public snapshot events already carry the external copy.


def _queue_record(record):
    global _faulted
    if len(_pending_events) + len(_pending_records) >= 512:
        _faulted = True
        raise RuntimeError('mmap outbound backlog; trading disabled')
    _pending_records.append(record)


def _write_event(payload, folder='events'):
    payload = dict(payload)
    payload.setdefault('event_id', 'evt_' + _new_id())
    payload.setdefault('account_alias', _CONFIG['account_alias'])
    payload.setdefault('account_type', _CONFIG['account_type'])
    payload.setdefault('occurred_at', _iso())
    message = _envelope(payload)
    if len(_pending_events) + len(_pending_records) >= 512:
        global _faulted
        _faulted = True
        raise RuntimeError('mmap outbound backlog; trading disabled')
    _pending_events.append({'folder': folder, 'envelope': message})
    return message['message_id']


def _link_live():
    return bool(_worker_session and not _faulted and time.monotonic() < _lease_deadline)


def _refresh_config_mode():
    return _link_live()


_base_local_halted = _local_halted
def _local_halted():
    return not _link_live() or _base_local_halted()


_base_active_auto_pause = _active_auto_pause
def _active_auto_pause(command=None):
    if _local_pause_latch and _local_pause_latch.get('paused') is True:
        if command is not None:
            permit = command.get('auto_permit') or {}
        else:
            path = _memory.join(_runtime(), 'local_authorization.json')
            if not _memory.exists(path):
                return _local_pause_latch
            message = _load_json(path)
            _verify(message, set(['LOCAL_AUTHORIZATION']))
            permit = message.get('payload', {}).get('auto_permit') or {}
        if (_local_pause_latch.get('permit_id') == permit.get('permit_id') and
                _local_pause_latch.get('generation') == permit.get('generation')):
            return _local_pause_latch
    return _base_active_auto_pause(command)


_base_auto_journal_usage = _auto_journal_usage
def _auto_journal_usage(permit_id):
    # The durable grant already counts this order. Original validation adds the
    # proposed order once, so exclude its own record during the final recheck.
    path = _journal_path(_VALIDATING_CLIENT_KEY) if _VALIDATING_CLIENT_KEY else None
    saved = _memory.values.pop(_memory.join(path), None) if path else None
    try:
        return _base_auto_journal_usage(permit_id)
    finally:
        if saved is not None:
            _memory.values[_memory.join(path)] = saved


def _check_execute(command):
    global _VALIDATING_CLIENT_KEY
    if not _link_live():
        raise RuntimeError('mmap control lease is not live')
    _VALIDATING_CLIENT_KEY = command['client_order_key']
    try:
        _validate_execute(command)
    finally:
        _VALIDATING_CLIENT_KEY = None


def _execute_order(command, message_id):
    key = command['client_order_key']
    if key in _executed_keys or key in _pending_execute or _memory.exists(_journal_path(key)):
        _ack(command, 'SUBMIT_UNKNOWN', message_id, {'reason': 'execution already reserved; reconcile'})
        return
    _check_execute(command)
    _pending_execute[key] = (command, message_id)
    _queue_record({'kind': 'PREPARE_EXECUTION', 'command': command, 'message_id': message_id})


def _consume_grant(grant):
    key = grant.get('client_order_key')
    pending = _pending_execute.pop(key, None)
    if pending is None or key in _executed_keys:
        return
    command, message_id = pending
    _executed_keys.add(key)  # Set before any call which could enter native QMT.
    try:
        if (grant.get('worker_session') != _worker_session or
                grant.get('adapter_session') != _adapter_session or
                grant.get('command_digest') != hashlib.sha256(_canonical(command)).hexdigest()):
            raise RuntimeError('invalid or expired execution grant')
        if grant.get('denied') is True:
            # A rejected request may never have obtained PRE_SUBMIT. Do not
            # manufacture journal evidence for a reservation which does not exist.
            status = 'SUBMIT_UNKNOWN' if grant.get('reason') == 'EXECUTION_UNCERTAIN' else 'RISK_REJECTED'
            _ack(command, status, message_id, {'reason': grant.get('reason')})
            return
        if grant.get('state') != 'PRE_SUBMIT' or _parse_time(grant['expires_at']) <= _now():
            raise RuntimeError('invalid or expired execution grant')
        _check_execute(command)
        if command['execution_mode'] == 'OBSERVE_ONLY':
            _write_journal(command, 'OBSERVE_ONLY_ACKNOWLEDGED')
            _ack(command, 'OBSERVE_ONLY_ACKNOWLEDGED', message_id)
            return
        profile, mapping = _mapping_for(command)
        order = command['resolved_order']
        def submit():
            # No channel operations in this callback: guard_incoming holds the
            # cross-process mutex through the native boundary. Worker startup
            # replacement must happen entirely before or after this call.
            if not _link_live() or _parse_time(grant['expires_at']) <= _now():
                raise RuntimeError('execution grant expired during final risk checks')
            _qmt_function('passorder')(
                mapping['op_type'], mapping['order_type'], _CONFIG['qmt_account_id'],
                command['qmt_symbol'], mapping['price_type'], order['limit_price'],
                order['volume'], profile.get('strategy_name', 'WorkBuddyQMT'), 2,
                key, _CONTEXT,
            )
        if not _channel.guard_incoming(_control_sequence, submit):
            raise RuntimeError('Worker replaced the execution control frame')
    except Exception as exc:
        _write_journal(command, 'SUBMIT_UNKNOWN', {'error': str(exc)[:200]})
        _pause_auto_locally(command, 'mmap execution result requires reconciliation')
        _request_order_deal_reconcile()
        _ack(command, 'SUBMIT_UNKNOWN', message_id, {'error': str(exc)[:200]})
        return
    _write_journal(command, 'SUBMIT_CALLED')
    _ack(command, 'SUBMIT_CALLED', message_id)


def _apply_control(frame):
    global _worker_session, _lease_deadline, _STATUS, _initialized_snapshots
    if frame.get('kind') == 'WORKER_RESET':
        if frame.get('account_alias') != MMAP_BOOTSTRAP['account_alias']:
            raise RuntimeError('mmap reset account mismatch')
        _worker_session = None
        _lease_deadline = 0.0
        _pending_execute.clear()
        _pending_records[:] = [record for record in _pending_records
                               if record.get('kind') in ('PAUSE', 'P0_PROBE')]
        _initialized_snapshots = False
        _STATUS = 'RECOVERING'
        return False
    if (frame.get('adapter_session') != _adapter_session or
            frame.get('account_alias') != MMAP_BOOTSTRAP['account_alias']):
        return False
    expires = _parse_time(frame['expires_at'])
    remaining = (expires - _now()).total_seconds()
    if remaining <= 0 or remaining > 10:
        raise RuntimeError('invalid mmap control lifetime')
    session = frame['worker_session']
    if _worker_session is not None and session != _worker_session:
        _pending_execute.clear()
        _initialized_snapshots = False
    _worker_session = session
    _lease_deadline = time.monotonic() + min(remaining, 3.0)
    config = frame['config']
    if config.get('qmt_mode') not in ('OBSERVE_ONLY', 'SIM_SIGNAL', 'MANUAL_LIVE', 'LIMITED_AUTO'):
        raise RuntimeError('invalid qmt_mode')
    if any(config.get(name) != MMAP_BOOTSTRAP['config'].get(name)
           for name in ('account_alias', 'account_type', 'adapter_instance', 'qmt_account_id', 'strategy_name')):
        raise RuntimeError('mmap account binding changed')
    source = dict(config)
    source.pop('qmt_mode', None)
    expected = dict(MMAP_BOOTSTRAP['config'])
    expected.pop('qmt_mode', None)
    if source != expected:
        raise RuntimeError('adapter settings changed; redeploy mmap strategy')
    _memory.values[_memory.join(ADAPTER_CONFIG_PATH)] = config
    _CONFIG['qmt_mode'] = config['qmt_mode']
    # Replace authoritative control values, including deletions, without
    # erasing session-local credit snapshots or unpublished journal results.
    # Preserve an immediate QMT-side pause even before Worker has received its
    # PAUSE record. A newer authorization generation makes old pauses inert.
    for name in ('local_authorization.json', 'local_halt.json'):
        _memory.values.pop(_memory.join(_runtime(), name), None)
    for name, value in frame.get('runtime', {}).items():
        if name not in ('local_authorization.json', 'local_halt.json', 'limited_auto_pause.json'):
            raise RuntimeError('unknown mmap control state')
        _memory.values[_memory.join(_runtime(), name)] = value
    _memory.values[_memory.join(_CONFIG['mapping_profile'])] = frame['profile']
    if _CONFIG['qmt_mode'] != 'OBSERVE_ONLY':
        _load_verified_profile()
    for value in frame.get('journals', []):
        _memory.values[_memory.join(_journal_path(value['client_order_key']))] = value
    if not _initialized_snapshots:
        _emit_snapshots(_CONTEXT)
        _mark_order_deal_reconciled()
        _write_p0_probe(_CONTEXT)
        _initialized_snapshots = True
    _STATUS = 'READY'
    return True


def poll_commands(C=None):
    global _LAST_SCAN, _STATUS, _faulted, _lease_deadline, _control_sequence
    if _channel is None:
        return
    try:
        incoming = _channel.peek()
        if incoming:
            sequence, frame = incoming
            _control_sequence = sequence
            if _apply_control(frame):
                for grant in frame.get('grants', []):
                    _consume_grant(grant)
                for envelope in frame.get('commands', []):
                    try:
                        _verify(envelope, set(['TRADE_INTENT', 'CANCEL_ORDER', 'REQUEST_SYNC', 'CREDIT_PRECHECK']))
                        command = envelope['payload']
                        kind = command.get('type')
                        if not _link_live():
                            raise RuntimeError('mmap lease is not live')
                        if kind == 'EXECUTE_ORDER':
                            _execute_order(command, envelope['message_id'])
                        elif kind == 'CANCEL_ORDER':
                            if _CONFIG.get('qmt_mode') == 'OBSERVE_ONLY':
                                raise RuntimeError('cancellation is disabled in OBSERVE_ONLY')
                            _cancel_order(command, envelope['message_id'])
                        elif kind == 'REQUEST_SYNC':
                            _request_sync(command, envelope['message_id'])
                        elif kind == 'CREDIT_PRECHECK':
                            _credit_precheck(command, envelope['message_id'])
                        else:
                            raise RuntimeError('unknown command type')
                    except Exception as exc:
                        _ack(envelope.get('payload', {}), 'QMT_REJECTED', envelope.get('message_id'), {'error': str(exc)[:200]})
                    _queue_record({'kind': 'RECEIVED', 'message_id': envelope['message_id']})
            _channel.ack(sequence)
        if not _link_live():
            _STATUS = 'RECOVERING'
        # Keep batches small; retain data until the authenticated channel has
        # accepted it. The channel itself retains the batch until Worker ACK.
        events = _pending_events[:8] if _worker_session else []
        records = _pending_records[:8] if _worker_session else []
        outgoing = {
            'account_alias': MMAP_BOOTSTRAP['account_alias'],
            'adapter_session': _adapter_session,
            'worker_session': _worker_session,
            'events': events, 'records': records,
        }
        capacity = getattr(_channel, 'max_payload_size', MMAP_BOOTSTRAP['mapping_size'] // 2 - 56)
        while len(_canonical(outgoing)) > capacity and len(events) + len(records) > 1:
            if events:
                events.pop()
            else:
                records.pop()
        if len(_canonical(outgoing)) > capacity:
            raise RuntimeError('one mmap record exceeds mailbox capacity')
        if _channel.send(outgoing):
            del _pending_events[:len(events)]
            del _pending_records[:len(records)]
        _LAST_SCAN = _iso()
    except Exception:
        _faulted = True
        _lease_deadline = 0.0
        _STATUS = 'ERROR'
        raise


def init(C):
    global _CONTEXT, _STATUS, _channel, _adapter_session
    _CONTEXT = C
    try:
        config = dict(MMAP_BOOTSTRAP['config'])
        config['qmt_mode'] = 'OBSERVE_ONLY'
        _memory.values[_memory.join(ADAPTER_CONFIG_PATH)] = config
        _memory.values[_memory.join(config['key_file'])] = MMAP_BOOTSTRAP['key_data']
        _load_config()
        _adapter_session = _new_id()
        lock_factory = globals().get('MMAP_LOCK_FACTORY')
        if not callable(lock_factory):
            raise RuntimeError('define MMAP_LOCK_FACTORY(name) using the verified interprocess lock')
        lock = lock_factory(MMAP_BOOTSTRAP['mapping_name'] + '_lock')
        _channel = MmapChannel(
            MMAP_BOOTSTRAP['mapping_name'], MMAP_BOOTSTRAP['mapping_size'],
            _KEYS[_ACTIVE_KEY], 'adapter', lock,
        )
        C.set_account(_CONFIG['qmt_account_id'])
        _STATUS = 'RECOVERING'
        _schedule(C, 'poll_commands', '%dnMilliSecond' % _CONFIG.get('command_poll_interval_ms', 500))
        _schedule(C, 'snapshot_task', '5nSecond')
        _schedule(C, 'order_deal_reconcile_task', '5nSecond')
        _schedule(C, 'heartbeat_task', '5nSecond')
        if _CONFIG['account_type'] == 'CREDIT':
            _schedule(C, 'credit_reference_task', '180nSecond')
            _schedule(C, 'credit_account_query_task', '30nSecond')
        poll_commands(C)
    except Exception:
        _STATUS = 'ERROR'
        raise


def after_init(C):
    # Initial snapshots wait for the authenticated Worker handshake.
    poll_commands(C)
