# Fase 06 - Sistema Solar, eclipsis, refraccio i traces

## Abast implementat

- `astro.apparent` es l'unic propietari del context UTC, snapshot coherent o
  fallback analitic, separacio angular, refraccio, overlap solar i contrast
  lunar.
- `astro.photometry` resol magnituds i colors planetaris abans de pintar.
- `scene.plans.bodies` construeix `CelestialBodiesPlan`, picks tipats i
  `TrailPlan` immutable sense imports Qt.
- `render.qpainter.bodies` consumeix aquests plans, conserva una unica imatge
  cachejada de trails i no decideix cap dada astronomica.
- La ruta per defecte es `scene`. El rollback temporal es
  `TERRALAB_CELESTIAL_RENDERING_PIPELINE=legacy`.

No s'han migrat NGC, Via Lactia, grid, mesures ni constelacions.

## Evidencia automatica

- `tests/test_celestial_bodies_vertical_slice.py`: snapshot stale coherent,
  eclipsi amb escala visual, fase monotona, refraccio, planeta/pick, RA wrap,
  cache de trails i scene/legacy.
- `tests/test_offscreen_functional_regressions.py`: regressions Torroja,
  hora local/UTC, corona, trails, estrelles suprimides i labels.
- Benchmark: `benchmarks/celestial_bodies_vertical_slice_20260729.json`.

El benchmark de 7 repeticions (6000 estrelles) registra P50/P95 per sistema
solar on/off, cost cold del planner de trails, frame warm, RSS i reutilitzacio
de cache; no crea una imatge de trails per frame.

## Acceptacio visual pendent

Cal confirmar en la UI desktop: animacio 24 h, horitzo Sol/Lluna, lluna nova /
quart / plena, eclipse de Torroja abans-durant-despres de totalitat, planetes i
labels, trails circumpolars, pan/zoom/scope i canvis rapids de temps. Fins que
hi hagi confirmacio humana, la fase queda `in_progress` al ledger.
