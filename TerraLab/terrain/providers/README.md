# Proveedores de elevación

API tipada para fuentes ASC, NPY y formatos GDAL.

- `common.py`: contratos, metadatos y helpers de fuente.
- `raster_dataset.py`: acceso GDAL por bloques y caché acotada.
- `asc_provider.py`: teselas históricas ASC/NPY.
- `geotiff_provider.py`: proveedores GeoTIFF.
- `chain.py`: composición y factorías.

Los módulos usan imports explícitos y forman un grafo acíclico.
