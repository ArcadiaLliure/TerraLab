# Arquitectura objectiu i auditoria de TerraLab

## 1. Abast, baseline i mètode

Aquest document defineix una arquitectura futura; no implementa canvis. Totes
les afirmacions sobre l'estat actual provenen del codi, les proves o les eines
del mateix repositori.

Baseline final de l'auditoria:

| Camp | Valor |
| --- | --- |
| Línia auditada | `origin/refactor` |
| Commit font | `4352e35f588a75b1e0fea8b71e3fce6801be53bf` |
| Merge equivalent | `1fbcf088a0bfc1f832fc0f2a8ba2808e3e783a7d` |
| Data | 2026-07-28 |
| Python de la validació | 3.13.7 |
| Mòduls Python de producte | 232 |
| LOC Python de producte | 86.002 |
| Símbols AST de producte | 2.808 |
| Arestes del graf intern del repositori | 830 |
| Components cíclics | 0 |
| Mòduls de producte amb dependència Qt | 59 |
| Captures àmplies detectades per AST | 440 |
| Supressions potencials detectades per heurística | 171 |

El checkout va començar a `refactor` i va ser mogut externament durant
l'auditoria a `fromcpu_togpu`, sobre el merge indicat. `origin/refactor` i el
merge tenen arbres idèntics; les mètriques finals es van reproduir sobre aquest
arbre. La branca local `refactor` era un commit per darrere del remot en acabar
l'auditoria.

Comprovacions reproduïdes:

```powershell
python tools/dev/code_inventory.py --root . --output-dir <directori-temporal>
python -m pytest -q
python -m pytest -q tests/architecture
python -m ruff check TerraLab scripts tests benchmarks tools/dev
python tools/dev/check_pyright_baseline.py
python benchmarks/benchmark_optimization.py --output <fitxer-temporal>
```

Resultat actual:

- `pytest`: 627 proves aprovades de 628. Falla
  `test_silent_generic_exception_debt_does_not_increase` perquè
  `TerraLab/tools/download_gaia_tap.py` aporta tretze
  `except Exception: pass` fora del baseline.
- Arquitectura: 13 proves aprovades de 14, amb la mateixa fallada.
- Ruff: quinze errors; set són assignacions `lambda` a
  `runtime/offscreen_renderer.py` i la resta són imports, redefinicions o
  variables no usades.
- Pyright incremental: 2.237 errors i 5 avisos, per sota del límit
  2.362/9; la barrera passa.
- El graf de mòduls és acíclic, però hi ha dependències bidireccionals entre
  paquets, per exemple `data`/`terrain`, `ui`/`widgets` i `config`/`terrain`.

Els artefactes antics de `docs/architecture` descriuen una refactorització
anterior i són útils com a història, però no s'han tractat com a veritat actual
quan divergeixen del codi. Per exemple, l'informe antic parla de 554 proves i
de l'eliminació de `TerraLab/tools`, mentre que el `HEAD` auditat recull 628
proves i torna a contenir aquest paquet.

## 2. Resum executiu

TerraLab ja té fonaments sòlids que s'han de conservar:

- `scene/camera.py` i `scene/projection.py` són independents de Qt;
- el domini de terreny no depèn de Qt, GDAL ni xarxa;
- raycast i malla treballen per lots NumPy amb pressupost de memòria;
- Gaia i NGC poden usar `mmap`;
- el render viu en un procés separat i el frame raster passa a la UI mitjançant
  triple buffer de memòria compartida;
- els missatges són versionats, JSON i sense pickle;
- la bústia de render aplica `latest wins`, evitant una cua de frames obsolets;
- hi ha proves de fronteres, imports segurs, lifecycle i regressions visuals
  offscreen.

Tanmateix, l'arquitectura encara no permet canviar el motor gràfic sense tocar
els algoritmes. La causa principal és que el procés de render no és un simple
adaptador. `OffscreenSceneRenderer` calcula efemèrides de fallback, eclipses,
magnituds, projecció, selecció, il·luminació, terreny i interacció alhora que
emet operacions QPainter. `StarsRenderer`, `MilkyWayOverlay` i els mixins de
terreny també barregen selecció/càlcul amb dibuix Qt.

La migració proposada aplica un patró d'estrangulament vertical. Cada capacitat
es transforma de punta a punta:

```text
dades/model -> planificador pur -> RenderPlan tipat -> adaptador QPainter
```

La ruta antiga roman com a rollback només fins que la nova ruta assoleix
paritat funcional, visual i de rendiment. Després s'elimina; no es mantenen
dues arquitectures permanents.

## 3. Flux actual verificat

```text
Usuari / Qt
  -> AstronomicalWidget + AstroCanvas
     -> AstroCanvas._process_scene_snapshot() construeix un dict per frame
        -> RuntimeSupervisor / QProcess / JSONL
           -> render_service._RenderMailbox (latest wins)
              -> OffscreenSceneRenderer
                 -> càlcul científic i visual + QPainter
                    -> QImage sobre SharedMemory
                       -> SharedFramePresenter només presenta el slot

AstroCanvas / clients
  -> compute_service
     -> efemèrides, catàlegs, terreny i assets
        -> artefactes per ruta o petits payloads JSON
```

Evidències principals:

- `ui/astro_canvas.py:183-467` llegeix directament estat de widgets, decideix
  capes, Bortle i magnitud i construeix el payload.
- `runtime/protocol.py:69-120` copia mappings i serialitza JSON durant
  validació i codificació.
- `runtime/render_service.py:46-123` conserva només l'última escena.
- `runtime/render_service.py:159-214` crea un `QImage` directament sobre el
  buffer compartit i delega a l'offscreen renderer.
- `runtime/offscreen_renderer.py:678-977` transforma el payload, calcula
  condicions celestes, construeix `RenderState` i pinta el frame.
- `ui/frame_presenter.py:169-195` crea una vista QImage del mateix buffer i la
  presenta sense copiar els píxels.

## 4. Auditoria arquitectònica

### 4.1 Dependències i acoblament

El graf de mòduls no té cicles, però el sentit de les dependències encara no
correspon a Ports & Adapters:

| Relació | Arestes | Problema |
| --- | ---: | --- |
| `ui -> data` | 30 | la vista coneix serveis i tipus concrets de dades |
| `ui -> widgets` | 17 | widgets contenen controladors i càlcul reutilitzat |
| `data -> terrain` | 15 | la infraestructura de dades coneix detalls del domini de terreny |
| `terrain -> data` | 4 | dependència de paquet en tots dos sentits |
| `runtime -> terrain` | 9 | el runtime construeix implementacions concretes |
| `runtime -> widgets` | 6 | el procés de render depèn de controladors de la vista |
| `render -> widgets` | 1 | `StarsRenderer` usa `telescope_runtime` |
| `widgets -> ui` | 1 | inversió de capa |
| `config -> terrain` i `terrain -> config` | 4 | frontera de configuració no estable |

`runtime/offscreen_renderer.py` depèn directament de 21 mòduls TerraLab:
astronomia, catàlegs, render Qt, escena, terreny, clima i quatre mòduls de
`widgets`. No hi ha un port `Renderer` ni un registre de backends; el
`render_service` construeix `OffscreenSceneRenderer()` de manera directa.

### 4.2 Responsabilitats mal repartides

Principals unitats de risc:

| Unitat | Mida | Complexitat AST | Responsabilitats barrejades |
| --- | ---: | ---: | --- |
| `OffscreenSceneRenderer` | 2.829 línies | 363 | model celeste, fotometria, projecció, pick, interacció, terreny, clima i QPainter |
| `HorizonBaker` | 1.801 | 218 | kernel cohesiu però molt gran; cal preservar vectorització |
| `build_deferred_controls_ui` | 1.782 | 32 | construcció de gairebé tota la UI en una funció |
| `AssetOnboardingDialog` | 1.733 | 315 | vista, flux, validació i operacions d'assets |
| `StarsRenderer` | 1.704 | 283 | índex espacial, culling, fotometria, sprites, caches i QPainter |
| `ConstellationDrawingController` | 1.687 | 291 | model editable, gestos, selecció i dibuix |
| `AstroCanvas` | 1.362 | 227 | vista, snapshot, càmera, input, pick i menús |
| `HorizonWorker` | 1.353 | 281 | QObject, subprocess, superfície, publicació i lifecycle |
| `SurfaceSamplingService` | 1.150 | 186 | selecció, mostreig, LOD, cache, persistència i mètriques |

Hi ha 136 funcions o mètodes de producte de cent línies o més i 114 amb
complexitat ciclomàtica mínima de vint. La mida per si sola no obliga a
fragmentar kernels cohesionats; sí obliga a separar orquestració, polítiques,
I/O i adapters quan canvien per motius diferents.

### 4.3 Lògica de negoci a la vista

`AstroCanvas._process_scene_snapshot`:

- consulta checkboxes i atributs amb `getattr`;
- aplica la jerarquia de visibilitat del terreny;
- resol el mode de contaminació lumínica;
- calcula Bortle i magnitud efectiva;
- decideix capes;
- serialitza instrumentació, exposició, obertura i atmosfera;
- reconstrueix grups de constel·lacions cada frame.

Aquest treball pertany a un controlador d'aplicació i a constructors de
snapshots tipats, no a un `QWidget`.

`SkyController` existeix i és independent de Qt, però la cerca de referències
només en troba la definició; no governa el flux actiu d'`AstroCanvas`. Per tant,
la presència nominal d'un controlador no constitueix MVC.

### 4.4 Lògica científica o de planificació dins del render

Exemples sustentats pel codi:

- `offscreen_renderer.py:707-790` calcula posicions solars/lunars de fallback,
  separació angular i transmissió d'eclipsi.
- `offscreen_renderer.py:1708-1725` calcula el límit de magnitud segons
  crepuscle i eclipsi.
- `offscreen_renderer.py:1727-1796` desprojecta cada mostra de fons, calcula el
  color físic i el converteix immediatament a `QColor/QImage`.
- `offscreen_renderer.py:1988-2744` combina geometria, fotometria, registre de
  pick i dibuix del Sol, Lluna i planetes.
- `stars_renderer.py` importa projecció, fotometria del telescopi i QPainter en
  la mateixa classe.
- `terrain/overlay_mixins/triangle_paint.py` resol materials, llum,
  interpolació, caches i `QImage` en un únic camí.
- `render/sky/milkyway_overlay.py` carrega fitxers, transforma coordenades,
  mostreja textures, aplica extinció i pinta.

La futura Vista no ha de decidir magnitud, visibilitat, categoria, atmosfera,
llum, cache científica o hit-test. Rebrà batches i primitives resoltes.

### 4.5 Duplicació

Hi ha dos orquestradors de composició:

- `render/sky_renderer.py::SkyRenderer`, que crea horizon, Milky Way, stars,
  grid i overlays;
- `runtime/offscreen_renderer.py::OffscreenSceneRenderer`, que torna a
  orquestrar fons, Milky Way, grid, stars, efemèrides, terreny i overlays.

En producte, `SkyRenderer()` només es construeix des del mateix mòdul; la ruta
activa del servei construeix `OffscreenSceneRenderer()`. `SkyRenderer` es manté
principalment per proves i perquè `sky_color_phys` és importat per l'offscreen
renderer.

També coexisteixen:

- `draw_background` i `_draw_background`;
- `draw_celestial_grid` i `_draw_grid`;
- `draw_analytic_trails*` i `_draw_trails`;
- `draw_skyfield_objects` i `_draw_ephemeris`;
- render de scope i terreny distribuït en diversos camins.

La migració no crearà una tercera ruta. Cada fase extreu un planificador pur i
fa que la ruta activa i les proves el consumeixin; després retira el duplicat.

### 4.6 SOLID

| Principi | Estat | Evidència i correcció |
| --- | --- | --- |
| SRP | incomplet | classes gegants i snapshots construïts al widget; separar casos d'ús, planners, repositoris i adapters |
| OCP | violat en render | backend concret instanciat al servei; introduir registre i port de backend |
| LSP | parcial | providers `RasterProvider`, `SurfaceProvider` i `StarCatalogStore` ja tenen ABC; render no té contracte ni suite de conformitat |
| ISP | incomplet | `dict[str, Any]`, `extras` i duck typing exposen massa estat; substituir per DTOs petits i capacitats explícites |
| DIP | violat a composició | runtime/render depenen de widgets i implementacions; ports propietat de l'aplicació i wiring només al composition root |

## 5. Auditoria de rendiment

### 5.1 Fortaleses que no s'han de perdre

- `HorizonBaker` agrupa azimuts i mostres, usa NumPy i limita temporals amb
  `PerformanceBudget`.
- El backend vectoritzat té rollback independent
  (`TERRALAB_RAYCAST_VECTORIZED`).
- `_GeoRasterDataset` llegeix blocs/finestres, té LRU per bytes i prefetch amb
  `ThreadPoolExecutor`.
- `SurfaceSamplingService` té `ByteLRU`, cache persistent atòmica i mètriques.
- Gaia es processa out-of-core amb lots d'un milió de files i memmap.
- `CatalogCache` obre NPY/NPZ amb `mmap_mode="r"` i `allow_pickle=False`.
- El procés de render treballa sobre un slot de memòria compartida; la UI
  presenta el mateix slot.
- La bústia descarta frames i moviments intermedis obsolets.
- Un pool intern de raycast no està activat perquè el benchmark de 5 m ja és
  inferior a dos segons i no supera la barrera de millora mínima del 20 %.

Benchmark sintètic reproduït durant l'auditoria, tres repeticions i mediana:

| Cas | Temps | Pic RSS |
| --- | ---: | ---: |
| Terreny 30 m, 150 km, 0,5° | 0,185794 s | 70,5 MB |
| Terreny 5 m, 150 km, 0,5° | 0,331155 s | 88,2 MB |
| Terreny 1 m, 150 km, 0,5° | 1,180688 s | 124,3 MB |
| Stream 62.000 estrelles | 0,000700 s | 124,3 MB global |
| Stream 1.000.000 estrelles | 0,016584 s | 124,3 MB global |
| Stream 43.600.000 estrelles | 0,722248 s | 124,3 MB global |
| Stream 157.700.000 estrelles | 2,611054 s | 124,3 MB global |

Aquests valors cobreixen CPU sintètica, no E/S freda real de DEM, Gaia,
Copernicus ni el temps complet d'un frame QPainter. La fase 01 ha d'afegir
baselines de frame per escena i de payload IPC.

### 5.2 Colls d'ampolla i oportunitats

1. **Serialització repetida.** `envelope()` valida amb `json.dumps`,
   `encode()` torna a validar amb un altre `json.dumps` i després serialitza
   finalment. Un scene snapshot pot ser recorregut tres vegades.
2. **Snapshot complet per frame.** Càmera, estat estàtic, instrument, recursos,
   constel·lacions i configuració es reconstrueixen i s'envien encara que no
   hagin canviat.
3. **Render CPU monolític.** QPainter comparteix un únic procés per fons,
   estrelles, labels, terreny i overlays; una capa lenta allarga el frame
   complet.
4. **Càlcul dins del renderer.** Dificulta cachejar planners per dependències
   reals i impedeix reutilitzar resultats entre backends.
5. **QImage temporals.** El terreny converteix arrays NumPy a QImage i crida
   `.copy()` per separar-ne la vida; és segur però pot duplicar buffers grans.
6. **Caches locals difícils d'invalidar.** Moltes claus viuen com atributs de
   classes gegants; no hi ha un graf explícit de dependències del frame.
7. **Mètriques incompletes.** Hi ha diagnòstics per capa i logs de perf, però
   no una barrera P50/P95 per escena visual ni bytes/temps d'IPC.

Optimitzacions futures, sempre després de separar responsabilitats:

- paquets de frame diferencials i recursos immutables identificats per versió;
- validació estructural única abans de codificar;
- buffers binaris/memmap/shared memory per batches grans i JSON només per
  control;
- planners purs memoitzables per `resource_version + camera + time + viewport`;
- batches de punts, línies, triangles i text, evitant una crida per element;
- adapters GPU sense reescriure el càlcul;
- lease/owner explícit de buffers QImage per evitar `.copy()` només quan una
  prova de lifetime i un benchmark demostrin que és segur;
- caches centralitzades per bytes amb mètriques de hit/miss i invalidació
  tipada;
- cancel·lació per generació entre planner i renderer.

## 6. Auditoria de còpies i propietat

El repositori conté 116 crides textuals a `.copy()`, 258 a `.astype()`, 1.158
a `np.asarray()` i 33 a `np.array()`. Aquests recomptes són indicadors, no
proves de còpia: `np.asarray()` retorna una vista quan dtype/layout ja
coincideixen.

Classificació:

| Camí | Estat actual | Decisió |
| --- | --- | --- |
| frame render -> UI | zero-copy via SharedMemory i QImage sobre el buffer | preservar |
| catàleg NPY | memmap read-only i slices/vistes | preservar |
| colors fallback del catàleg | tres arrays creats un cop quan no hi ha canals | acceptable i cachejable |
| `SharedFramePresenter.submit` | còpia superficial del dict | substituir per DTO immutable; impacte menor |
| protocol | diverses còpies de mapping i fins a tres recorreguts JSON | evitar en fase 17 |
| ephemeris/extras per frame | còpies petites perquè el renderer els muta | moure mutació al builder i usar snapshots immutables |
| `HorizonWorker` | `copy.copy(profile)` per publicació atòmica, arrays compartits | preservar i documentar copy-on-write |
| NPZ tancat | arrays copiats abans de tancar l'archive | necessari amb el format actual |
| QImage des de NumPy | `.copy()` garanteix vida independent del buffer | no retirar sense owner/lease i prova |
| materials/triangle raster | còpies abans de mutar un resultat cachejat | necessàries per immutabilitat; revisar només amb profiling |

Política objectiu:

- arrays científics són read-only per defecte;
- un `ResourceHandle` estable identifica propietari, dtype, forma, versió i
  lifecycle;
- en procés, es passen referències;
- entre processos, es passen handles a memmap/shared memory, no llistes;
- les vistes NumPy es prefereixen a còpies;
- cada còpia mutable porta justificació de propietat o benchmark;
- mai s'exposa un buffer després d'alliberar-ne el lease;
- caches no poden mutar objectes compartits: copy-on-write explícit.

## 7. Interpretació estricta de MVC

### Model

Inclou dades i algoritmes purs:

- efemèrides i coordenades aparents;
- moviment solar, lunar i planetari;
- càmera, projecció i raycasting;
- DEM, ortofoto, cobertura, llum i terreny;
- fotometria, magnitud, colors, geometria i materials;
- caches científiques, índexs i estructures de dades.

No importa `PyQt5`, QPainter, Three.js, OpenGL o Vulkan. Es pot executar en
consola i provar amb arrays/DTOs.

### Controlador / aplicació

- rep intencions tipades de l'usuari;
- executa casos d'ús sobre ports;
- construeix `SceneFrame`/`RenderPlan`;
- controla generacions, cancel·lació, lifecycle i errors;
- selecciona el backend al composition root;
- publica resultats a la Vista.

No conté fórmules científiques ni operacions gràfiques.

### Vista i adapters de render

- Qt tradueix events a intencions i presenta outputs;
- el backend rep un pla ja decidit;
- QPainter converteix primitives a crides Qt;
- Three.js converteix les mateixes primitives a buffers/materials JS;
- no resol Bortle, magnitud, visibilitat, categoria, il·luminació o selecció.

Aplicar matrius, pujar buffers, rasteritzar i compondre són operacions de
presentació; decidir què hi ha al frame i amb quins valors és responsabilitat
del planner pur.

### Classificació obligatòria per fitxer

La descripció conceptual anterior no és suficient per acceptar una fase. El
mapa normatiu [`mvc-file-map.md`](mvc-file-map.md) assigna cada fitxer de
producte a **un únic** rol:

- `MODEL`: dades i algoritmes purs;
- `VIEW`: UI, presenter o backend de render;
- `CONTROLLER`: aplicació, casos d'ús i ports que aquesta posseeix;
- `OUTSIDE_MVC`: bootstrap, runtime, I/O, persistència, CLI o eines, sempre amb
  un subrol tècnic;
- `MIXED`: només deute actual pendent de separar.

No es permet deduir el rol només pel nom de la classe ni usar etiquetes dobles
com `MODEL/CONTROLLER`. Si un fitxer conté dues responsabilitats, és `MIXED` i
s'ha de dividir. A l'estat final:

1. el 100% de `TerraLab/**/*.py`, inclosos entrypoints i eines, apareix
   exactament una vegada al manifest;
2. no hi ha entrades `UNKNOWN`, absents, duplicades ni `MIXED`;
3. cada fitxer modificat conserva o actualitza explícitament el seu rol;
4. els tests d'arquitectura comproven que els imports coincideixen amb el rol;
5. cada informe de fase mostra fitxer, rol abans/després, responsabilitat
   extreta i prova de frontera.

Els adapters gràfics són `VIEW`; els adapters de dades/runtime són
`OUTSIDE_MVC`; els ports són `CONTROLLER`; els planners renderer-neutral són
`MODEL`. Aquesta convenció elimina els casos que el terme genèric «adapter»
podria deixar ambigus.

## 8. Regla de dependències objectiu

```text
ui/qt -----------------------> application <---------------- adapters/runtime
                                      |
                                      v
                                model + scene
                                      ^
                                      |
data/terrain providers ------ ports d'aplicació

render/qpainter -------------> render contracts <----------- render/threejs
                                      ^
                                      |
                                application
```

Regles:

1. Model i scene no importen UI, runtime ni implementacions de render.
2. Model no importa el port `Renderer`; el port és propietat d'application.
3. UI no importa model científic concret, `numpy`, rasterio o renderers.
4. Render adapters poden importar la seva API gràfica i contractes, no UI.
5. Runtime coneix ports i factories, no classes científiques disperses.
6. Providers implementen ports; el domini no els construeix.
7. Wiring concret només a `bootstrap/composition.py`.
8. Cap `dict[str, Any]` creua una frontera estable sense schema.

## 9. Estructura de paquets objectiu

La migració evita moure tot el repositori d'un sol cop. L'estat final desitjat:

```text
TerraLab/
  application/             # CONTROLLER
    commands.py            # CONTROLLER
    controller.py          # CONTROLLER
    lifecycle.py           # CONTROLLER
    scene_builder.py       # CONTROLLER
    ports/
      compute.py            # CONTROLLER
      data.py               # CONTROLLER
      rendering.py          # CONTROLLER
  bootstrap/               # OUTSIDE_MVC/BOOTSTRAP
    composition.py         # OUTSIDE_MVC/BOOTSTRAP
    settings.py            # OUTSIDE_MVC/BOOTSTRAP
  scene/                   # MODEL
    camera.py              # MODEL
    projection.py          # MODEL
    contracts.py           # MODEL
    resources.py           # MODEL
    plans/
      sky.py                # MODEL
      stars.py              # MODEL
      bodies.py             # MODEL
      overlays.py           # MODEL
      terrain.py            # MODEL
  render/                  # VIEW, excepte els dos fitxers indicats
    registry.py            # CONTROLLER
    conformance.py         # OUTSIDE_MVC/OBSERVABILITY
    qpainter/              # VIEW
      backend.py           # VIEW
      primitives.py        # VIEW
      presenter.py         # VIEW
    threejs/               # VIEW
      backend.py           # VIEW
      bridge.py            # VIEW
      assets/
  adapters/
    runtime/               # OUTSIDE_MVC/RUNTIME_ADAPTER
    qt/                    # VIEW
  astro/                   # MODEL
  light_pollution/         # MODEL
  terrain/
    domain/                # MODEL
    raycast/               # MODEL
    mesh/                  # MODEL
    surface/               # MODEL; I/O implementa ports fora del directori
    providers/             # OUTSIDE_MVC/DATA_ADAPTER
  data/                    # OUTSIDE_MVC/DATA_ADAPTER; DTOs purs al MODEL
  ui/
    qt/                    # VIEW
```

Els paquets existents poden conservar noms quan ja respecten la frontera. No
es crea `core/` com a contenidor genèric ni es fragmenta per mida. Els
comentaris anteriors orienten, però el rol exacte de cada fitxer és sempre el
del manifest; les excepcions com `render/registry.py` no hereten el rol del
directori.

## 10. Contractes centrals

Es recomana `typing.Protocol` per ports interns i dataclasses
`frozen=True, slots=True` per DTOs. Els adapters poden usar ABC quan necessiten
lifecycle compartit.

Esbós conceptual, no implementació:

```python
class RendererBackend(Protocol):
    backend_id: str

    def capabilities(self) -> frozenset[RenderCapability]: ...
    def start(self, output: RenderOutputPort) -> None: ...
    def submit(self, frame: SceneFrame) -> None: ...
    def request_pick(self, request: PickRequest) -> None: ...
    def close(self) -> None: ...

class RenderOutputPort(Protocol):
    def frame_ready(self, output: RenderOutput) -> None: ...
    def pick_ready(self, result: PickResult) -> None: ...
    def backend_failed(self, error: RenderFailure) -> None: ...
```

`SceneFrame`:

- schema i generació;
- viewport i càmera;
- observador i temps ja resolts;
- ordre de capes;
- referències versionades a recursos immutables;
- batches de primitives o plans semàntics resolts;
- índex de pick/hit-test;
- diagnòstics i claus de cache.

Primitives mínimes:

- `ColorRGBA`, `PointBatch`, `SpriteBatch`, `LineBatch`, `PolylineBatch`;
- `TriangleMeshBatch` amb posicions, índexs, normals i materials;
- `ImageLayer` amb `ResourceHandle`;
- `TextBatch` amb font lògica i política de col·lisió ja resolta;
- `ClipRegion`, `BlendMode`, `ZOrder`;
- `PickRecord` independent del backend.

`RenderOutput` és una unió discriminada:

- `RasterFrameHandle` per QPainter/OpenGL/Vulkan offscreen;
- `HostedSurfaceHandle` o confirmació de frame per Three.js;
- `RecordingFrame` per tests headless.

La Vista seleccionada al composition root presenta el tipus de sortida admès
pel backend. El Model no veu cap d'aquests tipus.

## 11. Selecció de backend i flags

Configuració estable:

```text
render.backend = "qpainter"
TERRALAB_RENDER_BACKEND=qpainter
```

Precedència:

1. override explícit de test/CLI;
2. variable d'entorn;
3. preferència d'usuari validada;
4. `qpainter` per defecte.

La factory valida ID, capacitats i compatibilitat amb el presenter. Un valor
desconegut falla amb missatge accionable; no cau silenciosament a QPainter.

Durant la migració hi haurà flags interns per capability/layer:

```text
TERRALAB_SCENE_SKY_BACKGROUND
TERRALAB_SCENE_STARS
...
```

Cada flag permet `legacy` o `scene`. No és API estable, no es guarda al Model i
desapareix a la fase 22. No es permet qualsevol combinació arbitrària: el
registre declara dependències entre capabilities i les proves cobreixen els
dos camins de la fase activa.

## 12. Concurrència, lifecycle i errors

Es conserva l'aïllament en processos:

- UI: event loop i presentació;
- application/runtime: supervisor asíncron;
- compute: treball científic i I/O;
- render: backend seleccionat.

Normes:

- cap espera bloquejant a la UI;
- `latest wins` per frames i moviments, FIFO per accions destructives;
- generació a totes les peticions/resultats;
- cancel·lació cooperativa quan és viable i terminació de procés només com a
  últim recurs delimitat;
- propietari únic per worker, pool i recurs;
- `close()` idempotent i verificat;
- errors tipats per operació; captures àmplies només a una frontera externa,
  amb context i traceback;
- cap fallback científic dins del renderer;
- reinici de backend conserva l'últim `SceneFrame` immutable i reconnecta
  recursos per versió.

## 13. Estratègia de tests

Piràmide obligatòria:

1. **Model pur:** fórmules, propietats, vectorització, dtype i invariants.
2. **Contractes:** schemas, read-only, versions, capabilities i ownership.
3. **Planner:** input científic -> `RenderPlan`, sense Qt.
4. **Conformitat de backend:** el mateix pla s'accepta a QPainter, Recording i
   Three.js.
5. **Adaptador QPainter:** píxels, composició, text, alpha i lifecycle.
6. **Processos:** JSON/control, recursos binaris, shared memory, restart i
   cancel·lació.
7. **MVC/UI:** events -> commands, sense ciència a widgets.
8. **Visual:** escenes golden o mètriques perceptuals amb tolerància
   documentada.
9. **Manual:** arrencada normal, càmera, temps, scope, capes i terreny.
10. **Rendiment:** P50/P95, RSS, bytes IPC, copies i cache hit rate.

Escenes visuals mínimes:

- dia, crepuscle i nit;
- cel gran angular i scope dens;
- Sol/Lluna, fases, eclipsi i planetes;
- Via Làctia i NGC;
- grid, constel·lacions, selecció i mesures;
- perfil, relleu, ortofoto i S2GLC;
- Bortle 1/5/9, temps i atmosfera;
- resize, interacció contínua i restart del render worker.

Cap captura golden pot ser l'únic oracle científic. Les propietats del Model es
proven per separat.

## 14. Decisions i compromisos

### D1. El Model no coneix `Renderer`

El text inicial suggeria “el Model només coneix la interfície”, però la regla
més estricta exigeix que el motor no conegui res gràfic. Per això el port de
render pertany a application/controller.

Avantatge: independència completa. Cost: una capa addicional de composició.

### D2. `RenderPlan` declaratiu

QPainter i Three.js no comparteixen primitives imperatives. El contracte usa
batches de dades i semàntica, no mètodes com `drawEllipse`.

Avantatge: extensibilitat i batching. Cost: cal definir schemas i paritat de
blending/text.

### D3. Migració vertical, no reordenació massiva

Cada capability acaba en un frame visible i amb rollback.

Avantatge: risc acotat. Cost: coexistència temporal vigilada.

### D4. Referències en procés, handles entre processos

No s'envien arrays grans per JSON ni es fan còpies defensives per rutina.

Avantatge: memòria estable. Cost: lifecycle i versions explícits.

### D5. QPainter continua sent el backend de referència

La primera migració conserva aparença i comportament. Three.js valida el
contracte després, no defineix retroactivament el Model.

### D6. Arquitectura abans d'optimització

No es trasllada lògica al renderer “perquè la GPU pot fer-ho” fins que el
contracte pur existeixi i hi hagi benchmark.

### D7. Flags temporals amb data de retirada

Els rollback flags són obligatoris durant una fase i deute prohibit després de
la fase 22.

## 15. Criteri final de “renderer substituïble”

La migració només està completa si:

- `astro`, `scene` pur, `light_pollution`, `terrain/domain`, raycast, mesh i
  planners importen sense PyQt instal·lat;
- la suite impedeix imports Qt al Model i lògica científica a adapters;
- el backend es tria en un únic composition root;
- QPainter i Three.js passen la mateixa suite de conformitat de capabilities;
- cap càlcul científic es duplica al backend alternatiu;
- `AstroCanvas` només tradueix input i presenta output;
- `docs/architecture/mvc_file_roles.json` cobreix exactament
  `TerraLab/**/*.py`, amb un únic rol i sense `UNKNOWN` o `MIXED`;
- el nom, directori, manifest i contingut real de cada fitxer són coherents amb
  `MODEL`, `VIEW`, `CONTROLLER` o el subrol declarat d'`OUTSIDE_MVC`;
- tots els frames grans passen per referències/handles amb ownership explícit;
- no queda ruta legacy ni flag temporal;
- proves, lint, barrera Pyright, benchmarks i verificació manual són verdes;
- canviar `render.backend` és suficient per arrencar el backend alternatiu,
  sense editar cap fitxer de Model.
