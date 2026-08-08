"""Video format discovery, widget schemas, and argument materialization."""

import json
import os
from copy import deepcopy
from string import Template

import folder_paths

from .logger import logger
from .utils import cached, gifski_path


if "VHS_video_formats" not in folder_paths.folder_names_and_paths:
    folder_paths.folder_names_and_paths["VHS_video_formats"] = ((), {".json"})
if len(folder_paths.folder_names_and_paths["VHS_video_formats"][1]) == 0:
    folder_paths.folder_names_and_paths["VHS_video_formats"][1].add(".json")


def flatten_list(l):
    ret = []
    for e in l:
        if isinstance(e, list):
            ret.extend(e)
        else:
            ret.append(e)
    return ret

def iterate_format(video_format, for_widgets=True):
    """Provides an iterator over widgets, or arguments"""
    def indirector(cont, index):
        if isinstance(cont[index], list) and (not for_widgets
          or len(cont[index])> 1 and not isinstance(cont[index][1], dict)):
            inp = yield cont[index]
            if inp is not None:
                cont[index] = inp
                yield
    for k in video_format:
        if k == "extra_widgets":
            if for_widgets:
                yield from video_format["extra_widgets"]
        elif k.endswith("_pass"):
            for i in range(len(video_format[k])):
                yield from indirector(video_format[k], i)
            if not for_widgets:
                video_format[k] = flatten_list(video_format[k])
        else:
            yield from indirector(video_format, k)

base_formats_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "video_formats")
@cached(5)
def get_video_formats():
    format_files = {}
    for format_name in sorted(folder_paths.get_filename_list("VHS_video_formats")):
        format_files[format_name] = folder_paths.get_full_path("VHS_video_formats", format_name)
    for item in sorted(os.scandir(base_formats_dir), key=lambda entry: entry.name):
        if not item.is_file() or not item.name.endswith('.json'):
            continue
        format_files[item.name[:-5]] = item.path
    formats = []
    format_widgets = {}
    for format_name, path in format_files.items():
        with open(path, 'r') as stream:
            video_format = json.load(stream)
        if "gifski_pass" in video_format and gifski_path is None:
            #Skip format
            continue
        widgets = list(iterate_format(video_format))
        formats.append("video/" + format_name)
        if (len(widgets) > 0):
            format_widgets["video/"+ format_name] = widgets
    return formats, format_widgets

def _format_widget_kind(widget_definition):
    widget_type = widget_definition[1]
    return "COMBO" if isinstance(widget_type, list) else widget_type

def _merge_format_widget_options(definitions):
    option_sets = [
        definition[2] if len(definition) > 2 and isinstance(definition[2], dict) else {}
        for definition in definitions
    ]
    merged = deepcopy(option_sets[0]) if option_sets else {}

    for bound, reducer in (("min", min), ("max", max)):
        values = [options[bound] for options in option_sets if bound in options]
        if len(values) == len(option_sets) and values:
            merged[bound] = reducer(values)
        else:
            merged.pop(bound, None)

    steps = [options.get("step") for options in option_sets]
    if steps and all(step == steps[0] for step in steps) and steps[0] is not None:
        merged["step"] = steps[0]
    elif any(step is not None for step in steps):
        numeric_steps = [step for step in steps if isinstance(step, (int, float))]
        if numeric_steps:
            merged["step"] = min(numeric_steps)
        else:
            merged.pop("step", None)

    # Only retain non-constraint options that mean the same thing for every format.
    for key in list(merged):
        if key in {"default", "min", "max", "step"}:
            continue
        if not all(key in options and options[key] == merged[key] for options in option_sets):
            merged.pop(key, None)
    return merged

def build_format_widget_inputs(format_widgets):
    """Build stable public input specs for compatible format widget names."""
    definitions_by_name = {}
    for definitions in format_widgets.values():
        for definition in definitions:
            if not isinstance(definition, list) or len(definition) < 2:
                continue
            definitions_by_name.setdefault(definition[0], []).append(definition)

    inputs = {}
    incompatible = {}
    for name, definitions in definitions_by_name.items():
        kinds = list(dict.fromkeys(_format_widget_kind(definition) for definition in definitions))
        if len(kinds) != 1:
            incompatible[name] = kinds
            continue

        kind = kinds[0]
        options = _merge_format_widget_options(definitions)
        if kind == "COMBO":
            choices = []
            for definition in definitions:
                for choice in definition[1]:
                    if choice not in choices:
                        choices.append(choice)
            if not choices:
                incompatible[name] = ["EMPTY_COMBO"]
                continue
            if options.get("default") not in choices:
                options["default"] = choices[0]
            inputs[name] = (choices, options)
        else:
            inputs[name] = (kind, options)

    return inputs, incompatible

def apply_format_widgets(format_name, kwargs):
    if os.path.exists(os.path.join(base_formats_dir, format_name + ".json")):
        video_format_path = os.path.join(base_formats_dir, format_name + ".json")
    else:
        video_format_path = folder_paths.get_full_path("VHS_video_formats", format_name)
    with open(video_format_path, 'r') as stream:
        video_format = json.load(stream)
    for w in iterate_format(video_format):
        if w[0] not in kwargs:
            if len(w) > 2 and 'default' in w[2]:
                default = w[2]['default']
            else:
                if type(w[1]) is list:
                    default = w[1][0]
                else:
                    #NOTE: This doesn't respect max/min, but should be good enough as a fallback to a fallback to a fallback
                    default = {"BOOLEAN": False, "INT": 0, "FLOAT": 0, "STRING": ""}[w[1]]
            kwargs[w[0]] = default
            logger.warn(f"Missing input for {w[0]} has been set to {default}")
    wit = iterate_format(video_format, False)
    for w in wit:
        while isinstance(w, list):
            if len(w) == 1:
                #TODO: mapping=kwargs should be safer, but results in key errors, investigate why
                w = [Template(x).substitute(**kwargs) for x in w[0]]
                break
            elif isinstance(w[1], dict):
                w = w[1][str(kwargs[w[0]])]
            elif len(w) > 3:
                w = Template(w[3]).substitute(val=kwargs[w[0]])
            else:
                w = str(kwargs[w[0]])
        wit.send(w)
    return video_format
