# Copernicus

Client i preparació d'ortofotos Copernicus HR Image Mosaic.

- `planning.py`: selecció geogràfica i estimacions.
- `client.py`: peticions remotes.
- `manifest.py`: estat reprenable i metadades.
- `validation.py`: comprovacions de cobertura i resultat.
- `mosaic.py`: composició de tessel·les.
- `manager.py`: orquestració del cas d'ús.

`__init__.py` publica l'API estable; no existeix cap mòdul monolític paral·lel.
