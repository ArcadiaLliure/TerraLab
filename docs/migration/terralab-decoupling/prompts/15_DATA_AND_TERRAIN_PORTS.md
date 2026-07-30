# FASE 15 — Ports de dades, DEM, superfície i assets

## Rol i missió

Executa només la fase 15. Inverteix les dependències entre `data`, `terrain`,
workers i UI. Separa serveis científics/IO de QObject/QThread i conserva
providers, caches, subprocess de bake, progress, cancel·lació i publicació
atòmica.

## Prerequisits

- fases 01-14 `passed`;
- compute ports establerts;
- terreny visual complet sobre plans/adapters;
- fixtures DEM/ASC/GeoTIFF/RGB/categorical i tests de shutdown;
- working tree verificat.

## Evidència actual a revalidar

El graf tenia dependències `data -> terrain` i `terrain -> data`.
`terrain/terrain_coordinator.py` i `terrain/worker.py` importen Qt.
`SurfaceSamplingService`, `_GeoRasterDataset`, assets registry i source catalog
tenen responsabilitats grans però parts numèriques/IO valuoses. Els ADRs
exigeixen owner únic i domini sense Qt/GDAL/xarxa.

## Objectius

- ports application per elevation, terrain bake, surface sampling, catalog i
  assets;
- DTOs/commands/progress/errors tipats;
- serveis purs o infrastructure adapters sense Qt;
- bridge Qt separat mentre la Vista el necessiti;
- eliminar dependència bidireccional de paquet;
- providers implementen ports i són construïts al composition root/factory;
- owner únic de job/worker/recurs;
- preservar batch, LRU, mmap, persistent cache i atomic swap.

## Fitxers previstos

- `TerraLab/terrain/terrain_coordinator.py`
- `TerraLab/terrain/worker.py`
- `TerraLab/terrain/surface/service.py`
- `TerraLab/terrain/providers/*`
- `TerraLab/terrain/data_sources.py`
- `TerraLab/data/source_catalog.py`
- `TerraLab/data/assets/*`, `assets_manager.py`, `layer_manager.py`
- `TerraLab/runtime/compute_service.py`, `clients.py`
- nous `application/ports/terrain.py`, `assets.py`
- nous `adapters/runtime/terrain.py`, `adapters/qt/terrain.py`
- tests terrain/data/assets/process/lifecycle

## Regles

- no moguis GDAL/rasterio al domini;
- no materialitzis rasters complets;
- no trenquis schema/fingerprints/cache sense migració;
- `HorizonWorker` o substitut manté owner únic;
- publicació parcial/final continua generacional i atòmica;
- progress throttled abans de UI;
- cancel·lació tanca datasets/subprocess;
- ports no exposen QFile/QThread/QObject.

## Procediment

1. Mapeja dependències de paquet i consumers.
2. Defineix ports des dels casos d'ús.
3. Extreu coordinació no-Qt a application/runtime.
4. Converteix QThread worker en adapter, no servei científic.
5. Separa source selection/catalog contracts de providers concrets.
6. Injecta provider factories.
7. Conserva bake subprocess i artifacts versionats; evita object serialization.
8. Migra assets/layer operations per ports.
9. Prova cancel/shutdown/resource close i latest job.
10. Afegeix test de graf de paquet direccional.
11. Elimina imports Qt i dependències invertides quan zero consumers.

## Proves obligatòries

- batch/scalar elevation i NoData chain;
- ASC materialization/cancel;
- raster windows/prefetch/LRU;
- vectorized raycast i profile round-trip;
- surface partial/final, RGB/categorical i mode switch;
- cache memory/persistent i fingerprints;
- job cancel/restart/shutdown, no terminate QThread;
- assets managed vs external ownership;
- source selection/manual/automatic;
- import safety i graf sense cicles de paquet;
- suite completa.

## Rendiment i còpies

Executa benchmarks CPU i, si fixtures ho permeten, cold/warm DEM/surface.
Mesura bytes read, cache hits, P50/P95, RSS i workers. Preserva els llindars
existents. Verifica que `copy.copy(profile)` continua compartint arrays quan
serveix publicació atòmica i que no apareix `deepcopy`.

## Validació manual

- afegir/enllaçar DEM;
- bake/progress/cancel/restart;
- preview -> final;
- canviar DEM/surface;
- RGB/categorical;
- import/remove managed i unlink external;
- tancar durant job i reobrir.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Ports i casos d'ús són `CONTROLLER`; algoritmes/DTOs són `MODEL`;
  providers, DEM, xarxa, persistència i caches físiques són `OUTSIDE_MVC`.
- Assigna un únic rol abans/després a cada fitxer tocat i divideix services/
  coordinators `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- ports tipats i providers injectats;
- serveis Model/application sense Qt;
- sense dependència `data <-> terrain` bidireccional;
- owner/lifecycle únics;
- paritat funcional/visual;
- rendiment dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 15 `passed`.
