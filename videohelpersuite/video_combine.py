"""VideoCombine orchestration behind the nodes compatibility facade."""

import datetime
import itertools
import os
import re
import subprocess
import sys

import folder_paths
import numpy as np
import torch
from comfy.utils import ProgressBar
from PIL import ExifTags, Image

from .format_registry import apply_format_widgets, build_format_widget_inputs, get_video_formats
from .logger import logger
from .media_encode import (
    _has_audio_input, _raise_unsupported_audio_format,
    _video_format_supports_audio, build_audio_mux_args, ffmpeg_process,
    gifski_process, tensor_to_bytes, tensor_to_shorts, to_pingpong,
)
from .output_artifacts import (
    _build_output_metadata, _cleanup_partial_output_files,
    _get_workflow_extra_options, _sequence_output_paths,
    _split_output_files, _video_format_saves_metadata,
)
from .utils import (
    ContainsAll, ENCODE_ARGS, debug_log, ffmpeg_path, floatOrInt,
    imageOrLatent, merge_filter_args, requeue_workflow,
)
from .video_metadata import create_ffmetadata_file


class VideoCombine:
    @classmethod
    def INPUT_TYPES(s):
        ffmpeg_formats, format_widgets = get_video_formats()
        format_widgets["image/webp"] = [['lossless', "BOOLEAN", {'default': True}]]
        format_widget_inputs, incompatible_widgets = build_format_widget_inputs(format_widgets)
        if incompatible_widgets:
            logger.warn(
                "Format widgets with incompatible same-name types will use "
                f"widget-only frontend fallbacks: {sorted(incompatible_widgets)}"
            )
        optional_inputs = {
            "audio": ("AUDIO",),
            "meta_batch": ("VHS_BatchManager",),
            "vae": ("VAE",),
        }
        reserved_names = {
            "images", "frame_rate", "loop_count", "filename_prefix",
            "format", "pingpong", "save_output", *optional_inputs,
        }
        for name, config in format_widget_inputs.items():
            if name in reserved_names:
                logger.warn(
                    f"Format widget {name!r} conflicts with a VideoCombine input and "
                    "will use a widget-only frontend fallback"
                )
                continue
            optional_inputs[name] = config
        return {
            "required": {
                "images": (imageOrLatent,),
                "frame_rate": (
                    floatOrInt,
                    {"default": 8, "min": 1, "step": 1},
                ),
                "loop_count": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "filename_prefix": ("STRING", {"default": "AnimateDiff"}),
                "format": (["image/gif", "image/webp"] + ffmpeg_formats, {'formats': format_widgets}),
                "pingpong": ("BOOLEAN", {"default": False}),
                "save_output": ("BOOLEAN", {"default": True}),
            },
            "optional": optional_inputs,
            "hidden": ContainsAll({
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID"
            }),
        }

    RETURN_TYPES = ("VHS_FILENAMES",)
    RETURN_NAMES = ("Filenames",)
    OUTPUT_NODE = True
    CATEGORY = "Video Helper Suite 🎥🅥🅗🅢"
    FUNCTION = "combine_video"

    def combine_video(
        self,
        frame_rate: int,
        loop_count: int,
        images=None,
        latents=None,
        filename_prefix="AnimateDiff",
        format="image/gif",
        pingpong=False,
        save_output=True,
        prompt=None,
        extra_pnginfo=None,
        audio=None,
        unique_id=None,
        manual_format_widgets=None,
        meta_batch=None,
        vae=None,
        **kwargs
    ):
        if latents is not None:
            images = latents
        if images is None:
            return ((save_output, []),)
        if vae is not None:
            if isinstance(images, dict):
                images = images['samples']
            else:
                vae = None

        if isinstance(images, torch.Tensor) and images.size(0) == 0:
            return ((save_output, []),)
        num_frames = len(images)
        pbar = ProgressBar(num_frames)
        if vae is not None:
            downscale_ratio = getattr(vae, "downscale_ratio", 8)
            width = images.size(-1)*downscale_ratio
            height = images.size(-2)*downscale_ratio
            frames_per_batch = (1920 * 1080 * 16) // (width * height) or 1
            #Python 3.12 adds an itertools.batched, but it's easily replicated for legacy support
            def batched(it, n):
                while batch := tuple(itertools.islice(it, n)):
                    yield batch
            def batched_encode(images, vae, frames_per_batch):
                for batch in batched(iter(images), frames_per_batch):
                    image_batch = torch.from_numpy(np.array(batch))
                    yield from vae.decode(image_batch)
            images = batched_encode(images, vae, frames_per_batch)
            first_image = next(images)
            #repush first_image
            images = itertools.chain([first_image], images)
            #A single image has 3 dimensions. Discard higher dimensions
            while len(first_image.shape) > 3:
                first_image = first_image[0]
        else:
            first_image = images[0]
            images = iter(images)
        # get output information
        output_dir = (
            folder_paths.get_output_directory()
            if save_output
            else folder_paths.get_temp_directory()
        )
        (
            full_output_folder,
            filename,
            _,
            subfolder,
            _,
        ) = folder_paths.get_save_image_path(
            filename_prefix,
            output_dir,
            first_image.shape[1],
            first_image.shape[0],
        )
        output_files = []
        partial_output_files = []

        def track_output_file(path):
            if path not in partial_output_files:
                partial_output_files.append(path)

        extra_options = _get_workflow_extra_options(extra_pnginfo)

        has_audio = _has_audio_input(audio)
        format_type, format_ext = format.split("/")
        video_format = None
        if format_type == "image":
            if has_audio:
                _raise_unsupported_audio_format({"extension": format_ext, "supports_audio": False}, format)
            if meta_batch is not None:
                raise Exception("Pillow('image/') formats are not compatible with batched output")
        else:
            # Use ffmpeg to save a video
            if ffmpeg_path is None:
                raise ProcessLookupError(f"ffmpeg is required for video outputs and could not be found.\nIn order to use video outputs, you must either:\n- Install imageio-ffmpeg with pip,\n- Place a ffmpeg executable in {os.path.abspath('')}, or\n- Install ffmpeg and add it to the system path.")

            if manual_format_widgets is not None:
                logger.warn("Format args can now be passed directly. The manual_format_widgets argument is now deprecated")
                kwargs.update(manual_format_widgets)

            has_alpha = first_image.shape[-1] == 4
            kwargs["has_alpha"] = has_alpha
            video_format = apply_format_widgets(format_ext, kwargs)
            if has_audio and not _video_format_supports_audio(video_format):
                _raise_unsupported_audio_format(video_format, format)
            if "pre_pass" in video_format and meta_batch is not None:
                #Performing a prepass requires keeping access to all frames.
                #Potential solutions include keeping just output frames in
                #memory or using 3 passes with intermediate file, but
                #very long gifs probably shouldn't be encouraged
                raise Exception("Formats which require a pre_pass are incompatible with Batch Manager.")

        # IMPORTANT: save_metadata=False must suppress workflow data in the PNG sidecar too,
        # not only ffmpeg tags. CreationTime is retained because it is not workflow metadata.
        metadata, video_metadata = _build_output_metadata(
            prompt,
            extra_pnginfo,
            include_workflow_metadata=_video_format_saves_metadata(video_format),
        )

        if meta_batch is not None and unique_id in meta_batch.outputs:
            (counter, output_process) = meta_batch.outputs[unique_id]
        else:
            # comfy counter workaround
            max_counter = 0

            # Loop through the existing files
            matcher = re.compile(f"{re.escape(filename)}_(\\d+)\\D*\\..+", re.IGNORECASE)
            for existing_file in os.listdir(full_output_folder):
                # Check if the file matches the expected format
                match = matcher.fullmatch(existing_file)
                if match:
                    # Extract the numeric portion of the filename
                    file_counter = int(match.group(1))
                    # Update the maximum counter value if necessary
                    if file_counter > max_counter:
                        max_counter = file_counter

            # Increment the counter by 1 to get the next available value
            counter = max_counter + 1
            output_process = None

        # save first frame as png to keep metadata
        first_image_file = f"{filename}_{counter:05}.png"
        file_path = os.path.join(full_output_folder, first_image_file)
        saved_workflow_image = False
        try:
            if extra_options.get('VHS_MetadataImage', True) != False:
                track_output_file(file_path)
                Image.fromarray(tensor_to_bytes(first_image)).save(
                    file_path,
                    pnginfo=metadata,
                    compress_level=4,
                )
                output_files.append(file_path)
                saved_workflow_image = True

            if format_type == "image":
                image_kwargs = {}
                if format_ext == "gif":
                    image_kwargs['disposal'] = 2
                if format_ext == "webp":
                    #Save timestamp information
                    exif = Image.Exif()
                    exif[ExifTags.IFD.Exif] = {36867: datetime.datetime.now().isoformat(" ")[:19]}
                    image_kwargs['exif'] = exif
                    image_kwargs['lossless'] = kwargs.get("lossless", True)
                file = f"{filename}_{counter:05}.{format_ext}"
                file_path = os.path.join(full_output_folder, file)
                track_output_file(file_path)
                if pingpong:
                    images = to_pingpong(images)
                def frames_gen(images):
                    for i in images:
                        pbar.update(1)
                        yield Image.fromarray(tensor_to_bytes(i))
                frames = frames_gen(images)
                # Use pillow directly to save an animated image
                next(frames).save(
                    file_path,
                    format=format_ext.upper(),
                    save_all=True,
                    append_images=frames,
                    duration=round(1000 / frame_rate),
                    loop=loop_count,
                    compress_level=4,
                    **image_kwargs
                )
                output_files.append(file_path)
            else:
                dim_alignment = video_format.get("dim_alignment", 2)
                if (first_image.shape[1] % dim_alignment) or (first_image.shape[0] % dim_alignment):
                    #output frames must be padded
                    to_pad = (-first_image.shape[1] % dim_alignment,
                              -first_image.shape[0] % dim_alignment)
                    padding = (to_pad[0]//2, to_pad[0] - to_pad[0]//2,
                               to_pad[1]//2, to_pad[1] - to_pad[1]//2)
                    padfunc = torch.nn.ReplicationPad2d(padding)
                    def pad(image):
                        image = image.permute((2,0,1))#HWC to CHW
                        padded = padfunc(image.to(dtype=torch.float32))
                        return padded.permute((1,2,0))
                    images = map(pad, images)
                    dimensions = (-first_image.shape[1] % dim_alignment + first_image.shape[1],
                                  -first_image.shape[0] % dim_alignment + first_image.shape[0])
                    logger.warn("Output images were not of valid resolution and have had padding applied")
                else:
                    dimensions = (first_image.shape[1], first_image.shape[0])
                if pingpong:
                    if meta_batch is not None:
                        logger.error("pingpong is incompatible with batched output")
                    images = to_pingpong(images)
                    if num_frames > 2:
                        num_frames += num_frames -2
                        pbar.total = num_frames
                if loop_count > 0:
                    loop_args = ["-vf", "loop=loop=" + str(loop_count)+":size=" + str(num_frames)]
                else:
                    loop_args = []
                if video_format.get('input_color_depth', '8bit') == '16bit':
                    images = map(tensor_to_shorts, images)
                    if has_alpha:
                        i_pix_fmt = 'rgba64'
                    else:
                        i_pix_fmt = 'rgb48'
                else:
                    images = map(tensor_to_bytes, images)
                    if has_alpha:
                        i_pix_fmt = 'rgba'
                    else:
                        i_pix_fmt = 'rgb24'
                file = f"{filename}_{counter:05}.{video_format['extension']}"
                file_path = os.path.join(full_output_folder, file)
                bitrate_arg = []
                bitrate = video_format.get('bitrate')
                if bitrate is not None:
                    bitrate_arg = ["-b:v", str(bitrate) + "M" if video_format.get('megabit') == 'True' else str(bitrate) + "K"]
                args = [ffmpeg_path, "-v", "error", "-f", "rawvideo", "-pix_fmt", i_pix_fmt,
                        # The image data is in an undefined generic RGB color space, which in practice means sRGB.
                        # sRGB has the same primaries and matrix as BT.709, but a different transfer function (gamma),
                        # called by the sRGB standard name IEC 61966-2-1. However, video hosting platforms like YouTube
                        # standardize on full BT.709 and will convert the colors accordingly. This last minute change
                        # in colors can be confusing to users. We can counter it by lying about the transfer function
                        # on a per format basis, i.e. for video we will lie to FFmpeg that it is already BT.709. Also,
                        # because the input data is in RGB (not YUV) it is more efficient (fewer scale filter invocations)
                        # to specify the input color space as RGB and then later, if the format actually wants YUV,
                        # to convert it to BT.709 YUV via FFmpeg's -vf "scale=out_color_matrix=bt709".
                        "-color_range", "pc", "-colorspace", "rgb", "-color_primaries", "bt709",
                        "-color_trc", video_format.get("fake_trc", "iec61966-2-1"),
                        "-s", f"{dimensions[0]}x{dimensions[1]}", "-r", str(frame_rate), "-i", "-"] \
                        + loop_args

                images = map(lambda x: x.tobytes(), images)
                env=os.environ.copy()
                if  "environment" in video_format:
                    env.update(video_format["environment"])

                if "pre_pass" in video_format:
                    images = [b''.join(images)]
                    os.makedirs(folder_paths.get_temp_directory(), exist_ok=True)
                    in_args_len = args.index("-i") + 2 # The index after ["-i", "-"]
                    pre_pass_args = args[:in_args_len] + video_format['pre_pass']
                    merge_filter_args(pre_pass_args)
                    try:
                        subprocess.run(pre_pass_args, input=images[0], env=env,
                                       capture_output=True, check=True)
                    except subprocess.CalledProcessError as e:
                        raise Exception("An error occurred in the ffmpeg prepass:\n" \
                                + e.stderr.decode(*ENCODE_ARGS))
                if "inputs_main_pass" in video_format:
                    in_args_len = args.index("-i") + 2 # The index after ["-i", "-"]
                    args = args[:in_args_len] + video_format['inputs_main_pass'] + args[in_args_len:]

                track_output_file(file_path)
                if output_process is None:
                    if 'gifski_pass' in video_format:
                        format = 'image/gif'
                        output_process = gifski_process(args, dimensions, frame_rate, video_format, file_path, env)
                        audio = None
                    else:
                        args += video_format['main_pass'] + bitrate_arg
                        merge_filter_args(args)
                        output_process = ffmpeg_process(args, video_format, video_metadata, file_path, env)
                    #Proceed to first yield
                    output_process.send(None)
                    if meta_batch is not None:
                        meta_batch.outputs[unique_id] = (counter, output_process)

                for image in images:
                    pbar.update(1)
                    output_process.send(image)
                if meta_batch is not None:
                    requeue_workflow((meta_batch.unique_id, not meta_batch.has_closed_inputs))
                if meta_batch is None or meta_batch.has_closed_inputs:
                    #Close pipe and wait for termination.
                    try:
                        total_frames_output = output_process.send(None)
                        output_process.send(None)
                    except StopIteration:
                        pass
                    if meta_batch is not None:
                        meta_batch.outputs.pop(unique_id)
                        if len(meta_batch.outputs) == 0:
                            meta_batch.reset()
                else:
                    #batch is unfinished
                    #TODO: Check if empty output breaks other custom nodes
                    return {"ui": {"unfinished_batch": [True]}, "result": ((save_output, []),)}
                final_output_path = file_path
                final_output_name = file
                if has_audio:
                    # Create audio file if input was provided
                    output_file_with_audio = f"{filename}_{counter:05}-audio.{video_format['extension']}"
                    output_file_with_audio_path = os.path.join(full_output_folder, output_file_with_audio)
                    track_output_file(output_file_with_audio_path)
                    try:
                        mux_metadata_path = None
                        if video_format.get('save_metadata', 'False') != 'False':
                            mux_metadata_path = create_ffmetadata_file(video_metadata, folder_paths.get_temp_directory())
                        mux_args, channels = build_audio_mux_args(
                            video_format,
                            file_path,
                            output_file_with_audio_path,
                            audio,
                            total_frames_output,
                            frame_rate,
                            metadata_path=mux_metadata_path,
                        )
                        audio_data = audio['waveform'].squeeze(0).transpose(0,1) \
                                .numpy().tobytes()
                        try:
                            res = subprocess.run(mux_args, input=audio_data,
                                                 env=env, capture_output=True, check=True)
                        except subprocess.CalledProcessError as e:
                            raise Exception(
                                "An error occured while muxing audio into the video output:\n"
                                + f"format={video_format['extension']} codec_args={video_format.get('audio_pass')} "
                                + f"sample_rate={audio['sample_rate']} channels={channels}\n"
                                + e.stderr.decode(*ENCODE_ARGS)
                            )
                    finally:
                        if mux_metadata_path is not None and os.path.exists(mux_metadata_path):
                            os.remove(mux_metadata_path)
                    if res.stderr:
                        print(res.stderr.decode(*ENCODE_ARGS), end="", file=sys.stderr)
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    debug_log("video_mux_complete", silent_path=file_path, muxed_path=output_file_with_audio_path)
                    final_output_path = output_file_with_audio_path
                    final_output_name = output_file_with_audio
                sequence_output_paths = _sequence_output_paths(final_output_path, total_frames_output)
                if sequence_output_paths is not None:
                    output_files.extend(sequence_output_paths)
                else:
                    output_files.append(final_output_path)
                file = final_output_name
            if extra_options.get('VHS_KeepIntermediate', True) == False:
                _, intermediate_files, _ = _split_output_files(output_files)
                for intermediate in intermediate_files:
                    if os.path.exists(intermediate):
                        os.remove(intermediate)
            preview = {
                "filename": file,
                "subfolder": subfolder,
                "type": "output" if save_output else "temp",
                "format": format,
                "frame_rate": frame_rate,
                "fullpath": output_files[-1],
            }
            if saved_workflow_image:
                preview["workflow"] = first_image_file
            if num_frames == 1 and 'png' in format and '%03d' in file:
                preview['format'] = 'image/png'
                preview['filename'] = file.replace('%03d', '001')
            return {"ui": {"gifs": [preview]}, "result": ((save_output, output_files),)}
        except Exception:
            _cleanup_partial_output_files(partial_output_files)
            raise
