import importlib
import re
import unittest
from pathlib import Path

from tests._support import (
    TempWorkspace,
    import_fresh,
    install_base_stubs,
    install_nodes_dependency_stubs,
    purge_modules,
)


ROOT = Path(__file__).resolve().parents[1]


class CoreModuleBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        purge_modules(
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
            "server",
            "folder_paths",
            "comfy",
            "torch",
            "nodes",
            "PIL",
            "cv2",
            "psutil",
        )
        install_base_stubs(self.workspace.path)
        install_nodes_dependency_stubs()

    def tearDown(self):
        self.workspace.cleanup()

    def test_domain_modules_exist_and_never_import_nodes_facade(self):
        expected = {
            "format_registry.py",
            "output_artifacts.py",
            "media_encode.py",
            "video_combine.py",
        }
        module_dir = ROOT / "videohelpersuite"
        self.assertTrue(expected.issubset({path.name for path in module_dir.glob("*.py")}))
        for name in expected:
            source = (module_dir / name).read_text(encoding="utf-8")
            self.assertNotRegex(source, r"(?:from\s+\.nodes\s+import|import\s+videohelpersuite\.nodes)")

    def test_nodes_facade_reexports_identical_domain_symbols(self):
        nodes_mod = import_fresh("videohelpersuite.nodes")
        format_mod = importlib.import_module("videohelpersuite.format_registry")
        artifact_mod = importlib.import_module("videohelpersuite.output_artifacts")
        encode_mod = importlib.import_module("videohelpersuite.media_encode")
        combine_mod = importlib.import_module("videohelpersuite.video_combine")

        expected_owners = {
            "iterate_format": format_mod,
            "get_video_formats": format_mod,
            "build_format_widget_inputs": format_mod,
            "apply_format_widgets": format_mod,
            "_split_output_files": artifact_mod,
            "_cleanup_partial_output_files": artifact_mod,
            "_build_output_metadata": artifact_mod,
            "build_audio_mux_args": encode_mod,
            "ffmpeg_process": encode_mod,
            "gifski_process": encode_mod,
            "VideoCombine": combine_mod,
        }
        for symbol, owner in expected_owners.items():
            self.assertIs(getattr(nodes_mod, symbol), getattr(owner, symbol), symbol)

        self.assertIs(nodes_mod.NODE_CLASS_MAPPINGS["VHS_VideoCombine"], combine_mod.VideoCombine)

    def test_nodes_facade_no_longer_owns_extracted_definitions(self):
        source = (ROOT / "videohelpersuite" / "nodes.py").read_text(encoding="utf-8")
        extracted = (
            "iterate_format",
            "get_video_formats",
            "build_format_widget_inputs",
            "apply_format_widgets",
            "_split_output_files",
            "_cleanup_partial_output_files",
            "build_audio_mux_args",
            "ffmpeg_process",
            "gifski_process",
        )
        for symbol in extracted:
            self.assertIsNone(re.search(rf"^def\s+{re.escape(symbol)}\s*\(", source, re.MULTILINE), symbol)
        self.assertNotRegex(source, r"^class\s+VideoCombine\s*:")

    def test_format_registry_owns_custom_format_folder_registration(self):
        facade_source = (ROOT / "videohelpersuite" / "nodes.py").read_text(encoding="utf-8")
        registry_source = (ROOT / "videohelpersuite" / "format_registry.py").read_text(encoding="utf-8")

        self.assertNotIn('folder_names_and_paths["VHS_video_formats"]', facade_source)
        self.assertIn('folder_names_and_paths["VHS_video_formats"]', registry_source)

    def test_serialized_node_ids_remain_exact(self):
        nodes_mod = import_fresh("videohelpersuite.nodes")
        self.assertEqual(
            set(nodes_mod.NODE_CLASS_MAPPINGS),
            {
                "VHS_VideoCombine", "VHS_LoadVideo", "VHS_LoadVideoPath",
                "VHS_LoadVideoFFmpeg", "VHS_LoadVideoFFmpegPath", "VHS_LoadImagePath",
                "VHS_LoadImages", "VHS_LoadImagesPath", "VHS_LoadAudio",
                "VHS_LoadAudioUpload", "VHS_AudioToVHSAudio", "VHS_VHSAudioToAudio",
                "VHS_PruneOutputs", "VHS_BatchManager", "VHS_VideoInfo",
                "VHS_VideoInfoSource", "VHS_VideoInfoLoaded", "VHS_SelectFilename",
                "VHS_VAEEncodeBatched", "VHS_VAEDecodeBatched", "VHS_SplitLatents",
                "VHS_SplitImages", "VHS_SplitMasks", "VHS_MergeLatents",
                "VHS_MergeImages", "VHS_MergeMasks", "VHS_GetLatentCount",
                "VHS_GetImageCount", "VHS_GetMaskCount", "VHS_DuplicateLatents",
                "VHS_DuplicateImages", "VHS_DuplicateMasks", "VHS_SelectEveryNthLatent",
                "VHS_SelectEveryNthImage", "VHS_SelectEveryNthMask", "VHS_SelectLatents",
                "VHS_SelectImages", "VHS_SelectMasks", "VHS_Unbatch", "VHS_SelectLatest",
            },
        )


if __name__ == "__main__":
    unittest.main()
