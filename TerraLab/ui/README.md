# Interfaz de usuario

Composición de la aplicación Qt.

`AstronomicalWidget` y `AstroCanvas` son las únicas clases canónicas del widget
principal y el lienzo. Sus responsabilidades se organizan en mixins con nombres
semánticos. Los workers Qt viven en `ui/workers`; el terreno se solicita
exclusivamente a través de `TerrainCoordinator`.

La importación de los módulos no crea un `QApplication`, archivos ni timers.
