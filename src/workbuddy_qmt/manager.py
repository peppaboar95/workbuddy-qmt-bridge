import argparse
import ctypes
import datetime as dt
import hashlib
import json
import locale
import os
import platform
import re
import shutil
import socket
import sys
import urllib.error
import urllib.request
import zipfile

from . import __version__
from .bootstrap import initialize
from .config import load_config
from .console import run as run_console_command
from .errors import BridgeError
from .modes import RUN_MODES
from .security import KeyRing
from .util import atomic_write_bytes, atomic_write_json, new_id
from .worker import main as worker_main

LAUNCHER_VERSION = 1
MCP_SERVER_NAME = "qmt-bridge"
SAFE_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_QMT_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_.-]{4,64}$")
RESET_PROFILE_CONFIRMATION = "RESET-QMT-PROFILE"
DISABLE_ACCOUNT_CONFIRMATION = "DISABLE-ACCOUNT"
LATEST_RELEASE_API = "https://api.github.com/repos/peppaboar95/workbuddy-qmt-bridge/releases/latest"
LATEST_RELEASE_PAGE = "https://github.com/peppaboar95/workbuddy-qmt-bridge/releases/latest"


class ManagerError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _local_app_data():
    value = os.environ.get("LOCALAPPDATA")
    if value:
        return os.path.abspath(value)
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


def default_launcher_path():
    return os.path.join(_local_app_data(), "WorkBuddyQMTBridge", "launcher.json")


def default_mcp_path():
    return os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")


def default_runtime_root(cwd=None):
    cwd = os.path.abspath(cwd or os.getcwd())
    local = os.path.join(cwd, "runtime")
    if os.path.exists(os.path.join(local, "config", "bridge.json")):
        return local
    return os.path.join(_local_app_data(), "WorkBuddyQMTBridge", "runtime")


def _read_json_object(path, label, missing=None):
    if not os.path.exists(path):
        return {} if missing is None else missing
    try:
        with open(path, "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ManagerError("INVALID_JSON", "%s 不是有效 JSON: %s" % (label, exc), {"path": path})
    if not isinstance(value, dict):
        raise ManagerError("INVALID_JSON", "%s 顶层必须是 JSON 对象" % label, {"path": path})
    return value


def _backup(path):
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = "%s.bak.%s" % (path, timestamp)
    shutil.copy2(path, backup)
    return backup


def _pretty_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_changed(path, data, overwrite=False):
    path = os.path.abspath(path)
    backup = None
    if os.path.exists(path):
        with open(path, "rb") as stream:
            current = stream.read()
        if current == data:
            return {"path": path, "changed": False, "backup": None}
        if not overwrite:
            raise ManagerError("FILE_EXISTS", "文件已存在且内容不同", {"path": path})
        backup = _backup(path)
    atomic_write_bytes(path, data)
    return {"path": path, "changed": True, "backup": backup}


def validate_mcp_config(path):
    value = _read_json_object(os.path.abspath(path), "WorkBuddy MCP 配置")
    servers = value.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ManagerError("INVALID_MCP_CONFIG", "mcpServers 必须是 JSON 对象", {"path": path})
    return value


def mcp_server_config(config_path, python_executable=None):
    config_path = os.path.abspath(config_path)
    config = load_config(config_path)
    python_executable = os.path.abspath(python_executable or sys.executable)
    return {
        "command": python_executable,
        "args": [
            "-m", "workbuddy_qmt.mcp_server",
            "--config", config_path,
            "--endpoint", "http://%s:%d" % (config.host, config.port),
        ],
        "disabled": False,
    }


def merge_mcp_config(path, config_path, python_executable=None):
    path = os.path.abspath(path)
    value = validate_mcp_config(path)
    servers = dict(value.get("mcpServers", {}))
    servers[MCP_SERVER_NAME] = mcp_server_config(config_path, python_executable)
    updated = dict(value)
    updated["mcpServers"] = servers
    encoded = _pretty_json(updated)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                current_object = json.load(stream)
        except (OSError, ValueError) as exc:
            raise ManagerError("INVALID_JSON", "WorkBuddy MCP 配置无法读取: %s" % exc, {"path": path})
        if current_object == updated:
            return {"path": path, "changed": False, "backup": None}
        backup = _backup(path)
    else:
        backup = None
    atomic_write_bytes(path, encoded)
    return {"path": path, "changed": True, "backup": backup}


def save_launcher_state(path, state):
    path = os.path.abspath(path)
    value = dict(state)
    value["version"] = LAUNCHER_VERSION
    atomic_write_json(path, value)
    return path


def load_launcher_state(path=None):
    path = os.path.abspath(path or default_launcher_path())
    value = _read_json_object(path, "启动器配置", missing=None)
    if not value:
        raise ManagerError("SETUP_REQUIRED", "尚未完成首次配置，请先运行 workbuddy-qmt setup", {"path": path})
    if value.get("version") != LAUNCHER_VERSION:
        raise ManagerError("LAUNCHER_CONFIG_ERROR", "启动器配置版本不受支持", {"path": path})
    config_path = value.get("bridge_config")
    if not isinstance(config_path, str) or not os.path.isabs(config_path):
        raise ManagerError("LAUNCHER_CONFIG_ERROR", "启动器配置缺少 bridge_config", {"path": path})
    return value


def _runtime_root_from_config(config_path):
    return os.path.dirname(os.path.dirname(os.path.abspath(config_path)))


def _deployment_guide(account, account_dir, script_path, adapter_config_path, profile_path):
    lines = [
        "WorkBuddy-QMT Bridge - QMT 部署说明",
        "",
        "账户别名: %s" % account.alias,
        "账户类型: %s" % account.account_type,
        "",
        "请按顺序完成：",
        "1. 在大 QMT 中为本账户创建独立策略实例。",
        "2. 将 qmt_adapter.py 的完整内容复制到该策略编辑器；脚本中的配置路径已自动写好，不要手工修改。",
        "3. 保持 qmt_adapter.json 中的 qmt_mode=OBSERVE_ONLY。",
        "4. 确认策略绑定的是安装向导中填写的账户，然后手工启动策略。",
        "5. 启动桌面的“启动QMT桥接.cmd”，再运行“查看QMT桥接状态.cmd”。",
        "6. 看到 Worker 和本账户 Adapter 均已就绪，才算完成首次只读连接。",
        "",
        "文件位置：",
        "- 账户目录: %s" % account_dir,
        "- 策略源码: %s" % script_path,
        "- Adapter 配置: %s" % adapter_config_path,
        "- Profile: %s" % profile_path,
        "",
        "安全提示：默认 OBSERVE_ONLY 不会实际报单。完成目标 QMT/券商环境的 P0 验证前，不要切换到其他模式。",
        "不要分享本目录中的账户号、Profile、密钥路径或诊断数据。",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8-sig")


def _adapter_template_path():
    return os.path.join(os.path.dirname(__file__), "assets", "qmt_embedded_adapter.py")


def _active_key_id(key_file):
    value = _read_json_object(key_file, "消息密钥")
    key_id = value.get("active_key_id")
    if not isinstance(key_id, str) or not key_id:
        raise ManagerError("KEY_CONFIG_ERROR", "消息密钥缺少 active_key_id", {"path": key_file})
    return key_id


def _safe_alias(alias):
    if not isinstance(alias, str) or not SAFE_ALIAS_RE.fullmatch(alias) or alias in {".", ".."}:
        raise ManagerError("INVALID_ACCOUNT_ALIAS", "账户别名不能用于生成安全目录", {"alias": alias})
    return alias


def _base_adapter_config(config, account, account_id, mapping_profile):
    limits = config.risks(account.alias)
    strategy_name = "WorkBuddyQMT" if account.account_type == "STOCK" else "WorkBuddyQMTCredit"
    value = {
        "account_alias": account.alias,
        "account_type": account.account_type,
        "adapter_instance": account.adapter_instance,
        "qmt_account_id": str(account_id),
        "data_dir": os.path.abspath(config.data_dir),
        "key_file": os.path.abspath(config.key_file),
        "mapping_profile": os.path.abspath(mapping_profile),
        "qmt_mode": "OBSERVE_ONLY",
        "strategy_name": strategy_name,
        "expected_profile_id": "REPLACE_AFTER_P0",
        "expected_qmt_build": "REPLACE_AFTER_P0",
        "expected_broker_build": "REPLACE_AFTER_P0",
        "console_heartbeat_seconds": 30,
        "price_guard_max_quote_age_seconds": limits.max_quote_age_seconds,
        "p0_probe_enabled": False,
        "max_batch": 10,
        "command_poll_interval_ms": 500,
        "order_deal_reconcile_seconds": 30,
        "max_message_bytes": config.max_message_bytes,
        "adapter_max_volume": limits.max_order_volume,
        "adapter_max_notional": limits.max_order_notional,
        "adapter_max_auto_session_notional": min(
            limits.max_auto_session_notional, limits.max_daily_notional
        ),
        "adapter_max_auto_orders": limits.max_auto_orders,
        "adapter_min_auto_order_interval_seconds": limits.min_auto_order_interval_seconds,
        "adapter_max_auto_concurrent_orders": limits.max_auto_concurrent_orders,
        "adapter_max_auto_symbol_position_notional": limits.max_auto_symbol_position_notional,
        "adapter_max_auto_account_drawdown": limits.max_auto_account_drawdown,
        "allow_cancel_while_halted": True,
        "limited_auto_credit_enabled": account.limited_auto_credit_enabled,
        "order_status_map": {
            "48": "QUEUED", "49": "QUEUED", "50": "REPORTED",
            "51": "CANCEL_REQUESTED", "52": "CANCEL_REQUESTED",
            "53": "PARTIALLY_CANCELLED", "54": "CANCELLED",
            "55": "PARTIALLY_FILLED", "56": "FILLED", "57": "REJECTED",
        },
        "field_map": {},
    }
    if account.account_type == "CREDIT":
        value.update({
            "credit_query_wrapper_verified": False,
            "credit_final_check_verified": False,
            "credit_query_cooldown_seconds": limits.credit_query_cooldown_seconds,
            "credit_snapshot_max_age_seconds": limits.max_credit_snapshot_age_seconds,
            "minimum_maintenance_ratio": limits.min_maintenance_ratio,
            "eligibility_allowed_values": {},
            "credit_field_map": {
                "maintenance_ratio": ["m_dPerAssurescaleValue"],
                "total_asset": ["m_dBalance"],
                "net_asset": ["m_dAssureAsset"],
                "total_debt": ["m_dTotalDebt"],
                "available_cash": ["m_dAvailable"],
                "available_margin": ["m_dEnableBailBalance"],
                "margin_debt": ["m_dFinDebt"],
                "margin_max_quota": ["m_dFinMaxQuota"],
                "margin_available_quota": ["m_dFinEnableQuota"],
                "short_debt": ["m_dSloDebt"],
                "short_market_value": ["m_dSloMarketValue"],
                "short_max_quota": ["m_dSloMaxQuota"],
                "short_available_quota": ["m_dSloEnableQuota"],
            },
        })
    return value


def generate_qmt_bundle(
        config_path, account_alias, account_id, output_root=None, overwrite=False,
        reset_profile=False, reset_profile_confirmation=None):
    config_path = os.path.abspath(config_path)
    config = load_config(config_path)
    account = config.account(account_alias)
    alias = _safe_alias(account.alias)
    if reset_profile and reset_profile_confirmation != RESET_PROFILE_CONFIRMATION:
        raise ManagerError(
            "LOCAL_CONFIRMATION_REQUIRED",
            "重置 Profile 必须提供专用确认词 %s" % RESET_PROFILE_CONFIRMATION,
            {"account_alias": alias},
        )
    account_id = str(account_id).strip()
    if (not account_id or "REPLACE" in account_id.upper() or
            not SAFE_QMT_ACCOUNT_RE.fullmatch(account_id)):
        raise ManagerError(
            "INVALID_QMT_ACCOUNT",
            "QMT 账户号必须为 4-64 位 ASCII 字母、数字、点、下划线或连字符，不能包含空格、引号或 JSON 片段",
            {"account_alias": alias},
        )
    output_root = os.path.abspath(output_root or os.path.join(_runtime_root_from_config(config_path), "qmt_ready"))
    account_dir = os.path.join(output_root, alias)
    adapter_config_path = os.path.join(account_dir, "qmt_adapter.json")
    profile_path = os.path.join(account_dir, "qmt_profile.json")
    script_path = os.path.join(account_dir, "qmt_adapter.py")
    guide_path = os.path.join(account_dir, "部署说明.txt")
    strategy_name = "WorkBuddyQMT" if account.account_type == "STOCK" else "WorkBuddyQMTCredit"
    profile = {
        "protocol_version": "1.0",
        "profile_id": "REPLACE_AFTER_P0",
        "qmt_build": "REPLACE_AFTER_P0",
        "broker_build": "REPLACE_AFTER_P0",
        "account_type": account.account_type,
        "strategy_name": strategy_name,
        "adapter_binding": {
            "account_alias": account.alias,
            "account_type": account.account_type,
            "adapter_instance": account.adapter_instance,
            "qmt_account_id": account_id,
            "strategy_name": strategy_name,
        },
        "verified": False,
        "key_id": _active_key_id(config.key_file),
        "mappings": {},
        "signature": "",
    }
    adapter_config = _base_adapter_config(config, account, account_id, profile_path)
    template_path = _adapter_template_path()
    try:
        with open(template_path, "r", encoding="utf-8") as stream:
            template = stream.read()
    except OSError as exc:
        raise ManagerError("ASSET_MISSING", "缺少 QMT Adapter 模板: %s" % exc, {"path": template_path})
    marker = 'ADAPTER_CONFIG_PATH = r"D:\\workbuddy-qmt-bridge\\config\\qmt_adapter.json"'
    if template.count(marker) != 1:
        raise ManagerError("ASSET_INVALID", "QMT Adapter 模板中的配置路径标记不唯一", {"path": template_path})
    path_literal = json.dumps(adapter_config_path, ensure_ascii=True)
    script = template.replace(marker, "ADAPTER_CONFIG_PATH = %s" % path_literal)
    try:
        script_bytes = script.encode("gbk")
    except UnicodeEncodeError:
        raise ManagerError(
            "QMT_PATH_ENCODING_ERROR",
            "QMT Adapter 模板包含无法使用 GBK 编码的内容",
            {"path": adapter_config_path},
        )
    generated_outputs = {
        adapter_config_path: _pretty_json(adapter_config),
        script_path: script_bytes,
        guide_path: _deployment_guide(account, account_dir, script_path, adapter_config_path, profile_path),
    }
    profile_bytes = _pretty_json(profile)
    profile_exists = os.path.exists(profile_path)
    if not profile_exists or reset_profile:
        generated_outputs[profile_path] = profile_bytes
    for path, data in generated_outputs.items():
        if os.path.exists(path):
            with open(path, "rb") as stream:
                different = stream.read() != data
            profile_reset_allowed = path == profile_path and reset_profile
            if different and not overwrite and not profile_reset_allowed:
                raise ManagerError("FILE_EXISTS", "QMT 生成文件已存在且内容不同", {"path": path})
    result_by_path = {}
    for path, data in generated_outputs.items():
        allow_overwrite = overwrite or (path == profile_path and reset_profile)
        result_by_path[path] = _write_changed(path, data, overwrite=allow_overwrite)
    if profile_exists and not reset_profile:
        result_by_path[profile_path] = {
            "path": profile_path, "changed": False, "backup": None, "preserved": True,
        }
    results = [result_by_path[path] for path in (adapter_config_path, profile_path, script_path, guide_path)]
    return {
        "account_alias": alias,
        "account_type": account.account_type,
        "directory": account_dir,
        "adapter_config": adapter_config_path,
        "mapping_profile": profile_path,
        "adapter_script": script_path,
        "deployment_guide": guide_path,
        "profile_preserved": profile_exists and not reset_profile,
        "profile_reset": profile_exists and reset_profile,
        "files": results,
    }


def _adapter_sync_issues(config, account, script_path, adapter_path, profile_path):
    issues = []
    for path, label in ((script_path, "qmt_adapter.py"), (adapter_path, "qmt_adapter.json"),
                        (profile_path, "qmt_profile.json")):
        if not os.path.isfile(path):
            issues.append("缺少 %s" % label)
    if issues:
        return issues
    try:
        adapter = _read_json_object(adapter_path, "QMT Adapter 配置")
        profile = _read_json_object(profile_path, "QMT mapping profile")
    except ManagerError as exc:
        return [exc.message]

    expected = _base_adapter_config(
        config, account, adapter.get("qmt_account_id", ""), profile_path,
    )
    sync_fields = (
        "account_alias", "account_type", "adapter_instance", "data_dir", "key_file",
        "mapping_profile", "strategy_name", "price_guard_max_quote_age_seconds",
        "command_poll_interval_ms", "order_deal_reconcile_seconds", "max_message_bytes",
        "adapter_max_volume", "adapter_max_notional",
        "limited_auto_credit_enabled",
        "adapter_max_auto_session_notional", "adapter_max_auto_orders",
        "adapter_min_auto_order_interval_seconds", "adapter_max_auto_concurrent_orders",
        "adapter_max_auto_symbol_position_notional", "adapter_max_auto_account_drawdown",
    )
    if account.account_type == "CREDIT":
        sync_fields += (
            "credit_query_cooldown_seconds", "credit_snapshot_max_age_seconds",
            "minimum_maintenance_ratio",
        )
    drift = [name for name in sync_fields if adapter.get(name) != expected.get(name)]
    if drift:
        issues.append("与 bridge.json 不同步: %s" % ", ".join(drift))

    try:
        with open(script_path, "r", encoding="gbk") as stream:
            script = stream.read()
        expected_marker = "ADAPTER_CONFIG_PATH = %s" % json.dumps(os.path.abspath(adapter_path), ensure_ascii=True)
        if script.count(expected_marker) != 1:
            issues.append("qmt_adapter.py 未绑定当前 qmt_adapter.json 绝对路径")
    except (OSError, UnicodeError) as exc:
        issues.append("qmt_adapter.py 无法按 GBK 读取: %s" % exc)

    issues.extend(_adapter_profile_issues(config, adapter, profile))
    return issues


def _adapter_profile_issues(config, adapter, profile):
    issues = []
    expected_profile_fields = {
        "profile_id": adapter.get("expected_profile_id"),
        "qmt_build": adapter.get("expected_qmt_build"),
        "broker_build": adapter.get("expected_broker_build"),
    }
    missing_expected = [
        "expected_%s" % name for name, value in expected_profile_fields.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing_expected:
        issues.append("Adapter 缺少 Profile 绑定字段: %s" % ", ".join(missing_expected))
    for profile_name, expected_value in expected_profile_fields.items():
        if expected_value is not None and profile.get(profile_name) != expected_value:
            issues.append("Profile %s 与 Adapter 期望值不一致" % profile_name)
    expected_binding = {
        "account_alias": adapter.get("account_alias"),
        "account_type": adapter.get("account_type"),
        "adapter_instance": adapter.get("adapter_instance"),
        "qmt_account_id": adapter.get("qmt_account_id"),
        "strategy_name": adapter.get("strategy_name"),
    }
    if profile.get("adapter_binding") != expected_binding:
        issues.append("Profile 未绑定当前账户和 Adapter 实例")
    if profile.get("account_type") != adapter.get("account_type"):
        issues.append("Profile account_type 与 Adapter 不一致")
    if profile.get("strategy_name") != adapter.get("strategy_name"):
        issues.append("Profile strategy_name 与 Adapter 不一致")

    if adapter.get("qmt_mode") != "OBSERVE_ONLY":
        placeholders = [
            name for name, value in expected_profile_fields.items()
            if not isinstance(value, str) or not value.strip() or "REPLACE" in value.upper()
        ]
        if placeholders:
            issues.append("非观察模式仍含 P0 绑定占位符: %s" % ", ".join(placeholders))
        if profile.get("verified") is not True or not isinstance(profile.get("mappings"), dict) or not profile.get("mappings"):
            issues.append("非观察模式需要 verified=true 且 mappings 非空的 Profile")
        else:
            try:
                KeyRing.load(config.key_file).verify(profile)
            except Exception as exc:
                issues.append("Profile 签名校验失败: %s" % exc)
    return issues


def sync_adapter_modes(config_path, mode):
    if mode not in RUN_MODES:
        raise ManagerError("INVALID_MODE_SELECTION", "无效运行模式")
    config_path = os.path.abspath(config_path)
    config = load_config(config_path)
    ready_root = os.path.join(_runtime_root_from_config(config_path), "qmt_ready")
    pending = []
    results = []
    # Validate every enabled account before changing any on-disk mode.
    for account in config.accounts.values():
        if not account.enabled:
            continue
        path = os.path.join(ready_root, _safe_alias(account.alias), "qmt_adapter.json")
        if not os.path.isfile(path):
            if mode == "OBSERVE_ONLY":
                results.append({"account_alias": account.alias, "path": path, "changed": False, "missing": True})
                continue
            raise ManagerError(
                "ADAPTER_SYNC_FAILED", "缺少 Adapter 配置，请先配置该账户",
                {"account_alias": account.alias, "path": path},
            )
        with open(path, "rb") as stream:
            original = stream.read()
        try:
            adapter = json.loads(original)
        except (ValueError, UnicodeError) as exc:
            raise ManagerError("INVALID_JSON", "QMT Adapter 配置不是有效 JSON: %s" % exc, {"path": path})
        if not isinstance(adapter, dict):
            raise ManagerError("INVALID_JSON", "QMT Adapter 配置顶层必须是 JSON 对象", {"path": path})
        expected = {
            "account_alias": account.alias,
            "account_type": account.account_type,
            "adapter_instance": account.adapter_instance,
            "data_dir": os.path.abspath(config.data_dir),
            "key_file": os.path.abspath(config.key_file),
        }
        drift = [name for name, value in expected.items() if adapter.get(name) != value]
        if drift:
            raise ManagerError(
                "ADAPTER_SYNC_FAILED", "Adapter 账户或运行目录绑定不一致，未同步模式",
                {"account_alias": account.alias, "path": path, "fields": drift},
            )
        updated = dict(adapter, qmt_mode=mode)
        if mode != "OBSERVE_ONLY":
            profile_path = adapter.get("mapping_profile")
            if not isinstance(profile_path, str) or not os.path.isabs(profile_path) or not os.path.isfile(profile_path):
                raise ManagerError("ADAPTER_SYNC_FAILED", "缺少本机 Profile 文件", {"account_alias": account.alias, "path": path})
            profile = _read_json_object(profile_path, "QMT mapping profile")
            issues = _adapter_profile_issues(config, updated, profile)
            if issues:
                raise ManagerError(
                    "ADAPTER_SYNC_FAILED", "Adapter 交易模式同步前的 Profile 校验未通过",
                    {"account_alias": account.alias, "path": path, "issues": issues},
                )
        result = {"account_alias": account.alias, "path": path, "changed": adapter.get("qmt_mode") != mode, "backup": None}
        results.append(result)
        if result["changed"]:
            pending.append((path, original, _pretty_json(updated), result))
    attempted = []
    try:
        for path, original, encoded, result in pending:
            result["backup"] = _backup(path)
            attempted.append((path, original))
            atomic_write_bytes(path, encoded)
    except OSError as exc:
        rollback_errors = []
        for path, original in reversed(attempted):
            try:
                atomic_write_bytes(path, original)
            except OSError as rollback:
                rollback_errors.append({"path": path, "error": str(rollback)})
        raise ManagerError(
            "ADAPTER_SYNC_FAILED", "Adapter 配置同步失败，Worker 未启动: %s" % exc,
            {"rollback_errors": rollback_errors},
        )
    return results


def infer_qmt_account_id(config_path, account):
    runtime_root = _runtime_root_from_config(config_path)
    candidates = [
        os.path.join(runtime_root, "qmt_ready", account.alias, "qmt_adapter.json"),
        os.path.join(runtime_root, "config", "qmt_adapter.%s.json" % account.alias),
        os.path.join(runtime_root, "config", "qmt_adapter.%s.json" % account.account_type.lower()),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            value = _read_json_object(path, "QMT Adapter 配置")
        except ManagerError:
            continue
        account_id = value.get("qmt_account_id")
        if account_id:
            account_id = str(account_id).strip()
            if "REPLACE" not in account_id.upper() and SAFE_QMT_ACCOUNT_RE.fullmatch(account_id):
                return account_id
    return ""


def _require_worker_stopped(config_path):
    probe = probe_worker(config_path)
    if probe.get("state") == "RUNNING":
        raise ManagerError(
            "WORKER_RUNNING",
            "修改账户配置前必须先停止 Worker；QMT 策略也应同时停止",
            {"config": os.path.abspath(config_path)},
        )
    if probe.get("state") == "CONFLICT":
        raise ManagerError(
            "PORT_CONFLICT",
            "Worker 端口被其他进程占用，确认并关闭相关进程后再修改账户配置",
            {"config": os.path.abspath(config_path)},
        )


def set_account_enabled(config_path, account_alias, enabled):
    config_path = os.path.abspath(config_path)
    config = load_config(config_path)
    account = config.accounts.get(account_alias)
    if not account:
        raise ManagerError("UNKNOWN_ACCOUNT", "Bridge 配置中找不到账户", {"account_alias": account_alias})
    if account.enabled == bool(enabled):
        return {"changed": False, "backup": None, "account_alias": account.alias, "enabled": account.enabled}
    raw = _read_json_object(config_path, "Bridge 配置")
    for item in raw.get("accounts", []):
        if item.get("alias") == account.alias:
            item["enabled"] = bool(enabled)
            break
    else:
        raise ManagerError("UNKNOWN_ACCOUNT", "Bridge 配置中找不到账户", {"account_alias": account_alias})
    backup = _backup(config_path)
    try:
        atomic_write_json(config_path, raw)
        load_config(config_path)
        initialize(_runtime_root_from_config(config_path), None)
    except Exception:
        shutil.copy2(backup, config_path)
        raise
    return {"changed": True, "backup": backup, "account_alias": account.alias, "enabled": bool(enabled)}


def _desktop_dir():
    if os.name == "nt":
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer)
            if result == 0 and buffer.value:
                return buffer.value
        except (AttributeError, OSError):
            pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def open_local_path(path):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise ManagerError("PATH_NOT_FOUND", "要打开的路径不存在", {"path": path})
    if os.name != "nt" or not hasattr(os, "startfile"):
        return {"path": path, "opened": False, "message": "当前系统不支持自动打开，请手工打开该路径"}
    try:
        os.startfile(path)
    except OSError as exc:
        raise ManagerError("OPEN_PATH_FAILED", "无法打开路径: %s" % exc, {"path": path})
    return {"path": path, "opened": True}


def _cmd_value(value):
    return str(value).replace("%", "%%").replace('"', '""')


def _remove_generated_shortcut(target_dir, name, markers, reason):
    path = os.path.join(target_dir, name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as stream:
        content = stream.read()
    if all(marker in content for marker in markers):
        os.remove(path)
        return {"path": path, "removed": True}
    return {"path": path, "removed": False, "reason": reason}


def create_shortcuts(launcher_path, target_dir=None, python_executable=None, overwrite=True):
    launcher_path = os.path.abspath(launcher_path)
    target_dir = os.path.abspath(target_dir or _desktop_dir())
    os.makedirs(target_dir, exist_ok=True)
    python_executable = os.path.abspath(python_executable or sys.executable)
    prefix = '@echo off\r\nchcp 65001 >nul\r\nset "PYTHONUTF8=1"\r\n'
    command = '"%s" -m workbuddy_qmt.manager --launcher-config "%s"' % (
        _cmd_value(python_executable), _cmd_value(launcher_path),
    )
    scripts = {
        "启动QMT桥接.cmd": (
            prefix + "title WorkBuddy QMT Bridge\r\n"
            "echo Choose the Worker mode first, then the bridge will start.\r\n"
            "echo Press Enter at the mode prompt to use the safe OBSERVE_ONLY default.\r\n"
            "echo This window must remain open while the Worker is running.\r\n"
            "echo.\r\n" + command + " start --human\r\n"
            "set EXIT_CODE=%ERRORLEVEL%\r\n"
            "echo.\r\n"
            "if not \"%EXIT_CODE%\"==\"0\" echo The bridge did not exit normally. Run the status script; it will diagnose problems automatically.\r\n"
            "pause\r\n"
        ),
        "查看QMT桥接状态.cmd": (
            prefix + "title WorkBuddy QMT Bridge Status\r\n"
            "echo Verifying the complete read-only bridge setup...\r\n"
            "echo.\r\n" + command + " verify --human\r\n"
            "set VERIFY_EXIT=%ERRORLEVEL%\r\n"
            "echo.\r\n"
            "echo Checking the current bridge status...\r\n"
            "echo Diagnostics will run automatically only when the status is abnormal.\r\n"
            "echo.\r\n" + command + " status --human\r\n"
            "set STATUS_EXIT=%ERRORLEVEL%\r\n"
            "echo.\r\npause\r\n"
            "if not \"%STATUS_EXIT%\"==\"0\" exit /b %STATUS_EXIT%\r\n"
            "exit /b %VERIFY_EXIT%\r\n"
        ),
    }
    results = []
    for name, content in scripts.items():
        encoding = "mbcs" if os.name == "nt" else locale.getpreferredencoding(False)
        try:
            encoded = content.encode(encoding)
        except UnicodeEncodeError:
            raise ManagerError(
                "SHORTCUT_PATH_ENCODING_ERROR",
                "Python 或启动器路径无法写入 Windows 批处理文件，请使用不含特殊字符的安装路径",
                {"python": python_executable, "launcher": launcher_path},
            )
        results.append(_write_changed(os.path.join(target_dir, name), encoded, overwrite=overwrite))
    obsolete_specs = (
        ("legacy_doctor", "诊断QMT桥接.cmd", (b"@echo off", b"workbuddy_qmt.manager", b" doctor"), "文件内容不是本项目生成的诊断脚本，已保留"),
        ("obsolete_mode", "设置QMT桥接运行模式.cmd", (b"@echo off", b"workbuddy_qmt.manager", b" mode --human"), "文件内容不是本项目生成的模式脚本，已保留"),
        ("obsolete_verify", "验证QMT桥接.cmd", (b"@echo off", b"workbuddy_qmt.manager", b" verify --human"), "文件内容不是本项目生成的验证脚本，已保留"),
        ("obsolete_open_qmt_ready", "打开QMT配置目录.cmd", (b"@echo off", b"workbuddy_qmt.manager", b" open qmt-ready --human"), "文件内容不是本项目生成的目录脚本，已保留"),
    )
    obsolete_results = {
        key: _remove_generated_shortcut(target_dir, name, markers, reason)
        for key, name, markers, reason in obsolete_specs
    }
    return {
        "directory": target_dir,
        "files": results,
        "legacy_doctor": obsolete_results["legacy_doctor"],
        "obsolete_mode": obsolete_results["obsolete_mode"],
        "obsolete_verify": obsolete_results["obsolete_verify"],
        "obsolete_open_qmt_ready": obsolete_results["obsolete_open_qmt_ready"],
    }


def _worker_token(config):
    try:
        with open(config.worker_token_file, "r", encoding="ascii") as stream:
            token = stream.read().strip()
    except OSError as exc:
        raise ManagerError("TOKEN_ERROR", "无法读取 Worker 令牌: %s" % exc, {"path": config.worker_token_file})
    if len(token) < 32:
        raise ManagerError("TOKEN_ERROR", "Worker 令牌长度不足", {"path": config.worker_token_file})
    return token


def _port_is_open(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_worker(config_path, timeout=0.75):
    config = load_config(config_path)
    if config.port < 1 or config.port > 65535:
        raise ManagerError("CONFIG_ERROR", "Worker 端口必须位于 1-65535", {"port": config.port})
    token = _worker_token(config)
    payload = json.dumps({
        "method": "qmt_health", "params": {}, "request_id": new_id("probe"),
    }).encode("utf-8")
    request = urllib.request.Request(
        "http://%s:%d/rpc" % (config.host, config.port), data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"state": "CONFLICT", "message": "端口响应了非当前 Worker 请求", "status": exc.code}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        if _port_is_open(config.host, config.port, timeout):
            return {"state": "CONFLICT", "message": "端口已被其他服务占用", "error": str(exc)}
        return {"state": "STOPPED", "message": "Worker 未运行"}
    if not isinstance(result, dict) or "ok" not in result or "request_id" not in result:
        return {"state": "CONFLICT", "message": "端口返回了未知协议"}
    return {"state": "RUNNING", "message": "Worker 正在运行", "response": result}


class _Tee:
    def __init__(self, console, log):
        self.console = console
        self.log = log
        self.encoding = getattr(console, "encoding", "utf-8")

    def write(self, value):
        self.console.write(value)
        self.log.write(value)
        return len(value)

    def flush(self):
        self.console.flush()
        self.log.flush()

    def isatty(self):
        return bool(getattr(self.console, "isatty", lambda: False)())


def resolve_bridge_config(launcher_path=None, explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    state = load_launcher_state(launcher_path)
    return os.path.abspath(state["bridge_config"])


START_MODE_SELECTIONS = {
    "1": "OBSERVE_ONLY",
    "2": "SIM_SIGNAL",
    "3": "MANUAL_LIVE",
    "4": "LIMITED_AUTO",
}


def _console(config_path, command, **values):
    arguments = {"config": config_path, "command": command}
    arguments.update(values)
    try:
        return run_console_command(argparse.Namespace(**arguments))
    except BridgeError as exc:
        raise ManagerError(exc.code, exc.message, exc.details)


def select_start_mode(config_path, requested=None, confirm=None, interactive=False):
    status = _console(config_path, "status")
    state = status.get("state", {})
    current = state.get("mode", load_config(config_path).default_mode)
    if state.get("halted") == "true":
        if requested is not None:
            raise ManagerError("LOCAL_CONFIRMATION_REQUIRED", "桥接处于熔断状态，请先解除熔断")
        if interactive:
            print("当前模式: %s；熔断状态保持开启，启动后仍不会投递交易" % current)
        sync_adapter_modes(config_path, "OBSERVE_ONLY")
        return current
    if interactive:
        _human_header("启动前选择 Worker 运行模式")
        print("当前运行模式: %s" % current)
        print("说明：bridge.json 的 default_mode 只决定数据库首次创建时的初始模式。")
        print("      本次选择会写入数据库，作为 Worker 实际运行模式。")
        if requested is None:
            print("\n可选择：")
            print("  1. OBSERVE_ONLY  观察和空跑闭环，不向 QMT 实际报单（默认）")
            print("  2. SIM_SIGNAL    仅用于已确认的模拟柜台，可实际调用 passorder")
            print("  3. MANUAL_LIVE   人工实盘，还需要短时授权和逐笔审批")
            print("  4. LIMITED_AUTO  P1 有限自动交易；启动后还需创建限时策略许可")
            print("直接按 Enter 使用安全默认值 OBSERVE_ONLY。")
            selected = input("请输入序号 [1]: ").strip() or "1"
            requested = START_MODE_SELECTIONS.get(selected)
            if requested is None:
                raise ManagerError("INVALID_MODE_SELECTION", "请输入 1、2、3、4，或直接按 Enter 使用 OBSERVE_ONLY")
        if requested == "SIM_SIGNAL" and confirm != requested:
            print("\n警告：该模式可能通过 QMT Adapter 实际调用 passorder。")
            confirmation = input("确认策略连接模拟账号/模拟柜台后，输入 SIM_SIGNAL: ").strip()
            if confirmation != requested:
                raise ManagerError("LOCAL_CONFIRMATION_REQUIRED", "确认文字不匹配，未修改运行模式")
            confirm = confirmation
        if requested == "MANUAL_LIVE" and confirm != requested:
            print("\n警告：这是人工实盘模式，仍需短时 LIVE 授权和逐笔审批。")
            confirmation = input("确认后输入 MANUAL_LIVE: ").strip()
            if confirmation != requested:
                raise ManagerError("LOCAL_CONFIRMATION_REQUIRED", "确认文字不匹配，未修改运行模式")
            confirm = confirmation
        if requested == "LIMITED_AUTO" and confirm != requested:
            print("\n警告：这是 P1 有限自动交易模式；没有签名策略许可时仍会失败关闭。")
            confirmation = input("确认后输入 LIMITED_AUTO: ").strip()
            if confirmation != requested:
                raise ManagerError("LOCAL_CONFIRMATION_REQUIRED", "确认文字不匹配，未修改运行模式")
            confirm = confirmation
    if requested is None:
        sync_adapter_modes(config_path, current)
        return current
    if requested not in RUN_MODES:
        raise ManagerError("INVALID_MODE_SELECTION", "无效运行模式")
    if requested != "OBSERVE_ONLY" and confirm != requested:
        raise ManagerError(
            "LOCAL_CONFIRMATION_REQUIRED",
            "启动 %s 前必须传入 --confirm %s" % (requested, requested),
        )
    sync_adapter_modes(config_path, requested)
    if requested == current:
        mode = current
    else:
        mode = _console(config_path, "set-mode", mode=requested, confirm=confirm)["mode"]
    if interactive:
        print("\n本次启动模式: %s" % mode)
        if requested == "SIM_SIGNAL":
            print("Adapter 模式已同步为 SIM_SIGNAL；仍须确认策略连接模拟柜台。")
        elif requested == "MANUAL_LIVE":
            print("注意：还必须单独创建短时 LIVE 授权，并对每笔预览进行本机审批。")
        elif requested == "LIMITED_AUTO":
            print("注意：还必须通过 WorkBuddy/MCP 创建有效的 P1 策略许可，并保持健康检查通过。")
        elif requested == "OBSERVE_ONLY":
            print("当前仅观察和空跑闭环，不会向 QMT 实际报单。")
    return mode


def _human_header(title):
    print("=" * 60)
    print(title)
    print("=" * 60)


def _seconds_text(value):
    if value is None:
        return "暂无数据"
    if value < 1:
        return "不到 1 秒"
    return "%d 秒" % int(value)


def print_status_human(report):
    _human_header("WorkBuddy-QMT 桥接状态")
    print("配置文件: %s" % report["config"])
    probe = report["worker"]
    state = probe["state"]
    print("Worker: %s" % {
        "RUNNING": "正在运行",
        "STOPPED": "未启动",
        "CONFLICT": "端口冲突或响应异常",
    }.get(state, state))
    if state == "STOPPED":
        print("\n当前结论：桥接服务没有运行，WorkBuddy 暂时无法访问 QMT 数据。")
        print("下一步：双击桌面的“启动QMT桥接.cmd”，并保持该窗口打开。")
        return
    if state == "CONFLICT":
        print("原因: %s" % probe.get("message", "本地端口不能由当前 Worker 使用"))
        print("\n状态异常：需要检查端口是否被其他程序或另一套 Worker 占用。")
        return
    response = probe.get("response") or {}
    if not response.get("ok"):
        print("\n当前结论：Worker 有响应，但健康检查未通过。")
        print("下一步：检查 Worker 日志和本机配置。")
        return
    health = response.get("data") or {}
    mode = health.get("mode", "UNKNOWN")
    print("交易模式: %s" % mode)
    if health.get("halted"):
        print("熔断状态: 已熔断；原因: %s" % (health.get("halt_reason") or "未记录"))
    elif mode == "OBSERVE_ONLY":
        print("安全说明: 当前仅观察和空跑闭环，不会向 QMT 实际报单。")
    print("\n账户与 QMT Adapter：")
    try:
        configured = load_config(report["config"])
        enabled = {item.alias: item.enabled for item in configured.accounts.values()}
    except Exception:
        enabled = {}
    enabled_ready = []
    seen_accounts = set()
    for account in health.get("accounts", []):
        alias = account.get("account_alias", "<unknown>")
        seen_accounts.add(alias)
        is_enabled = enabled.get(alias, True)
        if not is_enabled:
            print("  - %s (%s): 未启用，不要求启动 QMT Adapter" % (alias, account.get("account_type", "?")))
            continue
        ready = bool(account.get("ready"))
        enabled_ready.append(ready)
        queue_depths = account.get("queue_depths") or {}
        pending = sum(value for value in queue_depths.values() if isinstance(value, int))
        print("  - %s (%s): %s" % (
            alias,
            account.get("account_type", "?"),
            "已连接，可用" if ready else "未就绪",
        ))
        print("      Adapter 状态: %s；心跳距今: %s；队列文件: %d" % (
            account.get("adapter_status", "OFFLINE"),
            _seconds_text(account.get("heartbeat_age_seconds")),
            pending,
        ))
    for alias, is_enabled in enabled.items():
        if is_enabled and alias not in seen_accounts:
            enabled_ready.append(False)
            print("  - %s: Worker 未返回该已启用账户的状态" % alias)
    unresolved = int(health.get("unresolved_submit_unknown", 0) or 0)
    if unresolved:
        print("\n警告：存在 %d 笔 SUBMIT_UNKNOWN，启用交易前必须人工核对。" % unresolved)
    print("\n当前结论：")
    issues = report.get("issues") or []
    if issues:
        print("桥接状态异常：")
        for issue in issues:
            print("  - %s" % issue.get("message", issue.get("code", "未知异常")))
        if any(issue.get("code") == "ADAPTER_NOT_READY" for issue in issues):
            print("下一步：打开大 QMT，确认对应策略实例已经手工启动。")
    elif enabled_ready and all(enabled_ready):
        print("Worker 和所有已启用账户的 QMT Adapter 均已就绪。")
    elif enabled_ready:
        print("Worker 正常，但至少一个已启用账户的 QMT Adapter 未就绪。")
        print("下一步：打开大 QMT，确认对应策略实例已经手工启动。")
    else:
        print("Worker 正常，但当前没有需要检查的已启用账户。")


def _status_issues(config_path, probe):
    state = probe.get("state")
    if state != "RUNNING":
        return [{"code": state or "UNKNOWN", "message": probe.get("message", "Worker 状态异常")}]
    response = probe.get("response") or {}
    if not response.get("ok"):
        return [{"code": "HEALTH_CHECK_FAILED", "message": "Worker 健康检查未通过"}]
    health = response.get("data") or {}
    issues = []
    if health.get("worker_status") != "READY":
        issues.append({"code": "WORKER_NOT_READY", "message": "Worker 尚未就绪"})
    if health.get("halted"):
        issues.append({"code": "TRADING_HALTED", "message": health.get("halt_reason") or "桥接已熔断"})
    try:
        config = load_config(config_path)
        enabled_aliases = {item.alias for item in config.accounts.values() if item.enabled}
    except Exception as exc:
        issues.append({"code": "CONFIG_ERROR", "message": str(exc)})
        enabled_aliases = set()
    health_accounts = {
        item.get("account_alias"): item
        for item in health.get("accounts", [])
        if isinstance(item, dict) and item.get("account_alias")
    }
    if not enabled_aliases:
        issues.append({"code": "NO_ENABLED_ACCOUNT", "message": "没有启用的账户"})
    for alias in sorted(enabled_aliases):
        account = health_accounts.get(alias)
        if not account:
            issues.append({"code": "ACCOUNT_STATUS_MISSING", "message": "%s 没有状态数据" % alias})
        elif not account.get("ready"):
            issues.append({"code": "ADAPTER_NOT_READY", "message": "%s 的 QMT Adapter 未就绪" % alias})
    unresolved = int(health.get("unresolved_submit_unknown", 0) or 0)
    if unresolved:
        issues.append({
            "code": "SUBMIT_UNKNOWN",
            "message": "存在 %d 笔需要人工核对的 SUBMIT_UNKNOWN" % unresolved,
        })
    return issues


def start_worker(config_path, human=False, start_mode=None, confirm=None):
    config_path = os.path.abspath(config_path)
    probe = probe_worker(config_path)
    if probe["state"] == "RUNNING":
        selected_mode = None
        if human or start_mode is not None:
            selected_mode = select_start_mode(
                config_path,
                requested=start_mode,
                confirm=confirm,
                interactive=human and start_mode is None,
            )
            probe = probe_worker(config_path)
        if human:
            issues = _status_issues(config_path, probe)
            print_status_human({
                "ok": not issues,
                "config": config_path,
                "worker": probe,
                "issues": issues,
            })
            if selected_mode is not None:
                print("\n现有 Worker 将继续运行；Worker 与 Adapter 模式已同步为 %s。" % selected_mode)
            else:
                print("\n无需重复启动：现有 Worker 将继续运行。")
        else:
            print(json.dumps(probe, ensure_ascii=False, indent=2))
        return 0
    if probe["state"] == "CONFLICT":
        raise ManagerError("PORT_CONFLICT", probe["message"], probe)
    selected_mode = select_start_mode(
        config_path,
        requested=start_mode,
        confirm=confirm,
        interactive=human and start_mode is None,
    )
    config = load_config(config_path)
    log_dir = os.path.join(config.data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "worker-%s.log" % dt.date.today().isoformat())
    with open(log_path, "a", encoding="utf-8") as log:
        original_out, original_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = _Tee(original_out, log), _Tee(original_err, log)
        try:
            if human:
                _human_header("正在启动 WorkBuddy-QMT Worker")
                print("配置文件: %s" % config_path)
                print("运行模式: %s" % selected_mode)
                print("Adapter 配置: 已自动同步启用账户的 qmt_mode（熔断时保持 OBSERVE_ONLY）。")
                print("监听地址: http://%s:%d" % (config.host, config.port))
                print("日志文件: %s" % log_path)
                print("\n窗口说明：")
                print("  - 请保持此窗口打开；关闭窗口会停止桥接服务。")
                print("  - 按 Ctrl+C 可以安全停止 Worker。")
                print("  - Worker 启动不等于 QMT 已连接，QMT 策略实例仍需手工启动。")
                print("\n启动结果：")
            else:
                print("配置: %s" % config_path, file=sys.stderr)
                print("日志: %s" % log_path, file=sys.stderr)
                print("关闭窗口或按 Ctrl+C 可停止 Worker。", file=sys.stderr)
            result = worker_main(["--config", config_path])
            if human:
                print("\nWorker 已停止。需要恢复桥接时，请重新运行启动脚本。")
            return result
        finally:
            sys.stdout, sys.stderr = original_out, original_err


def status_report(config_path):
    probe = probe_worker(config_path)
    issues = _status_issues(config_path, probe)
    return {
        "ok": not issues,
        "config": os.path.abspath(config_path),
        "worker": probe,
        "issues": issues,
    }


def doctor_report(config_path, launcher_path=None):
    config_path = os.path.abspath(config_path)
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("python", sys.version_info >= (3, 10), sys.version.split()[0])
    try:
        config = load_config(config_path)
        add("bridge_config", True, config_path)
    except Exception as exc:
        add("bridge_config", False, str(exc))
        return {"ok": False, "checks": checks, "worker": None}
    add("worker_token", os.path.isfile(config.worker_token_file), config.worker_token_file)
    add("message_keys", os.path.isfile(config.key_file), config.key_file)
    try:
        probe = probe_worker(config_path)
        add("worker", probe["state"] != "CONFLICT", probe["message"])
    except Exception as exc:
        probe = {"state": "ERROR", "message": str(exc)}
        add("worker", False, str(exc))
    if launcher_path:
        try:
            state = load_launcher_state(launcher_path)
            mcp_path = state.get("mcp_config", default_mcp_path())
            mcp = validate_mcp_config(mcp_path)
            expected = mcp_server_config(config_path, sys.executable)
            add("workbuddy_mcp", mcp.get("mcpServers", {}).get(MCP_SERVER_NAME) == expected, mcp_path)
        except Exception as exc:
            add("workbuddy_mcp", False, str(exc))
    ready_root = os.path.join(_runtime_root_from_config(config_path), "qmt_ready")
    for account in config.accounts.values():
        if not account.enabled:
            continue
        script = os.path.join(ready_root, account.alias, "qmt_adapter.py")
        adapter = os.path.join(ready_root, account.alias, "qmt_adapter.json")
        profile = os.path.join(ready_root, account.alias, "qmt_profile.json")
        sync_issues = _adapter_sync_issues(config, account, script, adapter, profile)
        detail = os.path.dirname(script)
        if sync_issues:
            detail += "；" + "；".join(sync_issues)
        add("qmt_bundle:%s" % account.alias, not sync_issues, detail)
    return {"ok": all(item["ok"] for item in checks), "checks": checks, "worker": probe}


def print_doctor_human(report):
    _human_header("WorkBuddy-QMT 安装与配置诊断")
    labels = {
        "python": "Python 版本",
        "bridge_config": "Bridge 配置",
        "worker_token": "Worker 本机令牌",
        "message_keys": "QMT 消息密钥",
        "worker": "Worker 端口",
        "workbuddy_mcp": "WorkBuddy MCP 配置",
    }
    failed = []
    for check in report.get("checks", []):
        name = check["name"]
        label = labels.get(name, "QMT 文件: %s" % name.split(":", 1)[1] if name.startswith("qmt_bundle:") else name)
        state = "通过" if check["ok"] else "需要处理"
        print("[%s] %s" % (state, label))
        print("       %s" % check.get("detail", ""))
        if not check["ok"]:
            failed.append(name)
    worker = report.get("worker") or {}
    print("\n诊断结论：")
    if failed:
        print("发现 %d 项需要处理。" % len(failed))
    else:
        print("安装文件和基础配置完整。")
    if worker.get("state") == "STOPPED":
        print("Worker 当前未启动；配置正常时，双击“启动QMT桥接.cmd”即可。")
    elif worker.get("state") == "CONFLICT":
        print("Worker 端口冲突：关闭占用端口的旧 Worker/其他程序后再启动。")
    if "workbuddy_mcp" in failed:
        print("- 重新运行首次配置以修复 MCP 节点，然后重启 WorkBuddy。")
    if any(name.startswith("qmt_bundle:") for name in failed):
        print("- QMT 文件缺失或与 bridge.json/Profile 不同步；重新运行首次配置生成文件，并按 P0 流程复核后重新签名。")
    if "worker_token" in failed or "message_keys" in failed or "bridge_config" in failed:
        print("- 重新运行首次配置；不要手工复制其他电脑的密钥或令牌。")


def verify_report(config_path, launcher_path=None):
    config_path = os.path.abspath(config_path)
    diagnostic = doctor_report(config_path, launcher_path)
    checks = list(diagnostic.get("checks", []))
    worker = diagnostic.get("worker") or {"state": "ERROR", "message": "无法检查 Worker"}
    worker_running = worker.get("state") == "RUNNING"
    checks.append({
        "name": "worker_running",
        "ok": worker_running,
        "detail": "Worker 正在运行" if worker_running else worker.get("message", "Worker 未运行"),
    })
    status = None
    if worker_running:
        status = status_report(config_path)
        health = ((worker.get("response") or {}).get("data") or {})
        by_alias = {
            item.get("account_alias"): item
            for item in health.get("accounts", [])
            if isinstance(item, dict) and item.get("account_alias")
        }
        for account in load_config(config_path).accounts.values():
            if not account.enabled:
                continue
            account_health = by_alias.get(account.alias) or {}
            ready = bool(account_health.get("ready"))
            checks.append({
                "name": "adapter:%s" % account.alias,
                "ok": ready,
                "detail": "Adapter 已连接，可用" if ready else "Adapter 尚未就绪；请检查 QMT 登录和策略实例",
            })
        for issue in status.get("issues", []):
            if issue.get("code") in {"ADAPTER_NOT_READY", "ACCOUNT_STATUS_MISSING"}:
                continue
            checks.append({
                "name": "runtime:%s" % issue.get("code", "UNKNOWN"),
                "ok": False,
                "detail": issue.get("message", "运行状态异常"),
            })
    return {
        "ok": all(item.get("ok") for item in checks),
        "version": __version__,
        "config": config_path,
        "checks": checks,
        "worker": worker,
        "status": status,
        "workbuddy_note": "MCP 配置文件已检查；WorkBuddy 是否已重新加载该配置，需要在重启 WorkBuddy 后由 qmt_health 确认。",
    }


def print_verify_human(report):
    _human_header("WorkBuddy-QMT 首次只读连接验证")
    labels = {
        "python": "Python 与软件包",
        "bridge_config": "Bridge 配置",
        "worker_token": "Worker 本机令牌",
        "message_keys": "QMT 消息密钥",
        "worker": "Worker 端口",
        "workbuddy_mcp": "WorkBuddy MCP 配置",
        "worker_running": "Worker 正在运行",
    }
    for check in report.get("checks", []):
        name = check.get("name", "unknown")
        if name.startswith("qmt_bundle:"):
            label = "QMT 文件: %s" % name.split(":", 1)[1]
        elif name.startswith("adapter:"):
            label = "QMT Adapter: %s" % name.split(":", 1)[1]
        elif name.startswith("runtime:"):
            label = "运行状态: %s" % name.split(":", 1)[1]
        else:
            label = labels.get(name, name)
        print("[%s] %s" % ("完成" if check.get("ok") else "待处理", label))
        print("       %s" % check.get("detail", ""))
    print("\n验证结论：")
    if report.get("ok"):
        print("首次只读连接已完成。当前仍应保持 OBSERVE_ONLY，不会实际报单。")
        print("下一步：重启 WorkBuddy，在对话中调用 qmt_health，再查询账户和持仓。")
    else:
        print("尚未完成首次只读连接。先处理上方标记为“待处理”的项目。")
        print("可双击桌面的“查看QMT桥接状态.cmd”获取进一步诊断。")
    print("\n%s" % report.get("workbuddy_note", ""))


def _release_version_tuple(value):
    matched = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value or "").strip())
    if not matched:
        return None
    return tuple(int(part) for part in matched.groups())


def upgrade_check_report(timeout=4.0):
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "workbuddy-qmt-bridge/%s" % __version__},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        raise ManagerError("UPGRADE_CHECK_FAILED", "无法查询 GitHub 最新版本: %s" % exc)
    latest = release.get("tag_name")
    current_tuple = _release_version_tuple(__version__)
    latest_tuple = _release_version_tuple(latest)
    if not latest_tuple:
        raise ManagerError("UPGRADE_CHECK_FAILED", "GitHub 最新 Release 版本号无法识别", {"tag_name": latest})
    if current_tuple == latest_tuple:
        state = "UP_TO_DATE"
    elif current_tuple and current_tuple < latest_tuple:
        state = "UPDATE_AVAILABLE"
    else:
        state = "AHEAD_OF_RELEASE"
    return {
        "ok": True,
        "state": state,
        "current_version": __version__,
        "latest_version": str(latest).lstrip("v"),
        "release_url": release.get("html_url") or LATEST_RELEASE_PAGE,
        "automatic_update": False,
    }


def print_upgrade_check_human(report):
    _human_header("WorkBuddy-QMT 版本检查")
    print("当前版本: %s" % report["current_version"])
    print("最新版本: %s" % report["latest_version"])
    if report["state"] == "UP_TO_DATE":
        print("结论：当前已经是最新正式版本。")
    elif report["state"] == "UPDATE_AVAILABLE":
        print("结论：发现新版本。请先停止 Worker 和 QMT 策略并备份 runtime，再手工升级。")
    else:
        print("结论：当前版本高于最新正式 Release，可能是开发版本。")
    print("Release: %s" % report["release_url"])
    print("本命令只检查版本，不会自动下载、安装或改动配置。")


def _account_rows(config_path):
    config = load_config(config_path)
    return [{
        "alias": item.alias,
        "account_type": item.account_type,
        "enabled": item.enabled,
        "adapter_instance": item.adapter_instance,
        "qmt_bundle": os.path.join(_runtime_root_from_config(config_path), "qmt_ready", item.alias),
    } for item in config.accounts.values()]


def run_account_command(args, config_path):
    config_path = os.path.abspath(config_path)
    if args.account_command == "list":
        result = {"ok": True, "accounts": _account_rows(config_path)}
    else:
        _require_worker_stopped(config_path)
        config = load_config(config_path)
        account = config.accounts.get(args.account_alias)
        if not account:
            raise ManagerError("UNKNOWN_ACCOUNT", "Bridge 配置中找不到账户", {"account_alias": args.account_alias})
        if args.account_command == "disable":
            if args.confirm != DISABLE_ACCOUNT_CONFIRMATION:
                raise ManagerError(
                    "LOCAL_CONFIRMATION_REQUIRED",
                    "停用账户必须提供 --confirm %s；现有 QMT 文件和 Profile 会保留" % DISABLE_ACCOUNT_CONFIRMATION,
                    {"account_alias": account.alias},
                )
            change = set_account_enabled(config_path, account.alias, False)
            result = {"ok": True, "account": change, "profile_preserved": True}
        else:
            change = None
            if args.account_command == "enable":
                change = set_account_enabled(config_path, account.alias, True)
            config = load_config(config_path)
            account = config.accounts.get(account.alias)
            if not account.enabled:
                raise ManagerError(
                    "ACCOUNT_DISABLED",
                    "账户尚未启用，请先运行 account enable",
                    {"account_alias": account.alias},
                )
            account_id = args.qmt_account_id or infer_qmt_account_id(config_path, account)
            bundle = None
            warnings = []
            if account_id:
                bundle = generate_qmt_bundle(
                    config_path, account.alias, account_id, overwrite=args.force,
                )
            else:
                warnings.append("尚未提供 QMT 账户号，账户已启用但未生成 QMT 文件")
            result = {
                "ok": True,
                "account": change or {"account_alias": account.alias, "enabled": True, "changed": False},
                "qmt_bundle": bundle,
                "warnings": warnings,
                "profile_preserved": bool(bundle and bundle.get("profile_preserved")),
            }
    if getattr(args, "human", False):
        _human_header("WorkBuddy-QMT 账户配置")
        if args.account_command == "list":
            for item in result["accounts"]:
                print("- %s (%s): %s" % (item["alias"], item["account_type"], "已启用" if item["enabled"] else "未启用"))
                print("  QMT 目录: %s" % item["qmt_bundle"])
        else:
            account_result = result["account"]
            print("账户: %s" % account_result["account_alias"])
            print("状态: %s" % ("已启用" if account_result["enabled"] else "已停用"))
            if result.get("qmt_bundle"):
                print("QMT 文件: %s" % result["qmt_bundle"]["directory"])
                print("Profile: %s" % ("已保留" if result["qmt_bundle"]["profile_preserved"] else "新建未验证模板"))
            for warning in result.get("warnings", []):
                print("提示: %s" % warning)
            print("修改账户后请保持 OBSERVE_ONLY，重新启动 QMT 策略、Worker 和 WorkBuddy，再运行 verify。")
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


def _redact_text(value, replacements):
    text = str(value)
    for original, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        if original:
            text = text.replace(original, replacement)
    return text


def _redact_structure(value, replacements, key_name=""):
    sensitive = ("token", "secret", "signature", "account_id", "qmt_account_id", "key_value")
    if any(name in key_name.lower() for name in sensitive):
        return "<redacted>"
    if isinstance(value, dict):
        return {key: _redact_structure(item, replacements, str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item, replacements, key_name) for item in value]
    if isinstance(value, str):
        return _redact_text(value, replacements)
    return value


def _file_manifest(path):
    if not os.path.isfile(path):
        return {"name": os.path.basename(path), "exists": False}
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return {
        "name": os.path.basename(path),
        "exists": True,
        "size": os.path.getsize(path),
        "sha256": digest.hexdigest(),
    }


def create_support_bundle(config_path, launcher_path=None, output=None, force=False):
    config_path = os.path.abspath(config_path)
    runtime_root = _runtime_root_from_config(config_path)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output = os.path.abspath(output or os.path.join(os.getcwd(), "workbuddy-qmt-support-%s.zip" % timestamp))
    if os.path.exists(output) and not force:
        raise ManagerError("FILE_EXISTS", "诊断包已存在；请换一个输出路径或使用 --force", {"path": output})
    config_raw = _read_json_object(config_path, "Bridge 配置")
    replacements = {
        os.path.expanduser("~"): "%USERPROFILE%",
        runtime_root: "%RUNTIME%",
        config_path: "%RUNTIME%\\config\\bridge.json",
        sys.executable: "%PYTHON%",
    }
    diagnostic = doctor_report(config_path, launcher_path)
    manifest = []
    for account in load_config(config_path).accounts.values():
        account_dir = os.path.join(runtime_root, "qmt_ready", account.alias)
        manifest.append({
            "account_alias": account.alias,
            "account_type": account.account_type,
            "enabled": account.enabled,
            "files": [_file_manifest(os.path.join(account_dir, name)) for name in (
                "qmt_adapter.py", "qmt_adapter.json", "qmt_profile.json", "部署说明.txt",
            )],
        })
    summary = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bridge_version": __version__,
        "python_version": platform.python_version(),
        "windows_release": platform.release(),
        "redacted": True,
        "contains_logs": False,
        "contains_secrets": False,
    }
    entries = {
        "README.txt": (
            "此诊断包已经脱敏，仅包含版本、配置结构、检查结果和文件哈希。\r\n"
            "它不包含日志正文、数据库、Worker Token、消息密钥、完整 QMT 账户号或 Profile 内容。\r\n"
            "分享前仍请自行检查压缩包内容。\r\n"
        ),
        "summary.json": json.dumps(summary, ensure_ascii=False, indent=2),
        "diagnostics.json": json.dumps(_redact_structure(diagnostic, replacements), ensure_ascii=False, indent=2),
        "bridge-config.redacted.json": json.dumps(_redact_structure(config_raw, replacements), ensure_ascii=False, indent=2),
        "qmt-files.json": json.dumps(manifest, ensure_ascii=False, indent=2),
    }
    os.makedirs(os.path.dirname(output), exist_ok=True)
    mode = "w"
    with zipfile.ZipFile(output, mode, compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in entries.items():
            archive.writestr(name, text.encode("utf-8"))
    return {"ok": True, "path": output, "redacted": True, "entries": sorted(entries)}


def print_human_error(code, message, details=None):
    _human_header("操作未完成")
    print("错误代码: %s" % code)
    print("原因: %s" % message)
    details = details or {}
    path = details.get("path")
    if path:
        print("相关路径: %s" % path)
    print("\n下一步：")
    if code == "SETUP_REQUIRED":
        print("重新运行“首次安装与配置.cmd”完成初始化。")
    elif code == "PORT_CONFLICT":
        print("运行“查看QMT桥接状态.cmd”，根据自动诊断结果关闭占用端口的旧 Worker或其他程序。")
    elif code in {"TOKEN_ERROR", "KEY_CONFIG_ERROR", "CONFIG_ERROR", "LAUNCHER_CONFIG_ERROR"}:
        print("运行“查看QMT桥接状态.cmd”确认缺失项，然后重新运行首次配置修复。")
    else:
        print("运行“查看QMT桥接状态.cmd”，根据自动诊断的失败项目修复后重试。")


def _ask(prompt, default=None):
    suffix = " [%s]" % default if default else ""
    value = input("%s%s: " % (prompt, suffix)).strip()
    return value or (default or "")


def _ask_yes_no(prompt, default=True):
    suffix = "Y/n" if default else "y/N"
    yes_values = {"y", "yes", "1", "true", "是", "启用"}
    no_values = {"n", "no", "0", "false", "否", "不", "禁用"}
    while True:
        value = input("%s [%s]: " % (prompt, suffix)).strip().lower()
        if not value:
            return default
        if value in yes_values:
            return True
        if value in no_values:
            return False
        print("无法识别输入。请输入 y/yes/是 或 n/no/否；直接按 Enter 使用默认值。")


def run_setup(args, launcher_path):
    if sys.version_info < (3, 10):
        raise ManagerError("PYTHON_VERSION", "需要 Python 3.10 或更高版本")
    interactive = not args.non_interactive
    reset_profiles = set(args.reset_profile or [])
    if args.confirm_reset_profile and not reset_profiles:
        raise ManagerError("INVALID_REQUEST", "--confirm-reset-profile 只能与 --reset-profile 一起使用")
    if reset_profiles and args.confirm_reset_profile != RESET_PROFILE_CONFIRMATION:
        raise ManagerError(
            "LOCAL_CONFIRMATION_REQUIRED",
            "重置 Profile 必须提供 --confirm-reset-profile %s" % RESET_PROFILE_CONFIRMATION,
            {"accounts": sorted(reset_profiles)},
        )
    if interactive:
        print("\n============================================================")
        print("WorkBuddy-QMT 首次配置向导")
        print("============================================================")
        print("带 [默认值] 的问题可直接按 Enter 接受默认值。")
        print("向导不会启用实盘、签署 QMT profile，也不会启动 QMT。")
        print("遇到错误时请先阅读错误中的 USER ACTION/路径提示，再重新运行。")

        print("\n[向导 1/5] 确认运行数据目录")
        print("这里保存 bridge.json、本机密钥、数据库、队列和 QMT 生成文件。")
        if args.root:
            print("启动脚本已指定目录，无需输入。")
    runtime_root = os.path.abspath(args.root or default_runtime_root())
    if interactive and not args.root:
        runtime_root = os.path.abspath(_ask("运行数据目录", runtime_root))
    if interactive:
        print("将使用: %s" % runtime_root)

        print("\n[向导 2/5] 确认 WorkBuddy MCP 配置")
        print("直接按 Enter 使用当前用户的默认 mcp.json；已有文件只会备份后合并 qmt-bridge。")
        print("如果文件不是有效 JSON，向导会停止且不会覆盖原文件。")
    mcp_path = os.path.abspath(args.mcp_config or default_mcp_path())
    if interactive and not args.mcp_config:
        mcp_path = os.path.abspath(_ask("WorkBuddy MCP 配置", mcp_path))
    validate_mcp_config(mcp_path)
    if interactive:
        print("MCP 配置检查通过: %s" % mcp_path)
    config_path = os.path.join(runtime_root, "config", "bridge.json")
    is_new = not os.path.exists(config_path)
    if interactive:
        print("\n[向导 3/5] 配置账户并生成 QMT 文件")
        if is_new:
            print("这是新环境：普通账户默认启用，信用账户默认不启用。")
            print("输入 y 启用、输入 n 禁用、直接按 Enter 接受括号中的默认选择。")
        else:
            print("发现已有 bridge.json，将复用其中的账户和风控设置，不自动更改 enabled。")
    account_types = {"STOCK"}
    if args.accounts:
        account_types = {value.strip().upper() for value in args.accounts.split(",") if value.strip()}
    elif is_new and interactive:
        account_types = set()
        if _ask_yes_no("配置普通证券账户", True):
            account_types.add("STOCK")
        if _ask_yes_no("配置信用账户", False):
            account_types.add("CREDIT")
        if not account_types:
            raise ManagerError("ACCOUNT_REQUIRED", "至少需要启用一个账户")
    config_path = initialize(runtime_root, account_types if is_new else None)
    config = load_config(config_path)
    account_changes = []
    if not is_new and interactive:
        print("已有环境可在此调整账户启用状态；直接按 Enter 会保留当前选择。")
        desired_states = {}
        for configured_account in config.accounts.values():
            desired_states[configured_account.alias] = _ask_yes_no(
                "启用 %s (%s)" % (configured_account.alias, configured_account.account_type),
                configured_account.enabled,
            )
        changed_aliases = [
            alias for alias, enabled in desired_states.items()
            if config.accounts[alias].enabled != enabled
        ]
        if changed_aliases:
            disabling = [alias for alias in changed_aliases if not desired_states[alias]]
            if disabling:
                print("停用账户不会删除其 QMT 文件或 Profile，但重启后 Worker 将不再加载这些账户。")
                confirmation = input("确认停用后输入 %s: " % DISABLE_ACCOUNT_CONFIRMATION).strip()
                if confirmation != DISABLE_ACCOUNT_CONFIRMATION:
                    raise ManagerError("LOCAL_CONFIRMATION_REQUIRED", "确认文字不匹配，未修改账户状态")
            _require_worker_stopped(config_path)
            for alias in changed_aliases:
                account_changes.append(set_account_enabled(config_path, alias, desired_states[alias]))
            config = load_config(config_path)
    unknown_reset_profiles = reset_profiles - set(config.accounts)
    if unknown_reset_profiles:
        raise ManagerError(
            "INVALID_REQUEST", "--reset-profile 包含未知账户别名",
            {"accounts": sorted(unknown_reset_profiles)},
        )
    disabled_reset_profiles = {
        alias for alias in reset_profiles if not config.accounts[alias].enabled
    }
    if disabled_reset_profiles:
        raise ManagerError(
            "INVALID_REQUEST", "不能通过 setup 重置未启用账户的 Profile",
            {"accounts": sorted(disabled_reset_profiles)},
        )
    if interactive:
        print("账户状态：")
        for configured_account in config.accounts.values():
            print("  - %s (%s): %s" % (
                configured_account.alias,
                configured_account.account_type,
                "已启用" if configured_account.enabled else "未启用",
            ))
        print("接下来只询问已启用账户的 QMT 账户号。")
        print("已有账户号会显示在 [括号] 中；直接 Enter 接受，留空且无默认值则暂时跳过。")
    ids = {"STOCK": args.stock_account_id or "", "CREDIT": args.credit_account_id or ""}
    bundles = []
    skipped_accounts = []
    for account in config.accounts.values():
        if not account.enabled:
            continue
        account_id = ids.get(account.account_type, "") or infer_qmt_account_id(config_path, account)
        if interactive:
            account_id = _ask("%s (%s) 的 QMT 账户号，留空暂时跳过" % (account.alias, account.account_type), account_id)
        if not account_id:
            skipped_accounts.append(account.alias)
            continue
        reset_profile = account.alias in reset_profiles
        try:
            bundle = generate_qmt_bundle(
                config_path, account.alias, account_id,
                overwrite=args.force,
                reset_profile=reset_profile,
                reset_profile_confirmation=args.confirm_reset_profile,
            )
        except ManagerError as exc:
            if exc.code != "FILE_EXISTS" or not interactive or not _ask_yes_no("%s，是否备份后更新" % exc.details.get("path", "QMT 文件"), False):
                raise
            bundle = generate_qmt_bundle(
                config_path, account.alias, account_id,
                overwrite=True,
                reset_profile=reset_profile,
                reset_profile_confirmation=args.confirm_reset_profile,
            )
        bundles.append(bundle)
        if interactive:
            print("已生成 %s 的 QMT 文件: %s" % (account.alias, bundle["directory"]))
            if bundle["profile_preserved"]:
                print("已保留现有签名 Profile: %s" % bundle["mapping_profile"])
            elif bundle["profile_reset"]:
                print("已按专用确认重置 Profile，并为原文件创建时间戳备份。")
    if interactive:
        print("\n[向导 4/5] 合并 MCP 并保存启动器配置")
        print("如需修改 mcp.json，会先在同目录创建带时间戳的 .bak 备份。")
        print("启动器配置不保存 QMT 账户号、Worker 令牌或消息密钥。")
    mcp_result = merge_mcp_config(mcp_path, config_path, sys.executable)
    state = {
        "runtime_root": runtime_root,
        "bridge_config": os.path.abspath(config_path),
        "mcp_config": mcp_path,
        "python_executable": os.path.abspath(sys.executable),
        "qmt_ready_dir": os.path.join(runtime_root, "qmt_ready"),
    }
    save_launcher_state(launcher_path, state)
    shortcut_result = None
    opened_qmt_ready = None
    if interactive:
        print("MCP 状态: %s" % ("已更新" if mcp_result["changed"] else "无需修改"))
        if mcp_result["backup"]:
            print("MCP 备份: %s" % mcp_result["backup"])
        print("启动器配置: %s" % launcher_path)
        print("\n[向导 5/5] 创建桌面启动入口")
        print("建议创建启动和状态两个脚本；状态脚本会先验证完整连接，再显示当前状态。")
    if not args.no_shortcuts:
        target = os.path.abspath(args.shortcut_dir) if args.shortcut_dir else None
        if interactive and target is None and not _ask_yes_no("在桌面创建启动和状态脚本", True):
            target = ""
        if target != "":
            shortcut_result = create_shortcuts(launcher_path, target_dir=target, python_executable=sys.executable)
    if interactive and not args.no_open and bundles:
        try:
            opened_qmt_ready = open_local_path(state["qmt_ready_dir"])
            print("已打开 QMT 文件目录；每个账户目录中都有“部署说明.txt”。")
        except ManagerError as exc:
            print("未能自动打开 QMT 文件目录: %s" % exc.message)
    result = {
        "ok": True,
        "new_runtime": is_new,
        "runtime_root": runtime_root,
        "bridge_config": os.path.abspath(config_path),
        "launcher_config": os.path.abspath(launcher_path),
        "mcp": mcp_result,
        "qmt_bundles": bundles,
        "skipped_accounts": skipped_accounts,
        "shortcuts": shortcut_result,
        "account_changes": account_changes,
        "opened_qmt_ready": opened_qmt_ready,
        "warnings": [] if bundles else ["尚未生成 QMT Adapter，请重新运行 setup 并填写账户号"],
        "next_steps": ["按账户目录中的部署说明配置并启动 QMT 策略", "双击启动QMT桥接.cmd", "运行查看QMT桥接状态.cmd", "重启 WorkBuddy 并调用 qmt_health"],
    }
    if interactive:
        print("\n============================================================")
        print("首次配置完成")
        print("============================================================")
        print("运行目录: %s" % runtime_root)
        print("Bridge 配置: %s" % os.path.abspath(config_path))
        print("QMT 文件根目录: %s" % state["qmt_ready_dir"])
        if bundles:
            print("已生成账户: %s" % ", ".join(item["account_alias"] for item in bundles))
        if skipped_accounts:
            print("已跳过账户: %s（需要时重新运行 setup 并填写账户号）" % ", ".join(skipped_accounts))
        if shortcut_result:
            print("桌面脚本目录: %s" % shortcut_result["directory"])
        else:
            print("未创建桌面脚本，可稍后重新运行 setup。")
        print("\nUSER ACTION - 完成首次只读连接：")
        print("  1. 按账户目录中的“部署说明.txt”创建并启动 QMT 策略。")
        print("  2. 保持 OBSERVE_ONLY，双击桌面的“启动QMT桥接.cmd”。")
        print("  3. 双击“查看QMT桥接状态.cmd”，它会先验证完整连接，再显示当前状态。")
        print("  4. 重启 WorkBuddy，调用 qmt_health，再查询账户和持仓。")
        print("完成 P0 现场验证和 Profile 签名前，不要切换到其他模式。")
    else:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="WorkBuddy-QMT 一键配置和启动工具")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--launcher-config", default=default_launcher_path())
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="运行首次配置向导")
    setup.add_argument("--root")
    setup.add_argument("--mcp-config")
    setup.add_argument("--accounts", help="新环境启用的账户类型，例如 STOCK,CREDIT")
    setup.add_argument("--stock-account-id")
    setup.add_argument("--credit-account-id")
    setup.add_argument("--shortcut-dir")
    setup.add_argument("--no-shortcuts", action="store_true")
    setup.add_argument("--no-open", action="store_true", help="完成后不自动打开 qmt_ready 目录")
    setup.add_argument("--non-interactive", action="store_true")
    setup.add_argument(
        "--force", action="store_true",
        help="备份后覆盖变化的 Adapter/配置文件；不会重置已有 Profile",
    )
    setup.add_argument(
        "--reset-profile", action="append", default=[], metavar="ACCOUNT_ALIAS",
        help="重置指定账户的 Profile；可重复使用，且必须提供专用确认词",
    )
    setup.add_argument("--confirm-reset-profile", metavar=RESET_PROFILE_CONFIRMATION)
    start = sub.add_parser("start", help="在当前窗口启动 Worker")
    start.add_argument("--config")
    start.add_argument("--human", action="store_true", help="输出适合桌面窗口阅读的中文说明")
    start.add_argument(
        "--mode", dest="start_mode",
        choices=RUN_MODES,
        help="指定本次 Worker 启动模式；桌面人机模式下省略时会显示选择菜单",
    )
    start.add_argument("--confirm", help="模拟柜台或人工实盘模式的本机确认文字")
    status = sub.add_parser("status", help="检查 Worker 状态")
    status.add_argument("--config")
    status.add_argument("--human", action="store_true", help="输出适合桌面窗口阅读的中文说明")
    doctor = sub.add_parser("doctor", help="检查本机配置")
    doctor.add_argument("--config")
    doctor.add_argument("--human", action="store_true", help="输出适合桌面窗口阅读的中文说明")
    verify = sub.add_parser("verify", help="验证首次只读连接是否完整")
    verify.add_argument("--config")
    verify.add_argument("--human", action="store_true", help="输出清晰的完成/待处理结果卡")
    open_command = sub.add_parser("open", help="打开运行目录、QMT 文件、日志或配置")
    open_command.add_argument("target", choices=("runtime", "qmt-ready", "logs", "config"))
    open_command.add_argument("--config")
    open_command.add_argument("--human", action="store_true")
    upgrade = sub.add_parser("upgrade-check", help="只读检查 GitHub 最新正式版本")
    upgrade.add_argument("--human", action="store_true")
    upgrade.add_argument("--timeout", type=float, default=4.0)
    support = sub.add_parser("support-bundle", help="生成不含日志、密钥和账户号的脱敏诊断包")
    support.add_argument("--config")
    support.add_argument("--output")
    support.add_argument("--redact", action="store_true", required=True, help="确认只生成脱敏诊断包")
    support.add_argument("--force", action="store_true")
    support.add_argument("--human", action="store_true")
    account = sub.add_parser("account", help="列出、启用、停用或重新配置账户")
    account.add_argument("--config")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    account_list = account_sub.add_parser("list", help="列出账户启用状态")
    account_list.add_argument("--human", action="store_true")
    account_enable = account_sub.add_parser("enable", help="启用账户并可生成 QMT 文件")
    account_enable.add_argument("account_alias")
    account_enable.add_argument("--qmt-account-id")
    account_enable.add_argument("--force", action="store_true")
    account_enable.add_argument("--human", action="store_true")
    account_disable = account_sub.add_parser("disable", help="停用账户但保留 QMT 文件和 Profile")
    account_disable.add_argument("account_alias")
    account_disable.add_argument("--confirm", metavar=DISABLE_ACCOUNT_CONFIRMATION)
    account_disable.add_argument("--human", action="store_true")
    account_configure = account_sub.add_parser("configure", help="为已启用账户重新生成 QMT 配置")
    account_configure.add_argument("account_alias")
    account_configure.add_argument("--qmt-account-id", required=True)
    account_configure.add_argument("--force", action="store_true")
    account_configure.add_argument("--human", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    launcher_path = os.path.abspath(args.launcher_config)
    try:
        if args.command == "upgrade-check":
            report = upgrade_check_report(timeout=args.timeout)
            if args.human:
                print_upgrade_check_human(report)
            else:
                print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0
        if args.command == "setup":
            return run_setup(args, launcher_path)
        config_path = resolve_bridge_config(launcher_path, args.config)
        if args.command == "account":
            return run_account_command(args, config_path)
        if args.command == "open":
            config = load_config(config_path)
            targets = {
                "runtime": _runtime_root_from_config(config_path),
                "qmt-ready": os.path.join(_runtime_root_from_config(config_path), "qmt_ready"),
                "logs": os.path.join(config.data_dir, "logs"),
                "config": config_path,
            }
            result = {"ok": True}
            result.update(open_local_path(targets[args.target]))
            if args.human:
                print("已打开: %s" % result["path"] if result["opened"] else result["message"] + ": " + result["path"])
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "support-bundle":
            report = create_support_bundle(
                config_path, launcher_path, output=args.output, force=args.force,
            )
            if args.human:
                _human_header("WorkBuddy-QMT 脱敏诊断包")
                print("已生成: %s" % report["path"])
                print("不包含日志正文、数据库、Token、消息密钥、完整账户号或 Profile 内容。")
                print("发送给他人前仍请自行检查压缩包内容。")
            else:
                print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0
        if args.command == "start":
            return start_worker(
                config_path,
                human=args.human,
                start_mode=args.start_mode,
                confirm=args.confirm,
            )
        if args.command == "verify":
            report = verify_report(config_path, launcher_path)
            if args.human:
                print_verify_human(report)
            else:
                print(json.dumps(report, ensure_ascii=True, indent=2))
        elif args.command == "status":
            report = status_report(config_path)
            if args.human:
                print_status_human(report)
                if report["ok"]:
                    print("\n状态正常，无需执行额外诊断。")
                else:
                    print("\n" + "-" * 60)
                    print("检测到桥接状态异常，正在自动执行配置诊断……")
                    print("-" * 60 + "\n")
                    diagnostic = doctor_report(config_path, launcher_path)
                    print_doctor_human(diagnostic)
            else:
                print(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            report = doctor_report(config_path, launcher_path)
            if args.human:
                print_doctor_human(report)
            else:
                print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["ok"] else 1
    except ManagerError as exc:
        if getattr(args, "human", False):
            print_human_error(exc.code, exc.message, exc.details)
        else:
            print(json.dumps({
                "ok": False,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            }, ensure_ascii=True, indent=2), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if getattr(args, "human", False):
            print_human_error("UNEXPECTED_ERROR", str(exc))
        else:
            print(json.dumps({
                "ok": False,
                "error": {"code": "UNEXPECTED_ERROR", "message": str(exc), "details": {}},
            }, ensure_ascii=True, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
