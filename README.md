# ComfyUI-VideoHelper-Adv

Video workflow nodes for ComfyUI, based on
[Kosinkadink/ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite).

This fork keeps the upstream VHS node surface compatible where practical, while
focusing on safer output behavior, stricter format validation, more predictable
preview/output metadata, and repo-appropriate automated tests.

## What This Fork Improves Over Upstream

The main changes in this fork are reliability and save-path hardening around
`VHS_VideoCombine`:

| Area | Upstream behavior risk | This fork |
|---|---|---|
| FFmpeg completion | A spawned ffmpeg process could appear successful even when it exited non-zero or did not create the expected file. | Encode completion checks both process return code and expected output existence before returning a result. |
| Early failures | Some validation failures could happen after a utility PNG or partial output was already written. | Predictable validation runs before durable artifact creation where possible; failures after writing clean up partial artifacts inside output/temp roots. |
| Audio/container compatibility | Missing `audio_pass` could fall back to implicit Opus args even for incompatible outputs. | Audio support is explicit. Built-in formats declare `supports_audio`, and unsupported `audio + format` combinations fail early with a clear error. |
| Metadata privacy | `save_metadata=false` could still leave prompt/workflow metadata in the VHS utility PNG sidecar. | `save_metadata=false` suppresses prompt/workflow metadata in both video metadata and the utility PNG sidecar. `CreationTime` may remain. |
| Image sequence outputs | `%03d` image-sequence outputs were represented as the pattern path rather than the actual generated frames. | `VHS_FILENAMES` contains concrete generated frame paths, and `VHS_PruneOutputs` understands expanded sequence outputs. |
| Prune safety | Prune behavior assumed at most three paths and could delete before discovering an invalid later path. | Prune classifies utility/intermediate/final artifacts, supports expanded sequences, deduplicates candidates, and validates all paths before deleting. |
| Filename templates | `VideoCombine` did not pass dimensions into ComfyUI's save helper, so `%width%` and `%height%` resolved to `0`. | `filename_prefix` dimension tokens resolve from the first frame dimensions. |
| Preview routing | Completed output preview routing has been aligned to avoid showing stale input-only previews for completed outputs. | Preview helper tests cover advanced routing states. |
| Tests and validation | Upstream is primarily plugin/runtime driven. | This fork adds repo-local unit tests, format validation, JS helper tests, a pre-push sweep, and a runtime validation matrix. |

## Installation

Install this fork into ComfyUI's `custom_nodes` directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/rookiestar28/ComfyUI-VideoHelper-Adv.git
cd ComfyUI-VideoHelper-Adv
pip install -r requirements.txt
```

Restart ComfyUI after installation.

Runtime requirements:

- `opencv-python`
- `imageio-ffmpeg`
- `av`
- `psutil`
- ffmpeg available through `imageio-ffmpeg`, the repo path, or the system PATH
- gifski is optional and only needed for gifski-based GIF output

## Main Nodes

### Load Video

Converts a video file into image frames, frame count, audio, and video info.

Common controls:

- `force_rate`: duplicates or drops frames to target a frame rate; `0` disables it.
- `force_size`: quickly resizes to common dimensions while preserving aspect ratio where applicable.
- `frame_load_cap`: maximum frames returned.
- `skip_first_frames`: skips frames after frame-rate adjustment.
- `select_every_nth`: samples every Nth source frame without frame duplication.

Both upload and path variants are available. FFmpeg-based variants also expose
mask/alpha behavior where supported.

### Load Image Sequence

Loads all supported images from a directory into an image batch, with controls
similar to Load Video:

- `image_load_cap`
- `skip_first_images`
- `select_every_nth`

Upload and path variants are available.

### Video Combine

Combines an image batch into a video, GIF, WebP, or image sequence. Optional
audio can be muxed only when the selected output format explicitly supports
audio.

Important inputs:

- `frame_rate`: output playback frame rate.
- `loop_count`: additional repeats for animated image outputs.
- `filename_prefix`: output prefix. Subfolders and ComfyUI format tokens are supported, including `%date:*%`, `%width%`, and `%height%`.
- `format`: output format from `video_formats/*.json` or built-in image formats.
- `pingpong`: plays frames forward and then backward for loopable motion.
- `save_output`: writes to ComfyUI output when true, or temp when false.
- `save_metadata`: when available for the selected video format, controls prompt/workflow metadata in the output video and VHS utility PNG sidecar.

`Video Combine` returns `VHS_FILENAMES`: `(save_output, output_paths)`.

- The list contains full paths for generated artifacts in creation/final-output order.
- For audio outputs, the final muxed file is returned instead of exposing the silent intermediate as the final result.
- For image-sequence formats such as `8bit-png` and `16bit-png`, the list contains the actual generated frame paths, not only the `%03d` pattern.

Workflow extra options still apply:

- `VHS_MetadataImage=false`: do not write the utility PNG sidecar.
- `VHS_KeepIntermediate=false`: remove true intermediate artifacts while preserving final artifacts.

### Prune Outputs

Deletes selected outputs from a `VHS_FILENAMES` result.

Options:

- `Intermediate`: delete true intermediate processing files.
- `Intermediate and Utility`: also delete the utility PNG sidecar.
- `All`: delete utility, intermediate, and final artifacts.

This fork validates all prune candidates are inside ComfyUI output/temp roots
before deleting anything, and handles expanded image-sequence frame lists.

### Audio Nodes

- `Load Audio (Path)`
- `Load Audio (Upload)`
- legacy `VHS_AUDIO` conversion helpers

### Batch, Info, and Utility Nodes

The fork keeps the upstream batch and utility nodes, including:

- Meta Batch Manager
- Video Info / Video Info Source / Video Info Loaded
- Select Filename / Select Latest
- VAE Encode Batched / VAE Decode Batched
- Split, merge, repeat, select, and count helpers for images, masks, and latents
- Unbatch

## Video Previews

Load Video, Load Images, and Video Combine provide previews.

Preview context menu actions include:

- Open preview
- Save preview
- Pause preview
- Hide preview
- Sync preview

### Advanced Previews

Advanced Previews can be enabled in ComfyUI settings through `VHS Advanced
Previews`. When enabled, preview routes can transcode or downscale media for
browser-friendly display.

Benefits:

- Load Video previews can reflect node settings such as skipped frames and load caps.
- Remote browser bandwidth can be reduced.
- Browser performance can improve for large or normally unsupported media.
- `VHS_STRICT_PATHS` can limit previews to ComfyUI subdirectories.

Tradeoffs:

- Preview generation can add delay for long inputs.
- Generated previews are lower quality than the original media; use Open preview for the source artifact.

## Video Formats

Video formats are JSON files under `video_formats/` or the ComfyUI
`VHS_video_formats` folder. They describe ffmpeg args and optional UI widgets.

Example:

```json
{
  "main_pass": [
    "-n",
    "-c:v", "libsvtav1",
    "-pix_fmt", "yuv420p10le",
    "-crf", ["crf", "INT", {"default": 23, "min": 0, "max": 100, "step": 1}]
  ],
  "audio_pass": ["-c:a", "libopus"],
  "supports_audio": true,
  "save_metadata": ["save_metadata", "BOOLEAN", {"default": true}],
  "extension": "webm",
  "environment": {"SVT_LOG": "1"}
}
```

Key fields:

- `main_pass`: ffmpeg arguments used for the main video/image encode.
- `audio_pass`: ffmpeg audio args used only when audio is connected.
- `supports_audio`: must be `true` when the format supports audio, or `false` for formats that must reject audio.
- `save_metadata`: exposes metadata saving as a widget for supported video formats.
- `extension`: output file extension and container hint.
- `environment`: optional environment variables for the ffmpeg process.
- `input_color_depth`: `8bit` or `16bit`.
- `dim_alignment`: optional output dimension alignment requirement.

Validate format files after editing:

```bash
python scripts/validate_video_formats.py
```

## Testing This Fork

This repository is a ComfyUI custom node repo, not a standalone web app or npm
package. It intentionally has no `package.json`, no Playwright harness, and no
`.pre-commit-config.yaml`.

Preferred full local sweep:

```bash
bash scripts/run_pre_push_checks.sh
```

That script runs:

- Python compile checks
- Python unit tests through `scripts/run_unittests.py`
- JavaScript syntax checks for `web/js/*.js`
- Node built-in helper tests under `tests/js/`
- `git diff --check`

Additional useful checks:

```bash
python scripts/validate_video_formats.py
python scripts/probe_video_format_outputs.py
python -m unittest tests.test_runtime_validation_matrix
```

Runtime/UI validation still requires a real local ComfyUI instance with this
plugin enabled. The repo records the required scenario coverage in
`tests/runtime_validation_matrix.json`; those fixtures are not a standalone
runtime runner.

## Compatibility Notes

- The node names and common workflow shape are kept close to upstream VHS for workflow compatibility.
- Output behavior is stricter than upstream when it prevents silent corruption, metadata leakage, unsupported audio muxing, or unsafe deletion.
- External custom `video_formats` should explicitly declare audio support with `supports_audio` and `audio_pass` when audio is intended.
- Path containment remains delegated to ComfyUI's `folder_paths` helper; this fork does not allow arbitrary output writes outside ComfyUI output/temp roots.
