import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import workbuddy_qmt

from workbuddy_qmt.bootstrap import initialize
from workbuddy_qmt.config import load_config
from workbuddy_qmt.manager import (
    ManagerError,
    _console,
    generate_qmt_bundle,
    select_start_mode,
    set_account_enabled,
    start_worker,
    sync_adapter_modes,
)
from workbuddy_qmt.security import KeyRing
from workbuddy_qmt.util import atomic_write_bytes, atomic_write_json


class StartAdapterSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = initialize(self.temp.name, {"STOCK"})
        self.bundle = generate_qmt_bundle(self.config_path, "main_stock", "test-stock-001")

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def _bytes(self, path):
        with open(path, "rb") as stream:
            return stream.read()

    def _verify_profile(self, bundle):
        adapter = self._read(bundle["adapter_config"])
        profile = self._read(bundle["mapping_profile"])
        profile.update({
            "profile_id": "verified-" + bundle["account_alias"],
            "qmt_build": "test-qmt",
            "broker_build": "test-broker",
            "verified": True,
            "mappings": {"STOCK:BUY:LIMIT": {"op_type": 23, "price_type": 11}},
        })
        for name in ("profile_id", "qmt_build", "broker_build"):
            adapter["expected_" + name] = profile[name]
        keyring = KeyRing.load(load_config(self.config_path).key_file)
        profile["signature"] = keyring.sign(profile)
        atomic_write_json(bundle["mapping_profile"], profile)
        atomic_write_json(bundle["adapter_config"], adapter)

    def _enable_credit(self):
        set_account_enabled(self.config_path, "main_credit", True)
        return generate_qmt_bundle(self.config_path, "main_credit", "test-credit-001")

    def _start(self, mode, confirm=None, **values):
        output = io.StringIO()
        with (
            mock.patch("workbuddy_qmt.manager.probe_worker", return_value={"state": "STOPPED"}),
            mock.patch("workbuddy_qmt.manager.worker_main", return_value=0) as worker,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            result = start_worker(self.config_path, start_mode=mode, confirm=confirm, **values)
        return result, worker

    def test_start_synchronizes_all_modes_before_worker_runs(self):
        credit = self._enable_credit()
        for bundle in (self.bundle, credit):
            self._verify_profile(bundle)
        for mode in ("SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO", "OBSERVE_ONLY"):
            with self.subTest(mode=mode):
                def running_worker(argv):
                    self.assertEqual(_console(self.config_path, "status")["state"]["mode"], mode)
                    for bundle in (self.bundle, credit):
                        self.assertEqual(self._read(bundle["adapter_config"])["qmt_mode"], mode)
                    return 0

                with (
                    mock.patch("workbuddy_qmt.manager.probe_worker", return_value={"state": "STOPPED"}),
                    mock.patch("workbuddy_qmt.manager.worker_main", side_effect=running_worker) as worker,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = start_worker(self.config_path, start_mode=mode, confirm=mode)
                self.assertEqual(result, 0)
                worker.assert_called_once()

    def test_sync_preserves_custom_settings_and_signed_profile_bytes(self):
        self._verify_profile(self.bundle)
        adapter = self._read(self.bundle["adapter_config"])
        adapter.update({
            "field_map": {"available_cash": ["custom_available_cash"]},
            "order_status_map": {"custom": "QUEUED"},
            "adapter_max_notional": 12345,
            "custom_setting": {"retained": True},
        })
        atomic_write_json(self.bundle["adapter_config"], adapter)
        profile_before = self._bytes(self.bundle["mapping_profile"])
        adapter_before = self._bytes(self.bundle["adapter_config"])

        results = sync_adapter_modes(self.config_path, "MANUAL_LIVE")

        adapter["qmt_mode"] = "MANUAL_LIVE"
        self.assertEqual(self._read(self.bundle["adapter_config"]), adapter)
        self.assertEqual(self._bytes(self.bundle["mapping_profile"]), profile_before)
        self.assertEqual(self._bytes(results[0]["backup"]), adapter_before)

    def test_same_mode_leaves_file_bytes_and_mtime_unchanged(self):
        path = self.bundle["adapter_config"]
        before = self._bytes(path)
        modified = os.stat(path).st_mtime_ns

        result = sync_adapter_modes(self.config_path, "OBSERVE_ONLY")[0]

        self.assertFalse(result["changed"])
        self.assertIsNone(result["backup"])
        self.assertEqual(self._bytes(path), before)
        self.assertEqual(os.stat(path).st_mtime_ns, modified)

    def test_disabled_account_is_not_modified(self):
        credit = self._enable_credit()
        self._verify_profile(self.bundle)
        set_account_enabled(self.config_path, "main_credit", False)
        before = self._bytes(credit["adapter_config"])

        results = sync_adapter_modes(self.config_path, "SIM_SIGNAL")

        self.assertEqual([item["account_alias"] for item in results], ["main_stock"])
        self.assertEqual(self._bytes(credit["adapter_config"]), before)

    def test_invalid_second_account_stops_start_without_partial_sync_or_mode_change(self):
        credit = self._enable_credit()
        self._verify_profile(self.bundle)
        before = self._bytes(self.bundle["adapter_config"])
        atomic_write_bytes(credit["adapter_config"], b"{broken")

        with (
            mock.patch("workbuddy_qmt.manager.probe_worker", return_value={"state": "STOPPED"}),
            mock.patch("workbuddy_qmt.manager.worker_main") as worker,
        ):
            with self.assertRaises(ManagerError):
                start_worker(self.config_path, start_mode="SIM_SIGNAL", confirm="SIM_SIGNAL")

        worker.assert_not_called()
        self.assertEqual(self._bytes(self.bundle["adapter_config"]), before)
        self.assertEqual(_console(self.config_path, "status")["state"]["mode"], "OBSERVE_ONLY")

    def test_missing_adapter_allows_observation_but_blocks_trade_start(self):
        os.remove(self.bundle["adapter_config"])
        result, worker = self._start("OBSERVE_ONLY")
        self.assertEqual(result, 0)
        worker.assert_called_once()

        with self.assertRaises(ManagerError) as raised:
            self._start("SIM_SIGNAL", confirm="SIM_SIGNAL")
        self.assertEqual(raised.exception.code, "ADAPTER_SYNC_FAILED")

    def test_unverified_or_tampered_profile_blocks_trade_start(self):
        for tampered in (False, True):
            with self.subTest(tampered=tampered):
                if tampered:
                    self._verify_profile(self.bundle)
                    profile = self._read(self.bundle["mapping_profile"])
                    profile["qmt_build"] = "tampered"
                    atomic_write_json(self.bundle["mapping_profile"], profile)
                before = self._bytes(self.bundle["adapter_config"])
                with self.assertRaises(ManagerError) as raised:
                    self._start("MANUAL_LIVE", confirm="MANUAL_LIVE")
                self.assertEqual(raised.exception.code, "ADAPTER_SYNC_FAILED")
                self.assertEqual(self._bytes(self.bundle["adapter_config"]), before)
                self.assertEqual(_console(self.config_path, "status")["state"]["mode"], "OBSERVE_ONLY")

    def test_wrong_account_binding_is_not_repaired_by_mode_sync(self):
        adapter = self._read(self.bundle["adapter_config"])
        adapter["adapter_instance"] = "other-instance"
        atomic_write_json(self.bundle["adapter_config"], adapter)
        before = self._bytes(self.bundle["adapter_config"])

        with self.assertRaises(ManagerError) as raised:
            sync_adapter_modes(self.config_path, "OBSERVE_ONLY")

        self.assertEqual(raised.exception.code, "ADAPTER_SYNC_FAILED")
        self.assertEqual(self._bytes(self.bundle["adapter_config"]), before)

    def test_write_failure_rolls_back_accounts_and_stops_worker(self):
        credit = self._enable_credit()
        for bundle in (self.bundle, credit):
            self._verify_profile(bundle)
        before = {bundle["adapter_config"]: self._bytes(bundle["adapter_config"]) for bundle in (self.bundle, credit)}
        failed = False

        def write(path, content):
            nonlocal failed
            if path == credit["adapter_config"] and not failed:
                failed = True
                raise OSError("injected write failure")
            atomic_write_bytes(path, content)

        with mock.patch("workbuddy_qmt.manager.atomic_write_bytes", side_effect=write):
            with self.assertRaises(ManagerError) as raised:
                self._start("SIM_SIGNAL", confirm="SIM_SIGNAL")

        self.assertEqual(raised.exception.details["rollback_errors"], [])
        for path, content in before.items():
            self.assertEqual(self._bytes(path), content)
        self.assertEqual(_console(self.config_path, "status")["state"]["mode"], "OBSERVE_ONLY")

    def test_confirmation_failure_does_not_change_adapter(self):
        before = self._bytes(self.bundle["adapter_config"])
        with mock.patch("workbuddy_qmt.manager.sync_adapter_modes") as sync:
            with self.assertRaises(ManagerError):
                select_start_mode(self.config_path, requested="LIMITED_AUTO", confirm="wrong")
        sync.assert_not_called()
        self.assertEqual(self._bytes(self.bundle["adapter_config"]), before)

    def test_desktop_enter_downgrades_worker_and_adapter_to_observation(self):
        self._verify_profile(self.bundle)
        select_start_mode(self.config_path, requested="SIM_SIGNAL", confirm="SIM_SIGNAL")
        with (
            mock.patch("builtins.input", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            mode = select_start_mode(self.config_path, interactive=True)

        self.assertEqual(mode, "OBSERVE_ONLY")
        self.assertEqual(self._read(self.bundle["adapter_config"])["qmt_mode"], mode)
        self.assertEqual(_console(self.config_path, "status")["state"]["mode"], mode)

    def test_halted_start_downgrades_adapter_without_clearing_halt(self):
        self._verify_profile(self.bundle)
        select_start_mode(self.config_path, requested="LIMITED_AUTO", confirm="LIMITED_AUTO")
        _console(self.config_path, "halt", reason="test halt")

        result, worker = self._start(None)

        self.assertEqual(result, 0)
        worker.assert_called_once()
        self.assertEqual(self._read(self.bundle["adapter_config"])["qmt_mode"], "OBSERVE_ONLY")
        state = _console(self.config_path, "status")["state"]
        self.assertEqual(state["halted"], "true")
        self.assertEqual(state["mode"], "LIMITED_AUTO")

    def test_running_worker_is_not_reconfigured_on_repeated_start(self):
        with (
            mock.patch("workbuddy_qmt.manager.probe_worker", return_value={"state": "RUNNING"}),
            mock.patch("workbuddy_qmt.manager.sync_adapter_modes") as sync,
            mock.patch("workbuddy_qmt.manager.worker_main") as worker,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(start_worker(self.config_path, start_mode="OBSERVE_ONLY"), 0)
        sync.assert_not_called()
        worker.assert_not_called()


class RunningAdapterModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = initialize(self.temp.name, {"STOCK"})
        self.bundle = generate_qmt_bundle(self.config_path, "main_stock", "test-stock-001")
        adapter_path = os.path.join(os.path.dirname(workbuddy_qmt.__file__), "assets", "qmt_embedded_adapter.py")
        spec = importlib.util.spec_from_file_location("mode_reload_adapter", adapter_path)
        self.adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.adapter)
        self.adapter.ADAPTER_CONFIG_PATH = self.bundle["adapter_config"]
        self.adapter._load_config()
        self.adapter._STATUS = "READY"

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def _verify_profile(self):
        profile = self._read(self.bundle["mapping_profile"])
        config = self._read(self.bundle["adapter_config"])
        profile.update({"profile_id": "test-profile", "qmt_build": "test-qmt", "broker_build": "test-broker", "verified": True, "mappings": {"STOCK:BUY:LIMIT": {"op_type": 23, "price_type": 11}}})
        for name in ("profile_id", "qmt_build", "broker_build"):
            config["expected_" + name] = profile[name]
        profile["signature"] = KeyRing.load(load_config(self.config_path).key_file).sign(profile)
        atomic_write_json(self.bundle["mapping_profile"], profile)
        atomic_write_json(self.bundle["adapter_config"], config)
        self.adapter._load_config()

    def test_already_running_adapter_reads_mode_before_processing_commands(self):
        self._verify_profile()
        command = os.path.join(self.adapter._partition(), "commands", "pending.json")
        atomic_write_json(command, {"pending": True})

        for mode in ("SIM_SIGNAL", "MANUAL_LIVE", "LIMITED_AUTO", "OBSERVE_ONLY"):
            with self.subTest(mode=mode):
                atomic_write_json(command, {"pending": True})
                sync_adapter_modes(self.config_path, mode)
                seen = []
                with (
                    mock.patch.object(self.adapter, "_process_file", side_effect=lambda path: seen.append(self.adapter._CONFIG["qmt_mode"])),
                    mock.patch.object(self.adapter, "_archive"),
                ):
                    self.adapter.poll_commands()
                self.assertEqual(seen, [mode])
                self.assertEqual(self.adapter._STATUS, "READY")

    def test_invalid_config_retains_pending_command_and_recovers_on_repair(self):
        command = os.path.join(self.adapter._partition(), "commands", "pending.json")
        atomic_write_json(command, {"pending": True})
        original = self._read(self.bundle["adapter_config"])
        atomic_write_bytes(self.bundle["adapter_config"], b"{broken")

        with (
            mock.patch.object(self.adapter, "_process_file") as process,
            mock.patch.object(self.adapter, "_write_event") as event,
        ):
            self.adapter.poll_commands()
            self.adapter.poll_commands()
            process.assert_not_called()
            self.assertTrue(os.path.isfile(command))
            self.assertEqual(self.adapter._STATUS, "ERROR")
            self.assertEqual(self.adapter._CONFIG["qmt_mode"], "OBSERVE_ONLY")
            event.assert_called_once()
            self.assertEqual(event.call_args[0][0]["error_code"], "ADAPTER_MODE_SYNC_FAILED")

            atomic_write_json(self.bundle["adapter_config"], original)
            with mock.patch.object(self.adapter, "_archive"):
                self.adapter.poll_commands()
            process.assert_called_once_with(command)
            self.assertEqual(self.adapter._STATUS, "READY")

    def test_only_mode_changes_can_be_applied_without_restarting_strategy(self):
        config = self._read(self.bundle["adapter_config"])
        config["qmt_account_id"] = "different-account"
        atomic_write_json(self.bundle["adapter_config"], config)

        with mock.patch.object(self.adapter, "_write_event"):
            self.assertFalse(self.adapter._refresh_config_mode())

        self.assertEqual(self.adapter._STATUS, "ERROR")
        self.assertEqual(self.adapter._CONFIG["qmt_account_id"], "test-stock-001")

    def test_invalid_mode_and_unverified_profile_fail_closed(self):
        for mode in ("NOT_A_MODE", "SIM_SIGNAL"):
            with self.subTest(mode=mode):
                config = self._read(self.bundle["adapter_config"])
                config["qmt_mode"] = mode
                atomic_write_json(self.bundle["adapter_config"], config)
                with mock.patch.object(self.adapter, "_write_event"):
                    self.assertFalse(self.adapter._refresh_config_mode())
                self.assertEqual(self.adapter._CONFIG["qmt_mode"], "OBSERVE_ONLY")
                self.assertEqual(self.adapter._STATUS, "ERROR")

    def test_same_config_avoids_reloading_json_every_poll(self):
        self.assertTrue(self.adapter._refresh_config_mode())
        with mock.patch.object(self.adapter, "_load_json") as read:
            self.adapter.poll_commands()
            self.adapter.poll_commands()
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
