import os
import unittest
from unittest.mock import patch

from tests._support import TempWorkspace, install_base_stubs, import_fresh, purge_modules


class UtilsReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.policy_environment = {
            name: os.environ.pop(name, None)
            for name in (
                "VHS_DEPLOYMENT_PROFILE",
                "VHS_PATH_POLICY",
                "VHS_EXTERNAL_READ_ROOTS",
                "VHS_URL_POLICY",
                "VHS_STRICT_PATHS",
            )
        }
        self.workspace = TempWorkspace()
        purge_modules("videohelpersuite.utils", "videohelpersuite.path_policy", "videohelpersuite.logger", "server", "folder_paths", "comfy", "torch")
        self.paths = install_base_stubs(self.workspace.path)
        self.utils = import_fresh("videohelpersuite.utils")

    def tearDown(self):
        self.workspace.cleanup()
        for name, value in self.policy_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_validate_path_allows_supported_urls(self):
        self.assertTrue(self.utils.validate_path("https://example.com/video.mp4"))

    def test_validate_path_rejects_urls_when_disabled(self):
        self.assertEqual(
            self.utils.validate_path("https://example.com/video.mp4", allow_url=False),
            "URLs are unsupported for this path",
        )

    def test_validate_path_default_rejects_file_outside_host_roots(self):
        outside = self.workspace.path / "outside.mp4"
        outside.write_bytes(b"synthetic")

        result = self.utils.validate_path(str(outside))

        self.assertIsInstance(result, str)
        self.assertNotIn(str(outside), result)
        self.assertNotIn(str(self.workspace.path), result)

    def test_try_download_video_reuses_existing_cached_file(self):
        cached_file = self.paths["temp_dir"] / "cached.mp4"
        cached_file.write_bytes(b"video")
        self.utils.ytdl_path = "yt-dlp"
        url = "https://example.com/video.mp4"
        self.utils.download_history[self.utils.download_cache_key(url)] = str(cached_file)

        def unexpected_run(*_args, **_kwargs):
            raise AssertionError("yt-dlp should not run when cached file still exists")

        self.utils.subprocess.run = unexpected_run

        result = self.utils.try_download_video(url, resolver=lambda _host: ["8.8.8.8"])

        self.assertEqual(result, str(cached_file))

    def test_try_download_video_invalidates_missing_cached_file_and_redownloads(self):
        stale_file = self.paths["temp_dir"] / "stale.mp4"
        fresh_file = self.paths["temp_dir"] / "fresh.mp4"
        fresh_file.write_bytes(b"video")
        url = "https://example.com/video.mp4"
        self.utils.ytdl_path = "yt-dlp"
        self.utils.download_history[self.utils.download_cache_key(url)] = str(stale_file)
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return type("Result", (), {"stdout": f"{fresh_file}\n".encode("utf-8")})()

        self.utils.subprocess.run = fake_run

        result = self.utils.try_download_video(url, resolver=lambda _host: ["8.8.8.8"])

        self.assertEqual(result, str(fresh_file))
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            self.utils.download_history[self.utils.download_cache_key(url)],
            str(fresh_file.resolve()),
        )

    def test_try_download_video_rejects_missing_fresh_download_path(self):
        missing_file = self.paths["temp_dir"] / "missing.mp4"
        url = "https://example.com/video.mp4"
        self.utils.ytdl_path = "yt-dlp"

        def fake_run(*args, **kwargs):
            return type("Result", (), {"stdout": f"{missing_file}\n".encode("utf-8")})()

        self.utils.subprocess.run = fake_run

        with self.assertRaisesRegex(Exception, "yt-dl did not produce a reusable downloaded file path"):
            self.utils.try_download_video(url, resolver=lambda _host: ["8.8.8.8"])

        self.assertNotIn(self.utils.download_cache_key(url), self.utils.download_history)

    def test_download_rejects_private_target_before_subprocess_launch(self):
        self.utils.ytdl_path = "yt-dlp"
        called = False

        def unexpected_run(*_args, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("downloader must not launch")

        self.utils.subprocess.run = unexpected_run
        with self.assertRaisesRegex(Exception, "URL access denied"):
            self.utils.try_download_video(
                "https://example.com/video.mp4",
                resolver=lambda _host: ["127.0.0.1"],
            )
        self.assertFalse(called)

    def test_download_policy_is_enforced_even_when_downloader_is_unavailable(self):
        self.utils.ytdl_path = None
        with self.assertRaisesRegex(Exception, "URL access denied"):
            self.utils.try_download_video(
                "https://example.com/video.mp4",
                resolver=lambda _host: ["127.0.0.1"],
            )

    def test_download_rejects_http_before_subprocess_launch(self):
        self.utils.ytdl_path = "yt-dlp"
        launched = []
        self.utils.subprocess.run = lambda *_args, **_kwargs: launched.append(True)

        with self.assertRaisesRegex(Exception, "unsupported scheme"):
            self.utils.try_download_video("http://example.com/video.mp4")

        self.assertEqual(launched, [])

    def test_download_rejects_result_outside_temp_without_deleting_it(self):
        outside = self.workspace.path / "outside.mp4"
        outside.write_bytes(b"synthetic")
        self.utils.ytdl_path = "yt-dlp"

        self.utils.subprocess.run = lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"stdout": f"{outside}\n".encode("utf-8")},
        )()

        with self.assertRaisesRegex(Exception, "contained temp file"):
            self.utils.try_download_video(
                "https://example.com/video.mp4",
                resolver=lambda _host: ["8.8.8.8"],
            )
        self.assertTrue(outside.exists())

    def test_download_rejects_missing_outside_result_before_existence_probe(self):
        outside = self.workspace.path / "missing-outside.mp4"
        self.utils.ytdl_path = "yt-dlp"
        self.utils.subprocess.run = lambda *_args, **_kwargs: type(
            "Result",
            (),
            {"stdout": f"{outside}\n".encode("utf-8")},
        )()

        with self.assertRaisesRegex(Exception, "contained temp file"):
            self.utils.try_download_video(
                "https://example.com/video.mp4",
                resolver=lambda _host: ["8.8.8.8"],
            )

    def test_sequence_validation_rejects_a_matching_canonical_escape(self):
        sequence_dir = self.paths["input_dir"] / "sequence"
        sequence_dir.mkdir()
        allowed_frame = sequence_dir / "frame_001.png"
        allowed_frame.write_bytes(b"synthetic")
        apparent_frame = sequence_dir / "frame_002.png"
        apparent_frame.write_bytes(b"synthetic")
        outside_frame = self.workspace.path / "outside-frame.png"
        outside_frame.write_bytes(b"synthetic")
        original_realpath = os.path.realpath

        def simulate_link_resolution(candidate):
            if os.path.abspath(candidate) == os.path.abspath(apparent_frame):
                return str(outside_frame.resolve())
            return original_realpath(candidate)

        with patch(
            "videohelpersuite.path_policy.os.path.realpath",
            side_effect=simulate_link_resolution,
        ):
            self.assertFalse(
                self.utils.validate_sequence(str(sequence_dir / "frame_%03d.png"))
            )

    def test_cached_download_is_reauthorized_against_current_temp_root(self):
        outside = self.workspace.path / "outside.mp4"
        outside.write_bytes(b"synthetic")
        url = "https://example.com/video.mp4"
        self.utils.ytdl_path = "yt-dlp"
        self.utils.download_history[self.utils.download_cache_key(url)] = str(outside)
        self.utils.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthorized cache hit must not launch or return")
        )

        with self.assertRaisesRegex(Exception, "contained temp file"):
            self.utils.try_download_video(
                url,
                resolver=lambda _host: ["8.8.8.8"],
            )

    def test_capability_summary_exposes_modes_and_tool_booleans_only(self):
        summary = self.utils.get_capability_summary()
        self.assertEqual(summary["deployment_profile"], "trusted_local")
        self.assertEqual(summary["filesystem_policy"], "host_roots")
        self.assertEqual(summary["url_policy"], "https")
        for name in ("ffmpeg", "ffprobe", "gifski", "ytdl"):
            self.assertIsInstance(summary[name], bool)


if __name__ == "__main__":
    unittest.main()
