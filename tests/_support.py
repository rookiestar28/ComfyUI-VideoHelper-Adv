import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import uuid
from pathlib import Path


class DummyResponse:
    def __init__(self, status=200, text="", *, data=None, path=None):
        self.status = status
        self.text = text
        self.data = data
        self.path = path
        self.headers = {}
        self.content_type = None
        self.body = b""


class DummyStreamResponse(DummyResponse):
    async def prepare(self, request):
        return self

    async def write(self, data):
        self.body += data


class DummyRoutes:
    def get(self, _path):
        return lambda fn: fn

    def post(self, _path):
        return lambda fn: fn


class DummyPromptServerInstance:
    def __init__(self):
        self.routes = DummyRoutes()
        self.prompt_queue = types.SimpleNamespace(currently_running={}, put=lambda *_args, **_kwargs: None)
        self.number = 0
        self.client_id = "test-client"
        self.sockets_metadata = {}


def purge_modules(*names):
    for name in names:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def _annotated_filepath(raw):
    if raw.endswith("]") and "[" in raw:
        filename, bracket = raw.rsplit("[", 1)
        return filename.strip(), bracket[:-1]
    return raw, None


def install_base_stubs(temp_root):
    temp_root = Path(temp_root)
    input_dir = temp_root / "input"
    output_dir = temp_root / "output"
    temp_dir = temp_root / "temp"
    for directory in (input_dir, output_dir, temp_dir):
        directory.mkdir(parents=True, exist_ok=True)

    web = types.SimpleNamespace(
        Response=DummyResponse,
        FileResponse=lambda path: DummyResponse(path=path),
        StreamResponse=DummyStreamResponse,
        json_response=lambda data, status=200: DummyResponse(
            status=status, text=json.dumps(data), data=data
        ),
    )

    prompt_server_instance = DummyPromptServerInstance()
    server_module = types.ModuleType("server")
    server_module.web = web
    server_module.uuid = uuid
    server_module.PromptServer = types.SimpleNamespace(instance=prompt_server_instance)
    sys.modules["server"] = server_module

    folder_paths_module = types.ModuleType("folder_paths")
    folder_paths_module.folder_names_and_paths = {}
    folder_paths_module.get_input_directory = lambda: str(input_dir)
    folder_paths_module.get_output_directory = lambda: str(output_dir)
    folder_paths_module.get_temp_directory = lambda: str(temp_dir)
    folder_paths_module.get_directory_by_type = lambda typ: {
        "input": str(input_dir),
        "output": str(output_dir),
        "temp": str(temp_dir),
        "path": str(output_dir),
    }.get(typ)
    folder_paths_module.annotated_filepath = _annotated_filepath
    folder_paths_module.get_annotated_filepath = lambda path: str(input_dir / path)
    folder_paths_module.exists_annotated_filepath = lambda path: (input_dir / path).exists()
    def get_save_image_path(prefix, output_dir, image_width=0, image_height=0):
        resolved_prefix = (
            prefix
            .replace("%width%", str(image_width))
            .replace("%height%", str(image_height))
        )
        return (str(output_dir), resolved_prefix, 0, "", resolved_prefix)

    folder_paths_module.get_save_image_path = get_save_image_path
    folder_paths_module.get_filename_list = lambda _name: []
    folder_paths_module.get_full_path = lambda _name, filename: str(temp_root / filename)
    sys.modules["folder_paths"] = folder_paths_module

    logger_module = types.ModuleType("videohelpersuite.logger")
    logger_module.logger = types.SimpleNamespace(
        info=lambda *_a, **_k: None,
        warn=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )
    sys.modules["videohelpersuite.logger"] = logger_module

    comfy_module = types.ModuleType("comfy")
    comfy_utils_module = types.ModuleType("comfy.utils")
    comfy_utils_module.common_upscale = lambda tensor, *_a, **_k: tensor

    class ProgressBar:
        def __init__(self, total):
            self.total = total

        def update(self, _value):
            return None

        def update_absolute(self, _value, _total):
            return None

    comfy_utils_module.ProgressBar = ProgressBar
    sys.modules["comfy"] = comfy_module
    sys.modules["comfy.utils"] = comfy_utils_module

    comfy_kdiff_module = types.ModuleType("comfy.k_diffusion")
    comfy_kdiff_utils_module = types.ModuleType("comfy.k_diffusion.utils")

    class FolderOfImages:
        IMG_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".bmp"]

    comfy_kdiff_utils_module.FolderOfImages = FolderOfImages
    sys.modules["comfy.k_diffusion"] = comfy_kdiff_module
    sys.modules["comfy.k_diffusion.utils"] = comfy_kdiff_utils_module

    torch_module = types.ModuleType("torch")

    class DummyTensor:
        pass

    torch_module.Tensor = DummyTensor
    torch_module.float32 = "float32"
    torch_module.uint8 = "uint8"
    torch_module.from_numpy = lambda array: array
    torch_module.cat = lambda values: sum(values[1:], values[0]) if values else []
    torch_module.zeros = lambda shape, dtype=None, device=None: []
    torch_module.nn = types.SimpleNamespace(
        ReplicationPad2d=lambda _padding: (lambda tensor: tensor)
    )
    sys.modules["torch"] = torch_module

    return {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "temp_dir": temp_dir,
    }


def install_nodes_dependency_stubs():
    sys.modules["nodes"] = types.SimpleNamespace(VHSLoadFormats={})

    image_latent = types.ModuleType("videohelpersuite.image_latent_nodes")
    latent_names = [
        "SplitLatents",
        "SplitImages",
        "SplitMasks",
        "MergeLatents",
        "MergeImages",
        "MergeMasks",
        "GetLatentCount",
        "GetImageCount",
        "GetMaskCount",
        "RepeatLatents",
        "RepeatImages",
        "RepeatMasks",
        "SelectEveryNthLatent",
        "SelectEveryNthImage",
        "SelectEveryNthMask",
        "SelectLatents",
        "SelectImages",
        "SelectMasks",
    ]
    for name in latent_names:
        setattr(image_latent, name, type(name, (), {}))
    sys.modules["videohelpersuite.image_latent_nodes"] = image_latent

    load_video_nodes = types.ModuleType("videohelpersuite.load_video_nodes")
    for name in [
        "LoadVideoUpload",
        "LoadVideoPath",
        "LoadVideoFFmpegUpload",
        "LoadVideoFFmpegPath",
        "LoadImagePath",
    ]:
        setattr(load_video_nodes, name, type(name, (), {}))
    sys.modules["videohelpersuite.load_video_nodes"] = load_video_nodes

    load_images_nodes = types.ModuleType("videohelpersuite.load_images_nodes")
    for name in [
        "LoadImagesFromDirectoryUpload",
        "LoadImagesFromDirectoryPath",
    ]:
        setattr(load_images_nodes, name, type(name, (), {}))
    sys.modules["videohelpersuite.load_images_nodes"] = load_images_nodes

    batched_nodes = types.ModuleType("videohelpersuite.batched_nodes")
    batched_nodes.VAEEncodeBatched = type("VAEEncodeBatched", (), {})
    batched_nodes.VAEDecodeBatched = type("VAEDecodeBatched", (), {})
    sys.modules["videohelpersuite.batched_nodes"] = batched_nodes


def install_load_video_dependency_stubs():
    sys.modules["nodes"] = types.SimpleNamespace(VHSLoadFormats={})

    pil_module = types.ModuleType("PIL")
    pil_module.Image = types.SimpleNamespace(open=lambda *_a, **_k: None)
    pil_module.ImageOps = types.SimpleNamespace(exif_transpose=lambda image: image)
    sys.modules["PIL"] = pil_module

    cv2_module = types.ModuleType("cv2")
    cv2_module.CAP_PROP_FPS = 5
    cv2_module.CAP_PROP_FRAME_WIDTH = 3
    cv2_module.CAP_PROP_FRAME_HEIGHT = 4
    cv2_module.CAP_PROP_FRAME_COUNT = 7
    cv2_module.COLOR_BGR2RGB = 1
    cv2_module.VideoCapture = lambda *_a, **_k: None
    cv2_module.cvtColor = lambda frame, _code: frame
    sys.modules["cv2"] = cv2_module

    if "psutil" not in sys.modules:
        psutil_module = types.ModuleType("psutil")
        psutil_module.virtual_memory = lambda: types.SimpleNamespace(available=0)
        psutil_module.swap_memory = lambda: types.SimpleNamespace(free=0)
        sys.modules["psutil"] = psutil_module


def import_fresh(module_name):
    purge_modules(module_name)
    return importlib.import_module(module_name)


class TempWorkspace:
    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="vhs-tests-"))

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)
