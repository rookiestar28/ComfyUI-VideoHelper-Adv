import { app } from '../../../scripts/app.js'
import { api } from '../../../scripts/api.js'
import { shouldUseAdvancedPreview } from "./previewRouting.js";
import { reconcileFormatWidgets } from "./formatWidgetState.js";
import { applyTextReplacements } from "./textReplacements.js";
import { createUploadTransport } from "./uploadTransport.js";
import { createMediaPreview } from "./mediaPreview.js";
import { createPathWidgets } from "./pathWidgets.js";
import { createLatentPreview } from "./latentPreview.js";
import { createPasteHandler } from "./pasteHandler.js";
import { configureSelectLatestNode } from "./selectLatest.js";

function chainCallback(object, property, callback) {
    if (object == undefined) {
        //This should not happen.
        console.error("Tried to add callback to non-existant object")
        return;
    }
    if (property in object && object[property]) {
        const callback_orig = object[property]
        object[property] = function () {
            const r = callback_orig.apply(this, arguments);
            return callback.apply(this, arguments) ?? r
        };
    } else {
        object[property] = callback;
    }
}

function getNodeById(id, graph=app.graph) {
    let cg = graph
    let node = undefined
    for (let sid of (''+id).split(':')) {
        node = cg?.getNodeById?.(sid)
        cg = node?.subgraph
    }
    return node
}

const convDict = {
    VHS_LoadImages : ["directory", null, "image_load_cap", "skip_first_images", "select_every_nth"],
    VHS_LoadImagesPath : ["directory", "image_load_cap", "skip_first_images", "select_every_nth"],
    VHS_VideoCombine : ["frame_rate", "loop_count", "filename_prefix", "format", "pingpong", "save_image"],
    VHS_LoadVideo : ["video", "force_rate", "force_size", "frame_load_cap", "skip_first_frames", "select_every_nth"],
    VHS_LoadVideoPath : ["video", "force_rate", "force_size", "frame_load_cap", "skip_first_frames", "select_every_nth"],
};
const renameDict  = {VHS_VideoCombine : {save_output : "save_image"}}
function useKVState(nodeType) {
    chainCallback(nodeType.prototype, "onNodeCreated", function () {
        chainCallback(this, "onConfigure", function(info) {
            if (!this.widgets) {
                //Node has no widgets, there is nothing to restore
                return
            }
            if (typeof(info.widgets_values) != "object") {
                //widgets_values is in some unknown inactionable format
                return
            }
            let widgetDict = info.widgets_values
            if (info.widgets_values.length) {
                //widgets_values is in the old list format
                if (this.type in convDict) {
                    //widget does not have a conversion format provided
                    let convList = convDict[this.type];
                    if(info.widgets_values.length >= convList.length) {
                        //has all required fields
                        widgetDict = {}
                        for (let i = 0; i < convList.length; i++) {
                            if(!convList[i]) {
                                //Element should not be processed (upload button on load image sequence)
                                continue
                            }
                            widgetDict[convList[i]] = info.widgets_values[i];
                        }
                    } else {
                        //widgets_values is missing elements marked as required
                        //let it fall through to failure state
                    }
                }
            }
            if ('force_size' in widgetDict) {
                //force size has been phased out, Migrate state
                if (widgetDict.force_size.includes?.('x')) {
                    let sizes = widgetDict.force_size.split('x')
                    if (sizes[0] != '?') {
                        widgetDict.custom_width = parseInt(sizes[0])
                    } else {
                        widgetDict.custom_width = 0
                    }
                    if (sizes[1] != '?') {
                        widgetDict.custom_height = parseInt(sizes[1])
                    } else {
                        widgetDict.custom_height = 0
                    }
                } else {
                    if (['Disabled', 'Custom Height'].includes(widgetDict.force_size)) {
                        widgetDict.custom_width = 0
                    }
                    if (['Disabled', 'Custom Width'].includes(widgetDict.force_size)) {
                        widgetDict.custom_height = 0
                    }
                }
            }
            if (widgetDict.videopreview?.params?.force_size) {
                delete widgetDict.videopreview.params.force_size
            }
            if (widgetDict.length == undefined) {
                for (let w of this.widgets) {
                    if (w.type =="button") {
                        continue
                    }
                    if (w.name in widgetDict) {
                        w.value = widgetDict[w.name];
                        w.callback?.(w.value)
                    } else {
                        //Check for a legacy name that needs migrating
                        if (this.type in renameDict && w.name in renameDict[this.type]) {
                            if (renameDict[this.type][w.name] in widgetDict) {
                                w.value = widgetDict[renameDict[this.type][w.name]]
                                w.callback?.(w.value)
                                continue
                            }
                        }
                        //attempt to restore default value
                        let inputs = LiteGraph.getNodeType(this.type).nodeData.input;
                        let initialValue = null;
                        if (inputs?.required?.hasOwnProperty(w.name)) {
                            if (inputs.required[w.name][1]?.hasOwnProperty("default")) {
                                initialValue = inputs.required[w.name][1].default;
                            } else if (inputs.required[w.name][0].length) {
                                initialValue = inputs.required[w.name][0][0];
                            }
                        } else if (inputs?.optional?.hasOwnProperty(w.name)) {
                            if (inputs.optional[w.name][1]?.hasOwnProperty("default")) {
                                initialValue = inputs.optional[w.name][1].default;
                            } else if (inputs.optional[w.name][0].length) {
                                initialValue = inputs.optional[w.name][0][0];
                            }
                        }
                        if (initialValue) {
                            w.value = initialValue;
                            w.callback?.(w.value)
                        }
                    }
                }
            } else {
                //Saved data was not a map made by this method
                //and a conversion dict for it does not exist
                //It's likely an array and that has been blindly applied
                if (info?.widgets_values?.length != this.widgets.length) {
                    //Widget could not have restored properly
                    //Note if multiple node loads fail, only the latest error dialog displays
                    app.ui.dialog.show("Failed to restore node: " + this.title + "\nPlease remove and re-add it.")
                    this.bgcolor = "#C00"
                }
            }
        });
        chainCallback(this, "onSerialize", function(info) {
            info.widgets_values = {};
            if (!this.widgets) {
                //object has no widgets, there is nothing to store
                return;
            }
            for (let w of this.widgets) {
                info.widgets_values[w.name] = w.value;
            }
        });
    })
}
var helpDOM = app.VHSHelp;
if (!app.VHSHelp) {
    helpDOM = document.createElement("div");
    app.VHSHelp = helpDOM
} else {
    app.extensionManager.dialog
      .showErrorDialog('Please check your custom_nodes directory and manually remove the duplicate.',
                       { title: 'Duplicate VHS install detected' })
    throw new Error('Duplicate VHS install detected. Check your custom_nodes directory')
}
function initHelpDOM() {
    let parentDOM = document.createElement("div");
    parentDOM.className = "VHS_floatinghelp"
    document.body.appendChild(parentDOM)
    parentDOM.appendChild(helpDOM)
    helpDOM.className = "litegraph";
    let scrollbarStyle = document.createElement('style');
    scrollbarStyle.innerHTML = `
            .VHS_floatinghelp {
                scrollbar-width: 6px;
                scrollbar-color: #0003  #0000;
                &::-webkit-scrollbar {
                    background: transparent;
                    width: 6px;
                }
                &::-webkit-scrollbar-thumb {
                    background: #0005;
                    border-radius: 20px
                }
                &::-webkit-scrollbar-button {
                    display: none;
                }
            }
            .VHS_loopedvideo::-webkit-media-controls-mute-button {
                display:none;
            }
            .VHS_loopedvideo::-webkit-media-controls-fullscreen-button {
                display:none;
            }
    `
    scrollbarStyle.id = 'scroll-properties'
    parentDOM.appendChild(scrollbarStyle)
    chainCallback(app.canvas, "onDrawForeground", function (ctx, visible_rect){
        let n = helpDOM.node
        if (!n || !n?.graph) {
            parentDOM.style['left'] = '-5000px'
            return
        }
        //draw : function(ctx, node, widgetWidth, widgetY, height) {
        //update widget position, even if off screen
        const transform = ctx.getTransform();
        const scale = app.canvas.ds.scale;//gets the litegraph zoom
        //calculate coordinates with account for browser zoom
        const bcr = app.canvas.canvas.getBoundingClientRect()
        const x = transform.e*scale/transform.a + bcr.x;
        const y = transform.f*scale/transform.a + bcr.y;
        //TODO: text reflows at low zoom. investigate alternatives
        Object.assign(parentDOM.style, {
            left: (x+(n.pos[0] + n.size[0]+15)*scale) + "px",
            top: (y+(n.pos[1]-LiteGraph.NODE_TITLE_HEIGHT)*scale) + "px",
            width: "400px",
            minHeight: "100px",
            maxHeight: "600px",
            overflowY: 'scroll',
            transformOrigin: '0 0',
            transform: 'scale(' + scale + ',' + scale +')',
            fontSize: '18px',
            backgroundColor: LiteGraph.NODE_DEFAULT_BGCOLOR,
            boxShadow: '0 0 10px black',
            borderRadius: '4px',
            padding: '3px',
            zIndex: 3,
            position: "absolute",
            display: 'inline',
        });
    });
    function setCollapse(el, doCollapse) {
        if (doCollapse) {
            el.children[0].children[0].innerHTML = '+'
            Object.assign(el.children[1].style, {
                color: '#CCC',
                overflowX: 'hidden',
                width: '0px',
                minWidth: 'calc(100% - 20px)',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
            })
            for (let child of el.children[1].children) {
                if (child.style.display != 'none'){
                    child.origDisplay = child.style.display
                }
                child.style.display = 'none'
            }
        } else {
            el.children[0].children[0].innerHTML = '-'
            Object.assign(el.children[1].style, {
                color: '',
                overflowX: '',
                width: '100%',
                minWidth: '',
                textOverflow: '',
                whiteSpace: '',
            })
            for (let child of el.children[1].children) {
                child.style.display = child.origDisplay
            }
        }
    }
    helpDOM.collapseOnClick = function() {
        let doCollapse = this.children[0].innerHTML == '-'
        setCollapse(this.parentElement, doCollapse)
    }
    helpDOM.selectHelp = function(name, value) {
        //attempt to navigate to name in help
        function collapseUnlessMatch(items,t) {
            var match = items.querySelector('[vhs_title="' + t + '"]')
            if (!match) {
                for (let i of items.children) {
                    if (i.innerHTML.slice(0,t.length+5).includes(t)) {
                        match = i
                        break
                    }
                }
            }
            if (!match) {
                return null
            }
            //For longer documentation items with fewer collapsable elements,
            //scroll to make sure the entirety of the selected item is visible
            //This has the unfortunate side effect of trying to scroll the main
            //window if the documentation windows is forcibly offscreen,
            //but it's easy to simply scroll the main window back and seems to
            //have no visual side effects
            match.scrollIntoView(false)
            window.scrollTo(0,0)
            for (let i of items.querySelectorAll('.VHS_collapse')) {
                if (i.contains(match)) {
                    setCollapse(i, false)
                } else {
                    setCollapse(i, true)
                }
            }
            return match
        }
        let target = collapseUnlessMatch(helpDOM, name)
        if (target && value) {
            collapseUnlessMatch(target, value)
        }
    }
    let titleContext = document.createElement("canvas").getContext("2d")
    titleContext.font = app.canvas.title_text_font;
    helpDOM.calculateTitleLength = function(text) {
        return titleContext.measureText(text).width
    }
    helpDOM.addHelp = function(node, nodeType, description) {
        if (!description) {
            return
        }
        //Pad computed size for the clickable question mark
        let originalComputeSize = node.computeSize
        node.computeSize = function() {
            let size = originalComputeSize.apply(this, arguments)
            if (!this.title) {
                return size
            }
            let title_width = helpDOM.calculateTitleLength(this.title)
            size[0] = Math.max(size[0], title_width + LiteGraph.NODE_TITLE_HEIGHT*2)
            return size
        }

        node.description = description
        chainCallback(node, "onDrawForeground", function (ctx) {
            if (this?.flags?.collapsed) {
                return
            }
            //draw question mark
            ctx.save()
            ctx.font = 'bold 20px Arial'
            ctx.fillText("?", this.size[0]-17, -8)
            ctx.restore()
        })
        chainCallback(node, "onMouseDown", function (e, pos, canvas) {
            if (this?.flags?.collapsed) {
                return
            }
            //On click would be preferred, but this'll be good enough
            if (pos[1] < 0 && pos[0] + LiteGraph.NODE_TITLE_HEIGHT > this.size[0]) {
                //corner question mark clicked
                if (helpDOM.node == this) {
                    helpDOM.node = undefined
                } else {
                    helpDOM.node = this;
                    helpDOM.innerHTML = this.description || "no help provided "
                    for (let e of helpDOM.querySelectorAll('.VHS_collapse')) {
                        e.children[0].onclick = helpDOM.collapseOnClick
                        e.children[0].style.cursor = 'pointer'
                    }
                    for (let e of helpDOM.querySelectorAll('.VHS_precollapse')) {
                        setCollapse(e, true)
                    }
                    for (let e of helpDOM.querySelectorAll('.VHS_loopedvideo')) {
                        e?.play()
                    }
                    helpDOM.parentElement.scrollTo(0,0)
                }
                return true
            }
        })
        let timeout = null
        chainCallback(node, "onMouseMove", function (e, pos, canvas) {
            if (timeout) {
                clearTimeout(timeout)
                timeout = null
            }
            if (helpDOM.node != this) {
                return
            }
            timeout = setTimeout(() => {
                let n = this
                if (pos[0] > 0 && pos[0] < n.size[0]
                    && pos[1] > 0 && pos[1] < n.size[1]) {
                    //TODO: provide help specific to element clicked
                    let inputRows = Math.max(n.inputs?.filter(i => !i.widget)?.length || 0, n.outputs?.length || 0)
                    if (pos[1] < LiteGraph.NODE_SLOT_HEIGHT * inputRows) {
                        let row = Math.floor((pos[1] - 7) / LiteGraph.NODE_SLOT_HEIGHT)
                        if (pos[0] < n.size[0]/2) {
                            if (row < n.inputs.length) {
                                helpDOM.selectHelp(n.inputs[row].name)
                            }
                        } else {
                            if (row < n.outputs.length) {
                                helpDOM.selectHelp(n.outputs[row].name)
                            }
                        }
                    } else {
                        //probably widget, but widgets have variable height.
                        let basey = LiteGraph.NODE_SLOT_HEIGHT * inputRows + 6
                        for (let w of n.widgets) {
                            if (w.y) {
                                basey = w.y
                            }
                            let wheight = LiteGraph.NODE_WIDGET_HEIGHT+4
                            if (w.computeSize) {
                                wheight = w.computeSize(n.size[0])[1]
                            }
                            if (pos[1] < basey + wheight) {
                                helpDOM.selectHelp(w.name, w.value)
                                break
                            }
                            basey += wheight
                        }
                    }
                }
            }, 500)
        })
        chainCallback(node, "onMouseLeave", function (e, pos, canvas) {
            if (timeout) {
                clearTimeout(timeout)
                timeout = null
            }
        });
    }
}

function fitHeight(node) {
    node.setSize([node.size[0], node.computeSize([node.size[0], node.size[1]])[1]])
    node?.graph?.setDirtyCanvas(true);
}
function startDraggingItems(node, pointer) {
    app.canvas.emitBeforeChange()
    app.canvas.graph?.beforeChange()
    // Ensure that dragging is properly cleaned up, on success or failure.
    pointer.finally = () => {
      app.canvas.isDragging = false
      app.canvas.graph?.afterChange()
      app.canvas.emitAfterChange()
    }
    app.canvas.processSelect(node, pointer.eDown, true)
    app.canvas.isDragging = true
}
function processDraggedItems(e) {
    if (e.shiftKey || LiteGraph.alwaysSnapToGrid)
      app.canvas?.graph?.snapToGrid(app.canvas.selectedItems)
    app.canvas.dirty_canvas = true
    app.canvas.dirty_bgcanvas = true
    app.canvas.onNodeMoved?.(findFirstNode(app.canvas.selectedItems))
}
function allowDragFromWidget(widget) {
    widget.onPointerDown = function(pointer, node) {
        pointer.onDragStart = () => startDraggingItems(node, pointer)
        pointer.onDragEnd = processDraggedItems
        app.canvas.dirty_canvas = true
        return true
    }
}

//Cloud specific auth code. Short circuits if not on cloud
const {
    debugLog,
    fetchWithOptionalAuth,
    matchesAcceptedMedia,
    uploadFile,
    addUploadWidget,
} = createUploadTransport({ app, api, chainCallback })

function addVAEOutputToggle(nodeType, nodeData) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        this.reject_ue_connection = (input) => input?.name == "vae"
    })
    chainCallback(nodeType.prototype, "onConnectionsChange", function(contype, slot, iscon, linfo) {
        let slotType = this.inputs[slot]?.type
        if (contype == LiteGraph.INPUT && slotType == "VAE") {
            if (iscon && linfo) {
                if (this.linkTimeout) {
                    clearTimeout(this.linkTimeout)
                    this.linkTimeout = false
                } else if (this.outputs[0].type == "IMAGE") {
                    this.linkTimeout = setTimeout(() => {
                        if (this.outputs[0].type != "IMAGE") {
                            return
                        }
                        this.linkTimeout = false
                        this.disconnectOutput(0);
                    }, 50)
                }
                this.outputs[0].name = 'LATENT';
                this.outputs[0].type = 'LATENT';
            } else{
                if (this.outputs[0].type == "LATENT") {
                    this.linkTimeout = setTimeout(() => {
                        this.linkTimeout = false
                        this.disconnectOutput(0);
                    }, 50)
                }
                this.outputs[0].name = "IMAGE";
                this.outputs[0].type = "IMAGE";
            }
        }
    });
}
function addVAEInputToggle(nodeType, nodeData) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        this.reject_ue_connection = (input) => input?.name == "vae"
    })
    chainCallback(nodeType.prototype, "onConnectionsChange", function(contype, slot, iscon, linf) {
        if (contype == LiteGraph.INPUT && slot == 3 && this.inputs[3].type == "VAE") {
            if (iscon && linf) {
                if (this.linkTimeout) {
                    clearTimeout(this.linkTimeout)
                    this.linkTimeout = false
                } else if (this.inputs[0].type == "IMAGE") {
                    this.linkTimeout = setTimeout(() => {
                        //workaround for out of order loading
                        if (this.inputs[0].type != "IMAGE") {
                            return
                        }
                        this.linkTimeout = false
                        this.disconnectInput(0);
                    }, 50)
                }
                this.inputs[0].type = 'LATENT';
            } else {
                if (this.inputs[0].type == "LATENT") {
                    this.linkTimeout = setTimeout(() => {
                        this.linkTimeout = false
                        this.disconnectInput(0);
                    }, 50)
                }
                this.inputs[0].type = "IMAGE";
            }
        }
    });
}
function cloneType(nodeType, nodeData) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        this.changeOutputType = function (new_type) {
            this.linkTimeout = setTimeout(() => {
                this.linkTimeout = false
                if (this.outputs[0].type != new_type) {
                    this.outputs[0].type = new_type
                    //check and potentially remove links
                    if (!this.outputs[0].links) {
                        return
                    }
                    let removed_links = []
                    for (let link_id of this.outputs[0].links) {
                        let link = app.graph.links[link_id]
                        if (!link) {
                            console.warn("[VHS] Missing link metadata during type clone", link_id)
                            continue
                        }
                        let target_node = app.graph.getNodeById(link.target_id)
                        let target_input = target_node.inputs[link.target_slot]
                        let keep = LiteGraph.isValidConnection(new_type, target_input.type)
                        if (!keep) {
                            link.disconnect(app.graph, 'input')
                            removed_links.push(link_id)
                        }
                        target_node.onConnectionsChange?.(LiteGraph.INPUT,
                            link.target_slot, keep, link, target_input)
                    }
                    this.outputs[0].links = this.outputs[0].links
                        .filter((v) => !removed_links.includes(v))
                }
            }, 50)
        }
        this.changeOutputType("VHS_DUMMY_NONE")
    });
    chainCallback(nodeType.prototype, "onConnectionsChange", function(contype, slot, iscon, linf) {
        if (contype == LiteGraph.INPUT && slot == 0) {
            let new_type = "VHS_DUMMY_NONE"
            if (iscon && linf) {
                new_type = app.graph.getNodeById(linf.origin_id).outputs[linf.origin_slot].type
            }
            if (this.linkTimeout) {
                clearTimeout(this.linkTimeout)
            }
            this.changeOutputType(new_type)
        }
    });
}

function addDateFormatting(nodeType, field, timestamp_widget = false) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        const widget = this.widgets.find((w) => w.name === field);
        widget.serializeValue = () => {
            return applyTextReplacements(app.rootGraph ?? app.graph, widget.value);
        };
    });
}
function initializeLoadFormat(nodeType, nodeData) {
    if (!nodeData?.input?.optional?.format) {
        return
    }
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        let node = this
        let formatWidget = this.widgets.find((w) => w.name === "format")
        formatWidget.options.formats = nodeData.input.optional.format[1].formats
        let base = {}
        for (let widget of this.widgets) {
           if (['force_rate', 'custom_width', 'custom_height',
               'frame_load_cap'].includes(widget.name)) {
               //TODO: filter these options?
               base[widget.name] = widget.options
           }
        }
        chainCallback(formatWidget, "callback", function(value) {
            let format = this.options.formats[value]
            if (!format) {
                return
            }
            if ('target_rate' in format) {
                format.force_rate = {'reset': format.target_rate}
            }
            if ('dim' in format) {
                format.custom_width = {'step': format.dim[0], 'mod': format.dim[1]}
                format.custom_height = {'step': format.dim[0], 'mod': format.dim[1]}
                if (format.dim[2]) {
                    format.custom_width.reset = format.dim[2]
                }
                if (format.dim[3]) {
                    format.custom_height.reset = format.dim[3]
                }
            }
            if ('frames' in format) {
                format.frame_load_cap = {'step': format.frames[0], 'mod': format.frames[1]}
            }
            for (let widget of node.widgets) {
                if (widget.name in base) {
                    let wasDefault = widget.options?.reset == widget.value
                    widget.options = Object.assign({}, base[widget.name], format[widget.name])
                    if (wasDefault && widget.options.reset != undefined) {
                        widget.value = widget.options.reset
                    }
                    widget.callback(widget.value)
                }
            }

        });
        let capWidget = this.widgets.find((w) => w.name === "frame_load_cap")
        capWidget.annotation = (value, width) => {
            let max_frames = this.video_query?.loaded?.frames
            if (!max_frames || value && value < max_frames) {
                return
            }
            let format = formatWidget.options.formats[formatWidget.value]
            const div = format?.frames?.[0] ?? 1
            const mod = format?.frames?.[1] ?? 0
            let loadable_frames = max_frames
            if ((max_frames % div) != mod) {
                loadable_frames = ((max_frames - mod)/div|0) * div + mod
            }
            return loadable_frames + "\u21FD"
        }
        let rateWidget = this.widgets.find((w) => w.name === "force_rate")
        rateWidget.annotation = (value, width) => {
            if (value == 0 && this.video_query?.source?.fps != undefined) {
                return roundToPrecision(this.video_query.source.fps, 2) + "\u21FD"
            }
        }
    });
}

const {
    addAudioPreview,
    addVideoPreview,
    addPreviewOptions,
    getCopiedPath,
} = createMediaPreview({
    app,
    api,
    chainCallback,
    allowDragFromWidget,
    fitHeight,
    shouldUseAdvancedPreview,
    debugLog,
    fetchWithOptionalAuth,
})

function addFormatWidgets(nodeType, nodeData) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        const formatWidget = this.widgets.find((widget) => widget.name === "format")
        const formats = nodeData?.input?.required?.format?.[1]?.formats ?? {}
        const formatState = {}
        chainCallback(formatWidget, "callback", (value) => {
            formatWidget.value = value
            reconcileFormatWidgets(this, formatWidget, formats, app, formatState)
            fitHeight(this);
        });
        formatWidget.callback?.(formatWidget.value)
    });
}
function addLoadCommon(nodeType, nodeData) {
    addVideoPreview(nodeType);
    initializeLoadFormat(nodeType, nodeData)
    addPreviewOptions(nodeType);
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        //widget.callback adds unused arguements which need culling
        const node = this
        function update(key) {
            return function(value) {
                let params = {}
                params[key] = this.value
                node?.updateParameters(params)
            }
        }
        let prior_ar = -2
        const widthWidget = this.widgets.find((w) => w.name === "custom_width");
        const heightWidget = this.widgets.find((w) => w.name === "custom_height");
        function updateAR(value) {
            let new_ar = -1
            if (widthWidget.value & heightWidget.value) {
                new_ar = widthWidget.value / heightWidget.value
            }
            if (new_ar != prior_ar) {
                node?.updateParameters({'custom_width': widthWidget.value,
                    'custom_height': heightWidget.value})
                prior_ar = new_ar
            }
        }
        const offsetWidget = this.widgets.find((w) => w.name === "start_time");
        if (offsetWidget) {
            Object.defineProperty(offsetWidget.options, "step", {
                set : (value) => {},
                get : () => {
                    return 1 / (this.video_query?.loaded?.fps ?? 1)
                }
            })
        }
        let widgetMap = {'frame_load_cap': 'frame_load_cap',
            'skip_first_frames': 'skip_first_frames', 'select_every_nth': 'select_every_nth',
            'start_time': 'start_time', 'force_rate': 'force_rate',
            'custom_width': updateAR, 'custom_height': updateAR,
            'image_load_cap': 'image_load_cap', 'skip_first_images': 'skip_first_images'
        }
        for (let widget of this.widgets) {
            if (widget.name in widgetMap) {
                if (typeof(widgetMap[widget.name]) == 'function') {
                    chainCallback(widget, "callback", widgetMap[widget.name]);
                } else {
                    chainCallback(widget, "callback", update(widgetMap[widget.name]))
                }
            }
            if (widget.type != "button") {
                widget.callback?.(widget.value)
            }
        }
    });
}

const {
    path_stem,
    searchBox,
    fitText,
    fitPath,
    roundToPrecision,
    drawAnnotated,
    mouseAnnotated,
} = createPathWidgets({
    app,
    api,
    fetchWithOptionalAuth,
    debugLog,
    LiteGraph,
})

const latentPreview = createLatentPreview({
    app,
    api,
    getNodeById,
    allowDragFromWidget,
    fitHeight,
})

app.registerExtension({
    name: "VideoHelperSuite.Core",
    settings: [
      {
        id: 'VHS.AdvancedPreviews',
        category: ['🎥🅥🅗🅢', 'Previews', 'Advanced Previews'],
        name: 'Advanced Previews',
        tooltip: 'Automatically transcode previews on request. Required for advanced functionality',
        type: 'combo',
        options: ['Never', 'Always', 'Input Only'],
        defaultValue: 'Input Only',
      },
      {
        id: 'VHS.AdvancedPreviewsMinWidth',
        category: ['🎥🅥🅗🅢', 'Previews', 'Min Width'],
        name: 'Minimum preview width',
        tooltip: 'Advanced previews have their resolution downscaled to the node size for performance. While a node can be resized to increase preview quality, a minimum width can be set that previews won\'t be downscaled beneath. Preveiws will never be upscaled, so this can safely be set large.',
        type: 'number',
        attrs: {
          min: 0,
          step: 1,
          max: 3840,
        },
        defaultValue: 0,
      },
      {
        id: 'VHS.AdvancedPreviewsDeadline',
        category: ['🎥🅥🅗🅢', 'Previews', 'Deadline'],
        name: 'Deadline',
        tooltip: 'Determines how much time can be spent when encoding advanced previews. Realtime results in reduced quality, but good will likely cause the preview to stutter as initial generation occurs',
        type: 'combo',
        options: ['realtime', 'good'],
        defaultValue: 'realtime',
      },
      {
        id: 'VHS.AdvancedPreviewsDefaultMute',
        category: ['🎥🅥🅗🅢', 'Previews', 'Default Mute'],
        name: 'Mute videos by default',
        type: 'boolean',
        defaultValue: false,
      },
      {
        id: 'VHS.Debug',
        category: ['🎥🅥🅗🅢', 'Debug'],
        name: 'Enable VHS debug logs',
        tooltip: 'Log upload, preview, and query diagnostics to the browser console.',
        type: 'boolean',
        defaultValue: false,
      },
      {
        id: 'VHS.LatentPreview',
        category: ['🎥🅥🅗🅢', 'Sampling', 'Latent Previews'],
        name: 'Display animated previews when sampling',
        type: 'boolean',
        defaultValue: false,
        onChange(value) {
            if (!value) {
                latentPreview.clear()
            }
        },
      },
      {
        id: "VHS.LatentPreviewRate",
        category: ['🎥🅥🅗🅢', 'Sampling', 'Latent Preview Rate'],
        name: "Playback rate override.",
        type: 'number',
        attrs: {
          min: 0,
          step: 1,
          max: 60
        },
        tooltip:
          'Force a specific frame rate for the playback of latent frames. This should not be confused with the output frame rate and will not match for video models.',
        defaultValue: 0,
      },
      {
        id: 'VHS.MetadataImage',
        category: ['🎥🅥🅗🅢', 'Output', 'MetadataImage'],
        name: 'Save png of first frame for metadata',
        type: 'boolean',
        defaultValue: true,
      },
      {
        id: 'VHS.KeepIntermediate',
        category: ['🎥🅥🅗🅢', 'Output', 'Keep Intermediate'],
        name: 'Keep required intermediate files after sucessful execution',
        type: 'boolean',
        defaultValue: true,
      },
    ],

    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if(nodeData?.name?.startsWith("VHS_")) {
            useKVState(nodeType);
            if (nodeData.description) {
                let description = nodeData.description
                let el = document.createElement("div")
                el.innerHTML = description
                if (!el.children.length) {
                    //Is plaintext. Do minor convenience formatting
                    let chunks = description.split('\n')
                    nodeData.description = chunks[0]
                    description = chunks.join('<br>')
                } else {
                    nodeData.description = el.querySelector('#VHS_shortdesc')?.innerHTML || el.children[1]?.firstChild?.innerHTML
                }
                chainCallback(nodeType.prototype, "onNodeCreated", function () {
                    helpDOM.addHelp(this, nodeType, description)
                    this.setSize(this.computeSize())
                })
            }
            //set widgetType to use VHS widgets where possible
            for(let inp of Object.values({...nodeData.input?.required, ...nodeData.input?.optional})) {
                if (["INT", "FLOAT"].includes(inp[0])) {
                    if (!inp[1]) {
                        inp[1] = {}
                    }
                    inp[1].widgetType ??= "VHS" + inp[0]
                }
            }
            chainCallback(nodeType.prototype, "onNodeCreated", function () {
                let new_widgets = []
                if (this.widgets) {
                    for (let w of this.widgets) {
                        let input = this.constructor.nodeData.input
                        let config = input?.required[w.name] ?? input.optional[w.name]
                        if (!config) {
                            continue
                        }
                        if (w?.type == "text" && config[1].vhs_path_extensions) {
                            new_widgets.push(app.widgets.VHSPATH({}, w.name, ["VHSPATH", config[1]]));
                        } else {
                            new_widgets.push(w)
                        }
                    }
                    this.widgets = new_widgets;
                }
            });
        }
        if (nodeData?.name == "VHS_LoadImages") {
            addUploadWidget(nodeType, nodeData, "directory", "folder");
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "directory");
                chainCallback(pathWidget, "callback", (value) => {
                    if (!value) {
                        return;
                    }
                    let params = {filename : value, type : "input", format: "folder"};
                    this.updateParameters(params, true);
                });
            });
            addLoadCommon(nodeType, nodeData);
        } else if (nodeData?.name == "VHS_LoadImagesPath") {
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "directory");
                chainCallback(pathWidget, "callback", (value) => {
                    if (!value) {
                        return;
                    }
                    let params = {filename : value, type : "path", format: "folder"};
                    this.updateParameters(params, true);
                });
            });
            addLoadCommon(nodeType, nodeData);
        } else if (nodeData?.name == "VHS_LoadVideo" || nodeData?.name == "VHS_LoadVideoFFmpeg") {
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "video");
                chainCallback(pathWidget, "callback", (value) => {
                    if (!value) {
                        return;
                    }
                    let parts = ["input", value];
                    let extension_index = parts[1].lastIndexOf(".");
                    let extension = parts[1].slice(extension_index+1);
                    let format = "video"
                    if (["gif", "webp", "avif"].includes(extension)) {
                        format = "image"
                    }
                    format += "/" + extension;
                    let params = {filename : parts[1], type : parts[0], format: format};
                    this.updateParameters(params, true);
                });
            });
            addUploadWidget(nodeType, nodeData, "video");
            addLoadCommon(nodeType, nodeData);
            addVAEOutputToggle(nodeType, nodeData);
        } else if (nodeData?.name == "VHS_LoadAudio") {
            addAudioPreview(nodeType)
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "audio_file");
                chainCallback(pathWidget, "callback", (filename) => {
                    this.updateParameters({filename, type: 'path'}, true);
                });
            });
        } else if (nodeData?.name == "VHS_LoadAudioUpload") {
            addUploadWidget(nodeType, nodeData, "audio", "audio");
            addAudioPreview(nodeType)
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "audio");
                chainCallback(pathWidget, "callback", (filename) => {
                    if (!filename) return
                    let params = {filename, type : "input"};
                    this.updateParameters(params, true);
                });
            });
        } else if (nodeData?.name == "VHS_LoadVideoPath" || nodeData?.name == "VHS_LoadVideoFFmpegPath") {
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "video");
                chainCallback(pathWidget, "callback", (value) => {
                    let extension_index = value.lastIndexOf(".");
                    let extension = value.slice(extension_index+1);
                    let format = "video"
                    if (["gif", "webp", "avif"].includes(extension)) {
                        format = "image"
                    }
                    format += "/" + extension;
                    let params = {filename : value, type: "path", format: format};
                    this.updateParameters(params, true);
                });
            });
            addLoadCommon(nodeType, nodeData);
            addVAEOutputToggle(nodeType, nodeData);
        } else if (nodeData?.name == "VHS_LoadImagePath") {
            addLoadCommon(nodeType, nodeData);
            addVAEOutputToggle(nodeType, nodeData);
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                const pathWidget = this.widgets.find((w) => w.name === "image");
                chainCallback(pathWidget, "callback", (value) => {
                    let extension_index = value.lastIndexOf(".");
                    let extension = value.slice(extension_index+1);
                    let format = "video" +  "/" + extension;
                    let params = {filename : value, type: "path", format: format};
                    this.updateParameters(params, true);
                });
            });
        } else if (nodeData?.name == "VHS_VideoCombine") {
            addDateFormatting(nodeType, "filename_prefix");
            chainCallback(nodeType.prototype, "onExecuted", function(message) {
                if (message?.gifs) {
                    this.updateParameters(message.gifs[0], true);
                }
            });
            addVideoPreview(nodeType, false);
            addPreviewOptions(nodeType);
            addFormatWidgets(nodeType, nodeData);
            addVAEInputToggle(nodeType, nodeData)
        } else if (nodeData?.name == "VHS_BatchManager") {
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                this.widgets.push({name: "count", type: "dummy", value: 0,
                    computeSize: () => {return [0,-4]},
                    afterQueued: function() {this.value++;}});
            });
        } else if (nodeData?.name == "VHS_Unbatch") {
            cloneType(nodeType, nodeData)
        } else if (nodeData?.name == "VHS_SelectLatest") {
            chainCallback(nodeType.prototype, "onNodeCreated", function() {
                configureSelectLatestNode(this, {
                    app,
                    api,
                    fetchWithOptionalAuth,
                    pathStem: path_stem,
                    chainCallback,
                })
            })
        }
    },
    async getCustomWidgets() {
        return {
            VHSPATH(node, inputName, inputData) {
                let w = {
                    name : inputName,
                    type : "VHS.PATH",
                    value : "",
                    draw : function(ctx, node, widget_width, y, H) {
                        //Adapted from litegraph.core.js:drawNodeWidgets
                        var show_text = app.canvas.ds.scale >= (app.canvas.low_quality_zoom_threshold ?? 0.5)
                        var margin = 15;
                        var text_color = LiteGraph.WIDGET_TEXT_COLOR;
                        var secondary_text_color = LiteGraph.WIDGET_SECONDARY_TEXT_COLOR;
                        ctx.textAlign = "left";
                        ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR;
                        ctx.fillStyle = LiteGraph.WIDGET_BGCOLOR;
                        ctx.beginPath();
                        if (show_text)
                            ctx.roundRect(margin, y, widget_width - margin * 2, H, [H * 0.5]);
                        else
                            ctx.rect( margin, y, widget_width - margin * 2, H );
                        ctx.fill();
                        if (show_text) {
                            if(!this.disabled)
                                ctx.stroke();
                            ctx.save();
                            ctx.beginPath();
                            ctx.rect(margin, y, widget_width - margin * 2, H);
                            ctx.clip();

                            //ctx.stroke();
                            let freeWidth = widget_width - (margin * 2 + 40)
                            ctx.fillStyle = secondary_text_color;
                            const label = this.label || this.name;
                            if (label != null) {
                                let [labelDisplay, labelWidth] = fitText(ctx, label, freeWidth)
                                freeWidth -= labelWidth
                                ctx.fillText(labelDisplay, margin * 2, y + H * 0.7);
                            }
                            ctx.fillStyle = this.value ? text_color : '#777';
                            ctx.textAlign = "right";
                            let disp_text = fitPath(ctx, String(this.value || this.options.placeholder), freeWidth)[0]
                            ctx.fillText(disp_text, widget_width - margin * 2, y + H * 0.7);
                            ctx.restore();
                        }
                    },
                    mouse : searchBox,
                    options : {},
                };
                if (inputData.length > 1) {
                    w.options = inputData[1]
                    if (inputData[1].default) {
                        w.value = inputData[1].default;
                    }
                }

                if (!node.widgets) {
                    node.widgets = [];
                }
                node.widgets.push(w);
                return w;
            },
            VHSFLOAT(node, inputName, inputData) {
                let w = {
                    name: inputName,
                    type: "VHS.ANNOTATED",
                    value: inputData[1]?.default ?? 0,
                    draw: drawAnnotated,
                    mouse: mouseAnnotated,
                    computeSize(width) {
                        return [width, 20]
                    },
                    callback(v) {
                        if (this.options.round) {
                            //TODO adopt ComfyUI_frontend#4291?
                            v = Math.round((v + Number.EPSILON) /
                                this.options.round) * this.options.round
                        }
                        if (this.options.max && v > this.options.max) {
                            v = this.options.max
                        }
                        if (this.options.min != null && v < this.options.min) {
                            v = this.options.min
                        }
                        this.value = v
                    },
                    config: inputData,
                    displayValue: function() {
                        return roundToPrecision(this.value, this.options.precision ?? 3)
                    },
                    options: Object.assign({},  inputData[1])
                }
                if (!node.widgets) {
                    node.widgets = []
                }
                node.widgets.push(w)
                return w
            },
            VHSINT(node, inputName, inputData) {
                let w = {
                    name: inputName,
                    type: "VHS.ANNOTATED",
                    value: inputData[1]?.default ?? 0,
                    draw: drawAnnotated,
                    mouse: mouseAnnotated,
                    computeSize(width) {
                        return [width, 20]
                    },
                    callback(v) {
                        if (this.options.max && v > this.options.max) {
                            v = this.options.max
                        }
                        if (this.options.min && v < this.options.min) {
                            v = this.options.min
                        }
                        if (v == 0) {
                            return
                        }
                        const s = this.options.step
                        let sh = this.options.mod ?? 0
                        this.value = Math.round((v - sh) / s) * s + sh
                    },
                    config: inputData,
                    displayValue: function() {
                        return this.value | 0
                    },
                    options: Object.assign({},  inputData[1])
                }
                if (!node.widgets) {
                    node.widgets = []
                }
                node.widgets.push(w)
                return w
            },
            VHSTIMESTAMP(node, inputName, inputData) {
                let w = {
                    name: inputName,
                    type: "VHS.TIMESTAMP",
                    value: inputData[1]?.default ?? 0,
                    draw: drawAnnotated,
                    mouse: mouseAnnotated,
                    computeSize(width) {
                        return [width, 20]
                    },
                    parseValue(v) {
                        if (typeof(v) == "string") {
                            let val = 0
                            for (let chunk of  v.split(":")) {
                                val = val * 60 + parseFloat(chunk)
                            }
                            return val
                        }
                    },
                    callback(v) {},
                    config: inputData,
                    options: Object.assign({}, inputData[1]),
                    displayValue() {
                        let seconds = this.value
                        let hours = seconds / 3600 | 0
                        seconds -= 3600 * hours
                        let minutes = seconds / 60 | 0
                        seconds -= 60 * minutes
                        let display = ""
                        if (hours > 0) {
                            display += hours + ":"
                        }
                        if (hours > 0 || minutes > 0) {
                            if (hours > 0) {
                                minutes = (''+minutes).padStart(2,'0')
                            }
                            display += minutes + ":"
                        }
                        seconds = roundToPrecision(seconds, 4)
                        if ((seconds[1] == '.' || seconds.length == 1) && (minutes > 0 || hours > 0)) {
                            seconds = '0'+seconds
                        }
                        display += seconds
                        return display
                    }
                }
                if (!node.widgets) {
                    node.widgets = []
                }
                node.widgets.push(w)
                return w
            },
        }
    },
    async loadedGraphNode(node) {
        //Check and migrate inputs named batch_manager from old workflows
        if (node.type?.startsWith("VHS_") && node.inputs) {
            const batchInput = node.inputs.find((i) => i.name == "batch_manager")
            if (batchInput) {
                batchInput.name = "meta_batch"
            }
        }
    },
    async beforeConfigureGraph(graphData, missingNodeTypes) {
        if(helpDOM?.node) {
            helpDOM.node = undefined
        }
    },
    async setup() {
        let originalGraphToPrompt = app.graphToPrompt
        let graphToPrompt = async function() {
            let res = await originalGraphToPrompt.apply(this, arguments);
            res.workflow.extra['VHS_latentpreview'] = app.ui.settings.getSettingValue("VHS.LatentPreview")
            res.workflow.extra['VHS_latentpreviewrate'] = app.ui.settings.getSettingValue("VHS.LatentPreviewRate")
            res.workflow.extra['VHS_MetadataImage'] = app.ui.settings.getSettingValue("VHS.MetadataImage")
            res.workflow.extra['VHS_KeepIntermediate'] = app.ui.settings.getSettingValue("VHS.KeepIntermediate")
            return res
        }
        app.graphToPrompt = graphToPrompt
        document.addEventListener(
            "paste",
            createPasteHandler({ app, LiteGraph, getCopiedPath }),
            true,
        )
    },
    async init() {
        if (app.ui.settings.getSettingValue("VHS.AdvancedPreviews") == true) {
            app.ui.settings.setSettingValue("VHS.AdvancedPreviews", 'Always')
        }
        if (app.ui.settings.getSettingValue("VHS.AdvancedPreviews") == false) {
            app.ui.settings.setSettingValue("VHS.AdvancedPreviews", 'Never')
        }
        if (app.VHSHelp != helpDOM) {
            helpDOM = app.VHSHelp
        } else {
            initHelpDOM()
        }
        let e = app.extensions.filter((w) => w.name == 'UVR5.AudioPreviewer')
        if (e.length) {
            let orig = e[0].beforeRegisterNodeDef
            e[0].beforeRegisterNodeDef = function(nodeType, nodeData, app) {
                if(!nodeData?.name?.startsWith("VHS_")) {
                    return orig.apply(this, arguments);
                }
            }
        }
    },
});
let previewImages = []
api.addEventListener('executing', ({ detail }) => {
    if (detail === null) {
        for (let graph of [app.graph, ...app.graph.subgraphs.values()]) {
            for (let node of graph._nodes) {
                if (node.type.startsWith("VHS_")) {
                    node.onPromptExecuted?.()
                }
            }
        }
    }
})
