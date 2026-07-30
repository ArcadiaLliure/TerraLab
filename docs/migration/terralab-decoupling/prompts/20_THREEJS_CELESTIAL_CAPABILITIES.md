# FASE 20 — Capabilities celestes completes a Three.js

## Rol i missió

Executa només la fase 20. Implementa en l'adapter Three.js totes les
capabilities celestes i informatives existents, consumint exactament els plans
ja generats. No modifiquis fórmules científiques ni planners per conveniència
del backend; només amplia contractes si és una necessitat real i compatible
amb QPainter/Recording.

## Prerequisits

- fases 01-19 `passed`;
- host/primitives Three.js estables;
- QPainter/Recording conformance completa;
- baselines visuals de totes les escenes celestes;
- working tree/gates verificats.

## Objectius i capabilities en abast

- fons de cel/horizon mask;
- estrelles wide/scope, sprites, colors, spikes;
- Sol, Lluna, planetes, eclipses i traces;
- Via Làctia i NGC;
- grid, brúixola, labels i HUD;
- selection/measurement visuals;
- scope visuals i constel·lacions.

Terreny i interacció completa de negoci queden per fase 21, tot i que pick
records celestes es poden provar.

## Fitxers previstos

- `TerraLab/render/threejs/backend.py`, bridge i assets
- nous mòduls Three.js per batches/materials celestes
- `TerraLab/render/conformance.py`
- només contractes/planners si una extensió backend-neutral és imprescindible
- tests/goldens/captures Three.js

## Regles

- JavaScript no recalcula AltAz, magnitud, Bortle, eclipsi, layout o pick;
- usa batches/instancing/buffer geometry;
- resources immutables per versió;
- blending, color space, alpha i z-order explícits;
- text/font té fallback empaquetat i mètriques coherents;
- no canviïs expected QPainter per fer-los coincidir;
- capability parcial continua rebutjada per full app fins fase 21.

## Procediment

1. Implementa una capability cada vegada en ordre del `SceneFrame`.
2. Executa conformitat semàntica després de cadascuna.
3. Compara captures Three.js amb QPainter mitjançant màscares/toleràncies
   justificades per rasteritzador, sense exigir identitat de píxel irracional.
4. Valida counts, IDs, ordre, pick records i colors numèrics exactes abans de
   comparar aparença.
5. Implementa resources de textures/sprites/fonts locals.
6. Verifica scope dens i updates de temps/càmera.
7. Verifica delta frames i no reupload d'estàtics.
8. Documenta diferències visuals inevitables.
9. Mantén QPainter default i capability manifest incomplet per terrain.

## Proves obligatòries

- totes les fixtures de fases 04-10;
- conformance per batch/capability;
- counts/pick IDs/resource versions;
- screenshot comparisons day/twilight/night/scope/eclipse/MilkyWay/NGC;
- labels/DPI/resize;
- restart/offline/packaging;
- QPainter i Recording suites;
- suite completa.

## Rendiment

Mesura frame P50/P95, update/upload bytes, draw calls, startup, RSS/GPU i scope
dens. Objectiu: no pitjor que QPainter en interacció sostinguda sense sacrificar
paritat. Una regressió justificada no s'accepta sense approval/waiver.

## Validació visual manual

Recorre les escenes celestes de baseline en QPainter i Three.js costat a costat:

- dia/crepuscle/nit/Bortle;
- wide/scope i instruments;
- sistema solar/eclipsi/trails;
- Via Làctia/NGC;
- grid/labels/HUD;
- selecció/mesures/constel·lacions;
- pan/zoom/time/resize/restart.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Tots els adapters/materials/batches Three.js són `VIEW`; plans i decisions
  celestes són `MODEL`; dispatch és `CONTROLLER`; transport és `OUTSIDE_MVC`.
- Assigna un únic rol abans/després a cada fitxer tocat. Si cal modificar un
  planner per satisfer Three.js, demostra que continua backend-neutral.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- totes les capabilities celestes conformes;
- cap ciència duplicada en JS;
- paritat semàntica i visual aprovada;
- recursos/batching eficients;
- QPainter default i rollback intactes;
- tests/arquitectura/Ruff/Pyright/build verds;
- ledger fase 20 `passed`;
- Three.js encara no es declara full backend sense fase 21.
