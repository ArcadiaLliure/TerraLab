# Render pur de terreny

Pipeline numèric de geometria projectada i materials.

- `config.py` i `sampling.py`: configuració serialitzable.
- `overlay_types.py`: valors immutables.
- `geometry.py`: simplificació i cobertura.
- `palette.py` i `materials.py`: color i composició.
- `lighting.py` i `atmosphere.py`: il·luminació i profunditat.
- `triangle_raster.py`: z-buffer vectoritzat.

No obre ràsters i no copia namespaces entre mòduls.
