"""Media frame conversion, encoder process, and audio mux helpers."""

import os
import subprocess
import sys

import folder_paths
import numpy as np

from .output_artifacts import _finalize_ffmpeg_process, _raise_ffmpeg_failure
from .logger import logger
from .utils import ENCODE_ARGS, ffmpeg_path, gifski_path, merge_filter_args
from .video_metadata import create_ffmetadata_file


def tensor_to_int(tensor, bits):
    tensor = tensor.cpu().numpy() * (2**bits-1) + 0.5
    return np.clip(tensor, 0, (2**bits-1))
def tensor_to_shorts(tensor):
    return tensor_to_int(tensor, 16).astype(np.uint16)
def tensor_to_bytes(tensor):
    return tensor_to_int(tensor, 8).astype(np.uint8)


def _has_audio_input(audio):
    # IMPORTANT: Load Video returns lazy audio; probing waveform here can hide extraction failures as silence.
    return audio is not None


def _video_format_supports_audio(video_format):
    if video_format.get("supports_audio") is False:
        return False
    return "audio_pass" in video_format


def _raise_unsupported_audio_format(video_format, format_name=None):
    extension = video_format.get("extension", "unknown")
    label = format_name or extension
    raise Exception(
        "Selected output format does not support audio: "
        + f"format={label} extension={extension}. "
        + "Choose a format with explicit audio support."
    )


def build_audio_mux_args(video_format, file_path, output_file_with_audio_path, audio, total_frames_output, frame_rate, metadata_path=None):
    if not _video_format_supports_audio(video_format):
        _raise_unsupported_audio_format(video_format)
    channels = audio['waveform'].size(1)
    min_audio_dur = total_frames_output / frame_rate + 1
    if video_format.get('trim_to_audio', 'False') != 'False':
        apad = []
    else:
        apad = ["-af", "apad=whole_dur="+str(min_audio_dur)]
    mux_args = [
        ffmpeg_path, "-v", "error", "-n",
        "-i", file_path,
        "-ar", str(audio['sample_rate']), "-ac", str(channels),
        "-f", "f32le", "-i", "-",
    ]
    metadata_input_index = None
    if metadata_path is not None:
        # IMPORTANT: ffmetadata must remain in the input section; placing it after output codec args
        # makes ffmpeg treat -c:v copy as an input decoder option and breaks audio muxing.
        mux_args += ["-f", "ffmetadata", "-i", metadata_path]
        metadata_input_index = 2
    mux_args += ["-c:v", "copy"] + video_format["audio_pass"]
    if metadata_input_index is not None:
        mux_args += ["-map_metadata", str(metadata_input_index), "-movflags", "use_metadata_tags"]
    mux_args += apad + ["-shortest", output_file_with_audio_path]
    merge_filter_args(mux_args, '-af')
    return mux_args, channels

def ffmpeg_process(args, video_format, video_metadata, file_path, env):

    res = b""
    frame_data = yield
    total_frames_output = 0
    metadata_path = None
    needs_main_pass = video_format.get('save_metadata', 'False') == 'False'
    if video_format.get('save_metadata', 'False') != 'False':
        # IMPORTANT: keep this comment payload contract aligned with web/js/videoMetadataParser.js.
        # Saved-video workflow re-import depends on the final muxed file retaining this metadata.
        metadata_path = create_ffmetadata_file(video_metadata, folder_paths.get_temp_directory())
    if metadata_path is not None:
        m_args = args[:1] + ["-i", metadata_path] + args[1:] + [
            "-map_metadata", "0",
            "-metadata", "creation_time=now",
            "-movflags", "use_metadata_tags",
        ]
        try:
            with subprocess.Popen(m_args + [file_path], stderr=subprocess.PIPE,
                                  stdin=subprocess.PIPE, env=env) as proc:
                try:
                    while frame_data is not None:
                        proc.stdin.write(frame_data)
                        #TODO: skip flush for increased speed
                        frame_data = yield
                        total_frames_output+=1
                    proc.stdin.flush()
                    proc.stdin.close()
                    res = _finalize_ffmpeg_process(proc, file_path, "metadata encode")
                    needs_main_pass = False
                except BrokenPipeError as e:
                    err = proc.stderr.read()
                    returncode = proc.wait()
                    #Check if output file exists. If it does, the re-execution
                    #will also fail. This obscures the cause of the error
                    #and seems to never occur concurrent to the metadata issue
                    if os.path.exists(file_path):
                        _raise_ffmpeg_failure("metadata encode", returncode, err)
                    if total_frames_output > 0:
                        _raise_ffmpeg_failure("metadata encode", returncode, err)
                    print(err.decode(*ENCODE_ARGS), end="", file=sys.stderr)
                    logger.warn("An error occurred when saving with metadata")
                    total_frames_output = 0
                    needs_main_pass = True
                    res = err
        finally:
            if os.path.exists(metadata_path):
                os.remove(metadata_path)
    if needs_main_pass:
        total_frames_output = 0
        with subprocess.Popen(args + [file_path], stderr=subprocess.PIPE,
                              stdin=subprocess.PIPE, env=env) as proc:
            try:
                while frame_data is not None:
                    proc.stdin.write(frame_data)
                    frame_data = yield
                    total_frames_output+=1
                proc.stdin.flush()
                proc.stdin.close()
                res = _finalize_ffmpeg_process(proc, file_path, "main encode")
            except BrokenPipeError as e:
                res = proc.stderr.read()
                returncode = proc.wait()
                _raise_ffmpeg_failure("main encode", returncode, res)
    yield total_frames_output
    if len(res) > 0:
        print(res.decode(*ENCODE_ARGS), end="", file=sys.stderr)

def gifski_process(args, dimensions, frame_rate, video_format, file_path, env):
    frame_data = yield
    with subprocess.Popen(args + video_format['main_pass'] + ['-f', 'yuv4mpegpipe', '-'],
                          stderr=subprocess.PIPE, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env=env) as procff:
        with subprocess.Popen([gifski_path] + video_format['gifski_pass']
                              + ['-W', f'{dimensions[0]}', '-H', f'{dimensions[1]}']
                              + ['-r', f'{frame_rate}']
                              + ['-q', '-o', file_path, '-'], stderr=subprocess.PIPE,
                              stdin=procff.stdout, stdout=subprocess.PIPE,
                              env=env) as procgs:
            try:
                while frame_data is not None:
                    procff.stdin.write(frame_data)
                    frame_data = yield
                procff.stdin.flush()
                procff.stdin.close()
                resff = procff.stderr.read()
                resgs = procgs.stderr.read()
                outgs = procgs.stdout.read()
            except BrokenPipeError as e:
                procff.stdin.close()
                resff = procff.stderr.read()
                resgs = procgs.stderr.read()
                raise Exception("An error occurred while creating gifski output\n" \
                        + "Make sure you are using gifski --version >=1.32.0\nffmpeg: " \
                        + resff.decode(*ENCODE_ARGS) + '\ngifski: ' + resgs.decode(*ENCODE_ARGS))
    if len(resff) > 0:
        print(resff.decode(*ENCODE_ARGS), end="", file=sys.stderr)
    if len(resgs) > 0:
        print(resgs.decode(*ENCODE_ARGS), end="", file=sys.stderr)
    #should always be empty as the quiet flag is passed
    if len(outgs) > 0:
        print(outgs.decode(*ENCODE_ARGS))

def to_pingpong(inp):
    if not hasattr(inp, "__getitem__"):
        inp = list(inp)
    yield from inp
    for i in range(len(inp)-2,0,-1):
        yield inp[i]
