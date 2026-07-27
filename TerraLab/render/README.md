# Render

Renderitzadors del cel i adaptadors de dibuix.

Cada capa té una entrada canònica: fons, estrelles, graella, horitzó i
overlays. Les funcions públiques no mantenen una implementació `_impl`
paral·lela. L'estat arriba mitjançant `SceneState` i el context Qt mitjançant
`render.qt.RenderContext`.
