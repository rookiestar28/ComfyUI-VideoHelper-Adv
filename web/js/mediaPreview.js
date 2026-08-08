export function createMediaPreview({
    app,
    api,
    chainCallback,
    allowDragFromWidget,
    fitHeight,
    shouldUseAdvancedPreview,
    debugLog,
    fetchWithOptionalAuth,
}) {
function addAudioPreview(nodeType, isInput=true) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        var element = document.createElement("audio");
        element.controls = true
        element.style['width'] = "100%"
        element.style['minHeight'] = "50px"
        const previewNode = this;
        var previewWidget = this.addDOMWidget("audiopreview", "preview", element, {
            serialize: false,
            hideOnZoom: true,
            getValue() {
                return element.value;
            },
            setValue(v) {
                element.value = v;
            },
        });
        previewWidget.computeSize = function(width) {
            return [width, 50];
        }
        var timeout = null;
        this.updateParameters = (params, force_update) => {
            if (!previewWidget.value.params) {
                if(typeof(previewWidget.value) != 'object') {
                    previewWidget.value =  {}
                }
                previewWidget.value.params = {}
            }
            Object.assign(previewWidget.value.params, params)
            if (!force_update &&
                app.ui.settings.getSettingValue("VHS.AdvancedPreviews") == 'Never') {
                return;
            }
            if (timeout) {
                clearTimeout(timeout);
            }
            if (force_update) {
                previewWidget.updateSource();
            } else {
                timeout = setTimeout(() => previewWidget.updateSource(),100);
            }
        };
        previewWidget.updateSource = function () {
            if (this.value.params == undefined) {
                return;
            }
            let params =  {}
            Object.assign(params, this.value.params);//shallow copy
            let advp = shouldUseAdvancedPreview({
                advancedPreviews: app.ui.settings.getSettingValue("VHS.AdvancedPreviews"),
                isInput,
                format: params.format,
            })
            params.timestamp = Date.now()
            if (!advp) {
                element.src = api.apiURL('/view?' + new URLSearchParams(params));
            } else {
                params.deadline = app.ui.settings.getSettingValue("VHS.AdvancedPreviewsDeadline")
                element.src = api.apiURL('/vhs/viewaudio?' + new URLSearchParams(params));
            }
            debugLog("audio_preview_source", { advp, params, src: element.src })
        }
        previewWidget.callback = previewWidget.updateSource


        //setup widget tracking
        function update(key) {
            return function(value) {
                let params = {}
                params[key] = this.value
                previewNode?.updateParameters(params)
            }
        }
        let widgetMap = { 'seek_seconds': 'start_time', 'duration': 'duration',
            'start_time': 'start_time' }
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

function addVideoPreview(nodeType, isInput=true) {
    chainCallback(nodeType.prototype, "onNodeCreated", function() {
        var element = document.createElement("div");
        const previewNode = this;
        var previewWidget = this.addDOMWidget("videopreview", "preview", element, {
            serialize: false,
            hideOnZoom: false,
            getValue() {
                return element.value;
            },
            setValue(v) {
                element.value = v;
            },
        });
        allowDragFromWidget(previewWidget)
        previewWidget.computeSize = function(width) {
            if (this.aspectRatio && !this.parentEl.hidden) {
                let height = (previewNode.size[0]-20)/ this.aspectRatio + 10;
                if (!(height > 0)) {
                    height = 0;
                }
                this.computedHeight = height + 10;
                return [width, height];
            }
            return [width, -4];//no loaded src, widget should not display
        }
        element.addEventListener('contextmenu', (e)  => {
            e.preventDefault()
            return app.canvas._mousedown_callback(e)
        }, true);
        element.addEventListener('pointerdown', (e)  => {
            e.preventDefault()
            return app.canvas._mousedown_callback(e)
        }, true);
        element.addEventListener('mousewheel', (e)  => {
            e.preventDefault()
            return app.canvas._mousewheel_callback(e)
        }, true);
        element.addEventListener('pointermove', (e)  => {
            e.preventDefault()
            return app.canvas._mousemove_callback(e)
        }, true);
        element.addEventListener('pointerup', (e)  => {
            e.preventDefault()
            return app.canvas._mouseup_callback(e)
        }, true);
        element.addEventListener('dragover', (e) => {
            //A little hacky, but allows drag events onto the preview itself
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
            app.dragOverNode = this
        })
        previewWidget.value = {hidden: false, paused: false, params: {},
            muted: app.ui.settings.getSettingValue("VHS.AdvancedPreviewsDefaultMute"), error: ""}
        previewWidget.parentEl = document.createElement("div");
        previewWidget.parentEl.className = "vhs_preview";
        previewWidget.parentEl.style['width'] = "100%"
        element.appendChild(previewWidget.parentEl);
        previewWidget.videoEl = document.createElement("video");
        previewWidget.videoEl.controls = false;
        previewWidget.videoEl.loop = true;
        previewWidget.videoEl.muted = true;
        previewWidget.videoEl.style['width'] = "100%"
        previewWidget.videoEl.addEventListener("loadedmetadata", () => {
            previewWidget.parentEl.hidden = previewWidget.value.hidden;
            previewWidget.setStatus("")
            previewWidget.aspectRatio = previewWidget.videoEl.videoWidth / previewWidget.videoEl.videoHeight;
            fitHeight(this);
        });
        previewWidget.videoEl.addEventListener("error", () => {
            if (!previewWidget.videoEl.currentSrc && !previewWidget.videoEl.src) {
                return
            }
            previewWidget.setStatus("Preview unavailable. Check VHS debug logs for details.")
            previewWidget.parentEl.hidden = true;
            fitHeight(this);
        });
        previewWidget.videoEl.onmouseenter =  () => {
            previewWidget.videoEl.muted = previewWidget.value.muted
        };
        previewWidget.videoEl.onmouseleave = () => {
            previewWidget.videoEl.muted = true;
        };

        previewWidget.imgEl = document.createElement("img");
        previewWidget.imgEl.style['width'] = "100%"
        previewWidget.imgEl.hidden = true;
        previewWidget.imgEl.onload = () => {
            previewWidget.parentEl.hidden = previewWidget.value.hidden;
            previewWidget.aspectRatio = previewWidget.imgEl.naturalWidth / previewWidget.imgEl.naturalHeight;
            previewWidget.setStatus("")
            fitHeight(this);
        };
        previewWidget.statusEl = document.createElement("div");
        previewWidget.statusEl.className = "vhs_preview_status";
        previewWidget.statusEl.style.fontSize = "12px";
        previewWidget.statusEl.style.padding = "4px 0";
        previewWidget.statusEl.style.color = "#ccc";
        previewWidget.statusEl.hidden = true;
        previewWidget.setStatus = function(message) {
            this.value.error = message || ""
            this.statusEl.hidden = !message
            this.statusEl.textContent = message || ""
        }
        previewWidget.parentEl.appendChild(previewWidget.videoEl)
        previewWidget.parentEl.appendChild(previewWidget.imgEl)
        previewWidget.parentEl.appendChild(previewWidget.statusEl)
        var timeout = null;
        this.updateParameters = (params, force_update) => {
            if (!previewWidget.value.params) {
                if(typeof(previewWidget.value) != 'object') {
                    previewWidget.value =  {hidden: false, paused: false}
                }
                previewWidget.value.params = {}
            }
            if (!Object.entries(params).some(([k,v]) => previewWidget.value.params[k] !== v)) {
                return
            }
            Object.assign(previewWidget.value.params, params)
            if (!force_update &&
                app.ui.settings.getSettingValue("VHS.AdvancedPreviews") == 'Never') {
                return;
            }
            if (timeout) {
                clearTimeout(timeout);
            }
            if (force_update) {
                previewWidget.updateSource();
            } else {
                timeout = setTimeout(() => previewWidget.updateSource(),100);
            }
        };
        previewWidget.updateSource = function () {
            if (this.value.params == undefined) {
                return;
            }
            let params =  {}
            Object.assign(params, this.value.params);//shallow copy
            let advp = shouldUseAdvancedPreview({
                advancedPreviews: app.ui.settings.getSettingValue("VHS.AdvancedPreviews"),
                isInput,
                format: params.format,
            })
            params.timestamp = Date.now()
            this.parentEl.hidden = this.value.hidden;
            this.setStatus("")
            if (params.format?.split('/')[0] == 'video'
                || advp && (params.format?.split('/')[1] == 'gif')
                || params.format == 'folder') {

                this.videoEl.autoplay = !this.value.paused && !this.value.hidden;
                if (!advp) {
                    this.videoEl.src = api.apiURL('/view?' + new URLSearchParams(params));
                } else {
                    let target_width = (previewNode.size[0]-20)*2 || 256;
                    let minWidth = app.ui.settings.getSettingValue("VHS.AdvancedPreviewsMinWidth")
                    if (target_width < minWidth) {
                        target_width = minWidth
                    }
                    if (!params.custom_width || !params.custom_height) {
                        params.force_size = target_width+"x?"
                    } else {
                        let ar = params.custom_width/params.custom_height
                        params.force_size = target_width+"x"+(target_width/ar)
                    }
                    params.deadline = app.ui.settings.getSettingValue("VHS.AdvancedPreviewsDeadline")
                    this.videoEl.src = api.apiURL('/vhs/viewvideo?' + new URLSearchParams(params));
                }
                debugLog("video_preview_source", { advp, params, src: this.videoEl.src })
                this.videoEl.hidden = false;
                this.imgEl.hidden = true;
            } else if (params.format?.split('/')[0] == 'image'){
                //Is animated image
                this.imgEl.src = api.apiURL('/view?' + new URLSearchParams(params));
                debugLog("image_preview_source", { params, src: this.imgEl.src })
                this.videoEl.hidden = true;
                this.imgEl.hidden = false;
            } else {
                this.parentEl.hidden = true;
                this.setStatus("Preview format is not supported for this node state.")
                debugLog("preview_unsupported_format", { params })
            }
            delete previewNode.video_query
            const doQuery = async () => {
                if (!previewWidget?.value?.params?.filename) {
                    return
                }
                let qurl = api.apiURL('/vhs/queryvideo?' + new URLSearchParams(previewWidget.value.params))
                let query = undefined
                let query_res = undefined
                try {
                    query_res = await fetchWithOptionalAuth(qurl)
                    query = await query_res.json()
                } catch(e) {
                    previewWidget.setStatus("Preview metadata lookup failed.")
                    debugLog("video_query_failed", { url: qurl, error: String(e) })
                    return
                }
                if (!query_res.ok || query?.error) {
                    previewWidget.setStatus(query?.error || "Preview metadata unavailable.")
                    debugLog("video_query_error", { url: qurl, query })
                    return
                }
                previewNode.video_query = query
                debugLog("video_query", { url: qurl, query })
            }
            doQuery()
        }
        previewWidget.callback = previewWidget.updateSource
    });
}
let copiedPath = undefined
function addPreviewOptions(nodeType) {
    chainCallback(nodeType.prototype, "getExtraMenuOptions", function(_, options) {
        // The intended way of appending options is returning a list of extra options,
        // but this isn't used in widgetInputs.js and would require
        // less generalization of chainCallback
        let optNew = []
        const previewWidget = this.widgets.find((w) => w.name === "videopreview");

        let url = null
        if (previewWidget.videoEl?.hidden == false && previewWidget.videoEl.src) {
            if (['input', 'output', 'temp'].includes(previewWidget.value.params.type)) {
                //Use full quality video
                url = api.apiURL('/view?' + new URLSearchParams(previewWidget.value.params));
                //Workaround for 16bit png: Just do first frame
                url = url.replace('%2503d', '001')
            }
        } else if (previewWidget.imgEl?.hidden == false && previewWidget.imgEl.src) {
            url = previewWidget.imgEl.src;
            url = new URL(url);
        }
        if (this.video_query?.source) {
            let info_string = this.video_query.source.size.join('x') +
                              '@' + this.video_query.source.fps + 'fps ' +
                              this.video_query.source.frames + 'frames'
            optNew.push({content: info_string, disabled: true})
        }
        if (url) {
            optNew.push(
                {
                    content: "Open preview",
                    callback: () => {
                        window.open(url, "_blank")
                    },
                },
                {
                    content: "Save preview",
                    callback: () => {
                        const a = document.createElement("a");
                        a.href = url;
                        a.setAttribute("download", previewWidget.value.params.filename);
                        document.body.append(a);
                        a.click();
                        requestAnimationFrame(() => a.remove());
                    },
                }
            );
            if (previewWidget.value.params.fullpath) {
                const fullpath = previewWidget.value.params.fullpath
                const blob = new Blob([fullpath],
                    { type: 'text/plain'})
                optNew.push({
                    content: "Copy output filepath",
                    callback: async () => {
                        copiedPath = fullpath
                        await navigator.clipboard.write([
                            new ClipboardItem({
                                'text/plain': blob
                            })])}
                });
            }
            if (previewWidget.value.params.workflow) {
                let wParams = {...previewWidget.value.params,
                    filename: previewWidget.value.params.workflow}
                let wUrl = api.apiURL('/view?' + new URLSearchParams(wParams));
                optNew.push({
                    content: "Save workflow image",
                    callback: () => {
                        const a = document.createElement("a");
                        a.href = wUrl;
                        a.setAttribute("download", previewWidget.value.params.workflow);
                        document.body.append(a);
                        a.click();
                        requestAnimationFrame(() => a.remove());
                    }
                });
            }
        }
        const PauseDesc = (previewWidget.value.paused ? "Resume" : "Pause") + " preview";
        if(previewWidget.videoEl.hidden == false) {
            optNew.push({content: PauseDesc, callback: () => {
                //animated images can't be paused and are more likely to cause performance issues.
                //changing src to a single keyframe is possible,
                //For now, the option is disabled if an animated image is being displayed
                if(previewWidget.value.paused) {
                    previewWidget.videoEl?.play();
                } else {
                    previewWidget.videoEl?.pause();
                }
                previewWidget.value.paused = !previewWidget.value.paused;
            }});
        }
        //TODO: Consider hiding elements if no video preview is available yet.
        //It would reduce confusion at the cost of functionality
        //(if a video preview lags the computer, the user should be able to hide in advance)
        const visDesc = (previewWidget.value.hidden ? "Show" : "Hide") + " preview";
        optNew.push({content: visDesc, callback: () => {
            if (!previewWidget.videoEl.hidden && !previewWidget.value.hidden) {
                previewWidget.videoEl.pause();
            } else if (previewWidget.value.hidden && !previewWidget.videoEl.hidden && !previewWidget.value.paused) {
                previewWidget.videoEl.play();
            }
            previewWidget.value.hidden = !previewWidget.value.hidden;
            previewWidget.parentEl.hidden = previewWidget.value.hidden;
            fitHeight(this);

        }});
        optNew.push({content: "Sync preview", callback: () => {
            //TODO: address case where videos have varying length
            //Consider a system of sync groups which are opt-in?
            for (let p of document.getElementsByClassName("vhs_preview")) {
                for (let child of p.children) {
                    if (child.tagName == "VIDEO") {
                        child.currentTime=0;
                    } else if (child.tagName == "IMG") {
                        child.src = child.src;
                    }
                }
            }
        }});
        const muteDesc = (previewWidget.value.muted ? "Unmute" : "Mute") + " Preview"
        optNew.push({content: muteDesc, callback: () => {
            previewWidget.value.muted = !previewWidget.value.muted
        }})
        if(options.length > 0 && options[0] != null && optNew.length > 0) {
            optNew.push(null);
        }
        options.unshift(...optNew);
    });
}

    return {
        addAudioPreview,
        addVideoPreview,
        addPreviewOptions,
        getCopiedPath: () => copiedPath,
    }
}
