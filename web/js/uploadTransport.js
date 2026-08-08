export function createUploadTransport({ app, api, chainCallback }) {
async function getAuthHeader() {
  try {
    const authStore = await api.getAuthStore()
    return authStore ? await authStore.getAuthHeader() : null
  } catch (error) {
    console.warn('Failed to get auth header:', error)
    return null
  }
}

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

let vhsServerFeaturesPromise = null
async function getServerFeatures() {
    if (vhsServerFeaturesPromise) {
        return vhsServerFeaturesPromise
    }
    vhsServerFeaturesPromise = (async () => {
        try {
            const headers = await getAuthHeader() ?? {}
            const resp = await fetch(api.apiURL("/features"), { headers })
            if (!resp.ok) {
                throw new Error(`${resp.status} ${resp.statusText}`)
            }
            const features = await resp.json()
            debugLog("server_features", features)
            return features
        } catch (error) {
            console.warn("[VHS] Failed to fetch server features:", error)
            return {}
        }
    })()
    return vhsServerFeaturesPromise
}

async function validateUpload(file, acceptedTypes, label) {
    if (!file) {
        return "No file selected."
    }
    if (!matchesAcceptedMedia(file, acceptedTypes)) {
        return `Unsupported ${label} file type: ${file.name}`
    }
    const features = await getServerFeatures()
    const maxUploadSize = Number(features?.max_upload_size ?? 0)
    debugLog("upload_probe", {
        file: file.name,
        label,
        size: file.size,
        maxUploadSize,
    })
    if (maxUploadSize > 0 && file.size > maxUploadSize) {
        return `${label} exceeds the current ComfyUI upload limit (${formatBytes(file.size)} > ${formatBytes(maxUploadSize)}).`
    }
    return null
}

async function fetchWithOptionalAuth(url, options={}) {
    const headers = {
        ...(options.headers ?? {}),
        ...((await getAuthHeader()) ?? {}),
    }
    return fetch(url, {
        ...options,
        headers,
    })
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
    const lastSlash = relativePath.lastIndexOf("/")
    if (lastSlash > 0) {
        return relativePath.slice(0, lastSlash + 1)
    }
    return options.subfolder ?? ""
}

function getAssetTagPathParts(subfolder="") {
    return subfolder
        .split(/[\\/]/)
        .map((part) => part.trim())
        .filter(Boolean)
}

function normalizeLegacyUploadPath(payload={}) {
    if (!payload?.name) {
        return null
    }
    return joinUploadPath(payload.subfolder ?? "", payload.name)
}

function normalizeAssetUploadPath(payload={}) {
    const filename = payload?.user_metadata?.filename
    return typeof filename === "string" && filename.length > 0 ? filename : null
}

async function sendMultipartUpload(url, body, progressCallback) {
    return await new Promise((resolve, reject) => {
        const req = new XMLHttpRequest()
        req.upload.onprogress = (event) => {
            if (event.lengthComputable && event.total > 0) {
                progressCallback?.(event.loaded / event.total)
            }
        }
        req.onerror = () => reject(new Error(`Upload request failed: ${url}`))
        req.onload = () => {
            let json = null
            try {
                json = req.responseText ? JSON.parse(req.responseText) : null
            } catch (error) {
                debugLog("upload_response_parse_failed", {
                    url,
                    status: req.status,
                    error: String(error),
                })
            }
            resolve({
                ok: req.status >= 200 && req.status < 300,
                status: req.status,
                statusText: req.statusText,
                responseText: req.responseText,
                json,
                url,
            })
        }
        req.open("post", url, true)
        getAuthHeader()
            .then((headers) => {
                headers ??= {}
                for (const key in headers) {
                    req.setRequestHeader(key, headers[key])
                }
                req.send(body)
            })
            .catch(reject)
    })
}

async function uploadViaLegacyPath(file, progressCallback, options={}) {
    const body = new FormData()
    const subfolder = getRelativeUploadSubfolder(file, options)
    const materializedFile = new File([file], file.name, {
        type: file.type,
        lastModified: file.lastModified,
    })
    body.append("image", materializedFile)
    if (subfolder) {
        body.append("subfolder", subfolder)
    }
    const response = await sendMultipartUpload(api.apiURL("/upload/image"), body, progressCallback)
    response.path = normalizeLegacyUploadPath(response.json)
    response.route = "legacy"
    return response
}

async function uploadViaAssetRoute(file, progressCallback, options={}) {
    const body = new FormData()
    const subfolder = getRelativeUploadSubfolder(file, options)
    const materializedFile = new File([file], file.name, {
        type: file.type,
        lastModified: file.lastModified,
    })
    body.append("file", materializedFile)
    body.append("tags", options.assetRootTag ?? "input")
    for (const tag of getAssetTagPathParts(subfolder)) {
        body.append("tags", tag)
    }
    body.append("name", file.name)
    if (file.type) {
        body.append("mime_type", file.type)
    }
    const response = await sendMultipartUpload(api.apiURL("/api/assets"), body, progressCallback)
    response.path = normalizeAssetUploadPath(response.json)
    response.route = "asset"
    return response
}

async function uploadFile(file, progressCallback, options={}) {
    try {
        const validationError = await validateUpload(file, options.acceptedTypes ?? [], options.label ?? "Media")
        if (validationError) {
            alert(validationError)
            debugLog("upload_rejected", { file: file?.name, reason: validationError })
            return null
        }
        const features = await getServerFeatures()
        const shouldTryAssetUpload = features?.assets === true

        if (shouldTryAssetUpload) {
            debugLog("upload_strategy", {
                file: file.name,
                strategy: "asset-first",
            })
            const assetResponse = await uploadViaAssetRoute(file, progressCallback, options)
            if (assetResponse.ok && assetResponse.path) {
                return assetResponse
            }
            // IMPORTANT: asset uploads can dedupe content without creating a path on disk.
            // Keep the legacy materialized upload fallback for VHS path-based widgets.
            debugLog("upload_asset_fallback", {
                file: file.name,
                status: assetResponse.status,
                statusText: assetResponse.statusText,
                path: assetResponse.path,
                payload: assetResponse.json,
            })
        }

        const legacyResponse = await uploadViaLegacyPath(file, progressCallback, options)
        if (!legacyResponse.ok || !legacyResponse.path) {
            alert(legacyResponse.status + " - " + legacyResponse.statusText);
            debugLog("upload_failed", {
                file: file.name,
                status: legacyResponse.status,
                statusText: legacyResponse.statusText,
                payload: legacyResponse.json,
            })
        }
        return legacyResponse
    } catch (error) {
        alert(error);
        debugLog("upload_exception", { file: file?.name, error: String(error) })
    }
}

function addUploadWidget(nodeType, nodeData, widgetName, type="video") {
    let accept = {'video': ["video/webm","video/mp4","video/x-matroska","image/gif", ".mkv", ".mov", ".gif"],
                  'audio': ["audio/mpeg","audio/wav","audio/x-wav","audio/ogg", "audio/flac", "audio/mp4", ".m4a", ".flac"],
                  'folder': ["image/*", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".avif"]}
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        const node = this
        const pathWidget = this.widgets.find((w) => w.name === widgetName);
        const fileInput = document.createElement("input");
        chainCallback(this, "onRemoved", () => {
            fileInput?.remove();
        });
        if (type == "folder") {
            Object.assign(fileInput, {
                type: "file",
                style: "display: none",
                webkitdirectory: true,
                onchange: async () => {
                    const directory = fileInput.files[0].webkitRelativePath;
                    const i = directory.lastIndexOf('/');
                    if (i <= 0) {
                        throw "No directory found";
                    }
                    const path = directory.slice(0,directory.lastIndexOf('/'))
                    if (pathWidget.options.values.includes(path)) {
                        alert("A folder of the same name already exists");
                        return;
                    }
                    let successes = 0;
                    const onProg = (p) => this.progress = (successes + p) / fileInput.files.length
                    for(const file of fileInput.files) {
                        const response = await uploadFile(file, onProg, { acceptedTypes: accept.folder, label: "Image sequence file" })
                        if (response?.ok) {
                            successes++;
                        } else {
                            this.progress = undefined
                            //Upload failed, but some prior uploads may have succeeded
                            //Stop future uploads to prevent cascading failures
                            //and only add to list if an upload has succeeded
                            if (successes > 0) {
                                break
                            } else {
                                return;
                            }
                        }
                    }
                    this.progress = undefined
                    pathWidget.options.values.push(path);
                    pathWidget.value = path;
                    if (pathWidget.callback) {
                        pathWidget.callback(path)
                    }
                },
            });
        } else {
            let accept = {'video': ["video/webm","video/mp4","video/x-matroska","image/gif", ".mp4", ".webm", ".mkv", ".mov", ".gif"],
                          'audio': ["audio/mpeg","audio/wav","audio/x-wav","audio/ogg", "audio/flac", "audio/mp4", ".mp3", ".wav", ".ogg", ".m4a", ".flac"]}[type]
            async function doUpload(file) {
                let resp = await uploadFile(file, (p) => node.progress = p, {
                    acceptedTypes: accept,
                    label: type === "audio" ? "Audio" : "Video",
                })
                node.progress = undefined
                if (!resp?.ok || !resp.path) {
                    return false
                }
                const filename = resp.path
                pathWidget.options.values.push(filename);
                pathWidget.value = filename;
                if (pathWidget.callback) {
                    pathWidget.callback(filename)
                }
                return true
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
        let uploadWidget = this.addWidget("button", "choose " + type + " to upload", "image", () => {
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
