import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "videohelpersuite"


class PathPolicyCoverageTests(unittest.TestCase):
    def test_in_scope_callers_use_the_central_policy_boundary(self):
        expected_tokens = {
            "utils.py": ("PATH_POLICY", "PathCapability.READ_MEDIA"),
            "server.py": ("get_path_policy", "PathCapability.PREVIEW_MEDIA"),
            "load_images_nodes.py": ("authorize_path", "PathCapability.LIST_DIRECTORY"),
            "load_video_nodes.py": ("authorize_path", "PathCapability.READ_MEDIA"),
            "output_artifacts.py": ("_authorize_output_file", "PathCapability.DELETE_ARTIFACT"),
            "media_encode.py": ("_authorize_output_file", "_delete_output_files"),
            "video_combine.py": ("_authorize_output_file", "_delete_output_files"),
            "nodes.py": ("_delete_output_files", "validate_path"),
        }
        for filename, tokens in expected_tokens.items():
            source = (MODULE_ROOT / filename).read_text(encoding="utf-8")
            for token in tokens:
                self.assertIn(token, source, f"{filename} missing {token}")

    def test_destructive_filesystem_calls_are_confined_to_policy_owners(self):
        allowed_owners = {"utils.py", "server.py", "output_artifacts.py"}
        destructive_calls = []
        for path in MODULE_ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr in {"remove", "unlink", "rmdir", "rmtree"}:
                    destructive_calls.append((path.name, node.lineno, node.func.attr))

        self.assertTrue(destructive_calls)
        self.assertEqual(
            {filename for filename, _line, _name in destructive_calls} - allowed_owners,
            set(),
            destructive_calls,
        )

    def test_legacy_boolean_path_helper_is_not_used_by_in_scope_callers(self):
        for filename in (
            "server.py",
            "load_images_nodes.py",
            "load_video_nodes.py",
            "output_artifacts.py",
            "media_encode.py",
            "video_combine.py",
            "nodes.py",
        ):
            source = (MODULE_ROOT / filename).read_text(encoding="utf-8")
            self.assertNotIn("is_safe_path", source, filename)


if __name__ == "__main__":
    unittest.main()
