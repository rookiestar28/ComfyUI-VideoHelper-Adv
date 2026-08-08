import server
import folder_paths
import os
import subprocess
import re
import tempfile
from contextlib import suppress

import asyncio
try:
    # IMPORTANT: keep this optional so the node package can still load without PyAV.
    import av
except ImportError:
    av = None

from .logger import logger
from .utils import is_url, get_sorted_dir_files_from_directory, ffmpeg_path, \
        validate_sequence, strip_path, try_download_video, ENCODE_ARGS, \
        debug_log, get_path_policy
from .path_policy import PathAccessDenied, PathCapability
from .query_cache import AuthorizedMetadataCache
from comfy.k_diffusion.utils import FolderOfImages


web = server.web


def error_response(status, message):
    debug_log("server_error", status=status, message=message)
    return web.Response(status=status, text=message)


def parse_int(query, key, default=0, minimum=None):
    try:
        value = int(float(query.get(key, default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def parse_float(query, key, default=0.0, minimum=None):
    try:
        value = float(query.get(key, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def decode_process_output(payload):
    if not payload:
        return ""
    return payload.decode(*ENCODE_ARGS)


def cleanup_temp_paths(paths):
    for path in paths or ():
        try:
            # CRITICAL: cleanup is a delete capability, not a best-effort path operation.
            authorized = get_path_policy().authorize_path(
                path,
                PathCapability.DELETE_ARTIFACT,
            )
            authorized = get_path_policy().reauthorize_path(authorized)
        except PathAccessDenied as exc:
            debug_log(
                "temp_cleanup_denied",
                capability=exc.capability.value,
                reason=exc.reason,
            )
            continue
        if authorized.root_id != "temp":
            debug_log("temp_cleanup_denied", reason="non_temp_root")
            continue
        with suppress(FileNotFoundError, OSError):
            os.remove(authorized.canonical)


async def cleanup_preview_process(proc, *, kill=False, label="preview"):
    if proc is None:
        return

    if kill and proc.returncode is None:
        with suppress(ProcessLookupError, OSError):
            proc.kill()

    with suppress(Exception):
        await proc.wait()

    transport = getattr(proc, "_transport", None)
    if transport is not None:
        # CRITICAL: explicitly close asyncio subprocess transports on Windows.
        # Relying on GC leaks Proactor pipe transports and emits closed-pipe warnings.
        with suppress(Exception):
            transport.close()

    debug_log("preview_process_cleanup", label=label, kill=kill, returncode=proc.returncode)


async def run_preview_prepass(*args):
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        stdout, stderr = await proc.communicate()
        return proc.returncode, stdout, stderr
    finally:
        await cleanup_preview_process(proc, label="preview_prepass")


async def stream_preview_response(request, *, args, filename, content_type, debug_event, cleanup_paths=None):
    proc = None
    response = web.StreamResponse()
    response.content_type = content_type
    response.headers["Content-Disposition"] = f"filename=\"{filename}\""
    prepared = False
    disconnected = False

    try:
        debug_log(debug_event, filename=filename, argument_count=len(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        await response.prepare(request)
        prepared = True
        while True:
            chunk = await proc.stdout.read(2**20)
            if not chunk:
                break
            await response.write(chunk)
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError) as exc:
        disconnected = True
        debug_log("preview_stream_disconnected", filename=filename, error=str(exc))
    except Exception as exc:
        debug_log("preview_stream_failed", filename=filename, error=str(exc))
        if not prepared:
            return error_response(500, f"Failed to generate preview for: {filename}")
    finally:
        await cleanup_preview_process(
            proc,
            kill=disconnected,
            label=filename,
        )
        cleanup_temp_paths(cleanup_paths)
        if prepared and not disconnected:
            with suppress(Exception):
                await response.write_eof()

    return response

@server.PromptServer.instance.routes.get("/vhs/viewvideo")
@server.PromptServer.instance.routes.get("/viewvideo")
async def view_video(request):
    query = request.rel_url.query
    path_res = await resolve_path(query)
    if isinstance(path_res, web.Response):
        return path_res
    file, filename, output_dir = path_res
    cleanup_paths = []

    if ffmpeg_path is None:
        # resolve_path already granted PREVIEW_MEDIA for the canonical target.
        return web.FileResponse(path=file)

    frame_rate = parse_float(query, 'frame_rate', 8.0, 0.0)
    if query.get('format', 'video') == "folder":
        os.makedirs(folder_paths.get_temp_directory(), exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=folder_paths.get_temp_directory(),
            prefix="vhs-folder-preview-",
            suffix=".txt",
            delete=False,
        ) as handle:
            concat_file = handle.name
        try:
            concat_authorized = get_path_policy().authorize_path(
                concat_file,
                PathCapability.WRITE_OUTPUT,
            )
        except PathAccessDenied:
            cleanup_temp_paths([concat_file])
            return error_response(403, "Preview workspace is not authorized.")
        if concat_authorized.root_id != "temp":
            cleanup_temp_paths([concat_file])
            return error_response(403, "Preview workspace is not authorized.")
        concat_file = concat_authorized.canonical
        skip_first_images = parse_int(query, 'skip_first_images', 0, 0)
        select_every_nth = parse_int(query, 'select_every_nth', 1, 1)
        valid_images = get_sorted_dir_files_from_directory(file, skip_first_images, select_every_nth, FolderOfImages.IMG_EXTENSIONS)
        if len(valid_images) == 0:
            cleanup_temp_paths([concat_file])
            return error_response(204, "No valid images were found for folder preview.")
        with open(concat_file, "w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for path in valid_images:
                f.write("file '" + os.path.abspath(path) + "'\n")
                f.write("duration 0.125\n")
        cleanup_paths.append(concat_file)
        in_args = ["-safe", "0", "-i", concat_file]
    else:
        in_args = ["-i", file]
        if '%' in file:
            in_args = ['-framerate', str(frame_rate)] + in_args
    #Do prepass to pull info
    #breaks skip_first frames if this default is ever actually needed
    base_fps = 30
    returncode, _, res_stderr = await run_preview_prepass(
        ffmpeg_path,
        *in_args,
        '-t',
        '0',
        '-f',
        'null',
        '-',
    )
    if returncode not in (0, None):
        debug_log(
            "view_video_prepass_failed",
            filename=filename,
            returncode=returncode,
            stderr_bytes=len(res_stderr or b""),
        )
        cleanup_temp_paths(cleanup_paths)
        return error_response(500, f"Failed to inspect media for preview: {filename}")

    match = re.search(': Video: (\\w+) .+, (\\d+) fps,', decode_process_output(res_stderr))
    if match:
        base_fps = float(match.group(2))
        if match.group(1) == 'vp9':
            #force libvpx for transparency
            in_args = ['-c:v', 'libvpx-vp9'] + in_args
    vfilters = []
    target_rate = parse_float(query, 'force_rate', 0.0, 0.0) or base_fps
    modified_rate = target_rate / parse_int(query, 'select_every_nth', 1, 1)
    start_time = 0
    if 'start_time' in query:
        start_time = parse_float(query, 'start_time', 0.0, 0.0)
    elif parse_float(query, 'skip_first_frames', 0.0, 0.0) > 0:
        start_time = parse_float(query, 'skip_first_frames', 0.0, 0.0)/target_rate
        if start_time > 1/modified_rate:
            start_time += 1/modified_rate
    if start_time > 0:
        if start_time > 4:
            post_seek = ['-ss', '4']
            pre_seek = ['-ss', str(start_time - 4)]
        else:
            post_seek = ['-ss', str(start_time)]
            pre_seek = []
    else:
        pre_seek = []
        post_seek = []

    args = [ffmpeg_path, "-v", "error"] + pre_seek + in_args + post_seek
    if target_rate != 0:
        args += ['-r', str(modified_rate)]
    if query.get('force_size','Disabled') != "Disabled":
        size = query['force_size'].split('x')
        if size[0] == '?' or size[1] == '?':
            size[0] = "-2" if size[0] == '?' else f"'min({size[0]},iw)'"
            size[1] = "-2" if size[1] == '?' else f"'min({size[1]},ih)'"
        else:
            #Aspect ratio is likely changed. A more complex command is required
            #to crop the output to the new aspect ratio
            ar = float(size[0])/float(size[1])
            vfilters.append(f"crop=if(gt({ar}\\,a)\\,iw\\,ih*{ar}):if(gt({ar}\\,a)\\,iw/{ar}\\,ih)")
        size = ':'.join(size)
        vfilters.append(f"scale={size}")
    if len(vfilters) > 0:
        args += ["-vf", ",".join(vfilters)]
    frame_cap = parse_int(query, 'frame_load_cap', 0, 0)
    if frame_cap > 0:
        args += ["-frames:v", str(frame_cap)]
    #TODO:reconsider adding high frame cap/setting default frame cap on node
    if query.get('deadline', 'realtime') == 'good':
        deadline = 'good'
    else:
        deadline = 'realtime'

    args += ['-c:v', 'libvpx-vp9','-deadline', deadline, '-cpu-used', '8', '-f', 'webm', '-']
    return await stream_preview_response(
        request,
        args=args,
        filename=filename,
        content_type='video/webm',
        debug_event="view_video_preview",
        cleanup_paths=cleanup_paths,
    )

@server.PromptServer.instance.routes.get("/vhs/viewaudio")
async def view_audio(request):
    query = request.rel_url.query
    path_res = await resolve_path(query)
    if isinstance(path_res, web.Response):
        return path_res
    file, filename, output_dir = path_res
    if ffmpeg_path is None:
        # resolve_path already granted PREVIEW_MEDIA for the canonical target.
        return web.FileResponse(path=file)

    in_args = ["-i", file]
    start_time = 0
    if 'start_time' in query:
        start_time = parse_float(query, 'start_time', 0.0, 0.0)
    args = [ffmpeg_path, "-v", "error", '-vn'] + in_args + ['-ss', str(start_time)]
    duration = parse_float(query, 'duration', 0.0, 0.0)
    if duration > 0:
        args += ['-t', str(duration)]
    if query.get('deadline', 'realtime') == 'good':
        deadline = 'good'
    else:
        deadline = 'realtime'

    args += ['-c:a', 'libopus','-deadline', deadline, '-cpu-used', '8', '-f', 'webm', '-']
    return await stream_preview_response(
        request,
        args=args,
        filename=filename,
        content_type='audio/webm',
        debug_event="view_audio_preview",
    )

query_cache = AuthorizedMetadataCache(max_entries=128)
@server.PromptServer.instance.routes.get("/vhs/queryvideo")
async def query_video(request):
    query = request.rel_url.query
    filepath = await resolve_path(query)
    #TODO: cache lookup
    if isinstance(filepath, web.Response):
        return filepath
    filepath = filepath[0]
    if filepath.endswith(".webp"):
        # ffmpeg doesn't support decoding animated WebP https://trac.ffmpeg.org/ticket/4907
        return web.json_response({})
    if av is None:
        return web.json_response({"error": "PyAV is not installed; video metadata probing is unavailable."}, status=503)
    policy = get_path_policy()
    try:
        authorized = policy.authorize_path(filepath, PathCapability.PREVIEW_MEDIA)
        source = query_cache.get(authorized, policy)
    except PathAccessDenied as exc:
        debug_log(
            "query_video_cache_denied",
            capability=exc.capability.value,
            reason=exc.reason,
        )
        return error_response(403, "Requested media path is not authorized.")
    except OSError:
        return error_response(404, "Media file not found.")
    if source is None:
        source = {}
        try:
            with av.open(filepath) as cont:
                stream = cont.streams.video[0]
                source['fps'] = float(stream.average_rate)
                source['duration'] = float(cont.duration / av.time_base)

                if stream.codec_context.name == 'vp9':
                    cc = av.Codec('libvpx-vp9', 'r').create()
                else:
                    cc = stream
                def fit():
                    for packet in cont.demux(video=0):
                        yield from cc.decode(packet)
                frame = next(fit())

                source['size'] = [frame.width, frame.height]
                source['alpha'] = 'a' in frame.format.name
                source['frames'] = stream.metadata.get('NUMBER_OF_FRAMES', round(source['duration'] * source['fps']))
        except Exception as exc:
            source = {}
            debug_log(
                "query_video_failed",
                filename=os.path.basename(filepath),
                error_type=type(exc).__name__,
            )
        if 'frames' in source:
            try:
                query_cache.put(authorized, source, policy)
            except PathAccessDenied as exc:
                debug_log(
                    "query_video_cache_store_denied",
                    capability=exc.capability.value,
                    reason=exc.reason,
                )
                return error_response(403, "Requested media path is not authorized.")
            except OSError:
                return error_response(404, "Media file not found.")
    if not 'frames' in source:
        return web.json_response({"error": "Failed to read video metadata."}, status=422)
    loaded = {}
    loaded['duration'] = max(0.0, source['duration'] - parse_float(query, 'start_time', 0.0, 0.0))
    loaded['fps'] = parse_float(query, 'force_rate', 0.0, 0.0) or source.get('fps',1)
    if loaded['fps'] <= 0:
        loaded['fps'] = source.get('fps', 1) or 1
    loaded['duration'] = max(0.0, loaded['duration'] - parse_int(query, 'skip_first_frames', 0, 0) / loaded['fps'])
    loaded['fps'] /= parse_int(query, 'select_every_nth', 1, 1)
    loaded['frames'] = max(0, round(loaded['duration'] * loaded['fps']))
    debug_log("query_video", filename=os.path.basename(filepath), source=source, loaded=loaded)
    return web.json_response({'source': source, 'loaded': loaded})

async def resolve_path(query):
    if "filename" not in query:
        return error_response(400, "Missing required query parameter: filename")
    filename = strip_path(query["filename"])
    if not filename:
        return error_response(400, "Empty filename.")

    #Path code misformats urls on windows and must be skipped
    if is_url(filename):
        try:
            file = await asyncio.to_thread(try_download_video, filename)
        except Exception as exc:
            debug_log("resolve_path_url_failed", error_type=type(exc).__name__)
            return error_response(502, "Failed to download media from URL.")
        if not file:
            return error_response(502, "Failed to download media from URL.")
        try:
            authorized = get_path_policy().authorize_path(
                file,
                PathCapability.PREVIEW_MEDIA,
            )
        except PathAccessDenied as exc:
            debug_log(
                "resolve_path_url_denied",
                capability=exc.capability.value,
                reason=exc.reason,
            )
            return error_response(403, "Downloaded media path is not authorized.")
        if authorized.root_id != "temp":
            return error_response(403, "Downloaded media path is not authorized.")
        file = authorized.canonical
        output_dir, safe_filename = os.path.split(file)
        debug_log("resolve_path_url", root_id=authorized.root_id)
        return file, safe_filename, output_dir
    else:
        filename, output_dir = folder_paths.annotated_filepath(filename)

        media_type = query.get("type", "output")
        if media_type == "path":
            #special case for path_based nodes
            #NOTE: output_dir may be empty, but non-None
            output_dir, filename = os.path.split(strip_path(filename))
        if output_dir is None:
            output_dir = folder_paths.get_directory_by_type(media_type)

        if output_dir is None:
            return error_response(404, f"Unknown media directory type: {media_type}")

        if "subfolder" in query:
            output_dir = os.path.join(output_dir, query["subfolder"])

        normalized_filename = filename.strip()
        filename = os.path.basename(normalized_filename)
        if media_type != "path" and filename != normalized_filename:
            return error_response(403, "Requested media path is not authorized.")
        file = os.path.join(output_dir, filename)

        try:
            # CRITICAL: authorize only after every user-controlled path component is joined.
            authorized = get_path_policy().authorize_path(
                file,
                PathCapability.PREVIEW_MEDIA,
            )
        except PathAccessDenied as exc:
            debug_log(
                "resolve_path_local_denied",
                capability=exc.capability.value,
                reason=exc.reason,
            )
            return error_response(403, "Requested media path is not authorized.")
        file = authorized.canonical
        output_dir = os.path.dirname(file)

        if not os.path.exists(file):
            return error_response(404, "Media file not found.")
        if query.get('format', 'video') == 'folder':
            if not os.path.isdir(file):
                return error_response(422, "Expected a directory for folder preview.")
        else:
            if not os.path.isfile(file) and not validate_sequence(file):
                    return error_response(422, "Media path is not a file or valid sequence.")
        debug_log("resolve_path_local", filename=filename, root_id=authorized.root_id, type=media_type)
        return file, filename, output_dir

@server.PromptServer.instance.routes.get("/vhs/getpath")
@server.PromptServer.instance.routes.get("/getpath")
async def get_path(request):
    query = request.rel_url.query
    if "path" not in query:
        return web.Response(status=204)
    #NOTE: path always ends in `/`, so this is functionally an lstrip
    requested_path = strip_path(query["path"])
    try:
        authorized_directory = get_path_policy().authorize_path(
            requested_path,
            PathCapability.LIST_DIRECTORY,
        )
    except PathAccessDenied as exc:
        debug_log(
            "get_path_denied",
            capability=exc.capability.value,
            reason=exc.reason,
        )
        return error_response(403, "Requested directory is not authorized.")
    path = authorized_directory.canonical

    if not os.path.isdir(path):
        return web.json_response([])

    #Use get so None is default instead of keyerror
    valid_extensions = query.get("extensions")
    if valid_extensions:
        valid_extensions = {
            ext.strip().lower().lstrip(".")
            for ext in valid_extensions.split(",")
            if ext.strip()
        }
    valid_items = []
    for item in os.scandir(path):
        try:
            item_authorized = get_path_policy().authorize_path(
                item.path,
                PathCapability.LIST_DIRECTORY,
            )
            item_authorized = get_path_policy().reauthorize_path(item_authorized)
            if os.path.isdir(item_authorized.canonical):
                valid_items.append((os.stat(item_authorized.canonical).st_mtime, item.name + "/"))
                continue
            if valid_extensions is None or item.name.split(".")[-1].lower() in valid_extensions:
                item_authorized = get_path_policy().authorize_path(
                    item.path,
                    PathCapability.READ_MEDIA,
                )
                item_authorized = get_path_policy().reauthorize_path(item_authorized)
                if os.path.isfile(item_authorized.canonical):
                    valid_items.append((os.stat(item_authorized.canonical).st_mtime, item.name))
        except (OSError, PathAccessDenied):
            #Broken symlinks can throw a very unhelpful "Invalid argument"
            pass
    valid_items.sort(key=lambda item: item[0])
    return web.json_response([item[1] for item in valid_items])
