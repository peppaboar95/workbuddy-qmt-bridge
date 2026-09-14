import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from workbuddy_qmt.bootstrap import initialize
from workbuddy_qmt.manager import (
    _ask_yes_no,
    create_shortcuts,
    create_support_bundle,
    generate_qmt_bundle,
    set_account_enabled,
    upgrade_check_report,
    verify_report,
)


class _FakeResponse:
    def __init__(self, value):
        self.data = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.data


class ManagerUxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = initialize(self.temp.name, {"STOCK"})

    def test_yes_no_reprompts_after_invalid_input(self):
        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=["maybe", "是"]), contextlib.redirect_stdout(output):
            result = _ask_yes_no("启用普通账户", False)
        self.assertTrue(result)
        self.assertIn("无法识别输入", output.getvalue())

    def test_existing_disabled_account_can_be_enabled_and_configured(self):
        with open(self.config_path, "r", encoding="utf-8") as stream:
            before = json.load(stream)
        self.assertFalse(next(item for item in before["accounts"] if item["alias"] == "main_credit")["enabled"])

        change = set_account_enabled(self.config_path, "main_credit", True)
        bundle = generate_qmt_bundle(self.config_path, "main_credit", "credit-test-001")

        self.assertTrue(change["changed"])
        self.assertTrue(os.path.isfile(change["backup"]))
        self.assertTrue(os.path.isfile(bundle["adapter_script"]))
        self.assertTrue(os.path.isfile(bundle["deployment_guide"]))

    def test_generated_adapter_supports_non_gbk_runtime_path(self):
        unicode_root = os.path.join(self.temp.name, "用户-🚀")
        config_path = initialize(unicode_root, {"STOCK"})
        bundle = generate_qmt_bundle(config_path, "main_stock", "stock-test-001")
        with open(bundle["adapter_script"], "r", encoding="gbk") as stream:
            script = stream.read()
        self.assertIn("ADAPTER_CONFIG_PATH = ", script)
        self.assertIn("\\ud83d\\ude80", script)
        with open(bundle["adapter_config"], "r", encoding="utf-8") as stream:
            adapter_config = json.load(stream)
        self.assertEqual(adapter_config["command_poll_interval_ms"], 500)
        self.assertEqual(adapter_config["order_deal_reconcile_seconds"], 30)
        self.assertIn("command_poll_interval_ms", script)
        self.assertIn("order_deal_reconcile_task", script)

    def test_verify_requires_running_worker(self):
        report = verify_report(self.config_path)
        self.assertFalse(report["ok"])
        worker_check = next(item for item in report["checks"] if item["name"] == "worker_running")
        self.assertFalse(worker_check["ok"])

    def test_shortcuts_are_grouped_and_merge_verify_into_status(self):
        desktop = os.path.join(self.temp.name, "Desktop")
        os.makedirs(desktop)
        obsolete = {
            "启动QMT桥接.cmd": b"@echo off\r\nworkbuddy_qmt.manager start --human\r\n",
            "查看QMT桥接状态.cmd": b"@echo off\r\nworkbuddy_qmt.manager status --human\r\n",
            "验证QMT桥接.cmd": b"@echo off\r\nworkbuddy_qmt.manager verify --human\r\n",
            "打开QMT配置目录.cmd": b"@echo off\r\nworkbuddy_qmt.manager open qmt-ready --human\r\n",
        }
        for name, content in obsolete.items():
            with open(os.path.join(desktop, name), "wb") as stream:
                stream.write(content)

        with mock.patch("workbuddy_qmt.manager._desktop_dir", return_value=desktop):
            result = create_shortcuts(
                os.path.join(self.temp.name, "launcher.json"),
                python_executable=sys.executable,
            )

        shortcut_dir = os.path.join(desktop, "WorkBuddy QMT Bridge")
        self.assertEqual(result["directory"], shortcut_dir)
        self.assertEqual(
            {os.path.basename(item["path"]) for item in result["files"]},
            {"启动QMT桥接.cmd", "查看QMT桥接状态.cmd"},
        )
        with open(os.path.join(shortcut_dir, "查看QMT桥接状态.cmd"), "rb") as stream:
            status_script = stream.read()
        self.assertLess(status_script.index(b" verify --human"), status_script.index(b" status --human"))
        self.assertTrue(all(not os.path.exists(os.path.join(desktop, name)) for name in obsolete))
        self.assertTrue(result["desktop_migration"])

    def test_support_bundle_excludes_account_id_and_secrets(self):
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "private-account-001")
        output = os.path.join(self.temp.name, "support.zip")
        result = create_support_bundle(self.config_path, output=output)

        self.assertTrue(result["redacted"])
        with open(os.path.join(self.temp.name, "data", "secrets", "worker.token"), "r", encoding="ascii") as stream:
            token = stream.read().strip()
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            combined = b"\n".join(archive.read(name) for name in names).decode("utf-8")
        self.assertEqual(names, {
            "README.txt", "summary.json", "diagnostics.json",
            "bridge-config.redacted.json", "qmt-files.json",
        })
        self.assertNotIn("private-account-001", combined)
        self.assertNotIn(token, combined)
        self.assertTrue(os.path.isfile(bundle["mapping_profile"]))

    def test_upgrade_check_is_read_only_and_reports_newer_release(self):
        response = _FakeResponse({"tag_name": "v99.0.0", "html_url": "https://example.invalid/release"})
        with mock.patch("urllib.request.urlopen", return_value=response):
            report = upgrade_check_report()
        self.assertEqual(report["state"], "UPDATE_AVAILABLE")
        self.assertFalse(report["automatic_update"])


if __name__ == "__main__":
    unittest.main()
