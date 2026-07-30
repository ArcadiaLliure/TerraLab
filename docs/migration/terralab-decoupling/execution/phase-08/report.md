# Fase 08 - Grid, bruixola, labels i HUD

## Resultat automatic

La ruta activa es `scene` amb
`TERRALAB_OVERLAY_RENDERING_PIPELINE=scene|legacy`. La ruta `scene` construeix
plans immutables abans de pintar i el rollback `legacy` conserva els metodes
temporals existents.

- `scene/plans/labels.py` concentra `GridPlan`, `CompassPlan`, `TextBatch` i
  `HudPlan`, mesura explicita, prioritat, clip, col·lisio i cache de layout.
- `application/ports/font_metrics.py` defineix el contracte de mesura; la Vista
  Qt l'implementa amb `QFontMetricsF` i publica font registrada/fallback amb
  `QFontInfo`.
- `render/qpainter/labels.py` nomes materialitza paths, ticks, caixes i text
  ja resolts. No consulta canvas, catalegs ni astronomia.
- Les etiquetes de planetes i NGC passen per un unic `TextBatch`; els adapters
  de cossos i cel profund ja no calculen metriques ni col·lisions.
- El HUD es pinta dins del frame del proces. `ui/frame_presenter` ja no pinta
  una segona copia sobre el frame.
- `render/grid_renderer.py` es ara una seam de Vista sense formula ni Qt i el
  recompte `MIXED` baixa de 54 a 53.

## Evidencia

- `python -m pytest -q tests/test_informational_overlays_vertical_slice.py tests/architecture/test_mvc_file_roles.py` - 27 passed.
- `python -m pytest -q tests/test_celestial_bodies_vertical_slice.py tests/test_deep_sky_vertical_slice.py tests/test_offscreen_functional_regressions.py tests/test_sky_background_slice.py` - 72 passed.
- `python -m pytest -q` - 726 passed in 89.10 s.
- `python -m pytest -q tests/architecture` - 17 passed in 9.27 s.
- Ruff, format, `compileall` i `git diff --check` - passed.
- `python tools/dev/check_pyright_baseline.py` - 2361 errors, 5 warnings; no supera la barrera 2362/9.

El benchmark de 100 repeticions es a
`benchmarks/informational_overlays_20260729.json`: 260 candidats, 40 labels
acceptades, 220 descartades per col·lisio, 100/100 cache hits, 40 draw calls,
P50 0.919 ms i P95 1.191 ms.

## Delta MVC

| Fitxer | Rol abans | Rol despres | Frontera provada |
| --- | --- | --- | --- |
| `scene/plans/labels.py` | — | MODEL | sense Qt/runtime; layout amb port fals |
| `application/{overlay_rendering.py,ports/font_metrics.py}` | — | CONTROLLER | flag i port sense paint |
| `render/qpainter/labels.py` | — | VIEW | sense canvas, cataleg ni astro |
| `render/grid_renderer.py` | MIXED | VIEW | geometria activa extreta al Model |
| `render/qpainter/{bodies,deep_sky}.py` | VIEW | VIEW | labels extretes al batch generic |
| `runtime/offscreen_renderer.py` | MIXED | MIXED transitori | wiring, diagnostics i rollback |
| `ui/frame_presenter.py` | VIEW | VIEW | una sola ruta HUD al frame |

## Pendent d'acceptacio humana

Les proves automatiques estan completes. Falta la comprovacio visual del
desktop descrita a `visual-parity.md`; fins a la teva confirmacio la fase es
manté `in_progress` al ledger.
