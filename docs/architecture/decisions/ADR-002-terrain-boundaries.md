# ADR-002: Límites del subsistema de terreno

## Status

Accepted

## Context

Dominio, acceso GDAL, raycast, persistencia, superficie y pintura Qt estaban
mezclados en varios módulos monolíticos y se comunicaban mediante nombres
copiados dinámicamente.

## Decision

El terreno se divide en paquetes dirigidos:

```text
domain ← providers/infrastructure ← raycast/mesh ← services
domain + surface + render puro ← overlay Qt
domain ← persistence
```

El dominio no depende de Qt, GDAL ni red. Los providers forman un grafo
acíclico. El render puro recibe arrays y valores inmutables; la E/S se completa
antes. `TerrainCoordinator` posee el único `HorizonWorker`.

## Consequences

Las fronteras se verifican por AST. Los tipos compartidos viven en módulos de
dominio o `render/overlay_types.py`, y no se reexportan mediante mutación de
`globals()`.

## Supersedes

`terrain/engine.py`, `providers.py`, `surface.py`, `render_pipeline.py` y el
overlay monolítico como propietarios múltiples de responsabilidades.
