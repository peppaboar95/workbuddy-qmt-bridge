import ast
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from workbuddy_qmt.bootstrap import initialize
from workbuddy_qmt import config as config_module
from workbuddy_qmt.config import load_config
from workbuddy_qmt.errors import BridgeError
from workbuddy_qmt.manager import (
    RESET_PROFILE_CONFIRMATION, ManagerError, _adapter_sync_issues, build_parser, generate_qmt_bundle,
    run_account_command,
)
from workbuddy_qmt.security import KeyRing
from workbuddy_qmt.util import atomic_write_json


class MmapDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = initialize(self.temp.name, {"STOCK"})

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def read_bytes(self, path):
        with open(path, "rb") as stream:
            return stream.read()

    def update_accounts(self, **changes):
        raw = self.read_json(self.config_path)
        raw["accounts"][0].update(changes)
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump(raw, stream)
        return raw

    def configure(self, *options):
        args = build_parser().parse_args([
            "account", "configure", "main_stock", "--qmt-account-id", "stock-test-001",
        ] + list(options))
        output = io.StringIO()
        with mock.patch("workbuddy_qmt.manager.probe_worker", return_value={"state": "STOPPED"}), contextlib.redirect_stdout(output):
            result = run_account_command(args, self.config_path)
        self.assertEqual(result, 0)
        return json.loads(output.getvalue())

    def bootstrap(self, path):
        with open(path, "r", encoding="gbk") as stream:
            tree = ast.parse(stream.read())
        values = [ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and any(
                      isinstance(target, ast.Name) and target.id == "MMAP_BOOTSTRAP"
                      for target in node.targets)]
        self.assertEqual(len(values), 1)
        return values[0]

    def test_legacy_configuration_defaults_to_file_without_new_adapter_fields(self):
        config = load_config(self.config_path)
        account = config.account("main_stock")
        self.assertEqual(getattr(account, "adapter_transport", None), "file")
        bundle = generate_qmt_bundle(self.config_path, account.alias, "stock-test-001")
        adapter = self.read_json(bundle["adapter_config"])
        self.assertNotIn("adapter_transport", adapter)
        self.assertNotIn("mmap_name", adapter)
        self.assertNotIn("mmap_size", adapter)
        with open(bundle["adapter_script"], "r", encoding="gbk") as stream:
            script = stream.read()
        self.assertIn("ADAPTER_CONFIG_PATH = " + json.dumps(bundle["adapter_config"], ensure_ascii=True), script)

    def test_transport_rejects_unknown_values_and_wrong_types(self):
        for value in ("socket", "MMAP", "", None, True, 1, []):
            with self.subTest(value=value):
                self.update_accounts(adapter_transport=value)
                with self.assertRaises(BridgeError):
                    load_config(self.config_path)

    def test_mmap_size_enforces_integer_bounds(self):
        for value in (65535, 16777217, True, 65536.0, "65536", None):
            with self.subTest(value=value):
                self.update_accounts(adapter_transport="mmap", mmap_size=value)
                with self.assertRaises(BridgeError):
                    load_config(self.config_path)
        for value in (65536, 16777216):
            self.update_accounts(adapter_transport="mmap", mmap_size=value)
            self.assertEqual(load_config(self.config_path).account("main_stock").mmap_size, value)

    def test_mmap_name_rejects_names_outside_safe_local_namespace(self):
        for value in ("Global\\x", "Local\\", "Local\\../x", "Local\\a.b", "Local\\a\\b", "Local\\映射", "Local/x", None, 42):
            with self.subTest(value=value):
                self.update_accounts(adapter_transport="mmap", mmap_name=value)
                with self.assertRaises(BridgeError):
                    load_config(self.config_path)
        self.update_accounts(adapter_transport="mmap", mmap_name="Local\\WBQMT_account-01")
        self.assertEqual(load_config(self.config_path).account("main_stock").mmap_name, "Local\\WBQMT_account-01")

    def test_default_mapping_name_is_stable_and_isolates_installations_and_instances(self):
        self.assertTrue(callable(getattr(config_module, "effective_mmap_name", None)), "effective mapping name derivation is missing")
        derive = config_module.effective_mmap_name
        self.update_accounts(adapter_transport="mmap")
        config = load_config(self.config_path)
        stock = config.account("main_stock")
        name = derive(config.data_dir, stock)
        self.assertRegex(name, r"^Local\\WBQMT_[0-9a-f]+$")
        self.assertEqual(name, derive(os.path.join(config.data_dir, "..", "data"), stock))
        self.assertNotEqual(name, derive(os.path.join(self.temp.name, "other-data"), stock))
        self.assertNotEqual(name, derive(config.data_dir, config.accounts["main_credit"]))
        self.assertEqual(stock.mmap_size, 4194304)

    def test_enabled_accounts_cannot_share_a_mapping_but_disabled_account_can(self):
        raw = self.update_accounts(adapter_transport="mmap", mmap_name="Local\\WBQMT_shared")
        raw["accounts"][1].update(adapter_transport="mmap", mmap_name="Local\\WBQMT_shared", enabled=True)
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump(raw, stream)
        with self.assertRaisesRegex(BridgeError, "mapping|mmap"):
            load_config(self.config_path)
        raw["accounts"][1]["enabled"] = False
        with open(self.config_path, "w", encoding="utf-8") as stream:
            json.dump(raw, stream)
        load_config(self.config_path)

    def test_configure_without_transport_options_preserves_bridge_bytes(self):
        before = self.read_bytes(self.config_path)
        self.configure()
        self.assertEqual(self.read_bytes(self.config_path), before)

    def test_configure_persists_only_explicit_options_and_preserves_profile(self):
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        profile = self.read_json(bundle["mapping_profile"])
        profile["signature"] = "existing-signature-must-stay"
        with open(bundle["mapping_profile"], "w", encoding="utf-8") as stream:
            json.dump(profile, stream)
        before = self.read_bytes(bundle["mapping_profile"])
        result = self.configure("--transport", "mmap", "--force")
        raw_account = self.read_json(self.config_path)["accounts"][0]
        self.assertEqual(raw_account["adapter_transport"], "mmap")
        self.assertNotIn("mmap_name", raw_account)
        self.assertNotIn("mmap_size", raw_account)
        self.assertEqual(self.read_bytes(bundle["mapping_profile"]), before)
        self.assertTrue(result["profile_preserved"])
        result = self.configure("--mmap-name", "Local\\WBQMT_explicit", "--mmap-size", "131072", "--force")
        raw_account = self.read_json(self.config_path)["accounts"][0]
        self.assertEqual(raw_account["adapter_transport"], "mmap")
        self.assertEqual(raw_account["mmap_name"], "Local\\WBQMT_explicit")
        self.assertEqual(raw_account["mmap_size"], 131072)

    def test_invalid_transport_configuration_rolls_back_bridge(self):
        before = self.read_bytes(self.config_path)
        with self.assertRaises(BridgeError):
            self.configure("--transport", "mmap", "--mmap-name", "Global\\bad")
        self.assertEqual(self.read_bytes(self.config_path), before)

    def test_generation_failure_rolls_back_requested_transport_change(self):
        generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        before = self.read_bytes(self.config_path)
        with self.assertRaises(ManagerError) as raised:
            self.configure("--transport", "mmap")
        self.assertEqual(raised.exception.code, "FILE_EXISTS")
        self.assertEqual(self.read_bytes(self.config_path), before)

    def test_mmap_bundle_binds_mapping_and_keeps_external_worker_paths(self):
        self.update_accounts(adapter_transport="mmap", mmap_name="Local\\WBQMT_deploy", mmap_size=131072)
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        adapter = self.read_json(bundle["adapter_config"])
        self.assertEqual(adapter["adapter_transport"], "mmap")
        self.assertEqual(adapter["mmap_name"], "Local\\WBQMT_deploy")
        self.assertEqual(adapter["mmap_size"], 131072)
        for name in ("data_dir", "key_file", "mapping_profile"):
            self.assertTrue(os.path.isabs(adapter[name]))
        bootstrap = self.bootstrap(bundle["adapter_script"])
        self.assertEqual(bootstrap["mapping_name"], "Local\\WBQMT_deploy")
        self.assertEqual(bootstrap["mapping_size"], 131072)
        self.assertEqual(bootstrap["account_alias"], "main_stock")
        self.assertEqual(bootstrap["key_data"], self.read_json(adapter["key_file"]))
        self.assertNotIn(self.read_bytes(load_config(self.config_path).worker_token_file).decode("utf-8"), repr(bootstrap))

    def test_mmap_sync_validates_bootstrap_and_detects_wrong_binding(self):
        self.update_accounts(adapter_transport="mmap", mmap_name="Local\\WBQMT_deploy")
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        config = load_config(self.config_path)
        account = config.account("main_stock")
        paths = (bundle["adapter_script"], bundle["adapter_config"], bundle["mapping_profile"])
        self.assertEqual(_adapter_sync_issues(config, account, *paths), [])
        with open(bundle["adapter_script"], "r", encoding="gbk") as stream:
            source = stream.read()
        source = source.replace("Local\\\\WBQMT_deploy", "Local\\\\WBQMT_wrong")
        with open(bundle["adapter_script"], "w", encoding="gbk") as stream:
            stream.write(source)
        self.assertTrue(_adapter_sync_issues(config, account, *paths))

    def test_cli_mmap_redeployment_preserves_signed_p0_bindings_and_custom_settings(self):
        self.update_accounts(adapter_transport="mmap")
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        adapter = self.read_json(bundle["adapter_config"])
        profile = self.read_json(bundle["mapping_profile"])
        profile.update(profile_id="p0-verified-profile", qmt_build="verified-qmt-build",
                       broker_build="verified-broker-build", verified=True,
                       mappings={"STOCK:STOCK:BUY:LIMIT": {
                           "op_type": 23, "order_type": 1101, "price_type": 11,
                       }})
        for name in ("profile_id", "qmt_build", "broker_build"):
            adapter["expected_" + name] = profile[name]
        keyring = KeyRing.load(load_config(self.config_path).key_file)
        profile["signature"] = keyring.sign(profile)
        atomic_write_json(bundle["mapping_profile"], profile)
        profile_before = self.read_bytes(bundle["mapping_profile"])
        adapter.update({
            "field_map": {"available_cash": ["broker_cash"]},
            "order_status_map": {"reported": "REPORTED"},
            "console_heartbeat_seconds": 45, "max_batch": 7,
            "p0_probe_enabled": True, "custom_setting": {"retained": True},
            "qmt_mode": "SIM_SIGNAL", "data_dir": "old-data", "key_file": "old-key",
            "mapping_profile": "old-profile", "strategy_name": "old-strategy",
            "mmap_name": "Local\\WBQMT_old", "mmap_size": 65536,
            "adapter_max_notional": 1, "adapter_max_auto_orders": 1,
        })
        atomic_write_json(bundle["adapter_config"], adapter)
        result = self.configure("--force")
        regenerated = self.read_json(bundle["adapter_config"])
        for name in ("expected_profile_id", "expected_qmt_build", "expected_broker_build",
                     "field_map", "order_status_map", "console_heartbeat_seconds",
                     "max_batch", "p0_probe_enabled", "custom_setting"):
            self.assertEqual(regenerated[name], adapter[name])
        config = load_config(self.config_path)
        self.assertEqual(regenerated["qmt_mode"], "OBSERVE_ONLY")
        self.assertEqual(regenerated["data_dir"], config.data_dir)
        self.assertEqual(regenerated["key_file"], config.key_file)
        self.assertEqual(regenerated["mapping_profile"], bundle["mapping_profile"])
        self.assertEqual(regenerated["strategy_name"], "WorkBuddyQMT")
        self.assertEqual(regenerated["mmap_size"], 4194304)
        self.assertNotEqual(regenerated["mmap_name"], "Local\\WBQMT_old")
        self.assertEqual(regenerated["adapter_max_notional"], 200000)
        self.assertEqual(regenerated["adapter_max_auto_orders"], 1000)
        self.assertEqual(self.bootstrap(bundle["adapter_script"])["config"], regenerated)
        self.assertEqual(self.read_bytes(bundle["mapping_profile"]), profile_before)
        keyring.verify(self.read_json(bundle["mapping_profile"]))
        self.assertTrue(result["profile_preserved"])
        self.assertEqual(_adapter_sync_issues(config, config.account("main_stock"),
                         bundle["adapter_script"], bundle["adapter_config"], bundle["mapping_profile"]), [])

    def test_mmap_regeneration_preserves_credit_wrapper_configuration(self):
        raw = self.read_json(self.config_path)
        raw["accounts"][1].update(enabled=True, adapter_transport="mmap")
        atomic_write_json(self.config_path, raw)
        bundle = generate_qmt_bundle(self.config_path, "main_credit", "credit-test-001")
        adapter = self.read_json(bundle["adapter_config"])
        custom = {
            "credit_query_wrapper_verified": True, "credit_final_check_verified": True,
            "eligibility_allowed_values": {"eligible": [True]},
            "credit_field_map": {"available_cash": ["broker_credit_cash"]},
        }
        adapter.update(custom)
        adapter["credit_query_cooldown_seconds"] = 1
        atomic_write_json(bundle["adapter_config"], adapter)
        generate_qmt_bundle(self.config_path, "main_credit", "credit-test-001", overwrite=True)
        regenerated = self.read_json(bundle["adapter_config"])
        for name, value in custom.items():
            self.assertEqual(regenerated[name], value)
        self.assertEqual(regenerated["credit_query_cooldown_seconds"], 180)
        self.assertEqual(self.bootstrap(bundle["adapter_script"])["config"], regenerated)

    def test_mmap_regeneration_never_borrows_settings_from_a_different_account_binding(self):
        self.update_accounts(adapter_transport="mmap")
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        original = self.read_json(bundle["adapter_config"])
        for field, value in (("account_alias", "other"), ("account_type", "CREDIT"),
                             ("adapter_instance", "other-instance"), ("qmt_account_id", "other-account")):
            with self.subTest(field=field):
                adapter = dict(original, expected_profile_id="belongs-to-another-account",
                               field_map={"available_cash": ["old-account-cash"]})
                adapter[field] = value
                atomic_write_json(bundle["adapter_config"], adapter)
                generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001", overwrite=True)
                regenerated = self.read_json(bundle["adapter_config"])
                self.assertEqual(regenerated["expected_profile_id"], "REPLACE_AFTER_P0")
                self.assertEqual(regenerated["field_map"], {})

    def test_mmap_redeployment_rejects_invalid_preserved_profile_binding(self):
        self.update_accounts(adapter_transport="mmap")
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        original = self.read_json(bundle["adapter_config"])
        for invalid in (None, "", [], 42):
            with self.subTest(value=invalid):
                adapter = dict(original, expected_profile_id=invalid)
                atomic_write_json(bundle["adapter_config"], adapter)
                before = self.read_bytes(bundle["adapter_config"])
                with self.assertRaises(ManagerError):
                    generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001", overwrite=True)
                self.assertEqual(self.read_bytes(bundle["adapter_config"]), before)

    def test_explicit_mmap_profile_reset_restores_expected_placeholders(self):
        self.update_accounts(adapter_transport="mmap")
        bundle = generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001")
        adapter = self.read_json(bundle["adapter_config"])
        adapter.update(expected_profile_id="old-profile", expected_qmt_build="old-qmt",
                       expected_broker_build="old-broker", field_map={"available_cash": ["broker_cash"]})
        atomic_write_json(bundle["adapter_config"], adapter)
        generate_qmt_bundle(self.config_path, "main_stock", "stock-test-001", overwrite=True,
                            reset_profile=True, reset_profile_confirmation=RESET_PROFILE_CONFIRMATION)
        regenerated = self.read_json(bundle["adapter_config"])
        for name in ("expected_profile_id", "expected_qmt_build", "expected_broker_build"):
            self.assertEqual(regenerated[name], "REPLACE_AFTER_P0")
        self.assertEqual(regenerated["field_map"], adapter["field_map"])
        self.assertFalse(self.read_json(bundle["mapping_profile"])["verified"])


if __name__ == "__main__":
    unittest.main()
