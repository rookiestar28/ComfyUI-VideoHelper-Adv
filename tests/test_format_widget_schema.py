import importlib
import unittest

from tests._support import (
    TempWorkspace,
    import_fresh,
    install_base_stubs,
    install_nodes_dependency_stubs,
    purge_modules,
)


class FormatWidgetSchemaTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()
        purge_modules(
            "videohelpersuite.nodes",
            "videohelpersuite.format_registry",
            "videohelpersuite.output_artifacts",
            "videohelpersuite.media_encode",
            "videohelpersuite.video_combine",
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
        )
        install_base_stubs(self.workspace.path)
        install_nodes_dependency_stubs()
        self.nodes_mod = import_fresh("videohelpersuite.nodes")
        self.combine_mod = importlib.import_module("videohelpersuite.video_combine")

    def tearDown(self):
        self.workspace.cleanup()

    def test_compatible_specs_merge_without_mutating_source(self):
        formats = {
            "video/a": [
                ["crf", "INT", {"default": 19, "min": 0, "max": 51, "step": 1}],
                ["pix_fmt", ["yuv420p", "yuv420p10le"]],
            ],
            "video/b": [
                ["crf", "INT", {"default": 23, "min": 1, "max": 63, "step": 1}],
                ["pix_fmt", ["yuv420p10le", "yuv444p"]],
            ],
        }
        original = repr(formats)

        inputs, incompatible = self.nodes_mod.build_format_widget_inputs(formats)

        self.assertEqual(repr(formats), original)
        self.assertEqual(incompatible, {})
        self.assertEqual(inputs["crf"][0], "INT")
        self.assertEqual(inputs["crf"][1]["default"], 19)
        self.assertEqual(inputs["crf"][1]["min"], 0)
        self.assertEqual(inputs["crf"][1]["max"], 63)
        self.assertEqual(
            inputs["pix_fmt"][0],
            ["yuv420p", "yuv420p10le", "yuv444p"],
        )

    def test_incompatible_same_name_widget_kinds_are_not_published(self):
        formats = {
            "video/a": [["quality", "INT", {"default": 20}]],
            "video/b": [["quality", ["low", "high"], {"default": "high"}]],
        }

        inputs, incompatible = self.nodes_mod.build_format_widget_inputs(formats)

        self.assertNotIn("quality", inputs)
        self.assertEqual(incompatible["quality"], ["INT", "COMBO"])

    def test_video_combine_publishes_compatible_format_widgets_as_optional_inputs(self):
        formats = {
            "video/a": [["crf", "INT", {"default": 19, "min": 0, "max": 51}]],
            "video/b": [["crf", "INT", {"default": 23, "min": 0, "max": 63}]],
        }
        original_get_formats = self.combine_mod.get_video_formats
        self.combine_mod.get_video_formats = lambda: (["video/a", "video/b"], formats)
        try:
            input_types = self.nodes_mod.VideoCombine.INPUT_TYPES()
        finally:
            self.combine_mod.get_video_formats = original_get_formats

        self.assertIn("crf", input_types["optional"])
        self.assertEqual(input_types["optional"]["crf"][0], "INT")
        self.assertIn("image/webp", input_types["required"]["format"][1]["formats"])

    def test_builtin_schema_publishes_every_compatible_widget_name(self):
        optional = self.nodes_mod.VideoCombine.INPUT_TYPES()["optional"]

        self.assertTrue(
            {
                "bitrate",
                "coder",
                "context",
                "crf",
                "dither",
                "gop_size",
                "input_color_depth",
                "level",
                "lossless",
                "megabit",
                "pix_fmt",
                "profile",
                "save_metadata",
                "slicecrc",
                "slices",
                "trim_to_audio",
            }.issubset(optional)
        )
        self.assertEqual(optional["pix_fmt"][0][0:2], ["yuv420p10le", "yuv420p"])

    def test_inactive_format_values_do_not_enter_selected_format_arguments(self):
        materialized = self.nodes_mod.apply_format_widgets(
            "h264-mp4",
            {"crf": 18, "quality": "INACTIVE_SENTINEL"},
        )

        self.assertIn("18", repr(materialized["main_pass"]))
        self.assertNotIn("INACTIVE_SENTINEL", repr(materialized))


if __name__ == "__main__":
    unittest.main()
