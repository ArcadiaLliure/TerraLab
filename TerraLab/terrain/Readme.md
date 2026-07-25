# Terreno

El DEM define geometría y elevación. La ortofoto o cobertura del suelo define
la apariencia. Ambas fuentes se relacionan mediante coordenadas geográficas,
nunca mediante posiciones de píxel supuestamente coincidentes.

## Flujo canónico

```text
catálogo tipado
    ↓
providers/ ──→ raycast/ ──→ domain/HorizonProfile
    ↓                         ↓
surface/ ───────────────→ mesh/ y render/
                              ↓
                   overlay.py (controlador Qt)
```

## Paquetes

| Paquete | Responsabilidad |
| --- | --- |
| `domain` | Perfiles, bandas y valores inmutables |
| `infrastructure` | Índices y cachés de teselas DEM |
| `providers` | Lectura tipada de ASC, NPY y GDAL |
| `raycast` | Muestreo radial y construcción del horizonte |
| `mesh` | Malla polar, elevaciones y normales |
| `surface` | Ortofoto, cobertura categórica y caché de muestras |
| `render` | Geometría proyectada, materiales, luz y rasterización |
| `overlay_mixins` | Responsabilidades del controlador de dibujo Qt |
| `persistence` | Esquema NPZ del perfil |
| `services` | Consultas de elevación y casos de uso |

`TerrainCoordinator` es el único propietario del `HorizonWorker`. El proceso
de bake publica progreso y resultados tipados, y la UI nunca mantiene un
segundo worker o thread paralelo.

## Invariantes

- `terrain/domain` no depende de Qt, red ni GDAL.
- Los proveedores no importan la UI.
- El render puro no abre rásteres.
- La superficie puede activarse o desactivarse sin alterar la geometría.
- Quitar el relieve invalida la imagen cacheada y vuelve a dibujar la
  superficie plana.
- El perfil se carga con `allow_pickle=False`.

Los detalles de representación están en
[`docs/categorical-surface-pipeline.md`](../../docs/categorical-surface-pipeline.md)
y las decisiones en
[`docs/architecture/decisions`](../../docs/architecture/decisions).
