# FASE 12 — Materials, llum i atmosfera de terreny purs

## Rol i missió

Executa només la fase 12. Separa paletes, identitat categòrica, llum solar i
lunar, atmosfera, variació, bloom i color final de qualsevol tipus Qt. Produeix
buffers RGBA/materials renderer-neutral que encara pot presentar l'adapter
existent.

## Prerequisits

- fases 01-11 `passed`;
- `TerrainGeometryPlan` actiu i equivalent;
- suite extensa de materials/lighting disponible;
- baselines original/vibrant, dia/nit/lluna, RGB/categorical;
- working tree verificat.

## Evidència actual a revalidar

`terrain/render/palette.py` importa PyQt i construeix colors/brushes.
`overlay_mixins/materials.py`, `light_context.py`, `lighting_cache.py`,
`interpolated_material.py` i `triangle_paint.py` combinen càlcul numèric,
cache i QImage/QColor.

## Objectius

- colors com `uint8 RGBA` o valors lineals tipats;
- identitat de classe/source separada del color;
- `TerrainMaterialPlan` i buffers de llum/atmosfera;
- cache keys separades per material estàtic, llum, càmera i temps;
- cap QColor/QBrush/QGradient/QImage al Model/planner;
- preservar original/vibrant, RGB, categòric i fallback;
- capability reversible per material pipeline.

## Fitxers previstos

- `TerraLab/terrain/render/palette.py`
- `materials.py`, `lighting.py`, `atmosphere.py`, `config.py`
- `terrain/overlay_mixins/materials.py`
- `light_context.py`, `lighting_cache.py`
- `interpolated_material.py`, `triangle_paint.py`
- nou `TerraLab/scene/plans/terrain_materials.py`
- tests de terrain pipeline/surface render

## Regles

- no canviïs espai de color o ordre de composició sense ADR i goldens;
- categòric mai barreja identitat encara que suavitzi vores visuals;
- il·luminació s'aplica abans d'atmosfera segons tests actuals;
- cache base no depèn de temps/càmera si no cal;
- resultats cachejats són immutables;
- no uses QColor com a oracle del color numèric.

## Procediment

1. Mapeja cada stage actual i ordre de composició.
2. Escriu tests numèrics de colors/alpha i identitat.
3. Converteix paletes Qt a dades pures; adapter convertirà al final.
4. Defineix context celeste tipat.
5. Extreu Lambert, solar/lunar, shadow, haze, vibrant grade i bloom.
6. Defineix plans/buffers i invalidació explícita.
7. Adapta temporalment `triangle_paint` perquè consumeixi RGBA pur.
8. Compara legacy/scene per cada estil/capa.
9. Revisa copies: només copy-on-write abans de mutació.
10. Elimina imports Qt dels mòduls de material purs.

## Proves obligatòries

- base material immutable;
- cache keys static/time/camera;
- original strict noop;
- vibrant palette sense perdre identitat;
- RGB preservat;
- categorical regions, protected small classes i smoothing;
- Lambert, Sol real, fallback invalid;
- night/full moon/moonless;
- atmosphere, haze, bloom i alpha;
- water/snow/conifer/occlusion;
- order-independent triangles/seams;
- tests complets de `test_terrain_surface_render.py` i
  `test_terrain_render_pipeline.py`.

## Rendiment i memòria

Mesura cada stage cold/warm, cache reuse durant rotació/temps, P50/P95 i RSS.
Evita buffers RGBA intermedis redundants; si en calen dos per immutabilitat,
documenta lifetime i peak. No empitjoris render >5 %.

## Validació visual manual

- original/vibrant;
- orthophoto i S2GLC categòric;
- dia, crepuscle, nit, lluna plena/sense lluna;
- canvis Sol azimuth;
- haze near/far, snow/water/forest;
- pan/temps per verificar invalidació.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Categories, RGBA, materials i llum resolta són `MODEL`; QColor/QBrush/shaders
  són `VIEW`; càrrega de textures és `OUTSIDE_MVC`.
- Assigna un únic rol abans/després a cada fitxer tocat i extreu les decisions
  dels fitxers de terreny `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- zero Qt a materials/lighting/atmosphere/planner;
- buffers tipats, identitat separada;
- paritat numèrica i visual;
- cache invariants i rollback verificats;
- rendiment/memòria dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 12 `passed`;
- QPainter terrain final encara no es retira.
