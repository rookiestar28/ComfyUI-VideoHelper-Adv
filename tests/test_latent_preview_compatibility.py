"""Run real tensor/encoding contracts apart from the legacy host-dependency stubs."""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path


class LatentPreviewCompatibilityTests(unittest.TestCase):
    def test_real_tensor_and_jpeg_contracts_in_isolated_process(self):
        root = Path(__file__).resolve().parents[1]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        # CRITICAL: suite fixtures replace PIL/Torch registries; real JPEG plugins need a fresh interpreter.
        result = subprocess.run(
            [sys.executable, str(root / "tests" / "_latent_preview_contract_worker.py"), "-v"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        evidence = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, evidence)
        self.assertIsNotNone(re.search(r"Ran [1-9]\d* tests", evidence), evidence)


if __name__ == "__main__":
    unittest.main()
