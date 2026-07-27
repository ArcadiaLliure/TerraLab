# ADR-001: Runtime UI canónico

## Status

Accepted

## Context

El widget y el canvas nuevos heredaban de una implementación monolítica y
ejecutaban coordinadores, timers y rutas de render en paralelo con el flujo
legacy.

## Decision

`AstronomicalWidget` hereda directamente de `CustomWidgetBase` y
`AstroCanvas` de `QWidget`. Sus responsabilidades se componen mediante mixins
semánticos. Existe un único timer de frame y el terreno se solicita sólo a
`TerrainCoordinator`. No se admiten bridges al runtime retirado, módulos
`part_N` ni agregadores que copien namespaces.

## Consequences

El orden de ciclo de vida es explícito y las pruebas pueden importar las
clases sin activar la aplicación. Los mixins deben declarar sus dependencias y
los cambios de estado compartido requieren revisar el propietario canónico.

## Supersedes

La herencia desde `ui/sky_widget_impl.py`, `widgets/sky_widget.py`,
`widgets/sky_legacy_components.py`, `ui/sky_runtime.py` y sus bridges.
