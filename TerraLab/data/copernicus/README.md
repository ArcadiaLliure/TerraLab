# Copernicus

Cliente y preparación de ortofotos Copernicus HR Image Mosaic.

- `planning.py`: selección geográfica y estimaciones.
- `client.py`: peticiones remotas.
- `manifest.py`: estado reanudable y metadatos.
- `validation.py`: comprobaciones de cobertura y resultado.
- `mosaic.py`: composición de teselas.
- `manager.py`: orquestación del caso de uso.

`__init__.py` publica la API estable; no existe un módulo monolítico paralelo.
