import os
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import (
    TempWorkspace,
    import_fresh,
    install_base_stubs,
    install_nodes_dependency_stubs,
    purge_modules,
)


class _FakeWaveformArray:
    def __init__(self, array):
        self.array = array

    def transpose(self, *axes):
        return _FakeWaveformArray(self.array.transpose(*axes))

    def numpy(self):
        return self.array


class _FakeWaveform:
    def __init__(self, channels=2, samples=8):
        self.array = np.zeros((1, channels, samples), dtype=np.float32)

    def size(self, dim):
        return self.array.shape[dim]

    def squeeze(self, axis):
        return _FakeWaveformArray(np.squeeze(self.array, axis=axis))


class _FakeImageTensor:
    def __init__(self, array):
        self.array = array
        self.shape = array.shape

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class _DummyPipe:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def write(self, data):
        self.owner.writes.append(data)
        if self.owner.raise_broken_pipe:
            raise BrokenPipeError()

    def flush(self):
        return None

    def close(self):
        self.closed = True


class _DummyStderr:
    def __init__(self, owner):
        self.owner = owner

    def read(self):
        return self.owner.stderr_data


class _DummyPopen:
    def __init__(
        self,
        args,
        *,
        returncode=0,
        stderr_data=b"",
        create_output=False,
        create_sequence_first_frame=False,
        raise_broken_pipe=False,
    ):
        self.args = args
        self.returncode = returncode
        self.stderr_data = stderr_data
        self.create_output = create_output
        self.create_sequence_first_frame = create_sequence_first_frame
        self.raise_broken_pipe = raise_broken_pipe
        self.writes = []
        self.stdin = _DummyPipe(self)
        self.stderr = _DummyStderr(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.wait()
        return False

    def wait(self):
        output_path = Path(self.args[-1])
        if self.create_output:
            output_path.write_bytes(b"encoded")
        if self.create_sequence_first_frame:
            first_frame = Path(str(output_path).replace("%03d", "001"))
            first_frame.write_bytes(b"encoded-frame")
        return self.returncode


class NodesReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        purge_modules(
            "videohelpersuite.nodes",
            "videohelpersuite.utils",
            "videohelpersuite.logger",
            "videohelpersuite.image_latent_nodes",
            "videohelpersuite.load_video_nodes",
            "videohelpersuite.load_images_nodes",
            "videohelpersuite.batched_nodes",
            "server",
            "folder_paths",
            "comfy",
            "torch",
            "nodes",
            "PIL",
            "cv2",
            "psutil",
        )
        self.paths = install_base_stubs(self.workspace.path)
        install_nodes_dependency_stubs()
        self.nodes_mod = import_fresh("videohelpersuite.nodes")

    def tearDown(self):
        self.workspace.cleanup()

    def test_build_audio_mux_args_injects_default_audio_codec(self):
        video_format = {"extension": "webm"}
        mux_args, channels = self.nodes_mod.build_audio_mux_args(
            video_format,
            "silent.webm",
            "with-audio.webm",
            {"waveform": _FakeWaveform(), "sample_rate": 44100},
            total_frames_output=8,
            frame_rate=8,
        )
        self.assertEqual(channels, 2)
        self.assertIn("-c:a", mux_args)
        self.assertIn("libopus", mux_args)
        self.assertEqual(video_format["audio_pass"], ["-c:a", "libopus"])

    def test_build_audio_mux_args_adds_ffmetadata_input_for_roundtrip_payload(self):
        video_format = {"extension": "mp4", "audio_pass": ["-c:a", "aac"]}
        mux_args, _channels = self.nodes_mod.build_audio_mux_args(
            video_format,
            "silent.mp4",
            "with-audio.mp4",
            {"waveform": _FakeWaveform(), "sample_rate": 48000},
            total_frames_output=12,
            frame_rate=12,
            metadata_path="metadata.txt",
        )
        metadata_index = mux_args.index("metadata.txt")
        copy_index = mux_args.index("-c:v")
        self.assertIn("-map_metadata", mux_args)
        self.assertEqual(mux_args[mux_args.index("-map_metadata") + 1], "2")
        self.assertIn("-movflags", mux_args)
        self.assertEqual(mux_args[mux_args.index("-movflags") + 1], "use_metadata_tags")
        self.assertEqual(mux_args[metadata_index - 1], "-i")
        self.assertEqual(mux_args[metadata_index - 2], "ffmetadata")
        self.assertEqual(mux_args[metadata_index - 3], "-f")
        self.assertLess(metadata_index, copy_index)

    def test_build_audio_mux_args_never_places_metadata_input_after_output_codecs(self):
        video_format = {"extension": "mp4", "audio_pass": ["-c:a", "aac"]}
        mux_args, _channels = self.nodes_mod.build_audio_mux_args(
            video_format,
            "silent.mp4",
            "with-audio.mp4",
            {"waveform": _FakeWaveform(), "sample_rate": 44100},
            total_frames_output=24,
            frame_rate=24,
            metadata_path="metadata.txt",
        )
        codec_index = mux_args.index("-c:v")
        metadata_flag_index = mux_args.index("-f", mux_args.index("-i", mux_args.index("-i") + 1) + 1)
        self.assertLess(metadata_flag_index, codec_index)
        self.assertEqual(mux_args[metadata_flag_index:metadata_flag_index + 4], ["-f", "ffmetadata", "-i", "metadata.txt"])

    def test_ffmpeg_process_raises_on_nonzero_exit(self):
        output_path = self.paths["output_dir"] / "failed.mp4"

        def fake_popen(args, **_kwargs):
            return _DummyPopen(args, returncode=1, stderr_data=b"encode failed")

        with mock.patch.object(self.nodes_mod.subprocess, "Popen", side_effect=fake_popen):
            process = self.nodes_mod.ffmpeg_process(
                ["ffmpeg"], {"extension": "mp4"}, {}, str(output_path), {}
            )
            process.send(None)
            process.send(b"frame")
            with self.assertRaisesRegex(Exception, "exit code 1"):
                process.send(None)
        self.assertFalse(output_path.exists())

    def test_ffmpeg_process_raises_when_expected_output_missing(self):
        output_path = self.paths["output_dir"] / "missing.mp4"

        def fake_popen(args, **_kwargs):
            return _DummyPopen(args, returncode=0, stderr_data=b"")

        with mock.patch.object(self.nodes_mod.subprocess, "Popen", side_effect=fake_popen):
            process = self.nodes_mod.ffmpeg_process(
                ["ffmpeg"], {"extension": "mp4"}, {}, str(output_path), {}
            )
            process.send(None)
            process.send(b"frame")
            with self.assertRaisesRegex(Exception, "did not create expected output"):
                process.send(None)

    def test_ffmpeg_process_accepts_existing_sequence_first_frame(self):
        output_path = self.paths["output_dir"] / "frames_%03d.png"

        def fake_popen(args, **_kwargs):
            return _DummyPopen(args, returncode=0, create_sequence_first_frame=True)

        with mock.patch.object(self.nodes_mod.subprocess, "Popen", side_effect=fake_popen):
            process = self.nodes_mod.ffmpeg_process(
                ["ffmpeg"], {"extension": "%03d.png"}, {}, str(output_path), {}
            )
            process.send(None)
            process.send(b"frame")
            total_frames = process.send(None)

        self.assertEqual(total_frames, 1)
        self.assertTrue((self.paths["output_dir"] / "frames_001.png").exists())

    def test_ffmpeg_process_counts_frames_when_output_exists(self):
        output_path = self.paths["output_dir"] / "encoded.mp4"

        def fake_popen(args, **_kwargs):
            return _DummyPopen(args, returncode=0, create_output=True)

        with mock.patch.object(self.nodes_mod.subprocess, "Popen", side_effect=fake_popen):
            process = self.nodes_mod.ffmpeg_process(
                ["ffmpeg"], {"extension": "mp4"}, {}, str(output_path), {}
            )
            process.send(None)
            process.send(b"frame-1")
            process.send(b"frame-2")
            total_frames = process.send(None)

        self.assertEqual(total_frames, 2)
        self.assertTrue(output_path.exists())

    def test_video_combine_meta_batch_image_format_preflight_creates_no_utility_png(self):
        combine = self.nodes_mod.VideoCombine()
        images = [_FakeImageTensor(np.zeros((2, 2, 3), dtype=np.float32))]
        meta_batch = types.SimpleNamespace(outputs={})

        with self.assertRaisesRegex(Exception, "not compatible with batched output"):
            combine.combine_video(
                images=images,
                frame_rate=8,
                loop_count=0,
                filename_prefix="Test",
                format="image/gif",
                save_output=True,
                meta_batch=meta_batch,
                unique_id="node",
                extra_pnginfo={"workflow": {"extra": {"VHS_MetadataImage": True}}},
            )

        self.assertEqual(list(self.paths["output_dir"].iterdir()), [])

    def test_video_combine_missing_ffmpeg_preflight_creates_no_utility_png(self):
        combine = self.nodes_mod.VideoCombine()
        images = [_FakeImageTensor(np.zeros((2, 2, 3), dtype=np.float32))]

        with mock.patch.object(self.nodes_mod, "ffmpeg_path", None):
            with self.assertRaisesRegex(ProcessLookupError, "ffmpeg is required"):
                combine.combine_video(
                    images=images,
                    frame_rate=8,
                    loop_count=0,
                    filename_prefix="Test",
                    format="video/fake-format",
                    save_output=True,
                    extra_pnginfo={"workflow": {"extra": {"VHS_MetadataImage": True}}},
                )

        self.assertEqual(list(self.paths["output_dir"].iterdir()), [])

    def test_video_combine_cleans_partial_artifacts_after_encode_failure(self):
        combine = self.nodes_mod.VideoCombine()
        images = [_FakeImageTensor(np.zeros((2, 2, 3), dtype=np.float32))]

        def failing_ffmpeg_process(_args, _video_format, _metadata, file_path, _env):
            frame_data = yield
            while frame_data is not None:
                Path(file_path).write_bytes(b"partial-video")
                frame_data = yield
            raise Exception("encode failed")

        with mock.patch.object(self.nodes_mod, "ffmpeg_path", "/usr/bin/ffmpeg"), \
             mock.patch.object(
                 self.nodes_mod,
                 "apply_format_widgets",
                 lambda _ext, _kwargs: {"extension": "mp4", "main_pass": []},
             ), \
             mock.patch.object(self.nodes_mod, "ffmpeg_process", failing_ffmpeg_process):
            with self.assertRaisesRegex(Exception, "encode failed"):
                combine.combine_video(
                    images=images,
                    frame_rate=8,
                    loop_count=0,
                    filename_prefix="Test",
                    format="video/fake-format",
                    save_output=True,
                    extra_pnginfo={"workflow": {"extra": {"VHS_MetadataImage": True}}},
                )

        self.assertEqual(list(self.paths["output_dir"].iterdir()), [])

    def test_output_cleanup_rejects_outside_roots(self):
        outside_path = self.workspace.path / "outside.txt"
        outside_path.write_bytes(b"keep")

        with self.assertRaisesRegex(Exception, "invalid directory"):
            self.nodes_mod._remove_output_file_if_exists(
                str(outside_path),
                output_dirs=[str(self.paths["output_dir"]), str(self.paths["temp_dir"])],
            )

        self.assertTrue(outside_path.exists())

    def test_prune_outputs_all_option_deletes_all_selected_outputs(self):
        prune = self.nodes_mod.PruneOutputs()
        files = []
        for name in ["meta.png", "silent.mp4", "final-audio.mp4"]:
            path = self.paths["output_dir"] / name
            path.write_bytes(b"x")
            files.append(str(path))
        prune.prune_outputs((True, files), "All")
        for path in files:
            self.assertFalse(os.path.exists(path))

    def test_video_combine_returns_only_muxed_video_when_audio_present(self):
        combine = self.nodes_mod.VideoCombine()
        images = [_FakeImageTensor(np.zeros((2, 2, 3), dtype=np.float32))]

        def fake_ffmpeg_process(_args, _video_format, _metadata, file_path, _env):
            frame_data = yield
            total = 0
            while frame_data is not None:
                total += 1
                frame_data = yield
            Path(file_path).write_bytes(b"silent-video")
            yield total

        def fake_subprocess_run(args, input=None, env=None, capture_output=None, check=None):
            Path(args[-1]).write_bytes(b"muxed-video")
            return types.SimpleNamespace(stderr=b"")

        audio = {"waveform": _FakeWaveform(), "sample_rate": 44100}

        with mock.patch.object(self.nodes_mod, "ffmpeg_path", "/usr/bin/ffmpeg"), \
             mock.patch.object(
                 self.nodes_mod,
                 "apply_format_widgets",
                 lambda _ext, _kwargs: {"extension": "mp4", "main_pass": [], "audio_pass": ["-c:a", "aac"]},
             ), \
             mock.patch.object(self.nodes_mod, "ffmpeg_process", fake_ffmpeg_process), \
             mock.patch.object(self.nodes_mod.subprocess, "run", side_effect=fake_subprocess_run):
            result = combine.combine_video(
                images=images,
                frame_rate=8,
                loop_count=0,
                filename_prefix="Test",
                format="video/fake-format",
                save_output=True,
                audio=audio,
                extra_pnginfo={"workflow": {"extra": {"VHS_MetadataImage": True}}},
            )

        output_files = result["result"][0][1]
        self.assertEqual(len(output_files), 2)
        self.assertTrue(output_files[0].endswith(".png"))
        self.assertTrue(output_files[1].endswith("-audio.mp4"))
        self.assertTrue(os.path.exists(output_files[1]))
        self.assertFalse(os.path.exists(output_files[1].replace("-audio.mp4", ".mp4")))


if __name__ == "__main__":
    unittest.main()
