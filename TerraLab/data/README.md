# Dades

Accés a recursos científics, catàleg de capes i persistència de dades
d'execució.

`assets_manager.py` és la façana estable per instal·lar i retirar recursos.
`layer_manager.py` coordina l'estat visible. Els subpaquets `assets`,
`catalogs`, `converters` i `copernicus` contenen responsabilitats independents.

Aquest paquet no depèn de `TerraLab.ui`.
