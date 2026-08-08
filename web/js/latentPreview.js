export function createLatentPreview({
    app,
    api,
    getNodeById,
    allowDragFromWidget,
    fitHeight,
}) {
    let latentPreviewNodes = new Set()

function getLatentPreviewCtx(id, width, height) {
    const node = getNodeById(id)
    if (!node) {
        return undefined
    }

    let previewWidget = node.widgets.find((w) => w.name == "vhslatentpreview")
    if (!previewWidget) {
        //check for and remove any native preview
        let nativePreview = node.widgets.findIndex((w) => w.name == '$$canvas-image-preview')
        if (nativePreview >= 0) {
            node.imgs = []
            node.widgets.splice(nativePreview,1)
        }
        let canvasEl = document.createElement("canvas")
        canvasEl.style.width = "100%"
        previewWidget = node.addDOMWidget("vhslatentpreview", "vhscanvas", canvasEl, {
            serialize: false,
            hideOnZoom: false,
        });
        previewWidget.serialize = false
        allowDragFromWidget(previewWidget)
        canvasEl.addEventListener('contextmenu', (e)  => {
            e.preventDefault()
            return app.canvas._mousedown_callback(e)
        }, true);
        canvasEl.addEventListener('pointerdown', (e)  => {
            e.preventDefault()
            return app.canvas._mousedown_callback(e)
        }, true);
        canvasEl.addEventListener('mousewheel', (e)  => {
            e.preventDefault()
            return app.canvas._mousewheel_callback(e)
        }, true);
        canvasEl.addEventListener('pointermove', (e)  => {
            e.preventDefault()
            return app.canvas._mousemove_callback(e)
        }, true);
        canvasEl.addEventListener('pointerup', (e)  => {
            e.preventDefault()
            return app.canvas._mouseup_callback(e)
        }, true);

        previewWidget.computeSize = function(width) {
            if (this.aspectRatio) {
                let height = (node.size[0]-20)/ this.aspectRatio + 10;
                if (!(height > 0)) {
                    height = 0;
                }
                this.computedHeight = height + 10;
                return [width, height];
            }
            return [width, -4];//no loaded src, widget should not display
        }
    }
    let canvasEl = previewWidget.element
    if (!previewWidget.ctx || canvasEl.width != width
        || canvasEl.height != height) {
        previewWidget.aspectRatio = width / height
        canvasEl.width = width
        canvasEl.height = height
        fitHeight(node)
    }
    return canvasEl.getContext("2d")
}
let animateIntervals = {}
function beginLatentPreview(id, previewImages, rate) {
    latentPreviewNodes.add(id)
    if (animateIntervals[id]) {
        clearTimeout(animateIntervals[id])
    }
    let displayIndex = 0
    let node = getNodeById(id)
    //While progress is safely cleared on execution completion.
    //Initial progress must be started here to avoid a race condition
    node.progress = 0
    animateIntervals[id] = setInterval(() => {
        if (getNodeById(id)?.progress == undefined
            || app.canvas.graph.rootGraph != node.graph.rootGraph) {
            clearTimeout(animateIntervals[id])
            delete animateIntervals[id]
            return
        }
        if (!previewImages[displayIndex]) {
            return
        }
        getLatentPreviewCtx(id, previewImages[displayIndex].width,
            previewImages[displayIndex].height)?.drawImage?.(previewImages[displayIndex],0,0)
        displayIndex = (displayIndex + 1) % previewImages.length
    }, 1000/rate);

}
let previewImagesDict = {}
api.addEventListener('VHS_latentpreview', ({ detail }) => {
    if (detail.id == null) {
        return
    }
    let previewImages = previewImagesDict[detail.id] = []
    previewImages.length = detail.length

    let idParts = detail.id.split(':')
    for (let i=1; i <= idParts.length; i++) {
        let id = idParts.slice(0,i).join(':')
        beginLatentPreview(id, previewImages, detail.rate)
    }
});
let td = new TextDecoder()
api.addEventListener('b_preview', async (e) => {
    if (Object.keys(animateIntervals).length == 0) {
        return
    }
    e.preventDefault()
    e.stopImmediatePropagation()
    e.stopPropagation()
    const dv = new DataView(await e.detail.slice(0,24).arrayBuffer())
    const index = dv.getUint32(4)
    const idlen = dv.getUint8(8)
    const id = td.decode(dv.buffer.slice(9,9+idlen))
    previewImagesDict[id][index] = await window.createImageBitmap(e.detail.slice(24))
    return false
}, true);

    return {
        clear() {
            for (const id of latentPreviewNodes) {
                const node = getNodeById(id)
                const index = node?.widgets?.findIndex((widget) => widget.name === "vhslatentpreview")
                if (index >= 0) node.widgets.splice(index, 1)[0].onRemove()
            }
            latentPreviewNodes = new Set()
        },
    }
}
