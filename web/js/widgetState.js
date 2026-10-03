function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value)
}

function serializableWidgets(widgets) {
    return widgets.filter(widget => widget.type !== "button"
        && widget.serialize !== false && widget.options?.serialize !== false)
}

export function prepareWidgetConfiguration(info) {
    if (!isRecord(info?.widgets_values)) return info
    // CRITICAL: hosts may compact keyed values before onConfigure; retain names before that call.
    return {
        ...info,
        widgets_values_named: {
            ...info.widgets_values,
            ...(isRecord(info.widgets_values_named) ? info.widgets_values_named : {}),
        },
    }
}

export function resolveWidgetState(type, widgets, info, conversion) {
    if (isRecord(info.widgets_values_named)) return structuredClone(info.widgets_values_named)
    const values = info.widgets_values
    if (isRecord(values)) return structuredClone(values)
    if (!Array.isArray(values)) return undefined
    const serialized = serializableWidgets(widgets)
    // IMPORTANT: compact arrays exclude controls; the historical schema would shift real values.
    if (values.length === serialized.length) {
        return Object.fromEntries(serialized.map((widget, index) => [widget.name, structuredClone(values[index])]))
    }
    if (conversion && values.length >= conversion.length) {
        return Object.fromEntries(conversion.flatMap((name, index) => name ? [[name, structuredClone(values[index])]] : []))
    }
    if (values.length === widgets.length) {
        return Object.fromEntries(widgets.map((widget, index) => [widget.name, structuredClone(values[index])]))
    }
    return undefined
}
