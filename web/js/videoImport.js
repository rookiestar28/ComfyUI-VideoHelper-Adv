import { parseVideoMetadataBuffer } from './videoMetadataParser.js'

function isVideoFile(file) {
    return file?.type?.startsWith("video/") || /\.(?:mp4|webm|mkv)$/i.test(file?.name ?? "")
}

function workflowFromMetadata(metadata) {
    let workflow = metadata?.workflow
    if (typeof workflow === "string") workflow = JSON.parse(workflow)
    if (!workflow || typeof workflow !== "object" || Array.isArray(workflow)) return null
    if (!Array.isArray(workflow.nodes) || (workflow.links !== undefined && !Array.isArray(workflow.links))) return null
    return workflow
}

export function createVideoImportExtension(app) {
    let installed = false
    function install() {
        if (!installed) {
            const original = app.handleFile
            app.handleFile = function(file, ...context) {
                if (!isVideoFile(file)) return original.apply(this, [file, ...context])
                const receiver = this
                return (async () => {
                    let workflow = null
                    try {
                        workflow = workflowFromMetadata(parseVideoMetadataBuffer(await file.arrayBuffer()))
                    } catch (_error) {
                        // Unreadable or malformed legacy metadata still belongs to the native importer.
                    }
                    if (!workflow) return original.apply(receiver, [file, ...context])
                    const [openSource, options] = context
                    const filename = file.name.replace(/\.\w+$/, "")
                    // CRITICAL: keep host graph loading outside parse fallback; retrying may load twice after mutation.
                    await receiver.loadGraphData(workflow, true, true, filename, {
                        openSource,
                        deferWarnings: options?.deferWarnings,
                    })
                })()
            }
            installed = true
        }
        // IMPORTANT: the input may be absent or recreated; refresh it at setup without wrapping again.
        const input = globalThis.document?.getElementById("comfy-file-input")
        if (!input) return
        const accepted = (input.accept ?? "").split(",").map(type => type.trim()).filter(Boolean)
        for (const type of ["video/webm", "video/mp4", "video/x-matroska"]) {
            if (!accepted.some(existing => existing.toLowerCase() === type)) accepted.push(type)
        }
        input.accept = accepted.join(",")
    }
    return { name: "VideoHelperSuite.VideoImport", init: install, setup: install }
}
