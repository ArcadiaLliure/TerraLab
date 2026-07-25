# Workers de render

Trabajo de rasterización costoso ejecutado fuera del hilo de interfaz.

`star_render.py` genera estrellas y trazas a partir de parámetros inmutables y
devuelve imágenes Qt. NumPy es una dependencia obligatoria; no existe una
implementación degradada silenciosa.
