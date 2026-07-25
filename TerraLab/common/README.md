# Infraestructura común

Primitivas compartidas que no pertenecen a un dominio científico concreto.

- `cache.py`: única implementación de `ByteLRU`.
- `cancellation.py`: generaciones y cancelación cooperativa.
- `data_library.py`: raíz y disposición de la biblioteca del usuario.
- `app_paths.py`: resolución de rutas sin escrituras al importar.
- `performance/`: presupuesto, flags y memoria.
- `exception_reporting.py`: diagnóstico de operaciones best-effort no fatales.

Evítese convertir este paquete en un contenedor de dependencias de la UI.
