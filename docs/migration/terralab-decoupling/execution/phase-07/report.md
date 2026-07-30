# Fase 07 - Via Lactia i cel profund

## Abast implementat

- `scene.plans.deep_sky` es el Model pur de transformacio equatorial/galactica,
  sampling bilineal, politica d'opacitat/extincio, `MilkyWayPlan`,
  `DeepSkyBatch`, labels i picks NGC tipats.
- `data.sky_resources` es l'unic propietari de la resolucio de paths, hash,
  descodificacio de PNG/dust i lifecycle del memmap `.npy`. Els handles
  `TextureResourceHandle` i `DeepSkyCatalogHandle` contenen path, versio,
  hash, disponibilitat i bytes ja carregats.
- `render.qpainter.deep_sky` nomes tradueix els plans resolts a `QImage` i
  crides QPainter. No obre fitxers ni converteix coordenades.
- La ruta per defecte es `scene` i les dues capabilities son independents:
  `TERRALAB_MILKYWAY_RENDERING_PIPELINE=scene|legacy` i
  `TERRALAB_DEEP_SKY_RENDERING_PIPELINE=scene|legacy`.
- El HUD de depuracio publica les dues selections. Les facanes anteriors es
  retenen exclusivament per al rollback temporal.

No s'han migrat grid, bruixola, HUD funcional, mesures ni constelacions.

## Cache i lifecycle

- Una identitat `(path, version)` nova fa una carrega en fred i calcula el hash;
  una identitat calenta retorna el mateix handle, sense `stat`, `open` ni
  `np.load` per frame.
- `MilkyWayPlanner` retorna el mateix pla per una clau que inclou viewport,
  camera, temps, configuracio, opacitat i versions/hash de textura/dust.
- L'adapter conserva una unica `QImage` per la clau del pla. No hi ha conversio
  de la textura completa per frame.
- Els artefactes NGC `.npy` usen `mmap_mode="r"` i `allow_pickle=False`; el
  repositori tanca explicitament els mmaps a `close()`.
- Dust absent o invalid retorna un handle no disponible i el planner manté la
  Via Lactia amb `dust_fallback=unavailable`.

## Delta MVC

| Fitxer creat/modificat | Rol abans | Rol despres | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `application/deep_sky_rendering.py` | — | CONTROLLER | resolucio dels flags reversibles | ciencia i paint | defaults `scene`, rollback independent i invalid explicit |
| `application/ports/sky_resources.py` | — | CONTROLLER | contractes de carrega versionada | I/O concret | Protocols sense Qt ni I/O |
| `data/sky_resources.py` | — | OUTSIDE_MVC/DATA_ADAPTER | hash, decode i mmap fisic | decisions de visibilitat i paint | warm handle identic; mmap read-only/pickle-free |
| `scene/plans/deep_sky.py` | — | MODEL | conversio, sampling, extincio, batches, labels i picks | Qt i paths | AST/imports sense Qt; plans immutables |
| `render/qpainter/deep_sky.py` | — | VIEW | QImage i calls QPainter | I/O, coordenades i politica | test que falla si l'adapter obre un fitxer |
| `runtime/offscreen_renderer.py` | MIXED transitori | MIXED transitori | wiring payload→resources→plan→adapter i compatibilitat legacy | ruta `scene` no obre ni pinta resources | dispatch default scene i metadades de capability |
| `scene/contracts.py` | MODEL | MODEL | serialitza revisions de textura/dust | lifecycle fisic | round-trip de revisions en `SceneFrame` |
| `ui/frame_presenter.py` | VIEW | VIEW | exposa debug de pipelines | seleccio de capability | metadades render mostrades sense decidir ruta |

El manifest `docs/architecture/mvc_file_roles.json` incorpora tots els nous
fitxers una sola vegada; el recompte MIXED continua en 54, sense augmentar.

## Evidencia automatica

- Baseline abans de la fase: `98 passed in 11.07s` per a Via Lactia, diffuse,
  NGC i regressions offscreen.
- `tests/test_deep_sky_vertical_slice.py`: 13 proves de flags, hash/version,
  seam/pols/flips/offset, galactic center, dia/Bortle/manual magnitude,
  fallback dust, forma/label/pick NGC, CSV→artifact→mmap, search/render,
  adapter sense I/O i dispatch per defecte.
- Suite final: `701 passed in 91.62s`; arquitectura MVC: `17 passed`.
- Benchmark: `benchmarks/deep_sky_vertical_slice_20260729.json`, 7 repeticions:
  cold resources 17.711 ms, cold plans 28.695 ms, warm P50/P95 4.920/5.452 ms,
  cache de pla/texture reutilitzada, NGC memmap i RSS +36,864 bytes.
- Smoke amb assets reals: `scene scene True 147` (pipeline Milky/NGC,
  textura carregada i NGC visibles a 320×180).

## Acceptacio visual pendent

Vegeu `visual-parity.md`. Cal observacio humana del desktop abans de marcar la
fase com a `passed`; fins llavors el ledger queda `in_progress`.
