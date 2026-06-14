import unittest
from pathlib import Path

from videohelpersuite.format_validation import (
    materialize_format_file,
    validate_format_data,
    validate_format_directory,
)


class VideoFormatValidationTests(unittest.TestCase):
    def test_all_format_json_files_pass_static_validation(self):
        results = validate_format_directory(Path("video_formats"), capabilities=None)
        failures = {
            result.name: {
                "errors": result.errors,
                "warnings": result.warnings,
            }
            for result in results
            if result.errors or result.warnings
        }
        self.assertEqual(failures, {})

    def test_materialize_webm_defaults_produces_explicit_codec(self):
        materialized = materialize_format_file(Path("video_formats/webm.json"))
        self.assertIn("-c:v", materialized["main_pass"])
        self.assertIn("libvpx-vp9", materialized["main_pass"])

    def test_supports_audio_true_requires_audio_pass(self):
        result = validate_format_data(
            "bad.json",
            {
                "extension": "mp4",
                "main_pass": ["-n", "-c:v", "libx264"],
                "supports_audio": True,
            },
            capabilities=None,
        )
        self.assertIn("'supports_audio' is true but 'audio_pass' is missing", result.errors)


if __name__ == "__main__":
    unittest.main()
