import os
import struct
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from scripts.runtime.run_path_policy_matrix import (
    _contained,
    _load_images_prompt,
    _policy_environment,
    _synthetic_png,
)


class PathPolicyRuntimeRunnerTests(unittest.TestCase):
    def test_policy_environment_removes_inherited_security_settings(self):
        inherited = {
            "PATH": os.environ.get("PATH", ""),
            "VHS_PATH_POLICY": "legacy_local",
            "VHS_EXTERNAL_READ_ROOTS": "private",
            "VHS_URL_POLICY": "https",
            "VHS_STRICT_PATHS": "0",
            "VHS_DEPLOYMENT_PROFILE": "trusted_local",
        }
        with patch.dict(os.environ, inherited, clear=True):
            environment = _policy_environment({"VHS_PATH_POLICY": "host_roots"})

        self.assertEqual(environment["VHS_PATH_POLICY"], "host_roots")
        self.assertEqual(environment["PATH"], inherited["PATH"])
        for name in (
            "VHS_EXTERNAL_READ_ROOTS",
            "VHS_URL_POLICY",
            "VHS_STRICT_PATHS",
            "VHS_DEPLOYMENT_PROFILE",
        ):
            self.assertNotIn(name, environment)

    def test_synthetic_fixture_is_a_valid_64_by_48_png(self):
        payload = _synthetic_png()
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        chunks = {}
        while offset < len(payload):
            length = struct.unpack(">I", payload[offset:offset + 4])[0]
            name = payload[offset + 4:offset + 8]
            data = payload[offset + 8:offset + 8 + length]
            chunks.setdefault(name, []).append(data)
            offset += 12 + length

        width, height = struct.unpack(">II", chunks[b"IHDR"][0][:8])
        self.assertEqual((width, height), (64, 48))
        scanlines = zlib.decompress(b"".join(chunks[b"IDAT"]))
        self.assertEqual(len(scanlines), 48 * (1 + 64 * 3))

    def test_load_prompt_exercises_path_loader_and_durable_output(self):
        fixture = _load_images_prompt(Path("/contained/input/images"), "runtime/policy")
        prompt = fixture["prompt"]
        self.assertEqual(prompt["1"]["class_type"], "VHS_LoadImagesPath")
        self.assertEqual(prompt["2"]["class_type"], "VHS_VideoCombine")
        self.assertEqual(prompt["2"]["inputs"]["images"], ["1", 0])
        self.assertEqual(prompt["2"]["inputs"]["format"], "video/h264-mp4")
        self.assertTrue(prompt["2"]["inputs"]["save_output"])

    def test_containment_rejects_sibling_prefix_and_accepts_child(self):
        root = Path("/workspace/project")
        self.assertTrue(_contained(root, root / "child"))
        self.assertFalse(_contained(root, Path("/workspace/project-sibling")))


if __name__ == "__main__":
    unittest.main()
