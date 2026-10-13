"""Video format discovery, widget schemas, and argument materialization."""

import json
import math
import numbers
import os
import re
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
def _discover_video_formats():
    format_files = {}
    for format_name in sorted(folder_paths.get_filename_list("VHS_video_formats")):
        format_files[format_name] = folder_paths.get_full_path("VHS_video_formats", format_name)
    for item in sorted(os.scandir(base_formats_dir), key=lambda entry: entry.name):
        if not item.is_file() or not item.name.endswith('.json'):
            continue
        format_files[item.name[:-5]] = item.path
    formats = []
    format_widgets = {}
    format_paths = {}
    for format_name, path in format_files.items():
        if path is None:
            continue
        with open(path, 'r') as stream:
            video_format = json.load(stream)
        if "gifski_pass" in video_format and gifski_path is None:
            #Skip format
            continue
        widgets = list(iterate_format(video_format))
        formats.append("video/" + format_name)
        format_paths[format_name] = path
        if (len(widgets) > 0):
            format_widgets["video/"+ format_name] = widgets
    return formats, format_widgets, format_paths

def get_video_formats():
    formats, format_widgets, _format_paths = _discover_video_formats()
    return formats, format_widgets

def get_video_format_paths():
    """Map each selectable format name to the definition file discovered for it."""
    return dict(_discover_video_formats()[2])

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

_FILTER_FLAGS = frozenset({"-vf", "-af", "-lavfi", "-filter_complex"})
# Standalone STRING widgets become exactly one argv token: no separators, quoting, or option prefix.
_STRING_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}")


class FormatWidgetError(ValueError):
    """Raised when a format widget value is not valid for the selected format."""


def _is_filter_flag(value):
    return isinstance(value, str) and (value in _FILTER_FLAGS or value.startswith("-filter"))

def _element_kind(node):
    """Classify one format argument element the way materialization interprets it."""
    if not isinstance(node, list) or not node:
        return None
    if len(node) == 1:
        return "template" if isinstance(node[0], list) else None
    if not isinstance(node[0], str):
        return None
    return "mapping" if isinstance(node[1], dict) else "widget"

def _template_names(text):
    names = set()
    for match in Template.pattern.finditer(text):
        name = match.group("named") or match.group("braced")
        if name is not None:
            names.add(name)
    return names

def _element_names(node):
    """Collect every widget name one argument element can consume while materializing."""
    kind = _element_kind(node)
    names = set()
    if kind == "template":
        for token in node[0]:
            if isinstance(token, str):
                names |= _template_names(token)
    elif kind == "mapping":
        names.add(node[0])
        for option in node[1].values():
            names |= _element_names(option)
    elif kind == "widget":
        names.add(node[0])
    return names

def _element_emits_filter_flag(node):
    if _is_filter_flag(node):
        return True
    kind = _element_kind(node)
    if kind == "template":
        return any(_is_filter_flag(token) for token in node[0])
    if kind == "mapping":
        return any(_element_emits_filter_flag(option) for option in node[1].values())
    if kind == "widget" and isinstance(node[1], list):
        return any(_is_filter_flag(choice) for choice in node[1])
    return False

def _template_filter_names(node):
    """Names consumed right after a filter flag inside one template or mapping option."""
    kind = _element_kind(node)
    names = set()
    if kind == "template":
        tokens = node[0]
        for previous, token in zip(tokens, tokens[1:]):
            if _is_filter_flag(previous) and isinstance(token, str):
                names |= _template_names(token)
    elif kind == "mapping":
        for option in node[1].values():
            names |= _template_filter_names(option)
    return names

def _filter_bound_names(video_format):
    """Names whose values can land inside an FFmpeg filtergraph argument."""
    bound = set()
    for key, arguments in video_format.items():
        if not key.endswith("_pass") or not isinstance(arguments, list):
            continue
        for index, argument in enumerate(arguments):
            if index > 0 and _element_emits_filter_flag(arguments[index - 1]):
                bound |= _element_names(argument)
            bound |= _template_filter_names(argument)
    return bound

def _element_definitions(node):
    kind = _element_kind(node)
    if kind == "widget":
        yield node
    elif kind == "mapping":
        for option in node[1].values():
            yield from _element_definitions(option)

def _format_widget_definitions(video_format):
    """Yield every widget definition, including ones nested inside value mappings."""
    for key, value in video_format.items():
        if key == "extra_widgets":
            for definition in value if isinstance(value, list) else ():
                if _element_kind(definition) == "widget":
                    yield definition
        elif key.endswith("_pass"):
            for argument in value if isinstance(value, list) else ():
                yield from _element_definitions(argument)
        else:
            yield from _element_definitions(value)

def _widget_default(definition):
    if len(definition) > 2 and isinstance(definition[2], dict) and 'default' in definition[2]:
        return definition[2]['default']
    if isinstance(definition[1], list):
        return definition[1][0] if definition[1] else None
    #NOTE: This doesn't respect max/min, but should be good enough as a fallback to a fallback to a fallback
    return {"BOOLEAN": False, "INT": 0, "FLOAT": 0, "STRING": ""}.get(definition[1])

def _check_bounds(name, value, options):
    if "min" in options and value < options["min"]:
        raise FormatWidgetError(f"Format widget {name!r} is below its minimum.")
    if "max" in options and value > options["max"]:
        raise FormatWidgetError(f"Format widget {name!r} is above its maximum.")

def _validate_widget_value(definition, value, filter_bound):
    name, kind = definition[0], definition[1]
    options = definition[2] if len(definition) > 2 and isinstance(definition[2], dict) else {}
    if isinstance(kind, list):
        for choice in kind:
            if type(choice) is type(value) and choice == value:
                return choice
        raise FormatWidgetError(f"Format widget {name!r} has a value outside its choices.")
    if kind == "BOOLEAN":
        if isinstance(value, bool):
            return value
        raise FormatWidgetError(f"Format widget {name!r} must be a boolean.")
    if kind == "INT":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, numbers.Integral) and not isinstance(value, bool):
            value = int(value)
            _check_bounds(name, value, options)
            return value
        raise FormatWidgetError(f"Format widget {name!r} must be an integer.")
    if kind == "FLOAT":
        if isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value):
            # Keep the caller's numeric type so materialized argv text is unchanged (1 stays "1").
            _check_bounds(name, value, options)
            return value
        raise FormatWidgetError(f"Format widget {name!r} must be a finite number.")
    if kind == "STRING":
        # SECURITY: free text must never reach a filtergraph; elsewhere it is one conservative token.
        if filter_bound:
            raise FormatWidgetError(
                f"Format widget {name!r} is free text and cannot be used in an FFmpeg filter argument."
            )
        if isinstance(value, str) and _STRING_TOKEN.fullmatch(value):
            return value
        raise FormatWidgetError(
            f"Format widget {name!r} must be a single token of letters, digits, '_', '.', '+', or '-'."
        )
    raise FormatWidgetError(f"Format widget {name!r} has an unsupported type.")

def _validated_widget_values(video_format, kwargs):
    filter_bound = _filter_bound_names(video_format)
    top_level = {definition[0] for definition in iterate_format(video_format)}
    values = {}
    for definition in _format_widget_definitions(video_format):
        name = definition[0]
        if name in kwargs:
            value = kwargs[name]
        else:
            value = _widget_default(definition)
            if name in top_level:
                logger.warn(f"Missing input for {name} has been set to {value}")
        values[name] = _validate_widget_value(definition, value, name in filter_bound)
    return values

def _widget_value(name, values):
    try:
        return values[name]
    except (KeyError, TypeError):
        raise FormatWidgetError(f"Format option {name!r} does not name a widget of this format.") from None

def _mapped_option(name, mapping, values):
    key = str(_widget_value(name, values))
    if key not in mapping:
        raise FormatWidgetError(f"Format widget {name!r} has no mapping for its value.")
    return mapping[key]

def apply_format_widgets(format_name, kwargs):
    # SECURITY: the prompt selects a discovered format by name; it never becomes a path fragment.
    video_format_path = get_video_format_paths().get(format_name) if isinstance(format_name, str) else None
    if video_format_path is None:
        raise FormatWidgetError(f"Unknown video format: {str(format_name)[:120]!r}")
    with open(video_format_path, 'r') as stream:
        video_format = json.load(stream)
    # IMPORTANT: materialize only validated values of this format's own widgets; raw prompt
    # kwargs (including widget-only names forwarded for other formats) must not be substituted.
    values = _validated_widget_values(video_format, kwargs)
    values["has_alpha"] = bool(kwargs.get("has_alpha", False))
    wit = iterate_format(video_format, False)
    for w in wit:
        while isinstance(w, list):
            if len(w) == 1:
                try:
                    w = [Template(x).substitute(values) for x in w[0]]
                except KeyError as missing:
                    raise FormatWidgetError(
                        f"Format template references unknown widget {missing.args[0]!r}."
                    ) from None
                break
            elif isinstance(w[1], dict):
                w = _mapped_option(w[0], w[1], values)
            elif len(w) > 3:
                w = Template(w[3]).substitute(val=_widget_value(w[0], values))
            else:
                w = str(_widget_value(w[0], values))
        wit.send(w)
    return video_format
