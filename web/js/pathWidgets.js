export function splitPath(path) {
    const value = String(path ?? "")
    const index = Math.max(value.lastIndexOf("/"), value.lastIndexOf("\\"))
    if (index >= 0) {
        return [value.slice(0, index + 1), value.slice(index + 1)]
    }
    return ["", value]
}

function displayRoot(path) {
    if (/^[A-Za-z]:[\\/]/.test(path)) {
        return path.slice(0, 3) + "…" + path[2]
    }
    if (/^(?:\\\\|\/\/)/.test(path)) {
        const separator = path[0]
        return separator + separator + "…" + separator
    }
    if (path.startsWith("/")) {
        return "/…/"
    }
    const separator = path.includes("\\") ? "\\" : "/"
    return "…" + separator
}

export function fitPathForDisplay(ctx, path, maxLength) {
    const value = String(path ?? "")
    if (maxLength <= 0) return ["", 0]
    const fullLength = ctx.measureText(value).width
    if (fullLength < maxLength) return [value, fullLength]

    const estimatedCharacters = Math.max(
        1,
        Math.floor(maxLength / Math.max(fullLength, 1) * value.length) - 1,
    )
    const filename = splitPath(value)[1]
    const root = displayRoot(value)
    let displayPath
    if (root.length + filename.length >= estimatedCharacters) {
        const filenameBudget = Math.max(1, estimatedCharacters - root.length)
        const shortenedFilename = filename.length > filenameBudget
            ? filename.slice(0, Math.max(0, filenameBudget - 1)) + "…"
            : filename
        displayPath = root.slice(0, Math.max(0, estimatedCharacters - shortenedFilename.length)) + shortenedFilename
    } else {
        const remaining = Math.max(0, estimatedCharacters - root.length - filename.length)
        const directory = splitPath(value)[0]
        const tail = directory.slice(Math.max(0, directory.length - remaining))
        displayPath = root + tail + filename
        if (displayPath.length > estimatedCharacters) {
            displayPath = displayPath.slice(displayPath.length - estimatedCharacters)
        }
    }
    while (displayPath.length > 1 && ctx.measureText(displayPath).width > maxLength) {
        displayPath = displayPath.slice(0, -2) + "…"
    }
    if (ctx.measureText(displayPath).width > maxLength) return ["", 0]
    return [displayPath, ctx.measureText(displayPath).width]
}

export function createPathWidgets({
    app,
    api,
    fetchWithOptionalAuth,
    debugLog,
    LiteGraph,
}) {
const path_stem = splitPath
function searchBox(event, [x,y], node) {
    //Ensure only one dialogue shows at a time
    if (this.prompt)
        return;
    this.prompt = true;

    let pathWidget = this;
    let dialog = document.createElement("div");
    dialog.className = "litegraph litesearchbox graphdialog rounded"
    dialog.innerHTML = '<span class="name">Path</span> <input autofocus="" type="text" class="value"><button class="rounded">OK</button><div class="helper"></div>'
    dialog.close = () => {
        dialog.remove();
    }
    document.body.append(dialog);
    if (app.canvas.ds.scale > 1) {
        dialog.style.transform = "scale(" + app.canvas.ds.scale + ")";
    }
    var name_element = dialog.querySelector(".name");
    var input = dialog.querySelector(".value");
    var options_element = dialog.querySelector(".helper");
    input.value = pathWidget.value;

    var timeout = null;
    let last_path = null;
    let extensions = pathWidget.options.vhs_path_extensions

    input.addEventListener("keydown", (e) => {
        dialog.is_modified = true;
        if (e.keyCode == 27) {
            //ESC
            dialog.close();
        } else if (e.keyCode == 13 && e.target.localName != "textarea") {
            pathWidget.value = input.value;
            if (pathWidget.callback) {
                pathWidget.callback(pathWidget.value);
            }
            dialog.close();
        } else {
            if (e.keyCode == 9) {
                //TAB
                input.value = last_path + options_element.firstChild.innerText;
                e.preventDefault();
                e.stopPropagation();
            } else if (e.ctrlKey && (e.keyCode == 87 || e.keyCode == 66)) {
                //Ctrl+w or Ctrl+b
                //most browsers won't support, but it's good QOL for those that do
                input.value = path_stem(input.value.slice(0,-1))[0]
                e.preventDefault();
                e.stopPropagation();
            } else if (e.ctrlKey && e.keyCode == 71) {
                //Ctrl+g
                //Temporarily disables extension filtering to show all files
                e.preventDefault();
                e.stopPropagation();
                extensions = undefined
                last_path = null;
            }
            if (timeout) {
                clearTimeout(timeout);
            }
            timeout = setTimeout(updateOptions, 10);
            return;
        }
        this.prompt=false;
        e.preventDefault();
        e.stopPropagation();
    });

    var button = dialog.querySelector("button");
    button.addEventListener("click", (e) => {
        pathWidget.value = input.value;
        if (pathWidget.callback) {
            pathWidget.callback(pathWidget.value);
        }
        //unsure why dirty is set here, but not on enter-key above
        node.graph.setDirtyCanvas(true);
        dialog.close();
        this.prompt = false;
    });
    var rect = app.canvas.canvas.getBoundingClientRect();
    var offsetx = -20;
    var offsety = -20;
    if (rect) {
        offsetx -= rect.left;
        offsety -= rect.top;
    }

    if (event) {
        dialog.style.left = event.clientX + offsetx + "px";
        dialog.style.top = event.clientY + offsety + "px";
    } else {
        dialog.style.left = canvas.width * 0.5 + offsetx + "px";
        dialog.style.top = canvas.height * 0.5 + offsety + "px";
    }
    //Search code
    let options = []
    function addResult(name, isDir) {
        let el = document.createElement("div");
        el.innerText = name;
        el.className = "litegraph lite-search-item";
        if (isDir) {
            el.className += " is-dir";
            el.addEventListener("click", (e) => {
                input.value = last_path+name
                if (timeout) {
                    clearTimeout(timeout);
                }
            timeout = setTimeout(updateOptions, 10);
            });
        } else {
            el.addEventListener("click", (e) => {
                pathWidget.value = last_path+name;
                if (pathWidget.callback) {
                    pathWidget.callback(pathWidget.value);
                }
                dialog.close();
                pathWidget.prompt = false;
            });
        }
        options_element.appendChild(el);
    }
    async function updateOptions() {
        timeout = null;
        let [path, remainder] = path_stem(input.value);
        if (last_path != path) {
            //fetch options.  Must block execution here, so update should be async?
            let params = {path : path}
            if (extensions) {
                params.extensions = extensions
            }
            let optionsURL = api.apiURL('/vhs/getpath?' + new URLSearchParams(params));
            try {
                let resp = await fetchWithOptionalAuth(optionsURL);
                options = await resp.json();
                options = options.map((o) => o.replace('.','\0'))
                options = options.sort()
                options = options.map((o) => o.replace('\0','.'))
            } catch(e) {
                options = []
                debugLog("path_lookup_failed", { url: optionsURL, error: String(e) })
            }
            last_path = path;
        }
        options_element.innerHTML = '';
        //filter options based on remainder
        for (let option of options) {
            if (option.startsWith(remainder)) {
                let isDir = option.endsWith('/')
                addResult(option, isDir);
            }
        }
    }

    setTimeout(async function() {
        input.focus();
        await updateOptions();
    }, 10);

    return dialog;
}
function button_action(widget) {
  if (
    widget.options?.reset == undefined &&
    widget.options?.disable == undefined
  ) {
    return 'None'
  }
  if (
    widget.options.reset != undefined &&
    widget.value != widget.options.reset
  ) {
    return 'Reset'
  }
  if (
    widget.options.disable != undefined &&
    widget.value != widget.options.disable
  ) {
    return 'Disable'
  }
  if (widget.options.reset != undefined) {
    return 'No Reset'
  }
  return 'No Disable'
}
function fitText(ctx, text, maxLength) {
    if (maxLength <= 0) {
        return ['', 0]
    }
    let fullLength = ctx.measureText(text).width
    if (fullLength < maxLength) {
        return [text, fullLength]
    }
    //determine approx safe cutoff
    let cutoff = maxLength / fullLength * text.length | 0
    let shortened = text.slice(0, Math.max(0, cutoff - 2)) + '…'
    return [shortened, ctx.measureText(shortened).width]
}
function fitPath(ctx, path, maxLength) {
    return fitPathForDisplay(ctx, path, maxLength)
}
function roundToPrecision(num, precision) {
    let strnum = Number(num).toFixed(precision)
    let deci = strnum.indexOf('.')
    if (deci > 0) {
        let i = strnum.length - 1
        while (i > deci && strnum[i] == '0') {
            i--
        }
        if (i == deci) {
            i--
        }
        return strnum.slice(0, i+1)
    }
    return strnum
}
function inner_value_change(widget, value, node, pos) {
  widget.value = value
  if (widget.options?.property && widget.options.property in node.properties) {
    node.setProperty(widget.options.property, value)
  }
  if (widget.callback) {
    widget.callback(widget.value, app.canvas, node, event)
  }
}
function drawAnnotated(ctx, node, widget_width, y, H) {
  const litegraph_base = LiteGraph
  // In vueNodes mode, always show text since Vue renders at 1:1 scale
  const show_text = LiteGraph.vueNodesMode || app.canvas.ds.scale >= (app.canvas.low_quality_zoom_threshold ?? 0.5)
  const margin = 15
  ctx.strokeStyle = litegraph_base.WIDGET_OUTLINE_COLOR
  ctx.fillStyle = litegraph_base.WIDGET_BGCOLOR
  ctx.beginPath()
  if (show_text)
    ctx.roundRect(margin, y, widget_width - margin * 2, H, [H * 0.5])
  else ctx.rect(margin, y, widget_width - margin * 2, H)
  ctx.fill()
  if (show_text) {
    if (!this.disabled) ctx.stroke()
    const button = button_action(this)
    if (button != 'None') {
      ctx.save()
      if (button.startsWith('No ')) {
        ctx.fillStyle = litegraph_base.WIDGET_OUTLINE_COLOR
        ctx.strokeStyle = litegraph_base.WIDGET_OUTLINE_COLOR
      } else {
        ctx.fillStyle = litegraph_base.WIDGET_TEXT_COLOR
        ctx.strokeStyle = litegraph_base.WIDGET_TEXT_COLOR
      }
      ctx.beginPath()
      if (button.endsWith('Reset')) {
        ctx.arc(widget_width - margin - 26, y + H/2, 4, Math.PI*3/2, Math.PI)
        ctx.stroke()
        ctx.beginPath()
        ctx.moveTo(widget_width - margin - 26, y + H/2 - 1.5)
        ctx.lineTo(widget_width - margin - 26, y + H/2 - 6.5)
        ctx.lineTo(widget_width - margin - 30, y + H/2 - 3.5)
        ctx.fill()
      } else {
        ctx.arc(widget_width - margin - 26, y + H/2, 4, Math.PI*2/3, Math.PI*8/3)
        ctx.moveTo(widget_width - margin - 26 - 8 ** .5, y + H/2 + 8 ** .5)
        ctx.lineTo(widget_width - margin - 26 + 8 ** .5, y + H/2 - 8 ** .5)
        ctx.stroke()
      }
      ctx.restore()
    }
    ctx.fillStyle = litegraph_base.WIDGET_TEXT_COLOR
    if (!this.disabled) {
      ctx.beginPath()
      ctx.moveTo(margin + 16, y + 5)
      ctx.lineTo(margin + 6, y + H * 0.5)
      ctx.lineTo(margin + 16, y + H - 5)
      ctx.fill()
      ctx.beginPath()
      ctx.moveTo(widget_width - margin - 16, y + 5)
      ctx.lineTo(widget_width - margin - 6, y + H * 0.5)
      ctx.lineTo(widget_width - margin - 16, y + H - 5)
      ctx.fill()
    }
    let freeWidth = widget_width - (40 + margin * 2 + 20)
    let [valueText, valueWidth] = fitText(ctx, (this.displayValue?.() ?? ""), freeWidth)
    freeWidth -= valueWidth

    ctx.textAlign = 'left'
    ctx.fillStyle = litegraph_base.WIDGET_SECONDARY_TEXT_COLOR
    if (freeWidth > 20) {
      let [name, nameWidth] = fitText(ctx, this.label || this.name, freeWidth)
      freeWidth -= nameWidth
      ctx.fillText(name, margin * 2 + 5, y + H * 0.7)
    }

    let value_offset = margin * 2 + 20
    ctx.textAlign = 'right'
    if (this.options.unit) {
      ctx.fillStyle = litegraph_base.WIDGET_OUTLINE_COLOR
      let [unitText, unitWidth] = fitText(ctx, this.options.unit, freeWidth)
      if (unitText == this.options.unit) {
        ctx.fillText(this.options.unit, widget_width - value_offset, y + H * 0.7)
        value_offset += unitWidth
        freeWidth -= unitWidth
      }
    }
    ctx.fillStyle = litegraph_base.WIDGET_TEXT_COLOR
    ctx.fillText(valueText, widget_width - value_offset, y + H * 0.7)
    ctx.fillStyle = litegraph_base.WIDGET_SECONDARY_TEXT_COLOR


    let annotation = ''
    if (this.annotation) {
      annotation = this.annotation(this.value, freeWidth)
    } else if (
      this.options.annotation &&
      this.value in this.options.annotation
    ) {
      annotation = this.options.annotation[this.value]
    }
    if (annotation) {
      ctx.fillStyle = litegraph_base.WIDGET_OUTLINE_COLOR
      let [annoDisplay, annoWidth] = fitText(ctx, annotation, freeWidth)
      ctx.fillText(
        annoDisplay,
        widget_width - 5 - valueWidth - value_offset,
        y + H * 0.7
      )
    }
  }
}
function mouseAnnotated(event, [x, y], node) {
    //NOTE: Mouse actions contain no history element.
    //This can cause overlapping actions since each triggers on different event type (down/move/up)
    //TODO: Consider further rework
    const widget_width = this.width || node.size[0]
    const old_value = this.value
    const margin = 15
    let isButton = 0
    if (x > margin + 6 && x < margin + 16) {
        isButton = -1
    } else if (x > widget_width - margin - 16 && x < widget_width - margin - 6) {
        isButton = 1
    } else if (x > widget_width - margin - 34 && x < widget_width - margin - 18) {
        isButton = 2
    }
    var allow_scroll = true
    if (allow_scroll && event.type == 'pointermove') {
        if (event.deltaX)
            this.value += event.deltaX * (this.options.step || 1)
        if (this.options.min != null && this.value < this.options.min) {
            this.value = this.options.min
        }
        if (this.options.max != null && this.value > this.options.max) {
            this.value = this.options.max
        }
    } else if (event.type == 'pointerdown') {
        const buttonType = button_action(this)
        if (isButton == 2) {
            if (buttonType == 'Reset') {
                this.value = this.options.reset
            } else if (buttonType == 'Disable') {
                this.value = this.options.disable
            }
        } else {
            this.value += isButton * (this.options.step || 1)
            if (this.options.min != null && this.value < this.options.min) {
                this.value = this.options.min
            }
            if (this.options.max != null && this.value > this.options.max) {
                this.value = this.options.max
            }
        }
    } //end mousedown
    else if (event.type == 'pointerup') {
        if (event.click_time < 200 && !isButton) {
            const d_callback = (v) => {
                this.value = this.parseValue?.(v) ?? Number(v)
                inner_value_change(this, this.value, node, [x, y])
            }
            const dialog = app.canvas.prompt(
                'Value',
                this.value,//TODO: Consider making this displayValue?
                d_callback,
                event
            )
            const input = dialog.querySelector(".value")
            input.addEventListener("keydown", (e) => {
                if (e.keyCode == 9) {
                    e.preventDefault();
                    e.stopPropagation();
                    d_callback(input.value)
                    dialog.close()
                    node?.graph?.setDirtyCanvas(true);
                    let i = node.widgets.findIndex((w) => w == this)
                    if (e.shiftKey)
                        i--
                    else
                        i++
                    if (node.widgets[i]?.type == "VHS.ANNOTATED") {//restrict to annotatedNUmbers
                        node.widgets[i]?.mouse(event, [x, y+24], node)
                    }
                }
            })
        }
    }

    if (old_value != this.value)
        setTimeout(
            function () {
                inner_value_change(this, this.value, node, [x, y])
            }.bind(this),
            20
        )
    return true
}

    return {
        path_stem,
        searchBox,
        fitText,
        fitPath,
        roundToPrecision,
        drawAnnotated,
        mouseAnnotated,
    }
}
