function chain(object, property, callback) {
    const original = object?.[property]
    object[property] = original
        ? function (...args) {
            const result = original.apply(this, args)
            return callback.apply(this, args) ?? result
        }
        : callback
}

export function configureSelectLatestNode(node, {
    app,
    api,
    fetchWithOptionalAuth,
    pathStem,
    chainCallback = chain,
}) {
    node.isVirtualNode = true

    function collectLinks(source, visited = new Set()) {
        if (!source || visited.has(source)) return []
        visited.add(source)
        const links = []
        for (const linkId of source.outputs?.[0]?.links ?? []) {
            const linkInfo = source.graph?.links?.[linkId]
            const target = source.graph?.getNodeById?.(linkInfo?.target_id)
            if (!linkInfo || !target) continue
            if (target.type === "Reroute") links.push(...collectLinks(target, visited))
            else links.push(linkInfo)
        }
        return links
    }

    node.apply_value_to_links = function (value, extraLinks = []) {
        const links = [...collectLinks(this), ...extraLinks]
        for (const linkInfo of links) {
            const target = this.graph?.getNodeById?.(linkInfo.target_id)
            const input = target?.inputs?.[linkInfo.target_slot]
            const widgetName = input?.widget?.name
            const widget = target?.widgets?.find?.((candidate) => candidate.name === widgetName)
            if (!widget) continue
            widget.value = value
            widget.callback?.(widget.value, app.canvas, target, app.canvas.graph_mouse, {})
        }
    }

    node.update_links = function (extraLinks = []) {
        if (!this.latest_file) return
        this.apply_value_to_links(this.latest_file, extraLinks)
    }

    node.clear_links = function () {
        this.apply_value_to_links("")
    }

    chainCallback(node, "onConnectionsChange", function (_type, _slot, connected) {
        if (connected) this.update_links()
    })

    const fetchFiles = async () => {
        const [path, remainder] = pathStem(node.widgets?.[0]?.value ?? "")
        const params = new URLSearchParams({ path })
        const optionsURL = api.apiURL("/vhs/getpath?" + params)
        try {
            const response = await fetchWithOptionalAuth(optionsURL)
            const payload = response?.ok === false ? [] : await response.json()
            const options = Array.isArray(payload)
                ? payload.filter((file) => (
                    typeof file === "string"
                    && file.startsWith(remainder)
                    && file.endsWith(node.widgets?.[1]?.value ?? "")
                ))
                : []
            const latest = options.length ? path + options[options.length - 1] : undefined
            node.latest_file = latest
            if (latest) node.update_links()
            else node.clear_links()
        } catch (_error) {
            node.latest_file = undefined
            node.clear_links()
        }
    }

    if (node.widgets?.[0]) node.widgets[0].callback = fetchFiles
    if (node.widgets?.[1]) node.widgets[1].callback = fetchFiles
    node.onPromptExecuted = fetchFiles
    node.applyToGraph = node.update_links
    return node
}
