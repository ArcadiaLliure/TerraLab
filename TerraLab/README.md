# Paquete `TerraLab`

Este directorio contiene todo el código que se distribuye como el paquete
Python `TerraLab`. Los datos científicos voluminosos, las herramientas de
desarrollo y los artefactos de benchmark viven fuera del paquete.

## Mapa del paquete

| Paquete | Responsabilidad |
| --- | --- |
| `astro` | Cálculo analítico, catálogos NGC y búsqueda |
| `cli` | Entradas de línea de comandos |
| `common` | Rutas, configuración, cachés y cancelación |
| `data` | Biblioteca de datos, recursos y catálogos |
| `debug` | Diagnóstico explícito de ejecución |
| `layers` | Capas visuales auxiliares |
| `light_pollution` | Modos y procesamiento de luminosidad |
| `render` | Renderizadores del cielo y contexto Qt |
| `scene` | Cámara, proyección y estado de escena |
| `terrain` | Cálculo y representación del terreno |
| `ui` | Widgets de aplicación, diálogos y workers |
| `util` | Conversiones científicas reutilizables |
| `weather` | Modelo y presentación meteorológica |
| `widgets` | Controles interactivos especializados |

## Entradas canónicas

- `python -m TerraLab` llama a `TerraLab.__main__:main`.
- `ui.astronomical_widget.AstronomicalWidget` es el widget principal.
- `ui.astro_canvas.AstroCanvas` es el lienzo astronómico.
- `terrain.terrain_coordinator.TerrainCoordinator` es el único propietario en
  runtime del worker de horizonte.
- `data.assets_manager.AssetManager` coordina la biblioteca de recursos.

No deben introducirse módulos numerados, copias dinámicas de namespaces ni
implementaciones paralelas de estas entradas. Los contratos están en
`tests/architecture`.
