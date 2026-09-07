import json
import os
import tempfile
import unittest

from workbuddy_qmt.bootstrap import initialize
from workbuddy_qmt.manager import (
    RESET_PROFILE_CONFIRMATION,
    ManagerError,
    build_parser,
    generate_qmt_bundle,
    run_setup,
)


class ManagerProfileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = initialize(self.temp.name, {"STOCK"})
        self.bundle = generate_qmt_bundle(
            self.config_path, "main_stock", "test-account-001"
        )
        self.profile_path = self.bundle["mapping_profile"]
        with open(self.profile_path, "r", encoding="utf-8") as stream:
            profile = json.load(stream)
        profile.update({
            "profile_id": "verified-profile-001",
            "qmt_build": "qmt-test-build",
            "broker_build": "broker-test-build",
            "verified": True,
            "mappings": {"STOCK:BUY:LIMIT": {"operation": 23, "price_type": 11}},
            "signature": "preserve-this-signature",
        })
        with open(self.profile_path, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(profile, stream, ensure_ascii=False, sort_keys=True)
        with open(self.profile_path, "rb") as stream:
            self.signed_profile_bytes = stream.read()

    def _read_profile_bytes(self):
        with open(self.profile_path, "rb") as stream:
            return stream.read()

    def test_normal_regeneration_preserves_existing_profile(self):
        bundle = generate_qmt_bundle(
            self.config_path, "main_stock", "test-account-001"
        )

        self.assertTrue(bundle["profile_preserved"])
        self.assertFalse(bundle["profile_reset"])
        self.assertEqual(self._read_profile_bytes(), self.signed_profile_bytes)
        profile_result = bundle["files"][1]
        self.assertTrue(profile_result["preserved"])
        self.assertIsNone(profile_result["backup"])

    def test_force_only_overwrites_generated_files_not_profile(self):
        with open(self.bundle["adapter_config"], "w", encoding="utf-8") as stream:
            stream.write("{}")

        bundle = generate_qmt_bundle(
            self.config_path, "main_stock", "test-account-001", overwrite=True
        )

        self.assertTrue(bundle["profile_preserved"])
        self.assertEqual(self._read_profile_bytes(), self.signed_profile_bytes)
        self.assertIsNotNone(bundle["files"][0]["backup"])

    def test_non_interactive_setup_force_preserves_existing_profile(self):
        mcp_path = os.path.join(self.temp.name, "mcp.json")
        launcher_path = os.path.join(self.temp.name, "launcher.json")
        with open(mcp_path, "w", encoding="utf-8") as stream:
            json.dump({"mcpServers": {}}, stream)
        args = build_parser().parse_args([
            "--launcher-config", launcher_path,
            "setup",
            "--root", self.temp.name,
            "--mcp-config", mcp_path,
            "--stock-account-id", "test-account-001",
            "--non-interactive",
            "--no-shortcuts",
            "--force",
        ])

        run_setup(args, launcher_path)

        self.assertEqual(self._read_profile_bytes(), self.signed_profile_bytes)

    def test_profile_reset_requires_dedicated_confirmation(self):
        with self.assertRaises(ManagerError) as raised:
            generate_qmt_bundle(
                self.config_path, "main_stock", "test-account-001",
                reset_profile=True,
            )

        self.assertEqual(raised.exception.code, "LOCAL_CONFIRMATION_REQUIRED")
        self.assertEqual(self._read_profile_bytes(), self.signed_profile_bytes)

    def test_confirmed_profile_reset_creates_backup(self):
        bundle = generate_qmt_bundle(
            self.config_path, "main_stock", "test-account-001",
            reset_profile=True,
            reset_profile_confirmation=RESET_PROFILE_CONFIRMATION,
        )

        self.assertFalse(bundle["profile_preserved"])
        self.assertTrue(bundle["profile_reset"])
        profile_result = bundle["files"][1]
        self.assertTrue(profile_result["changed"])
        self.assertTrue(os.path.exists(profile_result["backup"]))
        with open(profile_result["backup"], "rb") as stream:
            self.assertEqual(stream.read(), self.signed_profile_bytes)
        with open(self.profile_path, "r", encoding="utf-8") as stream:
            reset_profile = json.load(stream)
        self.assertFalse(reset_profile["verified"])
        self.assertEqual(reset_profile["mappings"], {})
        self.assertEqual(reset_profile["signature"], "")

    def test_parser_requires_target_alias_for_explicit_reset(self):
        parser = build_parser()
        args = parser.parse_args([
            "setup",
            "--reset-profile", "main_stock",
            "--confirm-reset-profile", RESET_PROFILE_CONFIRMATION,
        ])

        self.assertEqual(args.reset_profile, ["main_stock"])
        self.assertEqual(args.confirm_reset_profile, RESET_PROFILE_CONFIRMATION)


if __name__ == "__main__":
    unittest.main()
