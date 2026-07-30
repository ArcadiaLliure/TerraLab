# Fase 03 — SceneFrame tipado y constructor puro

## Resultado

Se extrajo la construcción de la escena de la UI a DTOs inmutables y a un
`SceneFrameBuilder` puro. La UI continúa adaptando eventos y estado Qt, mientras
que el protocolo JSONL v1 y el rasterizador QPainter permanecen compatibles.
No se ha iniciado la migración de la capa visual.

## Cambios de responsabilidad MVC

| Ruta | Antes | Después | Cambio verificable |
| --- | --- | --- | --- |
| `TerraLab/scene/contracts.py` | MODEL | MODEL | DTOs finitos, inmutables y sin Qt; el encoder/decoder v1 sólo aparece en el borde de compatibilidad. |
| `TerraLab/application/commands.py` | — | CONTROLLER | Intenciones y snapshots de entrada explícitos para construir una escena. |
| `TerraLab/application/scene_builder.py` | — | CONTROLLER | Decisiones de capas, Bortle, alcance, terreno y presentación puras. |
| `TerraLab/ui/astro_canvas.py` | MIXED | MIXED | Adaptador Qt delgado: publica entradas tipadas y no decide Bortle ni visibilidad Earth. |
| `TerraLab/ui/frame_presenter.py` | VIEW | VIEW | Acepta `SceneFrame` y serializa el contrato v1 sin cambiar el transporte. |
| `TerraLab/runtime/protocol.py` | OUTSIDE_MVC | OUTSIDE_MVC | Adaptador de compatibilidad `SceneFrame` ⇄ JSONL v1. |
| `TerraLab/runtime/render_service.py` | OUTSIDE_MVC | OUTSIDE_MVC | Decodifica a `SceneFrame` antes de enviar al backend; la temporización de raster permanece aislada. |
| `TerraLab/render/qpainter/backend.py` | VIEW | VIEW | Reutiliza el renderer heredado mediante el encoder v1. |

El manifiesto MVC regenerado queda en
`docs/architecture/mvc_file_roles.json`: MODEL 59, VIEW 31, CONTROLLER 9,
OUTSIDE_MVC 91 y MIXED 56 archivos.

## Contrato y rendimiento

El benchmark puro de 1.000 repeticiones está en
`benchmarks/scene_frame_builder_20260729T130000Z.json`:

- construcción: P50 `0,023 ms`, P95 `0,029305 ms`;
- codificación v1: P50 `0,0132 ms`, P95 `0,01621 ms`;
- payload estable de `2542 bytes`;
- no hay `deepcopy` y se preservan las identidades de los recursos publicados.

La comprobación visual automatizada se documenta en
[`visual-parity.md`](visual-parity.md). Los 10 frames estáticos coinciden
exactamente; la única escena no exacta mantiene el pulso temporal ya declarado.

## Verificación automática

- `python -m pytest -q`: **648 passed** en 75,96 s.
- Pruebas focalizadas del builder, protocolo, proceso de render y regresiones
  funcionales: **60 passed**.
- Ruff, `compileall`, construcción del paquete, regeneración/verificación MVC y
  barrera Pyright: correctos. Pyright queda en 2328 errores y 5 avisos, por
  debajo de la barrera documentada de 2362/9.

## Rollback

La reversión se limita a los archivos de fase 03: `scene/contracts.py`, los dos
módulos `application`, el adaptador de protocolo y las llamadas de UI/runtime/
backend que los usan. No hay migración persistente, caché ni cambio de versión
del protocolo: el JSONL v1, el pool de memoria compartida y el renderer legado
siguen siendo el punto de compatibilidad.

## Aceptación manual pendiente

Falta observar la aplicación de escritorio real siguiendo la lista de
`../baseline/README.md`: pan/zoom, tiempo, scope, capas, terreno, cambio de
tamaño, cierre y reinicio. Esta evidencia no puede sustituirse con hashes de
imágenes; por ello la fase permanece pendiente de confirmación humana pese a
que todos los controles automáticos han pasado.
