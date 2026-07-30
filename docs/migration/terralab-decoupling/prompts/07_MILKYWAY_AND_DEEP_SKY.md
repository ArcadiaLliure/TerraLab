# FASE 07 — Via Làctia i cel profund

## Rol i missió

Executa només la fase 07. Separa càrrega de recursos, transformació de
coordenades, sampling/extinció i planificació visual de Via Làctia/NGC de
l'adapter QPainter.

## Prerequisits

- fases 01-06 `passed`;
- sky, stars i solar system sobre scene pipeline;
- fixtures de textura, dust map i OpenNGC;
- baseline visual de galactic center, seam, daylight i Bortle;
- working tree verificat.

## Evidència actual a revalidar

`render/sky/milkyway_overlay.py` carrega paths, QImage i arrays, converteix
equatorial/galàctic, mostreja, aplica dust/light pollution, construeix cache i
pinta. `runtime/offscreen_renderer.NgcCache` carrega CSV o memmap i
`_draw_ngc` projecta, resol labels/pick i pinta.

## Objectius

- ports/repositoris de textura i catàleg amb handles versionats;
- loaders fora de render i sense E/S per frame;
- transformació i sampling purs;
- plans d'imatge/batches NGC renderer-neutral;
- política de daylight/Bortle/extinció al Model/planner;
- adapter QPainter només composa/dibuixa;
- pick records NGC tipats;
- capabilities reversibles `milkyway` i `deep_sky`.

## Fitxers previstos

- `TerraLab/render/sky/milkyway_overlay.py`
- `TerraLab/astro/ngc_catalog.py`
- `TerraLab/astro/search_engine.py`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/data/converters/planck.py`
- nous `application/ports/sky_resources.py`
- nous `scene/plans/deep_sky.py`
- nous `render/qpainter/deep_sky.py`
- tests de Milky Way, diffuse noise, NGC, search i runtime

## Procediment

1. Caracteritza cache keys, lifecycle i I/O actual.
2. Defineix `TextureResourceHandle` i `DeepSkyCatalogHandle` amb versió/hash.
3. Mou resolució de paths/càrrega a adapter de dades, no a render.
4. Conserva `mmap_mode="r"` i `allow_pickle=False`.
5. Extreu conversions galàctiques, sampling bilineal, dust/extinció i opacitat
   a mòduls purs.
6. Defineix `MilkyWayPlan` i `DeepSkyBatch`, incloent labels/pick ja resolts.
7. Implementa adapter QPainter.
8. Enruta cada capability amb rollback independent però dependències validades.
9. Compara seam 0/360, pols, offset 180°, flips i missing optional resource.
10. Verifica que un frame calent no obre fitxers ni recalcula textures si no
    canvia versió/càmera/temps.
11. Elimina E/S i ciència de la ruta de render migrada.

## Proves obligatòries

- PNG alpha i resource version;
- galactic center i conversió equatorial/galàctica;
- seam, poles, flips, RA offset;
- dust missing/invalid i fallback explícit;
- day, Bortle 1/5/9, manual magnitude;
- NGC shape/name/extent/pick;
- CSV -> artifact i memmap lifecycle;
- cerca consistent amb render;
- no file I/O a adapter de render;
- suite completa i arquitectura.

## Rendiment i còpies

Mesura cold load separat de warm frames, cache hits, P50/P95, RSS i mida de
textures. No converteixis una textura completa per frame. Si l'adapter necessita
QImage, el resource owner n'ha de garantir la vida o justificar una còpia
única per versió.

## Validació visual manual

- Via Làctia visible Bortle 1, amagada segons política en dia/Bortle alt;
- galactic center correctament orientat;
- pan per seam i pols;
- dust on/off;
- NGC de diferents formes i labels;
- pick/search/goto;
- missing asset i recàrrega d'asset sense crash.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Sampling i plans renderer-neutral són `MODEL`; repositoris/textures físiques
  són `OUTSIDE_MVC`; traducció al backend és `VIEW`; use cases són
  `CONTROLLER`.
- Assigna un únic rol abans/després a cada fitxer tocat i redueix `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- adapter sense E/S, conversions o decisions de visibilitat;
- resources versionats i cachejats;
- paritat visual/semàntica;
- rollback verificat;
- rendiment dins gates;
- tests, Ruff, Pyright i arquitectura verds;
- ledger actualitzat;
- no migris grid/HUD.
