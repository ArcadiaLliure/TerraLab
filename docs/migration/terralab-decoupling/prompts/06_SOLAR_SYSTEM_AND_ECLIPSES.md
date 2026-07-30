# FASE 06 — Sistema solar, eclipses, refracció i traces

## Rol i missió

Executa només la fase 06. Extreu del renderer tots els càlculs del Sol, Lluna,
planetes, eclipses, refracció, fotometria i traces celestes; produeix plans
renderer-neutral i conserva QPainter com a adapter visual.

## Prerequisits

- fases 01-05 `passed`;
- sky i stars scene pipeline per defecte, amb rollback;
- fixtures d'efemèrides deterministes, especialment Torroja 2026;
- baselines visuals de fases solar/lunar, planetes i trails;
- suite i working tree nets segons ledger.

## Evidència actual a revalidar

`runtime/offscreen_renderer.py` defineix `fast_sun_altaz`,
`fast_moon_altaz`, `angular_separation_deg`, refracció, transmissió solar,
alpha lunar i dibuix físic. `render/overlays_renderer.py` conté una altra ruta
Skyfield. Part de les traces viu a `render/stars_renderer.py`.

## Objectius

- un únic model pur d'efemèrides resoltes/fallback;
- geometria aparent i refracció independents de QPainter;
- eclipsi i fase lunar calculats al Model;
- fotometria planetària resolta abans del render;
- batches de discs, gradients declaratius, corona, paths i labels;
- traces calculades com polylines/batches purs;
- pick records per cos;
- adapters QPainter sense fórmules científiques;
- capabilities reversibles `solar_system` i `trails`.

## Fitxers previstos

- `TerraLab/astro/engine.py`
- `TerraLab/astro/ephemeris_coordinator.py`
- nous `TerraLab/astro/apparent.py`, `photometry.py` si la cohesió ho demana
- nou `TerraLab/scene/plans/bodies.py`
- nou `TerraLab/render/qpainter/bodies.py`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/render/overlays_renderer.py`
- `TerraLab/render/stars_renderer.py`
- tests d'ephemeris/offscreen/recovered features

No moguis encara NGC, Via Làctia, grid, mesures o constel·lacions.

## Procediment

1. Inventaria fórmules duplicades i declara la canònica amb tests numèrics.
2. Defineix snapshots tipats de cossos amb timestamp/context i coherència.
3. Mou fallback analític a `astro`; el renderer mai decideix si el snapshot és
   stale.
4. Mou separació, overlap, transmissió, refracció i geometria aparent a model.
5. Mou magnitud/fase/distància planetària a model.
6. Genera `CelestialBodiesPlan` i `TrailPlan`.
7. Representa gradients/corona/fase com dades declaratives o masks ja
   resoltes; cap `QPainterPath` al planner.
8. Implementa adapter QPainter.
9. Enruta per flags, compara legacy/scene i elimina fórmules duplicades de la
   ruta activa.
10. Prova snapshots coherents durant canvi ràpid de temps.
11. Verifica pick, labels i object IDs.

## Proves obligatòries

- Torroja 2026: parcialitat, totalitat i timestamps UTC/local;
- disc overlap físic tot i disc visual augmentat;
- fases lunars monotòniques;
- refracció i aplanament prop de l'horitzó;
- planetes registrats amb clau/noms/magnitud;
- snapshot stale coherent;
- circumpolar/trails, RA wrap i estrelles suprimides quan correspon;
- dia/crepuscle/nit;
- planner sense Qt;
- runtime restart i suite completa.

## Rendiment

Mesura frame amb sistema solar on/off i trails cold/warm. La cache de trails
només s'invalida per inputs tipats. Registra allocations de masks/gradients.
No dupliquis imatges de trails per frame.

## Validació visual manual

- animació de 24 h;
- Sol/Lluna a l'horitzó;
- fases lunar nova/quart/plena;
- eclipsi Torroja abans/durant/després de totalitat;
- planetes i labels;
- traces circumpolars;
- pan/zoom/scope i canvi ràpid de temps.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Efemèrides, eclipsis i plans són `MODEL`; casos d'ús són `CONTROLLER`;
  traducció QPainter és `VIEW`; procés/transport és `OUTSIDE_MVC`.
- Assigna un únic rol abans/després a cada fitxer tocat i separa la part
  migrada dels coordinadors/renderers `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- zero ciència solar/lunar/planetària a QPainter/offscreen;
- un únic propietari de fórmules;
- plans/pick tipats;
- paritat numèrica i visual;
- rollback verificat;
- P50/P95/RSS dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 06 `passed`;
- no migris Via Làctia/NGC.
