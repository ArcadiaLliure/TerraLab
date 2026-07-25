# Render

Renderizadores del cielo y adaptadores de dibujo.

Cada capa tiene una entrada canónica: fondo, estrellas, rejilla, horizonte y
overlays. Las funciones públicas no mantienen una implementación `_impl`
paralela. El estado llega mediante `SceneState` y el contexto Qt mediante
`render.qt.RenderContext`.
