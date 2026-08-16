// Excalidraw — exposed as window.CarrotCanvas (+ excalidraw.css)
//
// MIT, and that is why it is here rather than tldraw. tldraw is the better SDK
// and it is not free: its licence restricts use to development environments,
// requires a paid business licence to ship, and enforces a watermark. Carrot
// is distributed software, so that is a licence Carrot cannot honour by
// existing. Excalidraw does the same job — infinite canvas, shapes, arrows,
// text, freehand, images — under a licence that lets it ship.
//
// React comes with it. That is the cost of any real canvas library on the web
// today, and it buys a surface it would take months to hand-roll badly.
import React from 'react';
import { createRoot } from 'react-dom/client';
import { Excalidraw, exportToBlob, getSceneVersion } from '@excalidraw/excalidraw';
import '@excalidraw/excalidraw/index.css';

// A tiny façade, so nothing outside this file has to know React exists. The
// app mounts a canvas, gets told when it changes, and reads or writes a scene
// — that is the whole contract.
window.CarrotCanvas = {
    mount(el, { initialData, onChange, theme = 'dark' } = {}) {
        const root = createRoot(el);
        let api = null;
        root.render(
            React.createElement(Excalidraw, {
                initialData,
                theme,
                excalidrawAPI: (instance) => { api = instance; },
                onChange: (elements, appState, files) => {
                    if (onChange) onChange({ elements, appState, files });
                },
                UIOptions: {
                    // Excalidraw's own save/load talks to .excalidraw files on
                    // disk. A canvas here is a document Carrot already stores
                    // and autosaves, so its file menu would be a second, wrong
                    // way to do that.
                    canvasActions: { loadScene: false, saveToActiveFile: false, export: false },
                },
            }),
        );
        return {
            get api() { return api; },
            sceneVersion: (elements) => getSceneVersion(elements),
            exportToBlob,
            destroy: () => root.unmount(),
        };
    },
};
