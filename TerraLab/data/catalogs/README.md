# Catálogos astronómicos

Descubrimiento, carga y caché de catálogos de estrellas.

`constants.py` es el único propietario de los límites compartidos.
`star_catalog.py` transforma y fusiona datos; `scope_cache.py` conserva bundles
`mmap` para el modo telescópico. No se permite `allow_pickle=True`.
