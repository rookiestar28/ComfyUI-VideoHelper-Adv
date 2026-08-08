# ComfyUI-VideoHelper-Adv

Video workflow nodes for ComfyUI, based on
[Kosinkadink/ComfyUI-VideoHelperSuite](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite).

This fork keeps the upstream VHS node surface compatible where practical, while
focusing on safer output behavior, stricter format validation, more predictable
preview/output metadata, explicit filesystem trust boundaries, and
repo-appropriate automated tests.

<details><summary><h2>Latest Updates - Click to expand</h2></summary>

<details>

<summary><strong>Filesystem, URL, and metadata-query boundaries hardened</strong></summary>

- Media reads, directory listings, previews, output writes, and artifact deletes
  now pass through one canonical capability policy with safer host-root defaults.
- Explicit deployment, external-read allowlist, legacy-local compatibility, and
  HTTPS URL modes now fail closed when their configuration is contradictory or
  unsafe for the active listen posture.
- URL downloads reject credential-bearing, non-HTTPS, and private-address targets
  before launch, then require bounded results to remain inside ComfyUI temp.
- Video metadata queries now use a bounded, file-state-aware cache that
  reauthorizes both hits and stores instead of retaining unbounded stale path data.

</details>

<details>

<summary><strong>Frontend and core compatibility paths refreshed</strong></summary>

- Removed deprecated frontend-internal imports and kept the extension on supported
  ComfyUI entry points, including Desktop bridge-present and bridge-absent loading.
- Split the largest backend encode/output flow and frontend extension core into
  focused modules while preserving node IDs, imports, workflow behavior, and
  extension registration ownership.
- Format widgets now reconcile against backend-published schemas without deleting
  linked inputs or depending on private frontend configuration mutation.
- `Select Latest` remains a frontend-virtual workflow node with a safe backend
  rejection; missing or denied paths clear stale target widgets.
- Removed unreachable clipboard video-blob and nonexistent-node branches while
  preserving the supported copied-output-path paste interaction.

</details>

<details>

<summary><strong>Output execution and filter handling made more deterministic</strong></summary>

- Lazy-loaded audio inputs are preserved through output execution, and audio mux
  completion returns the final muxed artifact rather than a silent intermediate.
- Metadata-enabled audio outputs retain round-trip workflow metadata, while
  metadata-disabled outputs suppress prompt/workflow data in both video tags and
  the utility PNG sidecar.
- Repeated simple FFmpeg filters remain supported; unsupported mixed
  `-filter_complex` plus `-vf`/`-af` configurations now fail clearly before launch.
- Image-sequence results, partial-output cleanup, prune validation, filename
  dimensions, and completed-output preview routing remain covered as concrete
  output contracts.

</details>

<details>

<summary><strong>Validation and independent release safeguards added</strong></summary>

- Added repository-local Python, JavaScript syntax, Node helper, video-format, and
  diff gates with native Windows and Linux entry points.
- Added an owned, workspace-contained ComfyUI runtime matrix for output behavior,
  plus focused path-policy and deferred-path matrices that start and stop only
  their own loopback hosts.
- Added least-privilege hosted validation for Ubuntu/Python 3.10 and 3.12 plus
  Windows/Python 3.12, using Node.js 20 for frontend helper checks.
- Added deterministic package inspection and a manually dispatched, confirmation-
  and tag-gated Comfy Registry workflow. These safeguards do not publish anything
  unless the explicit publish path is separately invoked with release credentials.

</details>

</details>

## Table of Contents

- [What This Fork Improves Over Upstream](#what-this-fork-improves-over-upstream)
- [Installation](#installation)
- [Main Nodes](#main-nodes)
  - [Load Video](#load-video)
  - [Load Image Sequence](#load-image-sequence)
  - [Video Combine](#video-combine)
  - [Prune Outputs](#prune-outputs)
  - [Audio Nodes](#audio-nodes)
  - [Batch, Info, and Utility Nodes](#batch-info-and-utility-nodes)
- [Video Previews](#video-previews)
  - [Advanced Previews](#advanced-previews)
- [Filesystem and URL Security Policy](#filesystem-and-url-security-policy)
- [Video Formats](#video-formats)
- [Testing This Fork](#testing-this-fork)
- [Compatibility Notes](#compatibility-notes)

---

## What This Fork Improves Over Upstream

The fork began with reliability and save-path hardening around
`VHS_VideoCombine` and now also owns explicit security, compatibility, and
validation boundaries:

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
| Lazy audio | Lazy audio objects could be treated as absent before their waveform was materialized. | Audio presence is preserved through lazy evaluation and verified on the final muxed output. |
| Frontend compatibility | Deprecated or private frontend internals create breakage as ComfyUI and Desktop evolve. | Production modules use supported ComfyUI entry points, publish format-widget contracts from the backend, and retain stable workflow/node identities. |
| Filesystem and URL access | Path reads, previews, URL downloads, writes, and deletes can drift into inconsistent trust rules. | One canonical capability policy defaults reads to ComfyUI host roots, keeps writes/deletes in output or temp, and disables URL loading by default for remote-restricted deployments. |
| Metadata queries | An unbounded path-keyed metadata cache could retain stale or no-longer-authorized results. | Query metadata uses a bounded LRU with current authorization and file-state checks on every boundary. |
| Tests and validation | Upstream is primarily plugin/runtime driven. | This fork adds local and hosted unit/static gates, format validation, Node helper tests, owned live-host matrices, and deterministic release-package inspection. |

## Installation

Install this fork into ComfyUI's `custom_nodes` directory:

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/rookiestar28/ComfyUI-VideoHelper-Adv.git
cd ComfyUI-VideoHelper-Adv
python -m pip install -r requirements.txt
```

Use the same Python interpreter or environment that starts ComfyUI. Restart
ComfyUI after installation.

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

`Select Latest` is a frontend-virtual helper: ComfyUI applies its selected path
to connected widgets before queue submission. Direct backend/API submission of
that helper fails safely instead of executing as a path-reading node.

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
- The backend path policy applies the same authorization to previews as to node loads.

Tradeoffs:

- Preview generation can add delay for long inputs.
- Generated previews are lower quality than the original media; use Open preview for the source artifact.

## Filesystem and URL Security Policy

The default filesystem mode is `host_roots`. Media reads, directory listings,
and previews are limited to ComfyUI's input, output, and temp roots. Writes and
deletes are always limited to output and temp, including when an external read
root is configured.

Server environment options:

- `VHS_PATH_POLICY=host_roots|allowlist|legacy_local` selects the filesystem
  mode. `legacy_local` restores arbitrary local reads for compatibility and is
  accepted only for a loopback-only `trusted_local` deployment.
- `VHS_EXTERNAL_READ_ROOTS` is an OS-path-separator-delimited list of existing
  directories. It is required by `allowlist`, rejected in other modes, and
  grants read/list/preview capabilities only.
- `VHS_DEPLOYMENT_PROFILE=trusted_local|remote_restricted` may make the derived
  deployment profile more restrictive. A non-loopback or unknown listen
  address cannot be overridden to `trusted_local`.
- `VHS_URL_POLICY=disabled|https` controls URL-backed media loading. Its default
  is `https` for loopback-only trusted-local operation and `disabled` for a
  remote-restricted deployment.

`VHS_STRICT_PATHS` is a temporary deprecated alias. During its compatibility
window, any value maps to `host_roots`; it cannot enable `legacy_local`, and it
must not be combined with `VHS_PATH_POLICY`. The alias is scheduled for removal
in the next breaking release after the first public release containing this
policy.

HTTPS URL loading rejects credentials, fragments, non-HTTPS schemes, and
non-public resolved IP addresses before launching the downloader. Downloads
are time/size bounded and their results must resolve inside ComfyUI temp.
Redirect and DNS-rebinding behavior in the external downloader remains a
residual network risk; keep URL loading disabled for LAN, proxy, public, or
multi-user deployments unless that risk is separately isolated and accepted.

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

Repeated simple `-vf` or `-af` filters are merged. A format that mixes
`-filter_complex` with an additional simple filter is rejected explicitly;
arbitrary filter-graph composition is not inferred automatically.

Validate format files after editing:

```bash
python scripts/validate_video_formats.py
```

## Testing This Fork

This repository is a ComfyUI custom node repo, not a standalone web app or npm
package. It intentionally has no `package.json`, `npm test`, repo-managed
Playwright dependency/browser installation, or `.pre-commit-config.yaml`.

Install test tooling into a repository-local `.venv` from
`requirements-test.txt`. The wrappers below require that local environment and
Node.js 18 or newer.

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
```

```bash
# Linux / Git Bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
```

Windows PowerShell:

```powershell
powershell -NoProfile -File scripts/run_repo_checks.ps1
```

Linux or Git Bash:

```bash
bash scripts/run_pre_push_checks.sh
```

Both wrappers delegate to the same repository check runner and execute:

- Python compile checks
- Python unit tests through `scripts/run_unittests.py`
- JavaScript syntax checks for `web/js/*.js`
- Node built-in helper tests under `tests/js/`
- all `video_formats/*.json` schema checks
- `git diff --check`

The same contract runs in `.github/workflows/validate.yml` on Ubuntu with
Python 3.10/3.12 and Windows with Python 3.12. Hosted validation is
publication-incapable.

Additional useful checks:

```bash
python scripts/validate_video_formats.py
python scripts/probe_video_format_outputs.py
python -m unittest tests.test_runtime_validation_matrix
```

Live behavior uses owned, workspace-contained ComfyUI processes. Point these
commands at a trusted ComfyUI checkout and the Python interpreter that can run
that host; do not point them at an unreviewed reference checkout.

Run all eight output scenarios:

```bash
python scripts/runtime/run_runtime_matrix.py \
  --comfyui-root <TRUSTED_COMFYUI_ROOT> \
  --comfyui-python <TRUSTED_COMFYUI_PYTHON> \
  --all
```

Run the filesystem/URL policy matrix:

```bash
python scripts/runtime/run_path_policy_matrix.py \
  --comfyui-root <TRUSTED_COMFYUI_ROOT> \
  --comfyui-python <TRUSTED_COMFYUI_PYTHON>
```

Run the focused virtual-node, cache, and FFmpeg-filter matrix:

```bash
python scripts/runtime/run_deferred_paths_matrix.py \
  --comfyui-root <TRUSTED_COMFYUI_ROOT> \
  --comfyui-python <TRUSTED_COMFYUI_PYTHON>
```

The runners copy the plugin into a disposable workspace-contained layout,
listen only on an owned loopback port, stop only their own host, and write
content-free result documents under `.tmp/runtime_results/`. The production UI
probe is intentionally separate because this repository does not install or
manage a browser dependency.

## Compatibility Notes

- The node names and common workflow shape are kept close to upstream VHS for workflow compatibility.
- Output behavior is stricter than upstream when it prevents silent corruption, metadata leakage, unsupported audio muxing, or unsafe deletion.
- External custom `video_formats` should explicitly declare audio support with `supports_audio` and `audio_pass` when audio is intended.
- Path authorization is enforced by a central, canonical capability policy. External read roots never grant output-write or delete access.
- `legacy_local` is an explicit loopback-only compatibility mode; it is not the default and is rejected for remote-restricted deployments.
- URL loading should remain disabled for LAN, reverse-proxy, public, or multi-user deployments unless downloader isolation and residual redirect/DNS risks are accepted separately.
- This repository has independent versioning, validation, and release safeguards. It does not require an upstream pull request and does not imply that a Registry release has already occurred.
