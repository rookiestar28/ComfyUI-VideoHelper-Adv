import functools
import importlib.util
import io
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import torch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class HostPreviewer:
    def decode_latent_to_preview(self, _value):
        return "host-owned"


class TAESDPreviewerImpl(HostPreviewer):
    def __init__(self, decoder):
        self.taesd = decoder


class TAEHVPreviewerImpl(TAESDPreviewerImpl):
    compile_preview = True


class Latent2RGBPreviewer(HostPreviewer):
    def __init__(self):
        self.latent_rgb_factors = torch.eye(3)
        self.latent_rgb_factors_bias = None
        self.latent_rgb_factors_reshape = None


class LatentPreviewCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.selected = None
        self.host = types.ModuleType("latent_preview")
        self.host.LatentPreviewer = HostPreviewer
        self.host.TAESDPreviewerImpl = TAESDPreviewerImpl
        self.host.Latent2RGBPreviewer = Latent2RGBPreviewer
        self.host.get_previewer = lambda *_args, **_kwargs: self.selected
        self.extra = {"VHS_latentpreview": True, "VHS_latentpreviewrate": 8}
        self.server = types.SimpleNamespace(
            last_node_id="7",
            client_id="synthetic-client",
            prompt_queue=types.SimpleNamespace(currently_running={1: (None, None, None, {"extra_pnginfo": {"workflow": {"extra": self.extra}}})}),
            send_sync=lambda *args: self.messages.append(args),
        )
        server_module = types.ModuleType("server")
        server_module.PromptServer = types.SimpleNamespace(instance=self.server)
        server_module.BinaryEventTypes = types.SimpleNamespace(PREVIEW_IMAGE=1)
        utils = types.ModuleType("videohelpersuite.utils")

        def hook(owner, name):
            def decorate(callback):
                wrapped = functools.wraps(getattr(owner, name))(callback)
                setattr(owner, name, wrapped)
                return wrapped
            return decorate

        utils.hook = hook
        self.patch = mock.patch.dict(sys.modules, {
            "torch": torch,
            "torch.nn": torch.nn,
            "torch.nn.functional": torch.nn.functional,
            "latent_preview": self.host,
            "server": server_module,
            "videohelpersuite.utils": utils,
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)
        spec = importlib.util.spec_from_file_location("videohelpersuite._preview_contract_test", Path("videohelpersuite/latent_preview.py"))
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def decoder(self, output):
        return types.SimpleNamespace(decode=lambda _latent: output)

    def wrapped(self):
        return self.module.get_latent_video_previewer("cpu", types.SimpleNamespace())

    def packets(self):
        return [message[1] for message in self.messages if message[0] == 1]

    def test_four_channels_encode_real_rgb_jpeg_without_reordering_batch(self):
        decoded = torch.full((2, 4, 4, 5), -1.0)
        decoded[0, 0] = 1.0
        decoded[1, 2] = 1.0
        previewer = self.module.WrappedPreviewer(TAESDPreviewerImpl(self.decoder(decoded)))
        previewer.process_previews(torch.zeros(2, 4, 4, 5), 0, 2)
        packets = self.packets()
        self.assertEqual(len(packets), 2)
        for index, packet in enumerate(packets):
            self.assertEqual(packet[:8], b"\0\0\0\1" * 2)
            self.assertEqual(int.from_bytes(packet[8:12], "big"), index)
            self.assertEqual(struct.unpack("16p", packet[12:28])[0], b"7")
            image = Image.open(io.BytesIO(packet[28:]))
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (5, 4))
            pixel = image.getpixel((1, 1))
            self.assertGreater(pixel[0 if index == 0 else 2], 240)

    def test_unknown_temporal_and_subclass_previewers_retain_host_identity(self):
        decoder = self.decoder(torch.zeros(1, 3, 4, 5))
        class UnknownImage(TAESDPreviewerImpl):
            pass
        for candidate in [TAEHVPreviewerImpl(decoder), UnknownImage(decoder), HostPreviewer(), None]:
            with self.subTest(candidate=type(candidate).__name__):
                self.selected = candidate
                self.assertIs(self.wrapped(), candidate)
        self.assertEqual(self.messages, [])

    def test_known_image_and_factor_contracts_are_wrapped_but_disabled_and_invalid_rates_are_not(self):
        for candidate in [TAESDPreviewerImpl(self.decoder(torch.zeros(1, 3, 4, 5))), Latent2RGBPreviewer()]:
            self.selected = candidate
            self.assertIsInstance(self.wrapped(), self.module.WrappedPreviewer)
        self.extra["VHS_latentpreview"] = False
        self.assertIs(self.wrapped(), self.selected)
        self.extra["VHS_latentpreview"] = True
        for rate in ["8", -1, float("nan"), float("inf"), True]:
            self.extra["VHS_latentpreviewrate"] = rate
            self.assertIs(self.wrapped(), self.selected)

    def test_rgb_factor_bias_and_reshape_apply_to_real_tensor(self):
        candidate = Latent2RGBPreviewer()
        candidate.latent_rgb_factors_bias = torch.tensor([0.25, 0.5, 0.75])
        candidate.latent_rgb_factors_reshape = lambda value: value * 2
        previewer = self.module.WrappedPreviewer(candidate)
        actual = previewer.decode_latent_to_preview(torch.full((2, 3, 4, 5), 0.1))
        self.assertEqual(tuple(actual.shape), (2, 4, 5, 3))
        torch.testing.assert_close(actual[0, 0, 0], torch.tensor([0.45, 0.7, 0.95]))

    def test_five_dimensional_factors_emit_batch_major_frames_and_one_start_event(self):
        self.selected = Latent2RGBPreviewer()
        previewer = self.wrapped()
        latent = torch.zeros(2, 3, 2, 4, 5)
        for batch in range(2):
            for frame in range(2):
                latent[batch, :, frame] = -1 + (batch * 2 + frame) * 2 / 3
        with mock.patch.object(self.module.time, "time", return_value=100):
            previewer.decode_latent_to_preview_image("JPEG", latent)
        packets = self.packets()
        self.assertEqual(len(packets), 4)
        self.assertEqual([int.from_bytes(packet[8:12], "big") for packet in packets], [0, 1, 2, 3])
        for index, packet in enumerate(packets):
            image = Image.open(io.BytesIO(packet[28:]))
            self.assertAlmostEqual(image.getpixel((1, 1))[0], index * 85, delta=2)
        self.assertEqual([message[1] for message in self.messages if message[0] == "VHS_latentpreview"], [{"length": 4, "rate": 8, "id": "7"}])

    def test_malformed_decoder_and_input_are_skipped_without_events(self):
        for output in [torch.zeros(1, 2, 4, 5), torch.zeros(1, 3, 1, 4, 5)]:
            with self.subTest(shape=tuple(output.shape)):
                previewer = self.module.WrappedPreviewer(TAESDPreviewerImpl(self.decoder(output)))
                previewer.decode_latent_to_preview_image("JPEG", torch.zeros(1, 4, 4, 5))
                self.assertEqual(self.messages, [])
        previewer = self.module.WrappedPreviewer(Latent2RGBPreviewer())
        previewer.decode_latent_to_preview_image("JPEG", torch.zeros(3, 4, 5))
        self.assertEqual(self.messages, [])

    def test_extreme_aspect_ratio_resize_never_requests_zero_pixels(self):
        decoded = torch.zeros(1, 3, 1, 2048)
        previewer = self.module.WrappedPreviewer(TAESDPreviewerImpl(self.decoder(decoded)))
        previewer.process_previews(torch.zeros(1, 4, 1, 2048), 0, 1)
        image = Image.open(io.BytesIO(self.packets()[0][28:]))
        self.assertEqual(image.size, (512, 1))

    def test_malformed_factor_capabilities_delegate_and_channel_mismatch_skips_events(self):
        candidate = Latent2RGBPreviewer()
        candidate.latent_rgb_factors_bias = torch.zeros(2)
        self.selected = candidate
        self.assertIs(self.wrapped(), candidate)
        candidate = Latent2RGBPreviewer()
        candidate.compile_preview = True
        self.selected = candidate
        self.assertIs(self.wrapped(), candidate)
        previewer = self.module.WrappedPreviewer(Latent2RGBPreviewer())
        previewer.decode_latent_to_preview_image("JPEG", torch.zeros(1, 4, 4, 5))
        self.assertEqual(self.messages, [])


if __name__ == "__main__":
    unittest.main()
