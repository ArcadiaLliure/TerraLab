# FASE 04 — Slice vertical de fons de cel i màscara d'horitzó

## Rol i missió

Executa només la fase 04. Aquesta és la primera migració visible completa:
Model/planner pur -> `RenderPlan` -> adapter QPainter -> frame presentat. Migra
el fons físic del cel i la màscara bàsica d'horitzó, mantenint totes les altres
capes a la ruta existent.

El prompt és autònom. Torna a inspeccionar codi, tests i estat.

## Prerequisits

- fases 01-03 `passed`;
- backend registry i `SceneFrameBuilder` actius;
- captures baseline de dia, crepuscle, nit i eclipsi;
- suite/gates verds;
- cap canvi concurrent sense resoldre.

## Evidència actual a revalidar

- `render/sky_renderer.py::sky_color_phys` calcula color però retorna `QColor`.
- `render/sky_renderer.py::draw_background` llegeix estat del canvas.
- `runtime/offscreen_renderer.py::_draw_background` duplica part del pipeline,
  desprojecta samples i construeix `QImage`.
- `HorizonRenderer/draw_ground_mask` i el terreny poden participar en la
  silueta/màscara.

## Objectius

- fórmula de cel purament numèrica, sense Qt;
- planner que rep temps, Sol, Bortle, càmera i viewport tipats;
- output renderer-neutral amb RGBA/imatge o batch de mostres ben definit;
- adapter QPainter que només crea/compón QImage;
- cache key basada en dependències explícites;
- capability `sky_background` reversible;
- una sola fórmula compartida per ruta nova i tests.

## Fitxers previstos

- nou `TerraLab/scene/plans/sky.py`
- nou `TerraLab/render/qpainter/sky.py`
- `TerraLab/render/sky_renderer.py`
- `TerraLab/render/horizon_renderer.py`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/scene/contracts.py`
- `TerraLab/render/registry.py`
- tests de sky/projection/offscreen i goldens

No moguis estrelles, Via Làctia, grid, cossos, terreny 3D o HUD.

## Separació de responsabilitats

Planner:

- resol sampling i resolució adaptativa;
- desprojecta mitjançant el kernel pur;
- calcula color RGBA, crepuscle, Bortle i eclipsi;
- genera màscara/clip bàsic ja decidit;
- produeix dades read-only i cache token.

Adapter:

- converteix RGBA a QImage sense reinterpretar valors;
- aplica scaling/blend/clip indicats;
- no consulta canvas, parent widget o config;
- no calcula astronomia ni Bortle.

## Procediment

1. Escriu tests numèrics de `sky_color` amb tuples/arrays RGBA.
2. Caracteritza les dues implementacions actuals i tria una semàntica
   canònica basada en la ruta executable; qualsevol divergència és una decisió
   explícita, no una fusió intuïtiva.
3. Implementa `SkyBackgroundPlan` immutable.
4. Implementa planner i cache key.
5. Implementa QPainter adapter.
6. Enruta només aquesta capability pel nou pipeline amb flag temporal
   `legacy|scene`, default `legacy` fins que els gates passin.
7. Compara ambdues rutes sobre totes les escenes.
8. Quan hi hagi paritat, posa `scene` per defecte i conserva rollback durant
   la fase següent.
9. Retira la duplicació de fórmula, no la resta de l'orquestrador legacy.
10. Afegeix barrera d'imports Qt al planner.

## Proves

- color a altituds/azimuts límit;
- transicions Sol 20°, 6°, 0°, -6°, -12°, -18°;
- Bortle 1/5/9 i contaminació desactivada;
- eclipsi total/parcial;
- càmera, zoom, resize, DPR i interacció;
- cache hit/miss només quan canvien inputs;
- adapter sobre QImage offscreen;
- render worker/restart;
- suite completa i arquitectura.

## Rendiment

Mesura P50/P95 de planner, adapter i capa completa en idle/interacció. La ruta
scene no pot superar el baseline més 5 %. Registra mida/ownership del buffer i
demostra que no apareix una còpia de frame complet addicional.

## Validació visual manual

Amb flag legacy i scene:

- escombra 24 h a ubicació fixa;
- gira càmera 360°;
- zoom i resize;
- compara horizon banding, anti-sun, transició nocturna i eclipsi;
- confirma absència de parpelleig en interacció.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- `scene/plans/sky.py` és `MODEL`; `render/qpainter/sky.py` és `VIEW`; la
  coordinació del slice és `CONTROLLER`.
- Assigna un únic rol abans/després a cada fitxer tocat i separa la part
  migrada dels fitxers `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- planner zero-Qt i provable en consola;
- adapter sense decisions;
- ruta scene visible i default després de paritat;
- rollback legacy verificat;
- diferències visuals dins tolerància aprovada;
- cap regressió de temps/RSS;
- tests, Ruff, Pyright i arquitectura verds;
- ledger actualitzat;
- cap altra capa migrada.
