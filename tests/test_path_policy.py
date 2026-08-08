import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._support import TempWorkspace


class PathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        self.input_dir = self.workspace.path / "input"
        self.output_dir = self.workspace.path / "output"
        self.temp_dir = self.workspace.path / "temp"
        self.external_dir = self.workspace.path / "external"
        self.outside_dir = self.workspace.path / "outside"
        for directory in (
            self.input_dir,
            self.output_dir,
            self.temp_dir,
            self.external_dir,
            self.outside_dir,
        ):
            directory.mkdir()

        self.input_file = self.input_dir / "input.mp4"
        self.external_file = self.external_dir / "external.mp4"
        self.outside_file = self.outside_dir / "outside.mp4"
        for path in (self.input_file, self.external_file, self.outside_file):
            path.write_bytes(b"synthetic")

        from videohelpersuite.path_policy import (
            PathAccessDenied,
            PathCapability,
            PolicyConfigurationError,
            URLAccessDenied,
            build_path_policy,
        )

        self.PathAccessDenied = PathAccessDenied
        self.PathCapability = PathCapability
        self.PolicyConfigurationError = PolicyConfigurationError
        self.URLAccessDenied = URLAccessDenied
        self.build_policy = build_path_policy

    def tearDown(self):
        self.workspace.cleanup()

    @property
    def host_roots(self):
        return {
            "input": str(self.input_dir),
            "output": str(self.output_dir),
            "temp": str(self.temp_dir),
        }

    def policy(self, environ=None, listen="127.0.0.1"):
        return self.build_policy(
            environ={} if environ is None else environ,
            host_roots=self.host_roots,
            listen=listen,
        )

    def test_default_host_roots_separate_read_write_and_delete_capabilities(self):
        policy = self.policy()

        read = policy.authorize_path(self.input_file, self.PathCapability.READ_MEDIA)
        self.assertEqual(read.root_id, "input")
        self.assertEqual(Path(read.canonical), self.input_file.resolve())

        for capability in (
            self.PathCapability.WRITE_OUTPUT,
            self.PathCapability.DELETE_ARTIFACT,
        ):
            with self.assertRaises(self.PathAccessDenied):
                policy.authorize_path(self.input_file, capability)

        new_output = self.output_dir / "new.mp4"
        self.assertEqual(
            policy.authorize_path(new_output, self.PathCapability.WRITE_OUTPUT).root_id,
            "output",
        )
        self.assertEqual(
            policy.authorize_path(new_output, self.PathCapability.DELETE_ARTIFACT).root_id,
            "output",
        )

    def test_outside_sibling_traversal_malformed_and_cross_drive_fail_closed(self):
        policy = self.policy()
        sibling_prefix = self.workspace.path / "input-sibling" / "clip.mp4"
        sibling_prefix.parent.mkdir()
        sibling_prefix.write_bytes(b"synthetic")

        for candidate in (
            self.outside_file,
            sibling_prefix,
            self.input_dir / ".." / "outside" / "outside.mp4",
            "bad\x00path",
        ):
            with self.assertRaises(self.PathAccessDenied):
                policy.authorize_path(candidate, self.PathCapability.READ_MEDIA)

        if os.name == "nt":
            with self.assertRaises(self.PathAccessDenied):
                policy.authorize_path("Z:\\untrusted\\clip.mp4", self.PathCapability.READ_MEDIA)

    def test_external_allowlist_grants_read_list_preview_but_never_write_delete(self):
        policy = self.policy({
            "VHS_PATH_POLICY": "allowlist",
            "VHS_EXTERNAL_READ_ROOTS": str(self.external_dir),
        })

        for capability in (
            self.PathCapability.READ_MEDIA,
            self.PathCapability.LIST_DIRECTORY,
            self.PathCapability.PREVIEW_MEDIA,
        ):
            authorized = policy.authorize_path(self.external_file, capability)
            self.assertTrue(authorized.root_id.startswith("external_"))

        for capability in (
            self.PathCapability.WRITE_OUTPUT,
            self.PathCapability.DELETE_ARTIFACT,
        ):
            with self.assertRaises(self.PathAccessDenied):
                policy.authorize_path(self.external_file, capability)

    def test_allowlist_roots_require_explicit_mode_and_existing_directories(self):
        with self.assertRaises(self.PolicyConfigurationError):
            self.policy({"VHS_EXTERNAL_READ_ROOTS": str(self.external_dir)})
        with self.assertRaises(self.PolicyConfigurationError):
            self.policy({"VHS_PATH_POLICY": "allowlist"})
        with self.assertRaises(self.PolicyConfigurationError):
            self.policy({
                "VHS_PATH_POLICY": "allowlist",
                "VHS_EXTERNAL_READ_ROOTS": str(self.workspace.path / "missing"),
            })

    def test_overlapping_host_roots_fail_configuration(self):
        nested_temp = self.output_dir / "nested-temp"
        nested_temp.mkdir()
        with self.assertRaises(self.PolicyConfigurationError):
            self.build_policy(
                environ={},
                host_roots={
                    "input": str(self.input_dir),
                    "output": str(self.output_dir),
                    "temp": str(nested_temp),
                },
                listen="127.0.0.1",
            )

    def test_legacy_local_is_explicit_and_invalid_for_remote_profile(self):
        local = self.policy({"VHS_PATH_POLICY": "legacy_local"})
        self.assertEqual(
            Path(local.authorize_path(
                self.outside_file,
                self.PathCapability.READ_MEDIA,
            ).canonical),
            self.outside_file.resolve(),
        )
        with self.assertRaises(self.PathAccessDenied):
            local.authorize_path(self.outside_file, self.PathCapability.DELETE_ARTIFACT)

        with self.assertRaises(self.PolicyConfigurationError):
            self.policy(
                {"VHS_PATH_POLICY": "legacy_local"},
                listen="0.0.0.0",
            )

    def test_migration_alias_is_host_roots_only_and_conflicts_with_new_setting(self):
        alias = self.policy({"VHS_STRICT_PATHS": "false"})
        self.assertEqual(alias.filesystem_mode.value, "host_roots")
        with self.assertRaises(self.PathAccessDenied):
            alias.authorize_path(self.outside_file, self.PathCapability.READ_MEDIA)

        with self.assertRaises(self.PolicyConfigurationError):
            self.policy({
                "VHS_STRICT_PATHS": "1",
                "VHS_PATH_POLICY": "host_roots",
            })

    def test_deployment_profile_and_url_defaults_are_server_owned(self):
        local = self.policy({"VHS_DESKTOP_IS_REMOTE": "true"})
        self.assertEqual(local.deployment_profile.value, "trusted_local")
        self.assertEqual(local.url_mode.value, "https")

        remote = self.policy({"VHS_DESKTOP_IS_REMOTE": "false"}, listen="0.0.0.0,::")
        self.assertEqual(remote.deployment_profile.value, "remote_restricted")
        self.assertEqual(remote.url_mode.value, "disabled")

        forced_remote = self.policy({"VHS_DEPLOYMENT_PROFILE": "remote_restricted"})
        self.assertEqual(forced_remote.deployment_profile.value, "remote_restricted")

        with self.assertRaises(self.PolicyConfigurationError):
            self.policy(
                {"VHS_DEPLOYMENT_PROFILE": "trusted_local"},
                listen="0.0.0.0",
            )

        unknown = self.policy({}, listen=None)
        self.assertEqual(unknown.deployment_profile.value, "remote_restricted")
        self.assertEqual(unknown.url_mode.value, "disabled")

    def test_desktop_bridge_values_cannot_change_any_filesystem_capability(self):
        capability_matrix = []
        for value in (None, "true", "false", "forged", ""):
            environ = {} if value is None else {"VHS_DESKTOP_IS_REMOTE": value}
            policy = self.policy(environ)
            outcomes = []
            for candidate in (self.input_file, self.output_dir / "new.mp4", self.outside_file):
                for capability in self.PathCapability:
                    try:
                        policy.authorize_path(candidate, capability)
                    except self.PathAccessDenied:
                        outcomes.append(False)
                    else:
                        outcomes.append(True)
            capability_matrix.append(outcomes)

        self.assertTrue(all(row == capability_matrix[0] for row in capability_matrix))

    @unittest.skipUnless(os.name == "nt", "Windows case normalization coverage")
    def test_windows_mixed_case_containment_preserves_authorization(self):
        mixed_case_candidate = str(self.input_file).swapcase()
        authorized = self.policy().authorize_path(
            mixed_case_candidate,
            self.PathCapability.READ_MEDIA,
        )
        self.assertEqual(authorized.root_id, "input")

    def test_url_policy_rejects_unsupported_sensitive_and_private_targets(self):
        local = self.policy()
        accepted = local.validate_url("https://example.com/video.mp4?quality=high")
        self.assertEqual(accepted.scheme, "https")

        for url in (
            "http://example.com/video.mp4",
            "file:///tmp/video.mp4",
            "https://user:password@example.com/video.mp4",  # pragma: allowlist secret
            "https://example.com/video.mp4#fragment",
            "https:///missing-host.mp4",
        ):
            with self.assertRaises(self.URLAccessDenied):
                local.validate_url(url)

        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with self.assertRaises(self.URLAccessDenied):
                local.authorize_url_network(
                    "https://example.com/video.mp4",
                    resolver=lambda _host, address=address: [address],
                )

        self.assertEqual(
            local.authorize_url_network(
                "https://example.com/video.mp4",
                resolver=lambda _host: ["8.8.8.8"],
            ).host,
            "example.com",
        )

        remote = self.policy({}, listen="0.0.0.0")
        with self.assertRaises(self.URLAccessDenied):
            remote.validate_url("https://example.com/video.mp4")

    def test_denials_and_capability_summary_do_not_expose_private_paths(self):
        policy = self.policy()
        with self.assertRaises(self.PathAccessDenied) as captured:
            policy.authorize_path(self.outside_file, self.PathCapability.READ_MEDIA)

        message = str(captured.exception)
        self.assertNotIn(str(self.outside_file), message)
        self.assertNotIn(str(self.workspace.path), message)
        self.assertEqual(
            set(policy.capability_summary()),
            {"deployment_profile", "filesystem_policy", "url_policy", "legacy_alias"},
        )
        self.assertNotIn(str(self.workspace.path), repr(policy.capability_summary()))

    def test_symlink_escape_is_rejected_when_supported(self):
        link = self.input_dir / "linked.mp4"
        try:
            link.symlink_to(self.outside_file)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        with self.assertRaises(self.PathAccessDenied):
            self.policy().authorize_path(link, self.PathCapability.READ_MEDIA)

    def test_canonical_escape_is_rejected_without_platform_symlink_privileges(self):
        policy = self.policy()
        apparent_input = self.input_dir / "redirected.mp4"
        original_realpath = os.path.realpath

        def simulate_link_resolution(candidate):
            if os.path.abspath(candidate) == os.path.abspath(apparent_input):
                return str(self.outside_file.resolve())
            return original_realpath(candidate)

        with patch(
            "videohelpersuite.path_policy.os.path.realpath",
            side_effect=simulate_link_resolution,
        ):
            with self.assertRaises(self.PathAccessDenied):
                policy.authorize_path(
                    apparent_input,
                    self.PathCapability.READ_MEDIA,
                )


if __name__ == "__main__":
    unittest.main()
