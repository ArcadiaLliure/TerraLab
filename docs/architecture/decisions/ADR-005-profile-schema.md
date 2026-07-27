# ADR-005: Esquema de perfil de terreno

## Status

Accepted

## Context

El perfil NPZ contenía arrays de horizonte, bandas y malla usados por procesos,
UI y render, con lectores tolerantes que podían ocultar datos incompatibles.

## Decision

`terrain/persistence/profile_npz.py` es el único propietario del esquema. El
payload incluye versión y arrays NumPy validados, se escribe mediante temporal
y reemplazo atómico y se carga con `allow_pickle=False`. Los modelos resultantes
son `HorizonProfile` y tipos de dominio.

## Consequences

Los fallos de esquema son explícitos y una cancelación no publica perfiles
parciales. Cualquier cambio incompatible requiere migración y prueba de
round-trip.

## Supersedes

Lectura y escritura dispersas en el engine, el worker y el overlay, y payloads
dependientes de objetos Python serializados.
