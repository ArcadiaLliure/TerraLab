# Proveïdors d'elevació

API tipada per a fonts ASC, NPY i formats GDAL.

- `common.py`: contractes, metadades i funcions auxiliars de font.
- `raster_dataset.py`: accés GDAL per blocs i memòria cau acotada.
- `asc_provider.py`: tessel·les històriques ASC/NPY.
- `geotiff_provider.py`: proveïdors GeoTIFF.
- `chain.py`: composició i fàbriques.

Els mòduls utilitzen importacions explícites i formen un graf acíclic.
