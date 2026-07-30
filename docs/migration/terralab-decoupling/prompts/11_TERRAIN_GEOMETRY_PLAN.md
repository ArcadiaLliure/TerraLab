# FASE 11 — Pla renderer-neutral de geometria de terreny

## Rol i missió

Executa només la fase 11. Separa selecció geomètrica, projecció, z-buffer,
baricèntriques i hit geometry de qualsevol tipus Qt. Encara no migris la
composició final de materials/llum ni retiris l'adapter de terreny actual.

## Prerequisits

- fases 01-10 `passed`;
- cel, interacció i scope sobre scene pipeline;
- fixtures de perfil, relleu, near patch, NoData i seam;
- benchmarks de terreny i captures baseline;
- working tree/gates verificats.

## Evidència actual a revalidar

`terrain/render/geometry.py` i `triangle_raster.py` són principalment numèrics,
però els mixins `projection_geometry.py`, `band_draw.py`,
`triangle_paint.py` i `category_hit_test.py` combinen arrays amb `QPointF`,
`QPolygonF`, QImage i estat de l'overlay. La ruta activa es crea des de
`OffscreenSceneRenderer._draw_terrain`.

## Objectius

- `TerrainGeometryPlan` immutable;
- posicions/índexs/validitat/depth/baricèntriques renderer-neutral;
- suport perfil, preview, relleu polar i near patch cartesià;
- cache keys tipades per geometria/càmera/viewport/versió;
- hit geometry compartida amb picking;
- arrays read-only i ownership explícit;
- cap Qt en planners/kernels;
- capability interna reversible per comparar geometria sense canviar color.

## Fitxers previstos

- `TerraLab/terrain/render/geometry.py`
- `TerraLab/terrain/render/triangle_raster.py`
- `TerraLab/terrain/render/overlay_types.py`
- `TerraLab/terrain/overlay_mixins/projection_geometry.py`
- `TerraLab/terrain/overlay_mixins/profile_cache.py`
- `TerraLab/terrain/overlay_mixins/category_hit_test.py`
- nou `TerraLab/scene/plans/terrain_geometry.py`
- `TerraLab/scene/picking.py`
- tests de terrain rendering/representation/visibility

## Regles

- no canvies fórmules de curvatura, visibilitat o topologia;
- no triangulis a l'espai de pantalla si abans era terreny/world;
- conserva seam circular, occlusion, near patch i NoData;
- no materialitzis el DEM;
- no passis `projection_fn` callbacks opacs com a contracte final; usa càmera i
  kernels tipats;
- cap array cachejat es muta en lloc.

## Procediment

1. Caracteritza inputs/outputs geomètrics actuals i cache invalidation.
2. Escriu proves de paritat array a array.
3. Defineix DTOs de geometria i IDs de recurs.
4. Mou projecció/select/culling a planner pur reutilitzant kernels existents.
5. Separa rasterització numèrica de creació QImage/polígons.
6. Produeix hit records per triangle/profile.
7. Adapta temporalment la ruta QPainter existent perquè consumeixi el nou pla,
   sense migrar materials.
8. Enruta amb rollback geomètric.
9. Mesura scalar fallbacks i vector path.
10. Elimina duplicació només de geometria migrada.

## Proves obligatòries

- z-buffer nearest;
- seams compartits i diagonal;
- nadir sense cap/hole;
- NoData i projecció invertida;
- circular azimuth seam;
- LOD/extrema;
- profile/relief/flat/previews;
- visibilitat i oclusió;
- hit-test coherent amb triangle visible;
- cache rotation/resize;
- planner sense PyQt;
- suites `test_astro_rendering`, `test_terrain_surface_render`,
  `test_terrain_representation`, `test_terrain_visibility_range`.

## Rendiment i còpies

Executa `benchmark_terrain_render.py` o equivalent sobre la mateixa fixture.
Mesura planner, raster, cache hits, P50/P95 i RSS. Revisa qualsevol `.copy()` o
`.astype()` nova; les conversions s'han de fer una vegada per versió/dtype.

## Validació visual manual

Encara que el color sigui legacy:

- profile i relief;
- pan 360°, mirar zenit/nadir;
- near/far terrain i seam;
- toggle 3D;
- preview parcial -> final;
- resize/interacció.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Geometria, LOD, projecció i índexs renderer-neutral són `MODEL`; no hi ha
  `VIEW` nova fins a l'adapter de la fase 13.
- Assigna un únic rol abans/després a cada fitxer tocat i extreu la geometria
  dels fitxers de terreny `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- geometria i hit-test sense Qt;
- paritat d'arrays/invariants i visual;
- adapter actual consumeix el pla;
- rollback verificat;
- cap regressió >5 % ni còpia gran injustificada;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 11 `passed`;
- materials continuen fora d'abast.
