"""Regression coverage for prompt-reachable filesystem and argument boundaries."""

import asyncio
import importlib
import inspect
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import (
    TempWorkspace,
    import_fresh,
    install_base_stubs,
    install_load_video_dependency_stubs,
    install_nodes_dependency_stubs,
    purge_modules,
)


_POLICY_ENVIRONMENT = (
    "VHS_DEPLOYMENT_PROFILE",
    "VHS_PATH_POLICY",
    "VHS_EXTERNAL_READ_ROOTS",
    "VHS_URL_POLICY",
    "VHS_STRICT_PATHS",
)

_NODE_MODULES = (
    "videohelpersuite.nodes",
    "videohelpersuite.format_registry",
    "videohelpersuite.output_artifacts",
    "videohelpersuite.media_encode",
    "videohelpersuite.video_combine",
    "videohelpersuite.utils",
    "videohelpersuite.path_policy",
    "videohelpersuite.logger",
    "videohelpersuite.image_latent_nodes",
    "videohelpersuite.load_video_nodes",
    "videohelpersuite.load_images_nodes",
    "videohelpersuite.batched_nodes",
    "videohelpersuite.server",
    "server",
    "folder_paths",
    "comfy",
    "torch",
    "nodes",
    "PIL",
    "cv2",
    "psutil",
)


class _FakeImageTensor:
    def __init__(self, array):
        self.array = array
        self.shape = array.shape

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class _BoundaryTestCase(unittest.TestCase):
    def setUp(self):
        self.policy_environment = {
            name: os.environ.pop(name, None) for name in _POLICY_ENVIRONMENT
        }
        self.workspace = TempWorkspace()
        purge_modules(*_NODE_MODULES)
        self.paths = install_base_stubs(self.workspace.path)
        self.folder_paths = sys.modules["folder_paths"]
        self.outside_dir = self.workspace.path / "outside"
        self.outside_dir.mkdir()

    def tearDown(self):
        self.workspace.cleanup()
        for name, value in self.policy_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def write_custom_format(self, relative_name, definition):
        path = self.workspace.path / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(definition), encoding="utf-8")
        listed = getattr(self, "_custom_formats", [])
        listed.append(relative_name)
        self._custom_formats = listed
        self.folder_paths.get_filename_list = lambda _name: list(self._custom_formats)


class FormatWidgetBoundaryTests(_BoundaryTestCase):
    def setUp(self):
        super().setUp()
        install_nodes_dependency_stubs()
        self.nodes_mod = import_fresh("videohelpersuite.nodes")
        self.combine_mod = importlib.import_module("videohelpersuite.video_combine")
        self.registry_mod = importlib.import_module("videohelpersuite.format_registry")

    def combine(self, **overrides):
        arguments = {
            "images": [_FakeImageTensor(np.zeros((2, 2, 3), dtype=np.float32))],
            "frame_rate": 8,
            "loop_count": 0,
            "filename_prefix": "Boundary",
            "format": "video/h264-mp4",
            "save_output": True,
            "extra_pnginfo": {"workflow": {"extra": {"VHS_MetadataImage": True}}},
        }
        arguments.update(overrides)
        return self.nodes_mod.VideoCombine().combine_video(**arguments)

    def test_hidden_inputs_do_not_accept_arbitrary_names(self):
        hidden = self.nodes_mod.VideoCombine.INPUT_TYPES()["hidden"]

        self.assertIn("prompt", hidden)
        self.assertNotIn("manual_format_widgets", hidden)
        self.assertNotIn("arbitrary_undeclared_name", hidden)
        self.assertEqual(
            json.loads(json.dumps(hidden)),
            {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO", "unique_id": "UNIQUE_ID"},
        )

    def test_widget_only_format_names_remain_forwardable(self):
        formats = {
            "video/a": [["quality", "INT", {"default": 1, "min": 0, "max": 9}]],
            "video/b": [["quality", ["low", "high"], {"default": "low"}]],
        }
        with mock.patch.object(
            self.combine_mod,
            "get_video_formats",
            lambda: (["video/a", "video/b"], formats),
        ):
            input_types = self.nodes_mod.VideoCombine.INPUT_TYPES()

        hidden = input_types["hidden"]
        self.assertNotIn("quality", input_types["optional"])
        self.assertIn("quality", hidden)
        self.assertEqual(hidden["quality"], (None, {}))
        self.assertNotIn("arbitrary_undeclared_name", hidden)

    def test_deprecated_manual_format_widgets_are_rejected_before_writing(self):
        def forbidden_process(*_args, **_kwargs):
            raise AssertionError("encoder must not start")

        with mock.patch.object(self.combine_mod, "ffmpeg_path", "ffmpeg"), \
             mock.patch.object(self.combine_mod, "ffmpeg_process", forbidden_process):
            with self.assertRaisesRegex(Exception, "manual_format_widgets is no longer supported"):
                self.combine(manual_format_widgets={"crf": 1})

        self.assertEqual(list(self.paths["output_dir"].iterdir()), [])

    def test_combo_values_must_belong_to_the_selected_format(self):
        sentinel = "not_a_dither_mode"
        with self.assertRaisesRegex(Exception, "dither"):
            self.nodes_mod.apply_format_widgets("ffmpeg-gif", {"dither": sentinel, "has_alpha": False})

    def test_numeric_and_boolean_values_are_type_and_range_checked(self):
        for values in (
            {"crf": "19"},
            {"crf": 101},
            {"crf": True},
            {"save_metadata": "yes"},
        ):
            with self.subTest(values=values):
                with self.assertRaises(Exception):
                    self.nodes_mod.apply_format_widgets("h264-mp4", dict(values, has_alpha=False))

        materialized = self.nodes_mod.apply_format_widgets(
            "h264-mp4",
            {"crf": 18, "save_metadata": False, "has_alpha": False},
        )
        self.assertIn("18", materialized["main_pass"])

    def test_string_widgets_cannot_feed_filter_arguments(self):
        self.write_custom_format(
            "custom-filter.json",
            {
                "main_pass": ["-n", "-vf", ["label", "STRING", {"default": "plain"}, "drawbox=t=$val"]],
                "extension": "mp4",
                "supports_audio": False,
            },
        )

        with self.assertRaisesRegex(Exception, "label"):
            self.nodes_mod.apply_format_widgets("custom-filter.json", {"label": "plain", "has_alpha": False})

    def test_standalone_string_widgets_require_a_conservative_token(self):
        self.write_custom_format(
            "custom-token.json",
            {
                "main_pass": ["-n", "-tune", ["tune", "STRING", {"default": "film"}]],
                "extension": "mp4",
                "supports_audio": False,
            },
        )

        for unsafe in ("a b", "a/b", "a\\b", "a;b", "a:b", "a'b", "-y", "a\nb", ""):
            with self.subTest(value=unsafe):
                with self.assertRaisesRegex(Exception, "tune"):
                    self.nodes_mod.apply_format_widgets(
                        "custom-token.json",
                        {"tune": unsafe, "has_alpha": False},
                    )

        materialized = self.nodes_mod.apply_format_widgets(
            "custom-token.json",
            {"tune": "film_grain-1.0", "has_alpha": False},
        )
        self.assertIn("film_grain-1.0", materialized["main_pass"])

    def test_every_builtin_format_materializes_with_defaults(self):
        formats, _widgets = self.registry_mod.get_video_formats()
        self.assertIn("video/h264-mp4", formats)
        for format_name in formats:
            name = format_name.split("/", 1)[1]
            for has_alpha in (False, True):
                with self.subTest(format=name, has_alpha=has_alpha):
                    materialized = self.nodes_mod.apply_format_widgets(name, {"has_alpha": has_alpha})
                    for key, arguments in materialized.items():
                        if key.endswith("_pass"):
                            self.assertTrue(all(isinstance(argument, str) for argument in arguments))

    def test_prores_profiles_and_alpha_mapping_still_materialize(self):
        for profile in ("lt", "standard", "hq", "4444", "4444xq"):
            for has_alpha in (False, True):
                with self.subTest(profile=profile, has_alpha=has_alpha):
                    main_pass = self.nodes_mod.apply_format_widgets(
                        "ProRes",
                        {"profile": profile, "has_alpha": has_alpha},
                    )["main_pass"]
                    self.assertEqual(main_pass[main_pass.index("-profile:v") + 1], profile)
                    if profile.startswith("4444"):
                        expected = "yuva444p10le" if has_alpha else "yuv444p10le"
                        self.assertEqual(main_pass[main_pass.index("-pix_fmt") + 1], expected)

    def test_fixture_workflow_format_values_still_validate(self):
        fixtures = Path(__file__).resolve().parent
        checked = 0
        # old-prores.json keeps pre-schema numeric profiles that the host combo check already rejects.
        for fixture in sorted(set(fixtures.rglob("*.json")) - {fixtures / "old-prores.json"}):
            workflow = json.loads(fixture.read_text(encoding="utf-8"))
            nodes = workflow.get("nodes") if isinstance(workflow, dict) else None
            for node in nodes if isinstance(nodes, list) else ():
                values = node.get("widgets_values")
                if node.get("type") != "VHS_VideoCombine" or not isinstance(values, dict):
                    continue
                format_type, _separator, name = values["format"].partition("/")
                if format_type != "video":
                    continue
                with self.subTest(fixture=fixture.name, node=node.get("id")):
                    self.nodes_mod.apply_format_widgets(name, dict(values, has_alpha=False))
                    checked += 1
        self.assertGreater(checked, 0)

    def test_widget_only_names_are_validated_against_the_selected_format(self):
        self.write_custom_format(
            "quality-int.json",
            {"main_pass": ["-n", "-q", ["quality", "INT", {"default": 1, "min": 0, "max": 9}]],
             "extension": "mp4", "supports_audio": False},
        )
        self.write_custom_format(
            "quality-combo.json",
            {"main_pass": ["-n", "-q", ["quality", ["low", "high"], {"default": "low"}]],
             "extension": "mp4", "supports_audio": False},
        )

        hidden = self.nodes_mod.VideoCombine.INPUT_TYPES()["hidden"]
        self.assertIn("quality", hidden)
        self.assertIn("5", self.nodes_mod.apply_format_widgets("quality-int.json", {"quality": 5})["main_pass"])
        self.assertIn("high", self.nodes_mod.apply_format_widgets("quality-combo.json", {"quality": "high"})["main_pass"])
        with self.assertRaisesRegex(Exception, "quality"):
            self.nodes_mod.apply_format_widgets("quality-int.json", {"quality": "high"})
        with self.assertRaisesRegex(Exception, "quality"):
            self.nodes_mod.apply_format_widgets("quality-combo.json", {"quality": 5})

    def test_format_names_resolve_only_through_discovered_formats(self):
        with self.assertRaisesRegex(Exception, "Unknown video format"):
            self.nodes_mod.apply_format_widgets("../video_formats/h264-mp4", {"has_alpha": False})

    def test_nested_custom_format_names_are_supported_without_path_joins(self):
        self.write_custom_format(
            "nested/custom.json",
            {"main_pass": ["-n"], "extension": "mp4", "supports_audio": False},
        )

        formats = self.nodes_mod.VideoCombine.INPUT_TYPES()["required"]["format"][0]
        self.assertIn("video/nested/custom.json", formats)

        def fake_ffmpeg_process(_args, _video_format, _metadata, file_path, _env):
            frame_data = yield
            total = 0
            while frame_data is not None:
                total += 1
                frame_data = yield
            Path(file_path).write_bytes(b"encoded")
            yield total

        with mock.patch.object(self.combine_mod, "ffmpeg_path", "ffmpeg"), \
             mock.patch.object(self.combine_mod, "ffmpeg_process", fake_ffmpeg_process):
            result = self.combine(format="video/nested/custom.json")

        self.assertTrue(result["result"][0][1][-1].endswith(".mp4"))


class AnnotatedVideoLoaderBoundaryTests(_BoundaryTestCase):
    def setUp(self):
        super().setUp()
        install_load_video_dependency_stubs()
        self.load_video_nodes = import_fresh("videohelpersuite.load_video_nodes")
        self.outside_file = self.outside_dir / "clip.mp4"
        self.outside_file.write_bytes(b"synthetic")
        (self.paths["output_dir"] / "clip.mp4").write_bytes(b"synthetic")
        (self.paths["input_dir"] / "sub").mkdir()
        (self.paths["input_dir"] / "sub" / "clip.mp4").write_bytes(b"synthetic")

    def loader_classes(self):
        return (
            self.load_video_nodes.LoadVideoUpload,
            self.load_video_nodes.LoadVideoFFmpegUpload,
        )

    def test_validation_does_not_reveal_whether_outside_names_exist(self):
        relative_existing = os.path.relpath(self.outside_file, self.paths["input_dir"])
        relative_missing = os.path.relpath(self.outside_dir / "missing.mp4", self.paths["input_dir"])
        for loader in self.loader_classes():
            with self.subTest(loader=loader.__name__):
                existing = loader.VALIDATE_INPUTS(relative_existing)
                missing = loader.VALIDATE_INPUTS(relative_missing)
                absolute = loader.VALIDATE_INPUTS(str(self.outside_file))

                self.assertIsNot(existing, True)
                self.assertIsNot(absolute, True)
                self.assertEqual(existing, missing)
                self.assertEqual(existing, absolute)
                self.assertNotIn(str(self.outside_dir), str(existing))

    def test_loaders_do_not_cross_into_another_readable_root(self):
        for loader in self.loader_classes():
            with self.subTest(loader=loader.__name__):
                calls = []
                with mock.patch.object(
                    self.load_video_nodes,
                    "load_video",
                    side_effect=lambda **kwargs: calls.append(kwargs) or (None, 0, None, {}),
                ):
                    self.assertIsNot(loader.VALIDATE_INPUTS("../output/clip.mp4"), True)
                    with self.assertRaises(Exception):
                        loader().load_video(video="../output/clip.mp4")
                self.assertEqual(calls, [])

    def test_annotated_and_nested_names_remain_supported(self):
        for loader in self.loader_classes():
            with self.subTest(loader=loader.__name__):
                self.assertIs(loader.VALIDATE_INPUTS("clip.mp4 [output]"), True)
                self.assertIs(loader.VALIDATE_INPUTS("sub/clip.mp4"), True)

        calls = []
        with mock.patch.object(
            self.load_video_nodes,
            "load_video",
            side_effect=lambda **kwargs: calls.append(kwargs) or (None, 0, None, {}),
        ):
            self.load_video_nodes.LoadVideoUpload().load_video(video="clip.mp4 [output]")
        self.assertEqual(
            Path(calls[0]["video"]),
            (self.paths["output_dir"] / "clip.mp4").resolve(),
        )

    def test_control_characters_and_drive_names_are_rejected(self):
        for name in ("clip\n.mp4", "clip\x00.mp4", "C:clip.mp4", "sub:stream.mp4"):
            with self.subTest(name=repr(name)):
                self.assertIsNot(
                    self.load_video_nodes.LoadVideoUpload.VALIDATE_INPUTS(name),
                    True,
                )


class AnnotatedMediaLoaderBoundaryTests(_BoundaryTestCase):
    def setUp(self):
        super().setUp()
        install_nodes_dependency_stubs()
        self.nodes_mod = import_fresh("videohelpersuite.nodes")
        purge_modules("videohelpersuite.load_images_nodes", "videohelpersuite.load_video_nodes")
        install_load_video_dependency_stubs()
        self.load_images_nodes = import_fresh("videohelpersuite.load_images_nodes")
        (self.paths["output_dir"] / "speech.wav").write_bytes(b"synthetic")
        frames = self.paths["output_dir"] / "frames"
        frames.mkdir()
        (frames / "0001.png").write_bytes(b"synthetic")

    def test_audio_upload_does_not_cross_roots(self):
        self.assertIsNot(self.nodes_mod.LoadAudioUpload.VALIDATE_INPUTS("../output/speech.wav"), True)
        self.assertIs(self.nodes_mod.LoadAudioUpload.VALIDATE_INPUTS("speech.wav [output]"), True)

    def test_image_directory_upload_does_not_cross_roots(self):
        loader = self.load_images_nodes.LoadImagesFromDirectoryUpload
        self.assertIsNot(loader.VALIDATE_INPUTS("../output/frames"), True)
        self.assertEqual(
            loader.VALIDATE_INPUTS("../output/frames"),
            loader.VALIDATE_INPUTS("../output/missing-frames"),
        )
        self.assertIs(loader.VALIDATE_INPUTS("frames [output]"), True)

    def test_validators_keep_host_bounds_checks_enabled(self):
        for validator in (
            self.nodes_mod.LoadAudio.VALIDATE_INPUTS,
            self.nodes_mod.LoadAudioUpload.VALIDATE_INPUTS,
            self.load_images_nodes.LoadImagesFromDirectoryUpload.VALIDATE_INPUTS,
            self.load_images_nodes.LoadImagesFromDirectoryPath.VALIDATE_INPUTS,
        ):
            with self.subTest(validator=validator.__qualname__):
                kinds = {parameter.kind for parameter in inspect.signature(validator).parameters.values()}
                self.assertNotIn(inspect.Parameter.VAR_KEYWORD, kinds)


class PruneProvenanceTests(_BoundaryTestCase):
    def setUp(self):
        super().setUp()
        install_nodes_dependency_stubs()
        self.nodes_mod = import_fresh("videohelpersuite.nodes")

    def test_prune_requires_a_linked_filenames_producer(self):
        validate = self.nodes_mod.PruneOutputs.VALIDATE_INPUTS
        self.assertIsNot(validate(input_types={}), True)
        self.assertIsNot(validate(input_types={"filenames": "STRING"}), True)
        self.assertIs(validate(input_types={"filenames": "VHS_FILENAMES"}), True)

    def test_prune_rejects_malformed_filenames_before_deleting(self):
        target = self.paths["output_dir"] / "keep.png"
        target.write_bytes(b"synthetic")
        prune = self.nodes_mod.PruneOutputs()
        for filenames in (("x", "not-a-list"), [True, [1]], (True,), None):
            with self.subTest(filenames=filenames):
                with self.assertRaisesRegex(Exception, "VHS_FILENAMES result"):
                    prune.prune_outputs(filenames, "All")
        self.assertTrue(target.exists())


class FolderPreviewConcatTests(_BoundaryTestCase):
    def setUp(self):
        super().setUp()
        self.server_mod = import_fresh("videohelpersuite.server")
        self.captured = []

        async def fake_prepass(*_args):
            return 0, b"", b"Stream #0:0: Video: png, 1 fps,"

        async def fake_stream(request, *, args, filename, content_type, debug_event, cleanup_paths=None):
            concat_path = args[args.index("-i") + 1]
            self.captured.append(Path(concat_path).read_text(encoding="utf-8"))
            return types.SimpleNamespace(status=200)

        self.server_mod.ffmpeg_path = "ffmpeg"
        self.server_mod.run_preview_prepass = fake_prepass
        self.server_mod.stream_preview_response = fake_stream

    def view_folder(self, name):
        request = types.SimpleNamespace(rel_url=types.SimpleNamespace(query={
            "filename": name,
            "type": "output",
            "format": "folder",
        }))
        return asyncio.run(self.server_mod.view_video(request))

    def test_quote_bearing_entry_names_are_escaped(self):
        frames = self.paths["output_dir"] / "quoted"
        frames.mkdir()
        quoted = frames / "it's.png"
        quoted.write_bytes(b"synthetic")

        self.view_folder("quoted")

        lines = self.captured[0].splitlines()
        file_lines = [line for line in lines if line.startswith("file ")]
        self.assertEqual(len(file_lines), 1)
        expected = "file '" + str(quoted.resolve()).replace("'", "'\\''") + "'"
        self.assertEqual(file_lines[0], expected)

    def test_entries_with_control_characters_cannot_add_directives(self):
        frames = self.paths["output_dir"] / "frames"
        frames.mkdir()
        safe = frames / "0001.png"
        safe.write_bytes(b"synthetic")
        self.server_mod.get_sorted_dir_files_from_directory = lambda *_args, **_kwargs: [
            str(safe.resolve()),
            str(frames.resolve()) + os.sep + "bad\nname.png",
        ]

        self.view_folder("frames")

        self.assertEqual(
            self.captured[0].splitlines(),
            [
                "ffconcat version 1.0",
                "file '" + str(safe.resolve()).replace("'", "'\\''") + "'",
                "duration 0.125",
            ],
        )


class DownloaderIsolationTests(_BoundaryTestCase):
    def setUp(self):
        super().setUp()
        self.utils = import_fresh("videohelpersuite.utils")

    def test_downloader_ignores_ambient_configuration(self):
        downloaded = self.paths["temp_dir"] / "download.mp4"
        downloaded.write_bytes(b"synthetic")
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return types.SimpleNamespace(stdout=f"{downloaded}\n".encode("utf-8"))

        with mock.patch.object(self.utils, "ytdl_path", "yt-dlp"), \
             mock.patch.object(self.utils.subprocess, "run", fake_run):
            self.utils.try_download_video(
                "https://example.com/video.mp4",
                resolver=lambda _host: ["8.8.8.8"],
            )

        self.assertEqual(len(calls), 1)
        for flag in ("--ignore-config", "--no-playlist", "--restrict-filenames"):
            self.assertIn(flag, calls[0])
        self.assertEqual(calls[0][-1], "https://example.com/video.mp4")


if __name__ == "__main__":
    unittest.main()
