import os
import sys
import json
import subprocess
import numpy as np
import re
import datetime
from typing import List
import torch
from PIL import Image, ExifTags
from PIL.PngImagePlugin import PngInfo
from pathlib import Path
from string import Template
import itertools
import functools
from copy import deepcopy

import folder_paths
from .logger import logger
from .image_latent_nodes import *
from .load_video_nodes import LoadVideoUpload, LoadVideoPath, LoadVideoFFmpegUpload, LoadVideoFFmpegPath, LoadImagePath
from .load_images_nodes import LoadImagesFromDirectoryUpload, LoadImagesFromDirectoryPath
from .batched_nodes import VAEEncodeBatched, VAEDecodeBatched
from .utils import ffmpeg_path, get_audio, hash_path, validate_path, requeue_workflow, \
        gifski_path, calculate_file_hash, strip_path, try_download_video, is_url, \
        imageOrLatent, BIGMAX, merge_filter_args, ENCODE_ARGS, floatOrInt, cached, \
        ContainsAll, debug_log
from .video_metadata import create_ffmetadata_file
from comfy.utils import ProgressBar

audio_extensions = ['mp3', 'mp4', 'wav', 'ogg']

from .format_registry import (
    flatten_list, iterate_format, get_video_formats, _format_widget_kind,
    _merge_format_widget_options, build_format_widget_inputs,
    apply_format_widgets, base_formats_dir,
)
from .output_artifacts import (
    _first_sequence_output_path, _sequence_output_paths,
    _paths_form_numbered_sequence, _split_output_files,
    _ffmpeg_expected_output_exists, _decode_ffmpeg_stderr,
    _raise_ffmpeg_failure, _raise_missing_ffmpeg_output,
    _finalize_ffmpeg_process, _path_is_inside_directory,
    _remove_output_file_if_exists, _cleanup_partial_output_files,
    _delete_output_files,
    _get_workflow_extra_options, _video_format_saves_metadata,
    _build_output_metadata,
)
from .media_encode import (
    tensor_to_int, tensor_to_shorts, tensor_to_bytes, _has_audio_input,
    _video_format_supports_audio, _raise_unsupported_audio_format,
    build_audio_mux_args, ffmpeg_process, gifski_process, to_pingpong,
)
from .video_combine import VideoCombine

class LoadAudio:
    @classmethod
    def INPUT_TYPES(s):
        #Hide ffmpeg formats if ffmpeg isn't available
        return {
            "required": {
                "audio_file": ("STRING", {"default": "input/", "vhs_path_extensions": ['wav','mp3','ogg','m4a','flac']}),
                },
            "optional" : {
                "seek_seconds": ("FLOAT", {"default": 0, "min": 0, "widgetType": "VHSTIMESTAMP"}),
                "duration": ("FLOAT" , {"default": 0, "min": 0, "max": 10000000, "step": 0.01, "widgetType": "VHSTIMESTAMP"}),
                          }
        }

    RETURN_TYPES = ("AUDIO", "FLOAT")
    RETURN_NAMES = ("audio", "duration")
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢/audio"
    FUNCTION = "load_audio"
    def load_audio(self, audio_file, seek_seconds=0, duration=0):
        audio_file = strip_path(audio_file)
        if audio_file is None or validate_path(audio_file) != True:
            raise Exception("Audio source is not authorized or does not exist.")
        if is_url(audio_file):
            downloaded = try_download_video(audio_file)
            if downloaded is None:
                raise Exception("Audio URL could not be downloaded.")
            audio_file = downloaded
        #Eagerly fetch the audio since the user must be using it if the
        #node executes, unlike Load Video
        audio = get_audio(audio_file, start_time=seek_seconds, duration=duration)
        loaded_duration = audio['waveform'].size(2)/audio['sample_rate']
        return (audio, loaded_duration)

    @classmethod
    def IS_CHANGED(s, audio_file, **kwargs):
        return hash_path(audio_file)

    @classmethod
    def VALIDATE_INPUTS(s, audio_file, **kwargs):
        return validate_path(audio_file, allow_none=True)

class LoadAudioUpload:
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = []
        for f in os.listdir(input_dir):
            if os.path.isfile(os.path.join(input_dir, f)):
                file_parts = f.split('.')
                if len(file_parts) > 1 and (file_parts[-1] in audio_extensions):
                    files.append(f)
        return {"required": {
                    "audio": (sorted(files),),},
                "optional": {
                    "start_time": ("FLOAT" , {"default": 0, "min": 0, "max": 10000000, "step": 0.01, "widgetType": "VHSTIMESTAMP"}),
                    "duration": ("FLOAT" , {"default": 0, "min": 0, "max": 10000000, "step": 0.01, "widgetType": "VHSTIMESTAMP"}),
                     },
                }

    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢/audio"

    RETURN_TYPES = ("AUDIO", "FLOAT")
    RETURN_NAMES = ("audio", "duration")
    FUNCTION = "load_audio"

    def load_audio(self, start_time=0, duration=0, **kwargs):
        audio_file = folder_paths.get_annotated_filepath(strip_path(kwargs['audio']))
        if audio_file is None or validate_path(audio_file) != True:
            raise Exception("Audio source is not authorized or does not exist.")

        audio = get_audio(audio_file, start_time, duration)
        loaded_duration = audio['waveform'].size(2)/audio['sample_rate']
        return (audio, loaded_duration)

    @classmethod
    def IS_CHANGED(s, audio, **kwargs):
        audio_file = folder_paths.get_annotated_filepath(strip_path(audio))
        return hash_path(audio_file)

    @classmethod
    def VALIDATE_INPUTS(s, audio, **kwargs):
        audio_file = folder_paths.get_annotated_filepath(strip_path(audio))
        return validate_path(audio_file, allow_none=True)
class AudioToVHSAudio:
    """Legacy method for external nodes that utilized VHS_AUDIO,
    VHS_AUDIO is deprecated as a format and should no longer be used"""
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"audio": ("AUDIO",)}}
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢/audio"

    RETURN_TYPES = ("VHS_AUDIO", )
    RETURN_NAMES = ("vhs_audio",)
    FUNCTION = "convert_audio"

    def convert_audio(self, audio):
        ar = str(audio['sample_rate'])
        ac = str(audio['waveform'].size(1))
        mux_args = [ffmpeg_path, "-f", "f32le", "-ar", ar, "-ac", ac,
                    "-i", "-", "-f", "wav", "-"]

        audio_data = audio['waveform'].squeeze(0).transpose(0,1) \
                .numpy().tobytes()
        try:
            res = subprocess.run(mux_args, input=audio_data,
                                 capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            raise Exception("An error occured in the ffmpeg subprocess:\n" \
                    + e.stderr.decode(*ENCODE_ARGS))
        if res.stderr:
            print(res.stderr.decode(*ENCODE_ARGS), end="", file=sys.stderr)
        return (lambda: res.stdout,)

class VHSAudioToAudio:
    """Legacy method for external nodes that utilized VHS_AUDIO,
    VHS_AUDIO is deprecated as a format and should no longer be used"""
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"vhs_audio": ("VHS_AUDIO",)}}
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢/audio"

    RETURN_TYPES = ("AUDIO", )
    RETURN_NAMES = ("audio",)
    FUNCTION = "convert_audio"

    def convert_audio(self, vhs_audio):
        if not vhs_audio or not vhs_audio():
            raise Exception("audio input is not valid")
        args = [ffmpeg_path, "-i", '-']
        try:
            res =  subprocess.run(args + ["-f", "f32le", "-"], input=vhs_audio(),
                                  capture_output=True, check=True)
            audio = torch.frombuffer(bytearray(res.stdout), dtype=torch.float32)
        except subprocess.CalledProcessError as e:
            raise Exception("An error occured in the ffmpeg subprocess:\n" \
                    + e.stderr.decode(*ENCODE_ARGS))
        match = re.search(', (\\d+) Hz, (\\w+), ',res.stderr.decode(*ENCODE_ARGS))
        if match:
            ar = int(match.group(1))
            #NOTE: Just throwing an error for other channel types right now
            #Will deal with issues if they come
            ac = {"mono": 1, "stereo": 2}[match.group(2)]
        else:
            ar = 44100
            ac = 2
        audio = audio.reshape((-1,ac)).transpose(0,1).unsqueeze(0)
        return ({'waveform': audio, 'sample_rate': ar},)

class PruneOutputs:
    @classmethod
    def INPUT_TYPES(s):
        return {
                "required": {
                    "filenames": ("VHS_FILENAMES",),
                    "options": (["Intermediate", "Intermediate and Utility", "All"],)
                    }
                }

    RETURN_TYPES = ()
    OUTPUT_NODE = True
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"
    FUNCTION = "prune_outputs"

    def prune_outputs(self, filenames, options):
        if len(filenames[1]) == 0:
            return ()
        utility_files, intermediate_files, final_files = _split_output_files(filenames[1])
        delete_list = []
        if options in ["Intermediate", "Intermediate and Utility", "All"]:
            delete_list += intermediate_files
        if options in ["Intermediate and Utility", "All"]:
            delete_list += utility_files
        if options in ["All"]:
            delete_list += final_files
        delete_list = list(dict.fromkeys(delete_list))

        _delete_output_files(delete_list)
        return ()

class BatchManager:
    def __init__(self, frames_per_batch=-1):
        self.frames_per_batch = frames_per_batch
        self.inputs = {}
        self.outputs = {}
        self.unique_id = None
        self.has_closed_inputs = False
        self.total_frames = float('inf')
    def reset(self):
        self.close_inputs()
        for key in self.outputs:
            if getattr(self.outputs[key][-1], "gi_suspended", False):
                try:
                    self.outputs[key][-1].send(None)
                except StopIteration:
                    pass
        self.__init__(self.frames_per_batch)
    def has_open_inputs(self):
        return len(self.inputs) > 0
    def close_inputs(self):
        for key in self.inputs:
            if getattr(self.inputs[key][-1], "gi_suspended", False):
                try:
                    self.inputs[key][-1].send(1)
                except StopIteration:
                    pass
        self.inputs = {}

    @classmethod
    def INPUT_TYPES(s):
        return {
                "required": {
                    "frames_per_batch": ("INT", {"default": 16, "min": 1, "max": BIGMAX, "step": 1})
                    },
                "hidden": {
                    "prompt": "PROMPT",
                    "unique_id": "UNIQUE_ID"
                },
                }

    RETURN_TYPES = ("VHS_BatchManager",)
    RETURN_NAMES = ("meta_batch",)
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"
    FUNCTION = "update_batch"

    def update_batch(self, frames_per_batch, prompt=None, unique_id=None):
        if unique_id is not None and prompt is not None:
            requeue = prompt[unique_id]['inputs'].get('requeue', 0)
        else:
            requeue = 0
        if requeue == 0:
            self.reset()
            self.frames_per_batch = frames_per_batch
            self.unique_id = unique_id
        else:
            num_batches = (self.total_frames+self.frames_per_batch-1)//frames_per_batch
            print(f'Meta-Batch {requeue}/{num_batches}')
        #onExecuted seems to not be called unless some message is sent
        return (self,)


class VideoInfo:
    @classmethod
    def INPUT_TYPES(s):
        return {
                "required": {
                    "video_info": ("VHS_VIDEOINFO",),
                    }
                }

    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"

    RETURN_TYPES = ("FLOAT","INT", "FLOAT", "INT", "INT", "FLOAT","INT", "FLOAT", "INT", "INT")
    RETURN_NAMES = (
        "source_fps🟨",
        "source_frame_count🟨",
        "source_duration🟨",
        "source_width🟨",
        "source_height🟨",
        "loaded_fps🟦",
        "loaded_frame_count🟦",
        "loaded_duration🟦",
        "loaded_width🟦",
        "loaded_height🟦",
    )
    FUNCTION = "get_video_info"

    def get_video_info(self, video_info):
        keys = ["fps", "frame_count", "duration", "width", "height"]

        source_info = []
        loaded_info = []

        for key in keys:
            source_info.append(video_info[f"source_{key}"])
            loaded_info.append(video_info[f"loaded_{key}"])

        return (*source_info, *loaded_info)


class VideoInfoSource:
    @classmethod
    def INPUT_TYPES(s):
        return {
                "required": {
                    "video_info": ("VHS_VIDEOINFO",),
                    }
                }

    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"

    RETURN_TYPES = ("FLOAT","INT", "FLOAT", "INT", "INT",)
    RETURN_NAMES = (
        "fps🟨",
        "frame_count🟨",
        "duration🟨",
        "width🟨",
        "height🟨",
    )
    FUNCTION = "get_video_info"

    def get_video_info(self, video_info):
        keys = ["fps", "frame_count", "duration", "width", "height"]

        source_info = []

        for key in keys:
            source_info.append(video_info[f"source_{key}"])

        return (*source_info,)


class VideoInfoLoaded:
    @classmethod
    def INPUT_TYPES(s):
        return {
                "required": {
                    "video_info": ("VHS_VIDEOINFO",),
                    }
                }

    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"

    RETURN_TYPES = ("FLOAT","INT", "FLOAT", "INT", "INT",)
    RETURN_NAMES = (
        "fps🟦",
        "frame_count🟦",
        "duration🟦",
        "width🟦",
        "height🟦",
    )
    FUNCTION = "get_video_info"

    def get_video_info(self, video_info):
        keys = ["fps", "frame_count", "duration", "width", "height"]

        loaded_info = []

        for key in keys:
            loaded_info.append(video_info[f"loaded_{key}"])

        return (*loaded_info,)

class SelectFilename:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"filenames": ("VHS_FILENAMES",), "index": ("INT", {"default": -1, "step": 1, "min": -1})}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES =("Filename",)
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"
    FUNCTION = "select_filename"

    def select_filename(self, filenames, index):
        return (filenames[1][index],)
class Unbatch:
    class Any(str):
        def __ne__(self, other):
            return False
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"batched": ("*",)}}
    RETURN_TYPES = (Any('*'),)
    INPUT_IS_LIST = True
    RETURN_NAMES =("unbatched",)
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"
    FUNCTION = "unbatch"
    def unbatch(self, batched):
        if isinstance(batched[0], torch.Tensor):
            return (torch.cat(batched),)
        if isinstance(batched[0], dict):
            out = batched[0].copy()
            if 'samples' in out:
                out['samples'] = torch.cat([x['samples'] for x in batched])
            if 'waveform' in out:
                out['waveform'] = torch.cat([x['waveform'] for x in batched])
            out.pop('batch_index', None)
            return (out,)
        return (functools.reduce(lambda x,y: x+y, batched),)
    @classmethod
    def VALIDATE_INPUTS(cls, input_types):
        return True
class SelectLatest:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"filename_prefix": ("STRING", {'default': 'output/AnimateDiff', 'vhs_path_extensions': []}),
                             "filename_postfix": ("STRING", {"placeholder": ".webm"})}}
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES =("Filename",)
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"
    FUNCTION = "select_latest"
    EXPERIMENTAL = True

    def select_latest(self, filename_prefix, filename_postfix):
        assert False, "Not Reachable"

NODE_CLASS_MAPPINGS = {
    "VHS_VideoCombine": VideoCombine,
    "VHS_LoadVideo": LoadVideoUpload,
    "VHS_LoadVideoPath": LoadVideoPath,
    "VHS_LoadVideoFFmpeg": LoadVideoFFmpegUpload,
    "VHS_LoadVideoFFmpegPath": LoadVideoFFmpegPath,
    "VHS_LoadImagePath": LoadImagePath,
    "VHS_LoadImages": LoadImagesFromDirectoryUpload,
    "VHS_LoadImagesPath": LoadImagesFromDirectoryPath,
    "VHS_LoadAudio": LoadAudio,
    "VHS_LoadAudioUpload": LoadAudioUpload,
    "VHS_AudioToVHSAudio": AudioToVHSAudio,
    "VHS_VHSAudioToAudio": VHSAudioToAudio,
    "VHS_PruneOutputs": PruneOutputs,
    "VHS_BatchManager": BatchManager,
    "VHS_VideoInfo": VideoInfo,
    "VHS_VideoInfoSource": VideoInfoSource,
    "VHS_VideoInfoLoaded": VideoInfoLoaded,
    "VHS_SelectFilename": SelectFilename,
    # Batched Nodes
    "VHS_VAEEncodeBatched": VAEEncodeBatched,
    "VHS_VAEDecodeBatched": VAEDecodeBatched,
    # Latent and Image nodes
    "VHS_SplitLatents": SplitLatents,
    "VHS_SplitImages": SplitImages,
    "VHS_SplitMasks": SplitMasks,
    "VHS_MergeLatents": MergeLatents,
    "VHS_MergeImages": MergeImages,
    "VHS_MergeMasks": MergeMasks,
    "VHS_GetLatentCount": GetLatentCount,
    "VHS_GetImageCount": GetImageCount,
    "VHS_GetMaskCount": GetMaskCount,
    "VHS_DuplicateLatents": RepeatLatents,
    "VHS_DuplicateImages": RepeatImages,
    "VHS_DuplicateMasks": RepeatMasks,
    "VHS_SelectEveryNthLatent": SelectEveryNthLatent,
    "VHS_SelectEveryNthImage": SelectEveryNthImage,
    "VHS_SelectEveryNthMask": SelectEveryNthMask,
    "VHS_SelectLatents": SelectLatents,
    "VHS_SelectImages": SelectImages,
    "VHS_SelectMasks": SelectMasks,
    "VHS_Unbatch": Unbatch,
    "VHS_SelectLatest": SelectLatest,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "VHS_VideoCombine": "Video Combine 🎥🅥🅗🅢",
    "VHS_LoadVideo": "Load Video (Upload) 🎥🅥🅗🅢",
    "VHS_LoadVideoPath": "Load Video (Path) 🎥🅥🅗🅢",
    "VHS_LoadVideoFFmpeg": "Load Video FFmpeg (Upload) 🎥🅥🅗🅢",
    "VHS_LoadVideoFFmpegPath": "Load Video FFmpeg (Path) 🎥🅥🅗🅢",
    "VHS_LoadImagePath": "Load Image (Path) 🎥🅥🅗🅢",
    "VHS_LoadImages": "Load Images (Upload) 🎥🅥🅗🅢",
    "VHS_LoadImagesPath": "Load Images (Path) 🎥🅥🅗🅢",
    "VHS_LoadAudio": "Load Audio (Path)🎥🅥🅗🅢",
    "VHS_LoadAudioUpload": "Load Audio (Upload)🎥🅥🅗🅢",
    "VHS_AudioToVHSAudio": "Audio to legacy VHS_AUDIO🎥🅥🅗🅢",
    "VHS_VHSAudioToAudio": "Legacy VHS_AUDIO to Audio🎥🅥🅗🅢",
    "VHS_PruneOutputs": "Prune Outputs 🎥🅥🅗🅢",
    "VHS_BatchManager": "Meta Batch Manager 🎥🅥🅗🅢",
    "VHS_VideoInfo": "Video Info 🎥🅥🅗🅢",
    "VHS_VideoInfoSource": "Video Info (Source) 🎥🅥🅗🅢",
    "VHS_VideoInfoLoaded": "Video Info (Loaded) 🎥🅥🅗🅢",
    "VHS_SelectFilename": "Select Filename 🎥🅥🅗🅢",
    # Batched Nodes
    "VHS_VAEEncodeBatched": "VAE Encode Batched 🎥🅥🅗🅢",
    "VHS_VAEDecodeBatched": "VAE Decode Batched 🎥🅥🅗🅢",
    # Latent and Image nodes
    "VHS_SplitLatents": "Split Latents 🎥🅥🅗🅢",
    "VHS_SplitImages": "Split Images 🎥🅥🅗🅢",
    "VHS_SplitMasks": "Split Masks 🎥🅥🅗🅢",
    "VHS_MergeLatents": "Merge Latents 🎥🅥🅗🅢",
    "VHS_MergeImages": "Merge Images 🎥🅥🅗🅢",
    "VHS_MergeMasks": "Merge Masks 🎥🅥🅗🅢",
    "VHS_GetLatentCount": "Get Latent Count 🎥🅥🅗🅢",
    "VHS_GetImageCount": "Get Image Count 🎥🅥🅗🅢",
    "VHS_GetMaskCount": "Get Mask Count 🎥🅥🅗🅢",
    "VHS_DuplicateLatents": "Repeat Latents 🎥🅥🅗🅢",
    "VHS_DuplicateImages": "Repeat Images 🎥🅥🅗🅢",
    "VHS_DuplicateMasks": "Repeat Masks 🎥🅥🅗🅢",
    "VHS_SelectEveryNthLatent": "Select Every Nth Latent 🎥🅥🅗🅢",
    "VHS_SelectEveryNthImage": "Select Every Nth Image 🎥🅥🅗🅢",
    "VHS_SelectEveryNthMask": "Select Every Nth Mask 🎥🅥🅗🅢",
    "VHS_SelectLatents": "Select Latents 🎥🅥🅗🅢",
    "VHS_SelectImages": "Select Images 🎥🅥🅗🅢",
    "VHS_SelectMasks": "Select Masks 🎥🅥🅗🅢",
    "VHS_Unbatch":  "Unbatch 🎥🅥🅗🅢",
    "VHS_SelectLatest": "Select Latest 🎥🅥🅗🅢",
}
