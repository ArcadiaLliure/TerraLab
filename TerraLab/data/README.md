# Datos

Acceso a recursos científicos, catálogo de capas y persistencia de datos de
ejecución.

`assets_manager.py` es la fachada estable para instalación y retirada de
recursos. `layer_manager.py` coordina el estado visible. Los subpaquetes
`assets`, `catalogs`, `converters` y `copernicus` contienen responsabilidades
independientes.

Este paquete no depende de `TerraLab.ui`.
