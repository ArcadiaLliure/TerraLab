# FASE 05 — Slice vertical d'estrelles

## Rol i missió

Executa només la fase 05. Migra l'estrella completa: selecció de catàleg,
culling, magnitud, color, projecció, batching, sprites i pick records cap a un
planner pur i un adapter QPainter. No canviïs l'aspecte ni retallis estrelles
per assolir rendiment.

## Prerequisits

- fases 01-04 `passed`;
- fons del cel ja usa scene pipeline i té rollback;
- baselines d'escena gran angular/scope dens i benchmarks de catàleg;
- catàlegs/fixtures deterministes disponibles;
- working tree i instruccions locals verificats.

## Evidència actual a revalidar

`render/stars_renderer.py::StarsRenderer` té aproximadament 1.704 línies de
classe i un `render` de 866 línies. Gestiona índex de magnitud, índex espacial,
cache alt/az, límit fotomètric, colors, sprites, draw batches i QPainter.
Importa `widgets/telescope_runtime.py`. `CatalogCache` a
`runtime/offscreen_renderer.py` usa memmap i s'ha de preservar.

## Objectius

- `StarScenePlanner` sense Qt/UI/widgets;
- batches renderer-neutral de punts/sprites/halos/spikes;
- `StarPickBatch` associat a la generació;
- fotometria i límit de magnitud al Model/planner;
- QPainter adapter limitat a dibuix i cache de recursos gràfics;
- preservar memmap, vistes, índexs, LOD i caches;
- capability `stars` reversible;
- eliminar dependència `render -> widgets`.

## Fitxers previstos

- nou `TerraLab/scene/plans/stars.py`
- nou `TerraLab/scene/resources.py`
- nou `TerraLab/render/qpainter/stars.py`
- `TerraLab/render/stars_renderer.py`
- `TerraLab/widgets/telescope_runtime.py`
- `TerraLab/widgets/visual_magnitude_engine.py`
- `TerraLab/data/catalogs/star_catalog.py`
- `TerraLab/runtime/offscreen_renderer.py`
- tests de stars, magnitude, scope, runtime i goldens

No migris encara overlay de scope, traces, Sol/Lluna o constel·lacions.

## Disseny de dades

El planner ha de produir arrays read-only i contigus només quan el backend ho
requereixi:

- posició screen/NDC;
- radi/alpha/color;
- classe visual o sprite key;
- ordre/z;
- índex original estable per pick;
- metadata compacta per counters.

No produeix `QColor`, `QPointF`, `QPainterPath`, `QImage` o `QPixmap`.
L'adapter pot cachejar sprites per clau visual, no per objecte científic.

## Procediment

1. Caracteritza resultats actuals: visible indices, draw calls, pixels i
   counters en wide/scope/pure/interacting.
2. Escriu tests de planner abans d'extreure.
3. Mou fotometria compartida fora de `widgets` a un mòdul pur adequat.
4. Extreu prefilter, índex, alt/az, projecció i classificació visual.
5. Defineix resource handle del catàleg; comparteix memmap, no copiïs arrays.
6. Implementa batches i pick records.
7. Implementa adapter QPainter mantenint sprites/batching.
8. Enruta amb capability flag i compara legacy/scene.
9. Prova catàleg sense canals RGB i fallback `bp_rp`.
10. Prova warmup scope asíncron i cancel·lació per generació.
11. Elimina del renderer actiu el càlcul migrat; no deixis wrappers duplicats.
12. Posa scene per default només després de gates; conserva rollback fins a
    fase 06.

## Proves obligatòries

- colors de catàleg i BP-RP;
- magnitud Bortle/dia/crepuscle/eclipsi;
- `pure_colors`, interaction i spikes;
- prefilter sorted/unsorted i max_count existent;
- scope FOV, RA wrap, pols i densitat alta;
- índex warm/cold i cache invalidation;
- pick index estable;
- memmap lifecycle i `allow_pickle=False`;
- cap import Qt del planner;
- suite `test_astro_rendering`, `test_offscreen_functional_regressions`,
  `test_visual_magnitude_engine`, runtime isolation i completa.

## Rendiment i memòria

Mesura:

- P50/P95 planner, adapter i frame;
- catàleg 62k, 1M i fixture representativa gran;
- warm/cold scope;
- draw calls;
- RSS i bytes temporals;
- cache hit/miss.

No acceptis regressió >5 % ni una còpia del catàleg. Una conversió dtype només
és admissible una vegada per versió de recurs.

## Validació visual manual

- wide field dia/crepuscle/nit;
- Bortle 1/5/9;
- pure colors on/off;
- pan continu i zoom;
- scope petit/gran, diferents ISO/obertura/exposició;
- estrelles brillants, febles, colors i spikes;
- canvi de catàleg i restart render.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Selecció, magnitud i batches d'estrelles són `MODEL`; QPainter és `VIEW`;
  càrrega física de catàleg és `OUTSIDE_MVC`; l'orquestració és `CONTROLLER`.
- Assigna un únic rol abans/després a cada fitxer tocat i redueix el `MIXED`
  dels renderers/widgets afectats.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- cap càlcul d'estrelles a l'adapter;
- `render` ja no importa `widgets.telescope_runtime`;
- memòria compartida per referència/handle;
- paritat de visible indices i aparença;
- rollback verificat;
- rendiment dins gates;
- arquitectura, tests, Ruff i Pyright verds;
- ledger actualitzat;
- no migris sistema solar.
