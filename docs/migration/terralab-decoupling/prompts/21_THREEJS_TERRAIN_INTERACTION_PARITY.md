# FASE 21 — Terreny, interacció i paritat completa de Three.js

## Rol i missió

Executa només la fase 21. Completa terreny i interacció a Three.js, passa tota
la suite de conformitat i habilita `render.backend=threejs` com a opció estable
només després de paritat funcional, visual, lifecycle i rendiment.

## Prerequisits

- fases 01-20 `passed`;
- capabilities celestes Three.js completes;
- `TerrainGeometryPlan`, `TerrainMaterialPlan` i `PickIndex` estables;
- baselines terrain/interactions i fixtures;
- working tree/gates verificats.

## Objectius i capabilities en abast

- profile, relief, flat/preview;
- RGB/orthophoto/categorical;
- materials, llum, atmosfera, alpha i z;
- terrain/resource streaming/version;
- surface/category pick;
- pointer commands/pick results;
- selecció, mesures, scope i constel·lacions end-to-end;
- full backend selection/presenter integration.

## Fitxers previstos

- `TerraLab/render/threejs/*`
- Three.js terrain shaders/material adapters només de presentació
- bridge/resources/presenter
- `TerraLab/render/registry.py`
- `TerraLab/bootstrap/settings.py`, `composition.py`
- conformance, process, UI i visual tests

## Regles

- shader no decideix categoria, llum científica o atmosfera; rep buffers/
  paràmetres ja resolts segons contracte;
- si la GPU interpola/rasteritza, ha de respectar la semàntica canònica i tenir
  proves contra el planner/reference;
- no obre DEM/ortofoto des de JS arbitràriament;
- pick de negoci continua al controlador;
- resources grans per buffers/handles, no JSON;
- no canviïs Model per backend;
- no facis `threejs` default sense aprovació de producte; sí opció estable.

## Procediment

1. Implementa terrain geometry buffers i lifecycle.
2. Implementa RGBA/materials/blending amb color space explícit.
3. Implementa profile/relief/preview i transitions.
4. Implementa resource updates per surface/version.
5. Connecta pick index/result protocol i commands.
6. Executa totes les interaccions UI amb host Three.js.
7. Passa conformance completa, sense capabilities mancants.
8. Habilita selector estable `threejs`.
9. Prova canvi de backend entre arrencades i configuració invàlida.
10. Executa comparatives visuals/perf completes.
11. Prova build/install/offline/restart/shutdown/resource dispose.
12. Documenta limitacions; cap limitació obligatòria pot quedar com waiver per
    declarar full parity.

## Proves obligatòries

- totes les suites terrain de fases 11-13;
- full conformance;
- pick/interaction suites de fases 09-10;
- runtime/process/lifecycle;
- backend selector/host compatibility;
- screenshot matrix completa;
- resource leak/dispose;
- QPainter i Recording continuen verds;
- suite completa, Ruff, Pyright, build i install.

## Rendiment

Mesura cold/warm terrain, rotate/time/resize/interact, upload bytes, draw calls,
P50/P95, RSS/GPU, cache hits i dispose. No reupload de mesh/material immutable.
Compara amb QPainter i pressupostos de fase 01.

## Validació visual manual

Amb `TERRALAB_RENDER_BACKEND=threejs`:

- arrencada completa;
- totes les escenes celestes;
- profile/relief/RGB/categorical;
- toggles horizon/topography/surface/3D;
- dia/nit/weather/light pollution;
- hover/pick/selection/measure/scope/constellation;
- data/source changes;
- resize/restart/close.

Repeteix smoke essencial amb `qpainter`.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Shaders/materials/picking visual Three.js són `VIEW`; geometria/materials i
  pick semàntic són `MODEL`; interactions són `CONTROLLER`; streaming és
  `OUTSIDE_MVC`.
- Assigna un únic rol abans/després a cada fitxer tocat i no traslladis ciència
  al backend per aconseguir paritat.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- Three.js declara totes les capabilities i passa conformitat;
- selector estable funciona sense canviar Model;
- paritat funcional/visual humana aprovada;
- rendiment i recursos dins gates;
- cap fórmula o I/O científic a JS/backend;
- QPainter continua complet;
- tests/arquitectura/Ruff/Pyright/build/install verds;
- ledger fase 21 `passed`;
- legacy encara es retira només a fase 22.
