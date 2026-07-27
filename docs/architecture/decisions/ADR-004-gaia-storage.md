# ADR-004: Almacenamiento de Gaia

## Status

Accepted

## Context

Catálogos NPZ, JSON suplementarios, cachés de scope y descargas convivían con
varios loaders y podían forzar cargas completas en memoria.

## Decision

Los catálogos de runtime usan arrays NPY/memmap y bundles de scope con firma de
dataset. `data/catalogs` posee descubrimiento, fusión y caché. Los procesos de
importación viven en `util`/`cli`, trabajan por lotes y publican resultados
atómicos. Los lectores usan `allow_pickle=False`.

## Consequences

El modo general y el telescópico comparten una fuente canónica sin duplicar
todo el catálogo en RAM. Un cambio de dataset invalida su bundle derivado.

## Supersedes

Loaders y cachés contenidos en `sky_legacy_components.py` y herramientas Gaia
productivas dentro de `TerraLab.tools`.
