"""Render the restricted QMT deployment from the existing trading engine."""

import ast
from pathlib import Path


REPLACED_FUNCTIONS = {
    '_atomic_json', '_load_json', '_ensure_dirs', '_refresh_config_mode',
    '_write_event', '_dead_letter', '_archive', '_execute_order',
    '_process_file', 'poll_commands', 'init', 'after_init',
}


def render_mmap_adapter(template, adapter_config, key_data, mapping_name, mapping_size):
    """Bundle a standalone Python 3.6 script without forbidden I/O imports.

    Keep one implementation of QMT field normalization and trading checks. Only
    transport/storage entry points are replaced; generated scripts need no
    access to this package or to the external configuration directory.
    """
    lines = template.splitlines(keepends=True)
    spans = []
    for node in ast.parse(template).body:
        if isinstance(node, ast.FunctionDef) and node.name in REPLACED_FUNCTIONS:
            spans.append((node.lineno - 1, node.end_lineno))
        elif isinstance(node, ast.Import) and any(a.name in {'os', 'uuid'} for a in node.names):
            spans.append((node.lineno - 1, node.end_lineno))
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    engine = ''.join(lines)
    marker = 'ADAPTER_CONFIG_PATH = r"D:\\workbuddy-qmt-bridge\\config\\qmt_adapter.json"'
    engine = engine.replace(marker, 'ADAPTER_CONFIG_PATH = "__mmap_config__"')
    engine = engine.replace('os.path.', '_memory.').replace('os.listdir(', '_memory.listdir(')
    engine = engine.replace('uuid.uuid4().hex', '_new_id()')
    if 'os.' in engine or 'open(' in engine:
        raise ValueError('unconverted QMT file operation in mmap deployment')
    assets = Path(__file__).with_name('assets')
    channel = (assets / 'mmap_channel.py').read_text(encoding='utf-8')
    runtime = (assets / 'qmt_mmap_runtime.py').read_text(encoding='utf-8')
    bootstrap = {
        'mapping_name': mapping_name, 'mapping_size': mapping_size,
        'account_alias': adapter_config['account_alias'],
        'account_type': adapter_config['account_type'],
        'adapter_instance': adapter_config['adapter_instance'],
        'config': adapter_config, 'key_data': key_data,
    }
    # The bootstrap is deployed with the strategy, never learned from an
    # unauthenticated mapping name. Its key is as sensitive as message_keys.json.
    source = '#coding:gbk\nMMAP_BOOTSTRAP = ' + ascii(bootstrap) + '\n'
    source += channel + '\n' + engine + '\n' + runtime
    ast.parse(source, feature_version=(3, 6))
    return source
