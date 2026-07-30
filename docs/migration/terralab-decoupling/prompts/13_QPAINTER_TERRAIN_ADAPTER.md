# FASE 13 — Adapter QPainter complet de terreny

## Rol i missió

Executa només la fase 13. Connecta `TerrainGeometryPlan` i
`TerrainMaterialPlan` a un adapter QPainter pur de presentació, migra perfil,
relleu, superfícies i hit visual, i retira el dibuix duplicat de terreny de la
ruta activa.

## Prerequisits

- fases 01-12 `passed`;
- geometria i materials renderer-neutral actius;
- baselines visuals i benchmark de terreny;
- ownership dels buffers documentat;
- working tree/gates verificats.

## Objectius

- un únic `QPainterTerrainAdapter`;
- convertir plans a QImage/batches/polygons sense decisions científiques;
- suport profile, relief, flat/preview, RGB i categorical;
- composició/clip/alpha/z correctes;
- hit-test consumeix `PickIndex`, no QPolygon com a font de veritat;
- lifecycle de QImage/buffers segur;
- capability `terrain` scene per default i rollback legacy;
- retirar mixins de dibuix que quedin sense consumidor.

## Fitxers previstos

- nou `TerraLab/render/qpainter/terrain.py`
- `TerraLab/terrain/overlay.py`
- `TerraLab/terrain/overlay_mixins/*`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/render/registry.py`
- `TerraLab/ui` només wiring si imprescindible
- tests de terrain offscreen/surface/render/process

## Regles

- adapter no obre DEM, NPZ, ortofoto o cache;
- adapter no decideix materials, llum, culling o representació;
- no crida callbacks de projecció;
- QImage sobre NumPy conserva owner/lease; `.copy()` només si és necessari i
  mesurat;
- no eliminis un mixin fins demostrar zero consumers i paritat.

## Procediment

1. Escriu suite de conformitat específica de primitives de terreny.
2. Implementa adapter per profile bands i relief raster.
3. Implementa image/material overlay, clip i blending.
4. Connecta presenter/offscreen via `RendererBackend`.
5. Connecta pick/hit records ja generats.
6. Enruta `terrain` scene i compara legacy.
7. Prova transicions horizon master, topography, surface i 3D.
8. Prova partial -> final artifact, source switch i cache invalidation.
9. Audita references abans de retirar codi/mixins duplicats.
10. Mantén rollback fins que validació manual passi.
11. Actualitza ADR/README de terreny amb fitxers reals.

## Proves obligatòries

- totes les suites terrain de fases 11-12;
- offscreen terrain image/pixels;
- profile/relief selection;
- master horizon neutralitza estat stale;
- surface artifact publicat;
- source/style/mode switch;
- hover/pick categòric coherent;
- restart renderer i pool resize;
- no I/O a adapter;
- zero imports Qt en planners;
- suite completa.

## Rendiment i còpies

Executa benchmark de terreny abans/després amb escenaris:

- cold;
- warm same camera/time;
- rotate fixed time;
- time fixed camera;
- combined;
- resize;
- interaction.

Registra P50/P95, cache hits, QImage allocations, bytes copied i RSS. Si
`.copy()` de QImage és el coll, només introdueix lease zero-copy amb proves de
lifetime, repaint repetit i shutdown.

## Validació visual manual

- perfil/relleu/flat/preview;
- original/vibrant;
- RGB/categorical;
- horizon/topography/surface/3D toggles;
- day/night/moon/weather/light pollution;
- hover categoria;
- pan/zoom/nadir/resize i restart.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- `render/qpainter/terrain.py` i la rasterització QPainter són `VIEW`; plans,
  geometria i materials resolts continuen sent `MODEL`.
- Assigna un únic rol abans/després a cada fitxer tocat i elimina la part
  gràfica dels fitxers `MIXED` de terreny.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- terreny complet presentat pel nou adapter;
- cap càlcul o I/O a QPainter adapter;
- una sola ruta activa de dibuix de terreny;
- rollback legacy verificat i delimitat;
- paritat visual aprovada;
- rendiment/memòria dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 13 `passed`.
