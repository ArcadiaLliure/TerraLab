# Fase 04 — fondo de cielo como slice MVC

## Resultado

El fondo físico del cielo se ha migrado como el primer slice visible completo:
un plan inmutable y Qt-free del Model, un adaptador QPainter de Vista y una
selección reversible de Controller. La ruta `scene` es el default; `legacy`
permanece disponible durante la siguiente fase como rollback.

## Nombres y fronteras MVC

Los nombres nuevos hacen explícita la responsabilidad: `SkyBackgroundPlanner`,
`SkyBackgroundPlan`, `QPainterSkyBackgroundAdapter`,
`LegacySkyLayerAdapter`, `LegacyHorizonLayerAdapter`,
`paint_layers`, `paint_layer` y `paint_legacy_ground_mask`. No se realizó una
renombrada global fuera de la capability de fase 04.

| Archivo | Rol antes | Rol después | Responsabilidad conservada | Responsabilidad extraída | Prueba de frontera |
| --- | --- | --- | --- | --- | --- |
| `scene/plans/__init__.py` | — | MODEL | Límite público de planes puros | Ninguna | Import de planner sin Qt |
| `scene/plans/sky.py` | — | MODEL | Fórmula RGBA, sampling, clip y cache token | Fórmula desde Vista/runtime | AST sin Qt/View/runtime |
| `application/sky_background.py` | — | CONTROLLER | Resolución reversible `legacy|scene` | Selección desde renderer | Prueba de flag y error tipado |
| `render/qpainter/sky.py` | — | VIEW | RGBA resuelto → QImage/QPainter | Ninguna | AST sin astronomía, Bortle ni proyección |
| `render/sky_renderer.py` | MIXED | VIEW | Composición QPainter legacy restante | Fórmula de cielo y nombre ambiguo | `LegacySkyLayerAdapter.paint_layers` |
| `render/horizon_renderer.py` | MIXED | VIEW | Callback y máscara legacy de Vista | Nombre ambiguo | `LegacyHorizonLayerAdapter.paint_layer` |
| `runtime/offscreen_renderer.py` | MIXED | MIXED | Orquestación legacy de las otras capas | Fondo de cielo a plan/adaptador | Paridad scene/legacy píxel a píxel |
| `application/ports/rendering.py` | CONTROLLER | CONTROLLER | Puerto de backend | Capability `SKY_BACKGROUND` explícita | Selección de backend |
| `render/qpainter/backend.py` | VIEW | VIEW | Backend QPainter | Publicación de capability | Registro de capability |
| `bootstrap/composition.py` | OUTSIDE_MVC | OUTSIDE_MVC | Wiring concreto | Requisito de capability | Test de composición |

El manifiesto regenerado queda en MODEL 61, VIEW 34, CONTROLLER 10,
OUTSIDE_MVC 91 y MIXED 54. `MIXED` disminuye de 56 a 54; no se ha creado ningún
archivo `MIXED`.

## Contrato y rollback

`SkyBackgroundPlan` contiene muestras RGBA read-only, máscara de proyección y
un token de caché basado exclusivamente en viewport, cámara, Sol, Bortle,
eclipse e interacción. `QPainterSkyBackgroundAdapter` no consulta canvas,
configuración, astronomía ni contaminación lumínica.

La reversión verificada es:

```powershell
$env:TERRALAB_SKY_BACKGROUND_PIPELINE='legacy'
python -m TerraLab.runtime.render_service
```

El test de paridad de píxeles cubre ambos modos en el mismo estado. El default
es `scene`; un valor distinto de `legacy|scene` falla con `ValueError`.

## Evidencia automática

- Pruebas focalizadas del slice, registro, MVC y Vía Láctea: 42 passed.
- Paridad directa de la capa scene/legacy: 13 tests; píxeles idénticos.
- Render-service real: artefactos con 15 repeticiones por escena en
  `render-service/`.
- Benchmark: `benchmarks/sky_background_20260729T141600Z.json`.

La evidencia visual detallada está en [visual-parity.md](visual-parity.md).

## Validación manual pendiente

Con ambos flags, realizar la lista del prompt: barrido de 24 h, giro de cámara
360°, zoom, resize, comparación de banding de horizonte/anti-sol/transición
nocturna/eclipse y ausencia de parpadeo durante interacción. La fase no puede
marcarse como pasada hasta recibir esta confirmación humana.
