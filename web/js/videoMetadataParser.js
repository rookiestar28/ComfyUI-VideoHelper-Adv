function normalizeParsedMetadata(parsed) {
    if (!parsed || typeof parsed !== "object") {
        return null;
    }
    if (parsed.workflow) {
        return parsed;
    }
    if (parsed.nodes || parsed.links) {
        return { workflow: parsed };
    }
    return null;
}

function parseMetadataJson(content) {
    try {
        return normalizeParsedMetadata(JSON.parse(content));
    } catch (_error) {
        return null;
    }
}

export function parseWebmMetadataBuffer(arrayBuffer) {
    const videoData = new Uint8Array(arrayBuffer);
    const dataView = new DataView(videoData.buffer, videoData.byteOffset, videoData.byteLength);
    const decoder = new TextDecoder();
    let offset = 4 + 8;
    while (offset < videoData.length - 16) {
        if (dataView.getUint16(offset) === 0x4487) {
            const name = String.fromCharCode(...videoData.slice(offset - 7, offset));
            if (name === "COMMENT") {
                const vint = dataView.getUint32(offset + 2);
                const nOctets = Math.clz32(vint) + 1;
                if (nOctets < 4) {
                    // IMPORTANT: a one-octet EBML size starts with 0x80; signed shift makes valid small tags negative.
                    const length = (vint >>> (8 * (4 - nOctets))) & ~(1 << (7 * nOctets));
                    const content = decoder.decode(videoData.slice(offset + 2 + nOctets, offset + 2 + nOctets + length));
                    return parseMetadataJson(content);
                }
            }
        }
        offset += 1;
    }
    return null;
}

export function parseMp4MetadataBuffer(arrayBuffer) {
    const videoData = new Uint8Array(arrayBuffer);
    const dataView = new DataView(videoData.buffer, videoData.byteOffset, videoData.byteLength);
    const decoder = new TextDecoder();
    const findAtomStart = (atomType) => {
        for (let offset = 4; offset <= videoData.length - 4; offset += 1) {
            if (dataView.getUint32(offset) === atomType) {
                const atomStart = offset - 4;
                const atomSize = dataView.getUint32(atomStart);
                if (atomSize >= 8 && atomStart + atomSize <= videoData.length) {
                    return atomStart;
                }
            }
        }
        return -1;
    };

    const keysStart = findAtomStart(0x6b657973); // keys
    const ilstStart = findAtomStart(0x696c7374); // ilst
    if (keysStart >= 0 && ilstStart >= 0) {
        const keysEnd = keysStart + dataView.getUint32(keysStart);
        let keyOffset = keysStart + 16;
        const entryCount = dataView.getUint32(keysStart + 12);
        let commentIndex = null;
        for (let index = 1; index <= entryCount && keyOffset + 8 <= keysEnd; index += 1) {
            const entrySize = dataView.getUint32(keyOffset);
            if (entrySize < 8 || keyOffset + entrySize > keysEnd) {
                break;
            }
            const namespace = dataView.getUint32(keyOffset + 4);
            if (namespace === 0x6d647461) { // mdta
                const name = decoder.decode(videoData.slice(keyOffset + 8, keyOffset + entrySize));
                if (name === "comment") {
                    commentIndex = index;
                    break;
                }
            }
            keyOffset += entrySize;
        }
        if (commentIndex !== null) {
            const ilstEnd = ilstStart + dataView.getUint32(ilstStart);
            let itemOffset = ilstStart + 8;
            while (itemOffset + 8 <= ilstEnd) {
                const itemSize = dataView.getUint32(itemOffset);
                if (itemSize < 8 || itemOffset + itemSize > ilstEnd) {
                    break;
                }
                if (dataView.getUint32(itemOffset + 4) === commentIndex) {
                    let dataOffset = itemOffset + 8;
                    while (dataOffset + 8 <= itemOffset + itemSize) {
                        const dataSize = dataView.getUint32(dataOffset);
                        if (dataSize < 16 || dataOffset + dataSize > itemOffset + itemSize) {
                            break;
                        }
                        if (dataView.getUint32(dataOffset + 4) === 0x64617461) { // data
                            const content = decoder.decode(videoData.slice(dataOffset + 16, dataOffset + dataSize));
                            return parseMetadataJson(content);
                        }
                        dataOffset += dataSize;
                    }
                }
                itemOffset += itemSize;
            }
        }
    }

    let offset = videoData.length - 4;
    while (offset > 16) {
        if (dataView.getUint32(offset) === 0x64617461 && dataView.getUint32(offset - 8) === 0xa9636d74) {
            const size = dataView.getUint32(offset - 4) - 16;
            const content = decoder.decode(videoData.slice(offset + 12, offset + 12 + size));
            return parseMetadataJson(content);
        }
        offset -= 1;
    }
    return null;
}

export function parseVideoMetadataBuffer(arrayBuffer) {
    const videoData = new Uint8Array(arrayBuffer);
    const dataView = new DataView(videoData.buffer, videoData.byteOffset, videoData.byteLength);
    if (videoData.length < 12) {
        return null;
    }
    if (dataView.getUint32(0) === 0x1A45DFA3) {
        return parseWebmMetadataBuffer(arrayBuffer);
    }
    if (dataView.getUint32(4) === 0x66747970 && dataView.getUint32(8) === 0x69736F6D) {
        return parseMp4MetadataBuffer(arrayBuffer);
    }
    return null;
}
