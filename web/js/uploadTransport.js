export function createUploadTransport({ app, api, chainCallback }) {
class UploadError extends Error {}

function isVHSDebugEnabled() {
    return !!app.ui.settings.getSettingValue("VHS.Debug")
}

function debugLog(event, payload) {
    if (!isVHSDebugEnabled()) {
        return
    }
    if (payload === undefined) {
        console.debug("[VHS]", event)
    } else {
        console.debug("[VHS]", event, payload)
    }
}

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) {
        return "0 B"
    }
    const units = ["B", "KB", "MB", "GB", "TB"]
    let value = bytes
    let unitIndex = 0
    while (value >= 1024 && unitIndex < units.length - 1) {
        value /= 1024
        unitIndex += 1
    }
    const rounded = Number(value.toFixed(2))
    return `${rounded} ${units[unitIndex]}`
}

function getFileExtension(name="") {
    const lastDot = name.lastIndexOf(".")
    if (lastDot < 0) {
        return ""
    }
    return name.slice(lastDot + 1).toLowerCase()
}

function matchesAcceptedMedia(file, acceptedTypes=[]) {
    if (!acceptedTypes?.length) {
        return true
    }
    const extension = getFileExtension(file?.name)
    return acceptedTypes.some((accepted) => {
        if (!accepted) {
            return false
        }
        if (accepted.startsWith(".")) {
            return extension === accepted.slice(1).toLowerCase()
        }
        if (accepted.endsWith("/*")) {
            return file?.type?.startsWith?.(accepted.slice(0, -1))
        }
        return file?.type === accepted || extension === accepted.toLowerCase()
    })
}

async function getServerFeatures(options) {
    // IMPORTANT: features belong to this operation's host context; never cache across uploads.
    const resp = await fetchWithOptionalAuth("/features", options)
    if (!resp.ok) {
        throw new UploadError(`Unable to read ComfyUI upload features (HTTP ${resp.status}).`)
    }
    const features = await resp.json()
    if (!features || typeof features !== "object" || Array.isArray(features)) {
        throw new UploadError("ComfyUI returned invalid upload features. No upload was attempted.")
    }
    return features
}

function validateUpload(file, acceptedTypes, label, features={}) {
    if (!file) {
        return "No file selected."
    }
    if (!matchesAcceptedMedia(file, acceptedTypes)) {
        return `Unsupported ${label} file type: ${file.name}`
    }
    const maxUploadSize = Number(features?.max_upload_size ?? 0)
    debugLog("upload_probe", {
        label,
        size: file.size,
        maxUploadSize,
    })
    if (maxUploadSize > 0 && file.size > maxUploadSize) {
        return `${label} exceeds the current ComfyUI upload limit (${formatBytes(file.size)} > ${formatBytes(maxUploadSize)}).`
    }
    return null
}

async function fetchWithOptionalAuth(route, options={}) {
    if (typeof api.fetchApi !== "function") {
        throw new UploadError("ComfyUI's public request API is unavailable. Update the ComfyUI frontend.")
    }
    // CRITICAL: the host owns prefixes, auth, CSRF and workspace scope. Pass relative routes only.
    return await api.fetchApi(route, options)
}

function joinUploadPath(subfolder="", name="") {
    if (!subfolder) {
        return name
    }
    const normalized = subfolder.endsWith("/") ? subfolder.slice(0, -1) : subfolder
    return normalized ? `${normalized}/${name}` : name
}

function getRelativeUploadSubfolder(file, options={}) {
    const relativePath = file?.webkitRelativePath ?? ""
    const normalized = relativePath ? safeRelativePath(relativePath) : ""
    if (relativePath && (!normalized || normalized.split("/").at(-1) !== file.name)) {
        throw new UploadError("The selected file has an invalid relative upload path.")
    }
    const lastSlash = normalized.lastIndexOf("/")
    if (lastSlash > 0) {
        return normalized.slice(0, lastSlash)
    }
    const subfolder = options.subfolder ?? ""
    if (!subfolder) return ""
    const safe = typeof subfolder === "string" ? safeRelativePath(subfolder.replace(/[\\/]$/, "")) : null
    if (!safe) throw new UploadError("The upload subfolder is invalid.")
    return safe
}

function safeRelativePath(path) {
    if (typeof path !== "string" || !path || /[\0:]/.test(path)) return null
    const normalized = path.replace(/\\/g, "/")
    const parts = normalized.split("/")
    if (parts.some(part => !part || part === "." || part === "..")) return null
    if (/\s\[(?:input|output|temp)\]$/.test(normalized)) return null
    return normalized
}

function normalizeLegacyUploadPath(payload={}) {
    if (payload?.type !== undefined && payload.type !== "input") return null
    const name = safeRelativePath(payload?.name)
    if (!name || name.includes("/")) return null
    const subfolder = payload.subfolder ?? ""
    if (typeof subfolder !== "string") return null
    return safeRelativePath(joinUploadPath(subfolder, name))
}

function normalizeAssetUploadPath(payload={}) {
    if (payload?.tags !== undefined) {
        if (!Array.isArray(payload.tags)) return null
        const roots = payload.tags.filter(tag => ["input", "output", "models", "temp"].includes(tag))
        if (roots.length !== 1 || roots[0] !== "input") return null
    }
    // CRITICAL: an explicit invalid canonical path must never be replaced by user-controlled metadata.
    const path = Object.hasOwn(payload ?? {}, "loader_path") ? payload.loader_path : payload?.user_metadata?.filename
    return safeRelativePath(path)
}

async function sendMultipartUpload(route, body, progressCallback, options) {
    progressCallback?.(null)
    const resp = await fetchWithOptionalAuth(route, { method: "POST", body, signal: options.signal })
    let json = null
    try {
        json = await resp.json()
    } catch (_error) {
        debugLog("upload_response_parse_failed", { route, status: resp.status })
    }
    return { ok: resp.ok, status: resp.status, json }
}

async function uploadViaLegacyPath(file, progressCallback, options={}) {
    const body = new FormData()
    const subfolder = getRelativeUploadSubfolder(file, options)
    const materializedFile = new File([file], file.name, {
        type: file.type,
        lastModified: file.lastModified,
    })
    body.append("image", materializedFile)
    body.append("type", "input")
    if (subfolder) {
        body.append("subfolder", subfolder)
    }
    const response = await sendMultipartUpload("/upload/image", body, progressCallback, options)
    response.path = normalizeLegacyUploadPath(response.json)
    response.route = "legacy"
    return response
}

async function uploadViaAssetRoute(file, progressCallback, options={}) {
    const body = new FormData()
    const materializedFile = new File([file], file.name, {
        type: file.type,
        lastModified: file.lastModified,
    })
    body.append("file", materializedFile)
    body.append("tags", "input")
    body.append("name", file.name)
    if (file.type) {
        body.append("mime_type", file.type)
    }
    const response = await sendMultipartUpload("/api/assets", body, progressCallback, options)
    response.path = normalizeAssetUploadPath(response.json)
    response.route = "asset"
    return response
}

async function uploadFile(file, progressCallback, options={}) {
    try {
        const acceptedTypes = options.acceptedTypes ?? []
        const label = options.label ?? "Media"
        const basicError = validateUpload(file, acceptedTypes, label)
        if (basicError) {
            alert(basicError)
            return null
        }
        if ((options.assetRootTag ?? "input") !== "input" || !safeRelativePath(file.name) || /[\\/]/.test(file.name)) {
            throw new UploadError("VHS uploads require a safe filename and the input destination.")
        }
        const subfolder = getRelativeUploadSubfolder(file, options)
        const features = await getServerFeatures({ signal: options.signal })
        const validationError = validateUpload(file, acceptedTypes, label, features)
        if (validationError) {
            alert(validationError)
            debugLog("upload_rejected", { reason: "validation" })
            return null
        }
        // IMPORTANT: Asset tags are labels, not directories; choose path-preserving uploads before writing.
        const shouldTryAssetUpload = features?.assets === true && !subfolder

        if (shouldTryAssetUpload) {
            debugLog("upload_strategy", {
                strategy: "asset-first",
            })
            const assetResponse = await uploadViaAssetRoute(file, progressCallback, options)
            if (assetResponse.ok && assetResponse.path) {
                progressCallback?.(1)
                return assetResponse
            }
            // CRITICAL: success or uncertain failure may already have written bytes. Never replay that write.
            if (assetResponse.ok || ![404, 405, 501].includes(assetResponse.status)) {
                throw new UploadError(assetResponse.ok ? "Upload completed but returned no safe input path. No second upload was attempted." : `Upload failed (HTTP ${assetResponse.status}). No second upload was attempted.`)
            }
            debugLog("upload_asset_fallback", {
                status: assetResponse.status,
            })
        }

        const legacyResponse = await uploadViaLegacyPath(file, progressCallback, options)
        if (!legacyResponse.ok || !legacyResponse.path) {
            throw new UploadError(legacyResponse.ok ? "Upload completed but returned no safe input path." : `Upload failed (HTTP ${legacyResponse.status}).`)
        }
        progressCallback?.(1)
        return legacyResponse
    } catch (error) {
        if (error?.name !== "AbortError") {
            alert(error instanceof UploadError ? error.message : "Upload failed. Check the ComfyUI connection and try again.")
        }
        debugLog("upload_exception", { name: error?.name })
    } finally {
        progressCallback?.(undefined)
    }
}

function addUploadWidget(nodeType, nodeData, widgetName, type="video") {
    let accept = {'video': ["video/webm","video/mp4","video/x-matroska","image/gif", ".mkv", ".mov", ".gif"],
                  'audio': ["audio/mpeg","audio/wav","audio/x-wav","audio/ogg", "audio/flac", "audio/mp4", ".m4a", ".flac"],
                  'folder': ["image/*", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".avif"]}
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        const node = this
        let activeUpload = null
        const idleLabel = "choose " + type + " to upload"
        function beginUpload() {
            if (activeUpload) return false
            activeUpload = new AbortController()
            uploadWidget.name = "uploading " + type + "..."
            return true
        }
        function endUpload() {
            activeUpload = null
            node.progress = undefined
            uploadWidget.name = idleLabel
        }
        const pathWidget = this.widgets.find((w) => w.name === widgetName);
        const fileInput = document.createElement("input");
        chainCallback(this, "onRemoved", () => {
            activeUpload?.abort()
            fileInput?.remove();
        });
        if (type == "folder") {
            Object.assign(fileInput, {
                type: "file",
                style: "display: none",
                webkitdirectory: true,
                onchange: async () => {
                    if (!fileInput.files?.length || !beginUpload()) return false
                    try {
                        const files = Array.from(fileInput.files).sort((a, b) => a.webkitRelativePath < b.webkitRelativePath ? -1 : a.webkitRelativePath > b.webkitRelativePath ? 1 : 0)
                        const path = getRelativeUploadSubfolder(files[0])
                        if (!path || files.some(file => getRelativeUploadSubfolder(file) !== path)) {
                            alert("Select image sequence files from a single directory.")
                            return false
                        }
                        if (pathWidget.options.values.includes(path)) {
                            alert("A folder of the same name already exists")
                            return false
                        }
                        let successes = 0
                        const onProg = (p) => node.progress = typeof p === "number" ? (successes + p) / files.length : undefined
                        for (const file of files) {
                            const response = await uploadFile(file, onProg, { acceptedTypes: accept.folder, label: "Image sequence file", signal: activeUpload.signal })
                            if (!response?.ok || response.path !== joinUploadPath(path, file.name)) {
                                alert(`Image sequence upload stopped: ${successes} of ${files.length} files confirmed. The previous selection was kept.`)
                                return false
                            }
                            successes++
                        }
                        pathWidget.options.values.push(path)
                        pathWidget.value = path
                        pathWidget.callback?.(path)
                        return true
                    } catch (_error) {
                        alert("The selected directory has an invalid upload path.")
                        return false
                    } finally {
                        endUpload()
                    }
                },
            });
        } else {
            let accept = {'video': ["video/webm","video/mp4","video/x-matroska","image/gif", ".mp4", ".webm", ".mkv", ".mov", ".gif"],
                          'audio': ["audio/mpeg","audio/wav","audio/x-wav","audio/ogg", "audio/flac", "audio/mp4", ".mp3", ".wav", ".ogg", ".m4a", ".flac"]}[type]
            async function doUpload(file) {
                if (!beginUpload()) return false
                try {
                    const resp = await uploadFile(file, (p) => node.progress = p, {
                        acceptedTypes: accept,
                        label: type === "audio" ? "Audio" : "Video",
                        signal: activeUpload.signal,
                    })
                    if (!resp?.ok || !resp.path) return false
                    const filename = resp.path
                    pathWidget.options.values.push(filename)
                    pathWidget.value = filename
                    pathWidget.callback?.(filename)
                    return true
                } finally {
                    endUpload()
                }
            }
            Object.assign(fileInput, {
                type: "file",
                accept: accept.join(','),
                style: "display: none",
                onchange: async () => {
                    if (fileInput.files.length) {
                        return await doUpload(fileInput.files[0])
                    }
                },
            });
            this.onDragOver = (e) => !!e?.dataTransfer?.types?.includes?.('Files')
            this.onDragDrop = async function(e) {
                if (!e?.dataTransfer?.types?.includes?.('Files')) {
                    return false
                }
                //TODO: Allow dragging multiple files at once?
                const item = e.dataTransfer?.files?.[0]
                if (matchesAcceptedMedia(item, accept)) {
                    return await doUpload(item)
                }
                return false
            }
        }
        document.body.append(fileInput);
        let uploadWidget = this.addWidget("button", idleLabel, "image", () => {
            if (activeUpload) return
            //clear the active click event
            app.canvas.node_widget = null

            fileInput.click();
        });
        uploadWidget.options.serialize = false;


    });
}

    return {
        debugLog,
        fetchWithOptionalAuth,
        matchesAcceptedMedia,
        uploadFile,
        addUploadWidget,
    }
}
