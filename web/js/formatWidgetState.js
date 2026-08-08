function widgetType(definition) {
    const configuredType = definition[2]?.widgetType ?? definition[1]
    return Array.isArray(configuredType) ? "COMBO" : configuredType
}

function defaultValue(definition) {
    const configuredDefault = definition[2]?.default
    if (configuredDefault !== undefined) {
        return configuredDefault
    }
    if (Array.isArray(definition[1])) {
        return definition[1][0]
    }
    return {BOOLEAN: false, INT: 0, FLOAT: 0, STRING: ""}[definition[1]]
}

function applyDefinition(widget, definition) {
    widget.config = definition.slice(1)
    widget.options ??= {}
    if (Array.isArray(definition[1])) {
        widget.options.values = [...definition[1]]
    } else {
        delete widget.options.values
    }
    if (definition[2] && typeof definition[2] === "object") {
        Object.assign(widget.options, definition[2])
    }
    widget.hidden = false
}

function findWidget(node, name) {
    return node.widgets?.find((widget) => widget.name === name)
}

function createWidgetOnlyFallback(node, definition, app) {
    const type = widgetType(definition)
    const factory = app?.widgets?.[type]
    if (!factory) {
        throw new Error(`No widget factory is available for format field ${definition[0]} (${type})`)
    }
    const result = factory(node, definition[0], definition.slice(1), app)
    return result?.widget ?? node.widgets?.[node.widgets.length - 1]
}

export function reconcileFormatWidgets(
    node,
    formatWidget,
    formats,
    app,
    state = {},
    warn = (message) => console.warn(message),
) {
    state.values ??= {}
    state.warnedDynamicNames ??= new Set()

    const nextFormat = formatWidget.value
    const nextDefinitions = formats?.[nextFormat] ?? []
    const allNames = new Set(
        Object.values(formats ?? {}).flatMap((definitions) =>
            definitions.map((definition) => definition[0])
        )
    )

    if (state.activeFormat !== undefined) {
        state.values[state.activeFormat] ??= {}
        for (const definition of formats?.[state.activeFormat] ?? []) {
            const widget = findWidget(node, definition[0])
            if (widget) {
                state.values[state.activeFormat][definition[0]] = widget.value
            }
        }
    }

    for (const widget of node.widgets ?? []) {
        if (allNames.has(widget.name)) {
            widget.hidden = true
        }
    }

    const activeWidgets = []
    const dynamicNames = []
    for (const definition of nextDefinitions) {
        let widget = findWidget(node, definition[0])
        if (!widget) {
            widget = createWidgetOnlyFallback(node, definition, app)
            dynamicNames.push(definition[0])
            if (!state.warnedDynamicNames.has(definition[0])) {
                // IMPORTANT: never synthesize a socket with private ComfyUI widget metadata here.
                warn(`[VHS] Format field ${definition[0]} is using a widget-only fallback because no compatible backend schema input exists.`)
                state.warnedDynamicNames.add(definition[0])
            }
        }
        if (!widget) {
            throw new Error(`Widget factory did not create format field ${definition[0]}`)
        }

        applyDefinition(widget, definition)
        const cachedValue = state.values[nextFormat]?.[definition[0]]
        if (cachedValue !== undefined) {
            widget.value = cachedValue
        } else if (state.activeFormat !== undefined && state.activeFormat !== nextFormat) {
            widget.value = defaultValue(definition)
        }
        activeWidgets.push(widget)
    }

    const activeSet = new Set(activeWidgets)
    const reordered = (node.widgets ?? []).filter((widget) => !activeSet.has(widget))
    const formatIndex = reordered.indexOf(formatWidget)
    reordered.splice(formatIndex >= 0 ? formatIndex + 1 : reordered.length, 0, ...activeWidgets)
    node.widgets = reordered
    state.activeFormat = nextFormat

    return {
        activeNames: activeWidgets.map((widget) => widget.name),
        dynamicNames,
    }
}
