# Workers de UI

Adaptadores `QObject` para carga asíncrona de catálogos y efemérides.

Los workers emiten resultados mediante señales y no construyen widgets. NumPy
y Skyfield son dependencias obligatorias declaradas en `pyproject.toml`; no se
ocultan errores de instalación con fallbacks incompletos.
