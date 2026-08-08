import unittest
from unittest import mock

import numpy as np

from tests._support import (
    TempWorkspace,
    import_fresh,
    install_base_stubs,
    install_load_video_dependency_stubs,
    purge_modules,
)


class LoadVideoNodesReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        purge_modules(
            "videohelpersuite.load_video_nodes",
            "videohelpersuite.utils",
            "videohelpersuite.path_policy",
            "videohelpersuite.logger",
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
        install_load_video_dependency_stubs()
        self.load_video_nodes = import_fresh("videohelpersuite.load_video_nodes")

    def tearDown(self):
        self.workspace.cleanup()

    def test_load_video_ffmpeg_upload_handles_latent_dict_when_vae_is_connected(self):
        latent = {"samples": object()}
        audio = {"waveform": object(), "sample_rate": 44100}
        video_info = {"loaded_frame_count": 4}

        with mock.patch.object(
            self.load_video_nodes,
            "load_video",
            return_value=(latent, 4, audio, video_info),
        ):
            result = self.load_video_nodes.LoadVideoFFmpegUpload().load_video(
                video="clip.mp4",
                vae=object(),
            )

        self.assertIs(result[0], latent)
        self.assertIsNone(result[1])
        self.assertIs(result[2], audio)
        self.assertIs(result[3], video_info)

    def test_ffmpeg_frame_generator_assembles_frames_correctly_across_partial_reads(self):
        media_path = self.paths["input_dir"] / "clip.mp4"
        media_path.write_bytes(b"synthetic")
        frame = np.array([[[1, 2, 3, 4], [5, 6, 7, 8]]], dtype=np.uint16)
        frame_bytes = frame.astype(frame.dtype.newbyteorder("<")).tobytes()
        chunks = [frame_bytes[:5], frame_bytes[5:11], frame_bytes[11:]]

        class DummyStdout:
            def __init__(self, pieces):
                self.pieces = list(pieces)

            def read(self, _size):
                if self.pieces:
                    return self.pieces.pop(0)
                return b""

        class DummyPopen:
            def __init__(self, pieces):
                self.stdout = DummyStdout(pieces)
                self.stderr = mock.Mock(read=lambda: b"")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        probe_output = (
            b"  Duration: 00:00:01.00,\n"
            b"  Stream #0:0: Video: h264, yuv420p, 2x1, 1 fps,\n"
        )

        with mock.patch.object(
            self.load_video_nodes.subprocess,
            "run",
            return_value=mock.Mock(stderr=probe_output),
        ), mock.patch.object(
            self.load_video_nodes.subprocess,
            "Popen",
            return_value=DummyPopen(chunks),
        ):
            generator = self.load_video_nodes.ffmpeg_frame_generator(
                video=str(media_path),
                force_rate=0,
                frame_load_cap=1,
                start_time=0,
                custom_width=0,
                custom_height=0,
            )
            info = next(generator)
            assembled = next(generator)

        self.assertEqual(info[:3], (2, 1, 1.0))
        expected = frame.reshape(1, 2, 4).astype(np.float32) / (2**16 - 1)
        np.testing.assert_allclose(assembled, expected[:, :, :3])


if __name__ == "__main__":
    unittest.main()
