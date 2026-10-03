from PIL import Image
import time
import io
import struct
import math
import logging
from threading import Thread
import torch.nn.functional as F
import torch

import latent_preview
import server
serv = server.PromptServer.instance

from .utils import hook

rates_table = {'Mochi': 24//6, 'LTXV': 24//8, 'HunyuanVideo': 24//4,
               'Cosmos1CV8x8x8': 24//8, 'Wan21': 16//4, 'Wan22': 24//4}


class UnsupportedPreviewShape(ValueError):
    """A decoded frame cannot be represented by the RGB preview protocol."""


def supports_animated_preview(previewer):
    # CRITICAL: exact image/factor contracts only. Temporal subclasses own decode/compile lifecycle.
    if getattr(previewer, "compile_preview", False):
        return False
    if type(previewer) is getattr(latent_preview, "TAESDPreviewerImpl", None):
        return callable(getattr(getattr(previewer, "taesd", None), "decode", None))
    if type(previewer) is getattr(latent_preview, "Latent2RGBPreviewer", None):
        factors = getattr(previewer, "latent_rgb_factors", None)
        bias = getattr(previewer, "latent_rgb_factors_bias", None)
        reshape = getattr(previewer, "latent_rgb_factors_reshape", None)
        return (
            isinstance(factors, torch.Tensor) and factors.ndim == 2 and factors.size(0) == 3
            and (bias is None or (isinstance(bias, torch.Tensor) and bias.ndim == 1 and bias.size(0) == 3))
            and (reshape is None or callable(reshape))
        )
    return False

class WrappedPreviewer(latent_preview.LatentPreviewer):
    def __init__(self, previewer, rate=8):
        self.first_preview = True
        self.last_time = 0
        self.c_index = 0
        self.rate = rate
        if hasattr(previewer, 'taesd'):
            self.taesd = previewer.taesd
        elif hasattr(previewer, 'latent_rgb_factors'):
            self.latent_rgb_factors = previewer.latent_rgb_factors
            self.latent_rgb_factors_bias = previewer.latent_rgb_factors_bias
            self.latent_rgb_factors_reshape = getattr(previewer, 'latent_rgb_factors_reshape', None)
        else:
            raise Exception('Unsupported preview type for VHS animated previews')

    def decode_latent_to_preview_image(self, preview_format, x0):
        if not isinstance(x0, torch.Tensor) or x0.ndim not in (4, 5) or any(size == 0 for size in x0.shape):
            logging.warning("VHS animated preview skipped: unsupported latent shape")
            return None
        if x0.ndim == 5:
            #Keep batch major
            x0 = x0.movedim(2,1)
            x0 = x0.reshape((-1,)+x0.shape[-3:])
        num_images = x0.size(0)
        new_time = time.time()
        num_previews = int((new_time - self.last_time) * self.rate)
        self.last_time = self.last_time + num_previews/self.rate
        if num_previews > num_images:
            num_previews = num_images
        elif num_previews <= 0:
            return None
        if self.first_preview:
            self.last_time = new_time + 1/self.rate
        if self.c_index + num_previews > num_images:
            x0 = x0.roll(-self.c_index, 0)[:num_previews]
        else:
            x0 = x0[self.c_index:self.c_index + num_previews]
        try:
            Thread(target=self.process_previews, args=(x0, self.c_index,
                                                       num_images)).run()
        except UnsupportedPreviewShape:
            logging.warning("VHS animated preview skipped: unsupported decoded RGB shape")
        self.c_index = (self.c_index + num_previews) % num_images
        return None
    def process_previews(self, image_tensor, ind, leng):
        image_tensor = self.decode_latent_to_preview(image_tensor)
        if image_tensor.size(1) > 512 or image_tensor.size(2) > 512:
            image_tensor = image_tensor.movedim(-1,1)
            if image_tensor.size(2) < image_tensor.size(3):
                height = max(1, (512 * image_tensor.size(2)) // image_tensor.size(3))
                image_tensor = F.interpolate(image_tensor, (height,512), mode='bilinear')
            else:
                width = max(1, (512 * image_tensor.size(3)) // image_tensor.size(2))
                image_tensor = F.interpolate(image_tensor, (512, width), mode='bilinear')
            image_tensor = image_tensor.movedim(1,-1)
        if self.first_preview:
            self.first_preview = False
            serv.send_sync('VHS_latentpreview', {'length':leng, 'rate': self.rate, 'id': serv.last_node_id})
        previews_ubyte = (((image_tensor + 1.0) / 2.0).clamp(0, 1)  # change scale from -1..1 to 0..1
                         .mul(0xFF)  # to 0..255
                         ).to(device="cpu", dtype=torch.uint8)
        for preview in previews_ubyte:
            i = Image.fromarray(preview.numpy())
            message = io.BytesIO()
            message.write((1).to_bytes(length=4, byteorder='big')*2)
            message.write(ind.to_bytes(length=4, byteorder='big'))
            message.write(struct.pack('16p', serv.last_node_id.encode('ascii')))
            i.save(message, format="JPEG", quality=95, compress_level=1)
            #NOTE: send sync already uses call_soon_threadsafe
            serv.send_sync(server.BinaryEventTypes.PREVIEW_IMAGE,
                           message.getvalue(), serv.client_id)
            ind = (ind + 1) % leng
    def decode_latent_to_preview(self, x0):
        if hasattr(self, 'taesd'):
            decoded = self.taesd.decode(x0)
            if not isinstance(decoded, torch.Tensor) or decoded.ndim != 4 or decoded.size(1) < 3 or decoded.size(0) != x0.size(0) or any(size == 0 for size in decoded.shape):
                raise UnsupportedPreviewShape("Image preview requires nonempty NCHW RGB output")
            # IMPORTANT: JPEG cannot encode alpha; retain RGB before PIL conversion, including four-channel tiny VAEs.
            return decoded[:, :3].movedim(1, 3)
        else:
            if self.latent_rgb_factors_reshape is not None:
                x0 = self.latent_rgb_factors_reshape(x0)
            if not isinstance(x0, torch.Tensor) or x0.ndim != 4 or x0.size(1) != self.latent_rgb_factors.size(1):
                raise UnsupportedPreviewShape("Factor preview requires compatible frame channels")
            self.latent_rgb_factors = self.latent_rgb_factors.to(dtype=x0.dtype, device=x0.device)
            if self.latent_rgb_factors_bias is not None:
                self.latent_rgb_factors_bias = self.latent_rgb_factors_bias.to(dtype=x0.dtype, device=x0.device)
            latent_image = F.linear(x0.movedim(1, -1), self.latent_rgb_factors,
                                    bias=self.latent_rgb_factors_bias)
            if latent_image.ndim != 4 or latent_image.size(-1) != 3 or any(size == 0 for size in latent_image.shape):
                raise UnsupportedPreviewShape("Factor preview requires nonempty NHWC RGB output")
            return latent_image

@hook(latent_preview, 'get_previewer')
def get_latent_video_previewer(device, latent_format, *args, **kwargs):
    previewer = get_latent_video_previewer.__wrapped__(device, latent_format, *args, **kwargs)
    try:
        extra_info = next(serv.prompt_queue.currently_running.values().__iter__()) \
                [3]['extra_pnginfo']['workflow']['extra']
        prev_setting = extra_info.get('VHS_latentpreview', False)
        if extra_info.get('VHS_latentpreviewrate', 0) != 0:
            rate_setting = extra_info['VHS_latentpreviewrate']
        else:
            rate_setting = rates_table.get(latent_format.__class__.__name__, 8)
    except (KeyError, TypeError, IndexError, StopIteration, AttributeError):
        #For safety since there's lots of keys, any of which can fail
        prev_setting = False
    if prev_setting is not True or not supports_animated_preview(previewer):
        return previewer
    if isinstance(rate_setting, bool) or not isinstance(rate_setting, (int, float)) or not math.isfinite(rate_setting) or rate_setting <= 0:
        return previewer
    return WrappedPreviewer(previewer, rate_setting)
