export function createPasteHandler({ app, LiteGraph, getCopiedPath }) {
    return async function handlePaste(event) {
        const classList = event?.target?.classList
        if (!classList?.contains("litegraph") && !classList?.contains("graph-canvas-container")) {
            return
        }
        const data = event.clipboardData ?? globalThis.window?.clipboardData
        const filepath = data?.getData?.("text/plain")
        if (!filepath || filepath !== getCopiedPath?.()) {
            return
        }

        const pastedNode = LiteGraph.createNode("VHS_LoadVideoPath")
        if (!pastedNode?.widgets?.[0]) {
            return
        }
        app.canvas.graph.add(pastedNode)
        pastedNode.pos[0] = app.canvas.graph_mouse[0]
        pastedNode.pos[1] = app.canvas.graph_mouse[1]
        pastedNode.widgets[0].value = filepath
        pastedNode.widgets[0].callback?.(filepath)
        event.preventDefault()
        event.stopImmediatePropagation()
        return false
    }
}
