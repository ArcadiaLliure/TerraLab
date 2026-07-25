# Render puro de terreno

Pipeline numérico de geometría proyectada y materiales.

- `config.py` y `sampling.py`: configuración serializable.
- `overlay_types.py`: valores inmutables.
- `geometry.py`: simplificación y cobertura.
- `palette.py` y `materials.py`: color y composición.
- `lighting.py` y `atmosphere.py`: iluminación y profundidad.
- `triangle_raster.py`: z-buffer vectorizado.

No abre rásteres y no copia namespaces entre módulos.
