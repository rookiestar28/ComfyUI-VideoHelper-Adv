export function formatDate(text, date) {
    const parts = {
        d: (value) => value.getDate(),
        M: (value) => value.getMonth() + 1,
        h: (value) => value.getHours(),
        m: (value) => value.getMinutes(),
        s: (value) => value.getSeconds(),
    }
    const pattern = `${Object.keys(parts).map((key) => `${key}${key}?`).join("|")}|yyy?y?`
    return text.replace(new RegExp(pattern, "g"), (token) => {
        if (token === "yy") return `${date.getFullYear()}`.substring(2)
        if (token === "yyyy") return `${date.getFullYear()}`
        if (token[0] in parts) {
            return `${parts[token[0]](date)}`.padStart(token.length, "0")
        }
        return token
    })
}

function collectAllNodes(graph, output = [], visited = new Set()) {
    if (!graph || visited.has(graph)) {
        return output
    }
    visited.add(graph)
    const nodes = graph._nodes ?? graph.nodes ?? []
    for (const node of nodes) {
        output.push(node)
        if (node.subgraph) {
            collectAllNodes(node.subgraph, output, visited)
        }
    }
    return output
}

export function applyTextReplacements(graph, value, date = new Date()) {
    // IMPORTANT: keep this helper ComfyUI-internal-free; internal imports recreate legacy API warnings.
    const allNodes = collectAllNodes(graph)
    return value.replace(/%([^%]+)%/g, (match, text) => {
        const split = text.split(".")
        if (split.length !== 2) {
            if (split[0].startsWith("date:")) {
                return formatDate(split[0].substring(5), date)
            }
            if (text !== "width" && text !== "height") {
                console.warn("Invalid replacement pattern", text)
            }
            return match
        }

        let nodes = allNodes.filter(
            (node) => node.properties?.["Node name for S&R"] === split[0]
        )
        if (!nodes.length) {
            nodes = allNodes.filter((node) => node.title === split[0])
        }
        if (!nodes.length) {
            console.warn("Unable to find node", split[0])
            return match
        }
        if (nodes.length > 1) {
            console.warn("Multiple nodes matched", split[0], "using first match")
        }

        const widget = nodes[0].widgets?.find((candidate) => candidate.name === split[1])
        if (!widget) {
            console.warn("Unable to find widget", split[1], "on node", split[0], nodes[0])
            return match
        }
        return `${widget.value ?? ""}`.replaceAll(
            // eslint-disable-next-line no-control-regex
            /[/?<>\\:*|"\x00-\x1F\x7F]/g,
            "_",
        )
    })
}
