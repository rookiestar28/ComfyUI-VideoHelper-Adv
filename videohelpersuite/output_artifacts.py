"""Output artifact classification, cleanup, and metadata helpers."""

import datetime
import json
import os
import re

import folder_paths
from PIL.PngImagePlugin import PngInfo

from .utils import ENCODE_ARGS


_SEQUENCE_COUNTER_RE = re.compile(r"%(0?)(\d*)d")
_NUMBERED_SEQUENCE_FILE_RE = re.compile(r"^(.*?)(\d+)(\.[^.]*)$")


def _first_sequence_output_path(file_path):
    def replace_counter(match):
        width_text = match.group(2)
        if width_text:
            return str(1).zfill(int(width_text))
        return "1"

    if "%" not in file_path:
        return None
    candidate, replacements = _SEQUENCE_COUNTER_RE.subn(replace_counter, file_path, count=1)
    if replacements == 0:
        return None
    return candidate


def _sequence_output_paths(file_path, frame_count):
    try:
        count = int(frame_count)
    except (TypeError, ValueError):
        return None
    if count <= 0 or "%" not in file_path:
        return None

    output_paths = []
    for frame_index in range(1, count + 1):
        def replace_counter(match):
            width_text = match.group(2)
            if width_text:
                return str(frame_index).zfill(int(width_text))
            return str(frame_index)

        candidate, replacements = _SEQUENCE_COUNTER_RE.subn(replace_counter, file_path, count=1)
        if replacements == 0:
            return None
        output_paths.append(candidate)
    return output_paths


def _paths_form_numbered_sequence(paths):
    if len(paths) < 2:
        return False

    first_match = None
    numbers = []
    for path in paths:
        match = _NUMBERED_SEQUENCE_FILE_RE.fullmatch(os.path.basename(path))
        if match is None:
            return False
        directory = os.path.dirname(os.path.abspath(path))
        parts = (directory, match.group(1), len(match.group(2)), match.group(3))
        if first_match is None:
            first_match = parts
        elif parts != first_match:
            return False
        numbers.append(int(match.group(2)))

    return numbers == list(range(numbers[0], numbers[0] + len(numbers)))


def _split_output_files(output_files):
    files = list(output_files)
    if len(files) == 0:
        return [], [], []
    if _paths_form_numbered_sequence(files):
        return [], [], files
    if len(files) == 1:
        return [], [], files

    utility_files = [files[0]]
    remaining_files = files[1:]
    if _paths_form_numbered_sequence(remaining_files):
        return utility_files, [], remaining_files
    return utility_files, files[1:-1], [files[-1]]


def _ffmpeg_expected_output_exists(file_path):
    if os.path.exists(file_path):
        return True
    sequence_first_frame = _first_sequence_output_path(file_path)
    return sequence_first_frame is not None and os.path.exists(sequence_first_frame)


def _decode_ffmpeg_stderr(stderr):
    if not stderr:
        return ""
    return stderr.decode(*ENCODE_ARGS)


def _raise_ffmpeg_failure(context, returncode, stderr):
    detail = _decode_ffmpeg_stderr(stderr)
    if not detail:
        detail = f"ffmpeg exited with code {returncode}"
    raise Exception(
        f"An error occurred in the ffmpeg subprocess ({context}, exit code {returncode}):\n"
        + detail
    )


def _raise_missing_ffmpeg_output(file_path):
    expected = file_path
    sequence_first_frame = _first_sequence_output_path(file_path)
    if sequence_first_frame is not None:
        expected += f" or first sequence frame {sequence_first_frame}"
    raise Exception(
        "The ffmpeg subprocess completed but did not create expected output:\n"
        + expected
    )


def _finalize_ffmpeg_process(proc, file_path, context):
    # IMPORTANT: stderr is not a success signal; return code and artifact existence are both required.
    stderr = proc.stderr.read()
    returncode = proc.wait()
    if returncode != 0:
        _raise_ffmpeg_failure(context, returncode, stderr)
    if not _ffmpeg_expected_output_exists(file_path):
        _raise_missing_ffmpeg_output(file_path)
    return stderr


def _path_is_inside_directory(path, directory):
    try:
        return os.path.commonpath(
            [os.path.abspath(directory), os.path.abspath(path)]
        ) == os.path.abspath(directory)
    except ValueError:
        return False


def _remove_output_file_if_exists(file_path, output_dirs=None):
    if output_dirs is None:
        output_dirs = [
            folder_paths.get_output_directory(),
            folder_paths.get_temp_directory(),
        ]
    if not any(_path_is_inside_directory(file_path, directory) for directory in output_dirs):
        raise Exception("Tried to cleanup output from invalid directory: " + file_path)
    if os.path.exists(file_path):
        os.remove(file_path)


def _cleanup_partial_output_files(file_paths):
    cleaned = set()
    for file_path in reversed(file_paths):
        if file_path in cleaned:
            continue
        _remove_output_file_if_exists(file_path)
        cleaned.add(file_path)


def _get_workflow_extra_options(extra_pnginfo):
    if extra_pnginfo is None:
        return {}
    return extra_pnginfo.get('workflow', {}).get('extra', {})


def _video_format_saves_metadata(video_format):
    if video_format is None:
        return True
    return video_format.get('save_metadata', 'False') != 'False'


def _build_output_metadata(prompt, extra_pnginfo, include_workflow_metadata):
    metadata = PngInfo()
    video_metadata = {}
    if include_workflow_metadata:
        if prompt is not None:
            metadata.add_text("prompt", json.dumps(prompt))
            video_metadata["prompt"] = prompt
        if extra_pnginfo is not None:
            for x in extra_pnginfo:
                metadata.add_text(x, json.dumps(extra_pnginfo[x]))
                video_metadata[x] = extra_pnginfo[x]
    metadata.add_text("CreationTime", datetime.datetime.now().isoformat(" ")[:19])
    return metadata, video_metadata
