import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI lane.
    import tomli as tomllib

from scripts.release_guardrails import (
    APPROVED_DISPLAY_NAME,
    APPROVED_NODE_ID,
    APPROVED_PUBLISHER_ID,
    APPROVED_REPOSITORY,
    APPROVED_VERSION,
    ArchivePolicyError,
    build_release_archive,
    inspect_release_archive,
    load_release_metadata,
    preflight_registry,
    validate_release_metadata,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"


class ReleaseMetadataTests(unittest.TestCase):
    def test_approved_metadata_is_consistent(self):
        metadata = load_release_metadata(REPO_ROOT / "pyproject.toml")

        self.assertEqual(metadata.node_id, APPROVED_NODE_ID)
        self.assertEqual(metadata.publisher_id, APPROVED_PUBLISHER_ID)
        self.assertEqual(metadata.version, APPROVED_VERSION)
        self.assertEqual(metadata.display_name, APPROVED_DISPLAY_NAME)
        self.assertEqual(metadata.repository, APPROVED_REPOSITORY)
        validate_release_metadata(REPO_ROOT)

    def test_public_package_metadata_and_requirements_are_complete(self):
        metadata = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata["project"]
        self.assertEqual(project["readme"], "README.md")
        self.assertEqual(project["requires-python"], ">=3.10")
        self.assertEqual(
            project["urls"],
            {
                "Repository": APPROVED_REPOSITORY,
                "Documentation": f"{APPROVED_REPOSITORY}#readme",
                "Bug Tracker": f"{APPROVED_REPOSITORY}/issues",
            },
        )
        requirements = {
            line.strip()
            for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(set(project["dependencies"]), requirements)

    def test_invalid_semver_or_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            (root / "pyproject.toml").write_text(
                """[project]
name = "wrong"
version = "latest"

[project.urls]
Repository = "https://example.invalid"

[tool.comfy]
PublisherId = "wrong"
DisplayName = "wrong"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                validate_release_metadata(root)


class ReleaseArchiveTests(unittest.TestCase):
    def test_repo_archive_is_deterministic_and_runtime_only(self):
        with tempfile.TemporaryDirectory() as output_text:
            first = Path(output_text) / "first.zip"
            second = Path(output_text) / "second.zip"
            first_report = build_release_archive(REPO_ROOT, first)
            second_report = build_release_archive(REPO_ROOT, second)

            self.assertEqual(first_report.sha256, second_report.sha256)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            report = inspect_release_archive(first)
            self.assertGreater(report.file_count, 10)
            with zipfile.ZipFile(first) as archive:
                names = set(archive.namelist())
            for required in (
                "__init__.py",
                "LICENSE",
                "README.md",
                "pyproject.toml",
                "requirements.txt",
                "videohelpersuite/nodes.py",
                "web/js/VHS.core.js",
                "video_formats/h264-mp4.json",
            ):
                self.assertIn(required, names)
            for forbidden_prefix in (
                ".github/",
                ".planning/",
                ".sessions/",
                "reference/",
                "scripts/",
                "tests/",
                "testframework/",
            ):
                self.assertFalse(any(name.startswith(forbidden_prefix) for name in names))
            self.assertNotIn("ROADMAP.md", names)

    def test_inspector_rejects_path_traversal_and_secret_like_content(self):
        with tempfile.TemporaryDirectory() as output_text:
            traversal = Path(output_text) / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape.txt", "safe")
            with self.assertRaises(ArchivePolicyError):
                inspect_release_archive(traversal)

            secret = Path(output_text) / "secret.zip"
            with zipfile.ZipFile(secret, "w") as archive:
                archive.writestr("__init__.py", "safe")
                archive.writestr("pyproject.toml", "safe")
                archive.writestr("requirements.txt", "safe")
                archive.writestr("videohelpersuite/nodes.py", "ghp_" + "a" * 36)
                archive.writestr("web/js/VHS.core.js", "safe")
                archive.writestr("video_formats/h264-mp4.json", "{}")
            with self.assertRaises(ArchivePolicyError):
                inspect_release_archive(secret)

    def test_inspector_rejects_nested_internal_policy_files(self):
        with tempfile.TemporaryDirectory() as output_text:
            for index, forbidden in enumerate(
                ("docs/ROADMAP.md", "assets/.planning/note.md", "pkg/AGENTS.md")
            ):
                candidate = Path(output_text) / f"internal-{index}.zip"
                with zipfile.ZipFile(candidate, "w") as archive:
                    for required in (
                        "__init__.py",
                        "pyproject.toml",
                        "requirements.txt",
                        "videohelpersuite/nodes.py",
                        "web/js/VHS.core.js",
                        "video_formats/h264-mp4.json",
                    ):
                        archive.writestr(required, "{}")
                    archive.writestr(forbidden, "internal")
                with self.subTest(path=forbidden):
                    with self.assertRaises(ArchivePolicyError):
                        inspect_release_archive(candidate)


class RegistryPreflightTests(unittest.TestCase):
    def test_configured_credential_and_globally_missing_node_is_available(self):
        calls = []

        def fake_read(path, token_required):
            calls.append((path, token_required))
            if path == f"/nodes/{APPROVED_NODE_ID}":
                return 404, None
            raise AssertionError(path)

        result = preflight_registry(
            APPROVED_PUBLISHER_ID,
            APPROVED_NODE_ID,
            "not-logged",
            read_json=fake_read,
        )

        self.assertEqual(result, {"credential_configured": True, "publisher_id": APPROVED_PUBLISHER_ID, "node_id": APPROVED_NODE_ID, "node_state": "available"})
        self.assertEqual(
            calls,
            [
                (f"/nodes/{APPROVED_NODE_ID}", False),
            ],
        )

    def test_existing_node_requires_matching_public_publisher(self):
        def fake_read(path, token_required):
            if path == f"/nodes/{APPROVED_NODE_ID}":
                return 200, {"id": APPROVED_NODE_ID, "publisher": {"id": APPROVED_PUBLISHER_ID}}
            raise AssertionError(path)

        result = preflight_registry(
            APPROVED_PUBLISHER_ID,
            APPROVED_NODE_ID,
            "not-logged",
            read_json=fake_read,
        )
        self.assertEqual(result["node_state"], "owned")

    def test_invalid_registry_identifiers_fail_before_any_request(self):
        def fail_read(path, token_required):
            self.fail(f"unexpected request: {path}, token_required={token_required}")

        with self.assertRaises(ValueError):
            preflight_registry(
                "../publisher",
                APPROVED_NODE_ID,
                "not-logged",
                read_json=fail_read,
            )

    def test_foreign_node_collision_fails_closed(self):
        def fake_read(path, token_required):
            if path == f"/nodes/{APPROVED_NODE_ID}":
                return 200, {"id": APPROVED_NODE_ID, "publisher": {"id": "someone-else"}}
            raise AssertionError(path)

        with self.assertRaises(PermissionError):
            preflight_registry(
                APPROVED_PUBLISHER_ID,
                APPROVED_NODE_ID,
                "not-logged",
                read_json=fake_read,
            )


class PublishWorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.raw)
        cls.triggers = cls.workflow.get("on", cls.workflow.get(True))

    def test_automatic_version_push_and_manual_preflight_are_least_privilege(self):
        self.assertEqual(set(self.triggers), {"workflow_dispatch", "push"})
        self.assertEqual(self.triggers["push"]["branches"], ["main"])
        self.assertEqual(self.triggers["push"]["paths"], ["pyproject.toml"])
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(self.workflow["jobs"]), {"validate-package", "publish-node"})
        validate = self.workflow["jobs"]["validate-package"]
        publish = self.workflow["jobs"]["publish-node"]
        self.assertNotIn("environment", validate)
        self.assertNotIn("secrets.", json.dumps(validate))
        self.assertEqual(publish["needs"], "validate-package")
        self.assertNotIn("environment", publish)
        self.assertIn("github.repository_owner == 'rookiestar28'", validate["if"])
        self.assertIn("needs.validate-package.outputs.should_publish == 'true'", publish["if"])

    def test_actions_are_sha_pinned_and_checkout_drops_credentials(self):
        action_steps = [
            step
            for job in self.workflow["jobs"].values()
            for step in job["steps"]
            if "uses" in step
        ]
        self.assertTrue(action_steps)
        for step in action_steps:
            self.assertRegex(step["uses"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
        checkouts = [step for step in action_steps if step["uses"].startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 2)
        self.assertTrue(all(step["with"]["persist-credentials"] is False for step in checkouts))

    def test_identity_ref_confirmation_and_secret_isolation_are_explicit(self):
        lowered = self.raw.lower()
        self.assertIn(APPROVED_NODE_ID, lowered)
        self.assertIn(APPROVED_PUBLISHER_ID, lowered)
        self.assertIn("registry_access_token", lowered)
        self.assertEqual(lowered.count("secrets.registry_access_token"), 2)
        self.assertNotIn("comfy_registry_release_token", lowered)
        self.assertNotIn("release_environment_protected", lowered)
        self.assertIn("refs/heads/main", lowered)
        self.assertIn("approved-commit", lowered)
        self.assertIn("github_sha", lowered)
        self.assertIn("publish ", lowered)
        self.assertIn("github.event.before", lowered)
        self.assertIn("comfy-cli==1.12.0", lowered)
        self.assertIn("scripts/registry_publish_guard.py", lowered)
        run_source = "\n".join(
            step.get("run", "")
            for job in self.workflow["jobs"].values()
            for step in job["steps"]
        )
        self.assertNotIn("${{ github.event", run_source)
        self.assertNotIn("pull_request:", lowered)

    def test_archive_and_owner_preflight_gate_publish_command(self):
        validate_steps = self.workflow["jobs"]["validate-package"]["steps"]
        publish_steps = self.workflow["jobs"]["publish-node"]["steps"]
        validate_names = [step["name"] for step in validate_steps]
        publish_names = [step["name"] for step in publish_steps]
        self.assertLess(
            validate_names.index("Evaluate pushed version change"),
            validate_names.index("Validate automatic push metadata and exact archive"),
        )
        self.assertLess(
            publish_names.index("Registry collision and credential-presence preflight"),
            publish_names.index("Publish approved version to Comfy Registry"),
        )
        publish_command = next(
            step["run"]
            for step in publish_steps
            if step["name"] == "Publish approved version to Comfy Registry"
        )
        self.assertIn("comfy node publish", publish_command)
        self.assertNotIn("Comfy-Org/publish-node-action", self.raw)


class ReleaseCliTests(unittest.TestCase):
    def test_manual_publish_recovery_is_bound_to_exact_main_commit(self):
        current_commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        command = (
            sys.executable,
            "scripts/validate_release_metadata.py",
            "--expected-version",
            APPROVED_VERSION,
            "--operation",
            "publish",
            "--confirmation",
            f"PUBLISH {APPROVED_NODE_ID} {APPROVED_VERSION}",
            "--github-ref",
            "refs/heads/main",
            "--github-sha",
            current_commit,
            "--approved-commit",
            current_commit,
            "--dry-run",
        )
        accepted = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        mismatched_command = list(command)
        mismatched_command[-2] = "0" * 40
        mismatched = subprocess.run(
            mismatched_command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(mismatched.returncode, 2)
        self.assertIn("ERROR:", mismatched.stderr)

    def test_expected_policy_failures_are_concise_without_tracebacks(self):
        wrong_ref = subprocess.run(
            (
                sys.executable,
                "scripts/validate_release_metadata.py",
                "--expected-version",
                APPROVED_VERSION,
                "--operation",
                "publish",
                "--confirmation",
                f"PUBLISH {APPROVED_NODE_ID} {APPROVED_VERSION}",
                "--github-ref",
                "refs/heads/dev",
                "--dry-run",
            ),
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(wrong_ref.returncode, 2)
        self.assertIn("ERROR:", wrong_ref.stderr)
        self.assertNotIn("Traceback", wrong_ref.stderr)

        clean_env = os.environ.copy()
        clean_env.pop("REGISTRY_ACCESS_TOKEN", None)
        no_token = subprocess.run(
            (
                sys.executable,
                "scripts/registry_preflight.py",
                "--publisher",
                APPROVED_PUBLISHER_ID,
                "--node",
                APPROVED_NODE_ID,
            ),
            cwd=REPO_ROOT,
            env=clean_env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(no_token.returncode, 2)
        self.assertIn("ERROR:", no_token.stderr)
        self.assertNotIn("Traceback", no_token.stderr)


if __name__ == "__main__":
    unittest.main()
