# Workers de render

Tasca de rasterització costosa executada fora del fil de la interfície.

`star_render.py` genera estrelles i traces a partir de paràmetres immutables i
retorna imatges Qt. NumPy és una dependència obligatòria; no existeix cap
implementació degradada silenciosa.
