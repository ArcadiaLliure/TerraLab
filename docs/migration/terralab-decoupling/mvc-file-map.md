# Mapa canònic MVC per fitxer

## 1. Propòsit normatiu

Aquest document respon de manera inequívoca a la pregunta «a quina peça MVC
pertany aquest fitxer?». És normatiu per a tota la migració. L'arquitectura per
paquets ajuda a navegar, però no substitueix aquesta classificació per fitxer.

Cada fitxer `TerraLab/**/*.py`, inclosos entrypoints, CLI i eines, ha de tenir
**exactament un** `mvc_role`:

| `mvc_role` | Significat | Pot contenir | No pot contenir |
| --- | --- | --- | --- |
| `MODEL` | Model | dades, estat i algoritmes de negoci/científics purs | Qt, render, coordinació de casos d'ús |
| `VIEW` | Vista | widgets, presenters i adaptadors QPainter/Three.js/Recording | decisions científiques o casos d'ús |
| `CONTROLLER` | Controlador/aplicació | commands, casos d'ús, ports, lifecycle i construcció de frames | fórmules científiques o operacions gràfiques |
| `OUTSIDE_MVC` | suport tècnic | bootstrap, CLI, transport, persistència, providers i eines | assumir-se com a Model, Vista o Controlador |
| `MIXED` | deute transitori actual | més d'una responsabilitat detectada | existir a l'estat final |

`OUTSIDE_MVC` no és un calaix ambigu. Sempre porta un `technical_role`:
`BOOTSTRAP`, `ENTRYPOINT`, `DATA_ADAPTER`, `RUNTIME_ADAPTER`,
`PERSISTENCE_ADAPTER`, `OBSERVABILITY`, `CLI` o `DEV_TOOL`.

Un backend de render és `VIEW`, encara que tècnicament sigui un adapter. Un
provider de DEM és `OUTSIDE_MVC/DATA_ADAPTER`, no Model. Un port de render és
`CONTROLLER`, perquè és propietat de la capa d'aplicació; el Model no el coneix.

## 2. Manifest executable obligatori

La fase 01 ha de crear
`docs/architecture/mvc_file_roles.json`. Aquest Markdown explica la decisió; el
JSON és la font verificable durant l'execució. Cada entrada té aquest schema:

```json
{
  "path": "TerraLab/application/controller.py",
  "mvc_role": "CONTROLLER",
  "technical_role": "APPLICATION_SERVICE",
  "status": "TARGET",
  "introduced_or_resolved_in_phase": "16",
  "replaces": ["TerraLab/ui/astro_canvas.py"],
  "rationale": "Coordina casos d'ús i publica outputs; no calcula ni dibuixa."
}
```

Regles de validació:

1. El conjunt de `path` del manifest coincideix exactament amb
   `TerraLab/**/*.py`, exclosos només `__pycache__` i entorns generats. Tests i
   benchmarks externs a `TerraLab/` no formen part d'aquest manifest.
2. No hi ha paths duplicats, absents, inexistents ni `UNKNOWN`.
3. Un fitxer té un sol `mvc_role`; la responsabilitat secundària es descriu a
   `technical_role`, no amb una segona etiqueta MVC.
4. Un fitxer nou entra al manifest en la mateixa fase que el crea.
5. Un move conserva `replaces`; una eliminació queda a l'històric del ledger.
6. `MIXED` només pot disminuir. La fase 22 exigeix zero entrades `MIXED`.
7. Els tests d'arquitectura comproven tant cobertura com regles d'imports.

## 3. Classificador de l'estat auditat

Les regles següents s'apliquen en ordre. Una regla exacta té prioritat sobre una
regla de directori. Això classifica tots els fitxers actuals sense inferències.
Els `__init__.py` hereten el rol del directori que exposen, tret que apareguin
en una excepció explícita.

Sobre l'arbre auditat, el classificador cobreix 234 fitxers:
58 `MODEL`, 29 `VIEW`, 3 `CONTROLLER`, 88 `OUTSIDE_MVC` i 56 `MIXED`. La
mètrica anterior de 232 «mòduls de producte» excloïa els dos scripts
`TerraLab/tools`; el manifest MVC els inclou expressament com a `DEV_TOOL`.

### 3.1 Deute `MIXED` que s'ha de separar

| Fitxer o patró actual | Barreja actual | Destí obligatori |
| --- | --- | --- |
| `TerraLab/runtime/offscreen_renderer.py` | Model + Vista + Controlador | plans a `MODEL`; coordinació a `CONTROLLER`; QPainter a `VIEW`; eliminar el fitxer a fase 22 |
| `TerraLab/render/grid_renderer.py` | Model + Vista | layout a `MODEL/scene/plans`; paint a `VIEW/render/qpainter` |
| `TerraLab/render/horizon_renderer.py` | Model + Vista | geometria a `MODEL`; paint a `VIEW` |
| `TerraLab/render/overlays_renderer.py` | Model + Vista | decisions/layout a `MODEL`; paint a `VIEW` |
| `TerraLab/render/scope_renderer.py` | Model + Vista | càlcul de scope a `MODEL`; paint a `VIEW` |
| `TerraLab/render/sky_renderer.py` | Controlador + Vista | composició a `CONTROLLER`; paint a `VIEW` |
| `TerraLab/render/stars_renderer.py` | Model + Vista | selecció, magnitud i batches a `MODEL`; paint a `VIEW` |
| `TerraLab/render/sky/milkyway_overlay.py` | Model + Vista | sampling/planning a `MODEL`; textures/paint a `VIEW` |
| `TerraLab/astro/ephemeris_coordinator.py` | Model + Controlador + Qt | càlcul a `MODEL`; cas d'ús a `CONTROLLER`; bridge Qt a `VIEW` |
| `TerraLab/data/star_data_coordinator.py` | Model + Controlador + Qt/data adapter | selecció a `MODEL`; coordinació a `CONTROLLER`; I/O a `OUTSIDE_MVC` |
| `TerraLab/runtime/compute_service.py` | Controlador + runtime adapter | cas d'ús a `CONTROLLER`; procés/transport a `OUTSIDE_MVC` |
| `TerraLab/runtime/render_service.py` | Controlador + Vista + runtime adapter | dispatch a `CONTROLLER`; backend a `VIEW`; procés a `OUTSIDE_MVC` |
| `TerraLab/runtime/supervisor.py` | Controlador + runtime adapter | lifecycle abstracte a `CONTROLLER`; QProcess a `OUTSIDE_MVC` |
| `TerraLab/ui/astro_canvas.py` | Vista + Controlador + Model | events/presentació a `VIEW`; commands a `CONTROLLER`; decisions a `MODEL/plans` |
| `TerraLab/ui/astronomical_widget.py` | Vista + Controlador | widget a `VIEW`; casos d'ús a `CONTROLLER` |
| `TerraLab/ui/canvas_mixins/interaction.py` | Vista + Controlador | traducció d'event a `VIEW`; interaction use case a `CONTROLLER` |
| `TerraLab/ui/widget_mixins/*.py` | Vista + Controlador | controls Qt a `VIEW`; commands/lifecycle a `CONTROLLER` |
| `TerraLab/ui/widget_runtime_helpers.py` | Vista + Controlador + runtime | UI a `VIEW`; coordinació a `CONTROLLER`; procés a `OUTSIDE_MVC` |
| `TerraLab/widgets/constellation_drawing.py` | Model + Vista + Controlador | geometria a `MODEL`; use case a `CONTROLLER`; paint/input a `VIEW` |
| `TerraLab/widgets/measurement_tools.py` | Model + Vista + Controlador | geometria a `MODEL`; estat/commands a `CONTROLLER`; overlay a `VIEW` |
| `TerraLab/widgets/scope_ui_manager.py` | Vista + Controlador | UI a `VIEW`; mode use case a `CONTROLLER` |
| `TerraLab/widgets/telescope_runtime.py` | Model + Controlador + runtime | càlcul a `MODEL`; use case a `CONTROLLER`; adapter a `OUTSIDE_MVC` |
| `TerraLab/widgets/telescope_scope_mode.py` | Model + Vista + Controlador | transformacions a `MODEL`; mode a `CONTROLLER`; input/display a `VIEW` |
| `TerraLab/terrain/overlay.py` | Model + Vista | plans/materials a `MODEL`; raster QPainter a `VIEW` |
| `TerraLab/terrain/overlay_mixins/*.py` | Model + Vista | geometria/materials/hit-test a `MODEL`; paint a `VIEW` |
| `TerraLab/terrain/render/*.py` | Model + Vista | plans numèrics a `MODEL`; rasterització/paleta Qt a `VIEW` |
| `TerraLab/terrain/terrain_coordinator.py` | Model + Controlador + Qt | càlcul a `MODEL`; cas d'ús a `CONTROLLER`; bridge Qt a `VIEW` |
| `TerraLab/terrain/worker.py` | Model + Controlador + runtime | treball pur a `MODEL`; scheduling a `CONTROLLER`; worker a `OUTSIDE_MVC` |
| `TerraLab/terrain/surface/service.py` | Model + Controlador + data adapter | algoritmes a `MODEL`; use case a `CONTROLLER`; I/O/cache física a `OUTSIDE_MVC` |
| `TerraLab/weather/system.py` | Model + Controlador + provider | regles a `MODEL`; use case a `CONTROLLER`; xarxa a `OUTSIDE_MVC` |

Cap fila d'aquesta taula pot acabar simplement reetiquetada. Per deixar de ser
`MIXED`, el codi s'ha de separar i les proves han de demostrar les fronteres.

### 3.2 Fitxers actuals `MODEL`

| Paths exactes o patrons | Responsabilitat |
| --- | --- |
| `TerraLab/astro/{__init__,engine,ngc_catalog,search_engine}.py` | astronomia, catàlegs i cerca purs |
| `TerraLab/scene/*.py` | càmera, projecció i estat renderer-neutral |
| `TerraLab/light_pollution/{__init__,bortle,calibration,kernels,mlim,modes,processing}.py` | fotometria i models de contaminació |
| `TerraLab/layers/*.py` | dades de capa |
| `TerraLab/data/{constants,ray_precision,terrain_contracts,visibility_range}.py` | constants i contractes de domini |
| `TerraLab/data/catalogs/*.py` | índexs, caches lògiques i estructures de catàleg |
| `TerraLab/data/converters/*.py` | transformacions deterministes sense I/O |
| `TerraLab/terrain/{crs,ray_precision,representation,sampling,visibility_range}.py` | valors i algoritmes de terreny |
| `TerraLab/terrain/__init__.py` | límit públic del Model de terreny |
| `TerraLab/terrain/domain/**/*.py` | domini de terreny |
| `TerraLab/terrain/land_cover/**/*.py` | identitat de categories i estils numèrics |
| `TerraLab/terrain/mesh/**/*.py` | malla i normals |
| `TerraLab/terrain/raycast/**/*.py` | raycast i baking pur |
| `TerraLab/terrain/surface/{__init__,categorical,common,factory,geometry,light_pollution,rgb}.py` | superfícies i sampling pur |
| `TerraLab/widgets/{physical_math,spherical_math,visual_magnitude_engine}.py` | matemàtica/fotometria; s'han de moure fora de `widgets` |

### 3.3 Fitxers actuals `VIEW`

| Paths exactes o patrons | Responsabilitat |
| --- | --- |
| `TerraLab/common/{custom_widget_base,design_tokens}.py` | base i tokens visuals Qt |
| `TerraLab/render/**/*.py` no llistats com `MIXED` | context, primitives i límit de paquet de Vista |
| `TerraLab/ui/**/*.py` no llistats com `MIXED` o `CONTROLLER` | finestres, diàlegs, presenters i builders Qt |
| `TerraLab/ui/onboarding/{onboarding_bridge,onboarding_window}.py` | bridge i finestra d'onboarding |
| `TerraLab/widgets/**/*.py` no llistats com `MIXED` o `MODEL` | widgets, overlays i diàlegs Qt |

### 3.4 Fitxers actuals `CONTROLLER`

| Paths exactes | Responsabilitat |
| --- | --- |
| `TerraLab/ui/sky_controller.py` | controlador nominal; s'ha de connectar o substituir |
| `TerraLab/ui/layer_configurator.py` | traducció d'intencions de configuració |
| `TerraLab/ui/onboarding/first_run_manager.py` | cas d'ús de primera execució |

Que aquests fitxers siguin dins `ui/` és deute d'ubicació. El destí és
`TerraLab/application/`; cap controlador final viu dins la Vista.

### 3.5 Fitxers actuals `OUTSIDE_MVC`

| Paths exactes o patrons | `technical_role` |
| --- | --- |
| `TerraLab/__init__.py`, `TerraLab/__main__.py`, `TerraLab/config.py` | `ENTRYPOINT` / `BOOTSTRAP` |
| `TerraLab/cli/**/*.py` | `CLI` |
| `TerraLab/tools/**/*.py`, `TerraLab/util/**/*.py` | `DEV_TOOL` o importador offline |
| `TerraLab/common/*.py` no classificats com `VIEW` | infraestructura compartida/observabilitat |
| `TerraLab/common/performance/**/*.py` | pressupost i observabilitat |
| `TerraLab/debug/**/*.py` | `OBSERVABILITY` |
| `TerraLab/data/*.py` no classificats com `MODEL` o `MIXED` | `DATA_ADAPTER` |
| `TerraLab/data/assets/**/*.py` | `DATA_ADAPTER` |
| `TerraLab/data/copernicus/**/*.py` | `DATA_ADAPTER` |
| `TerraLab/runtime/{__init__,clients,frame_pool,protocol,service_io}.py` | `RUNTIME_ADAPTER` |
| `TerraLab/terrain/{asc_cache_builder,bake_process,data_sources,light_pollution_query,light_pollution_sampler,source_inspection,surface_store}.py` | `DATA_ADAPTER` / `RUNTIME_ADAPTER` |
| `TerraLab/terrain/infrastructure/**/*.py` | `DATA_ADAPTER` |
| `TerraLab/terrain/persistence/**/*.py` | `PERSISTENCE_ADAPTER` |
| `TerraLab/terrain/providers/**/*.py` | `DATA_ADAPTER` |
| `TerraLab/terrain/services/**/*.py` | `DATA_ADAPTER` fins a separar el cas d'ús |
| `TerraLab/terrain/surface/cache.py` | `PERSISTENCE_ADAPTER` |
| `TerraLab/weather/{__init__,metno_provider}.py` | `DATA_ADAPTER` |
| qualsevol `__init__.py` d'un paquet tècnic anterior | el mateix rol tècnic del paquet |

## 4. Mapa objectiu per fitxer

Aquest és el propietari MVC dels fitxers nous o consolidats que defineix el
pla. Un path no pot aparèixer en dues files.

### 4.1 `MODEL`

| Path objectiu | Contingut admès |
| --- | --- |
| `TerraLab/scene/contracts.py` | DTOs purs de frame, viewport, primitives i pick |
| `TerraLab/scene/resources.py` | identitat, ownership i lifecycle lògic de recursos |
| `TerraLab/scene/picking.py` | índex i geometria de pick renderer-neutral |
| `TerraLab/scene/plans/sky.py` | fons, horitzó i atmosfera |
| `TerraLab/scene/plans/stars.py` | selecció, magnitud i batches d'estrelles |
| `TerraLab/scene/plans/bodies.py` | Sol, Lluna, planetes, eclipsis i traces |
| `TerraLab/scene/plans/deep_sky.py` | Via Làctia i cel profund |
| `TerraLab/scene/plans/labels.py` | layout resolt de grid, brúixola, labels i HUD |
| `TerraLab/scene/plans/terrain_geometry.py` | geometria, LOD i índexs de terreny |
| `TerraLab/scene/plans/terrain_materials.py` | RGBA, materials, llum i atmosfera resolts |
| mòduls purs conservats de les seccions 3.2 | el mateix domini, sense imports Qt/runtime |

Els planners són `MODEL`: decideixen dades i geometria de l'escena sense
conèixer el port `RendererBackend`.

### 4.2 `CONTROLLER`

| Path objectiu | Contingut admès |
| --- | --- |
| `TerraLab/application/commands.py` | intencions i commands tipats |
| `TerraLab/application/controller.py` | casos d'ús i publicació de resultats |
| `TerraLab/application/lifecycle.py` | generacions, cancel·lació, restart i errors |
| `TerraLab/application/scene_builder.py` | composició de `SceneFrame` a partir de resultats del Model |
| `TerraLab/application/interaction.py` | picking, selecció i mesures com a casos d'ús |
| `TerraLab/application/ports/compute.py` | port de càlcul propietat de l'aplicació |
| `TerraLab/application/ports/data.py` | port de dades propietat de l'aplicació |
| `TerraLab/application/ports/rendering.py` | port de render; mai dins el Model |
| `TerraLab/render/registry.py` | registre controlat per aplicació; no instancia sense composition root |

### 4.3 `VIEW`

| Path objectiu | Contingut admès |
| --- | --- |
| `TerraLab/ui/qt/**/*.py` | widgets, events→commands i presentació d'outputs |
| `TerraLab/adapters/qt/**/*.py` | bridges Qt de Vista; cap cas d'ús |
| `TerraLab/render/qpainter/backend.py` | lifecycle i submissió del backend QPainter |
| `TerraLab/render/qpainter/primitives.py` | primitives→crides QPainter |
| `TerraLab/render/qpainter/presenter.py` | output raster→superfície Qt |
| `TerraLab/render/qpainter/{sky,stars,bodies,labels,terrain}.py` | traducció de plans resolts, sense càlcul científic |
| `TerraLab/render/threejs/backend.py` | lifecycle del backend Three.js |
| `TerraLab/render/threejs/bridge.py` | protocol host/JavaScript de presentació |
| `TerraLab/render/threejs/**/*.js` | buffers/materials/rasterització; cap regla científica |
| `TerraLab/render/recording/**/*.py` | Vista headless que registra primitives |

### 4.4 `OUTSIDE_MVC`

| Path objectiu | `technical_role` |
| --- | --- |
| `TerraLab/bootstrap/composition.py` | `BOOTSTRAP`; únic wiring concret |
| `TerraLab/bootstrap/settings.py` | `BOOTSTRAP`; parse/validació de configuració |
| `TerraLab/adapters/runtime/**/*.py` | `RUNTIME_ADAPTER`; processos i transport |
| `TerraLab/data/**/*.py` que implementi ports | `DATA_ADAPTER` |
| `TerraLab/terrain/providers/**/*.py` | `DATA_ADAPTER` |
| `TerraLab/terrain/persistence/**/*.py` | `PERSISTENCE_ADAPTER` |
| `TerraLab/render/conformance.py` | `OBSERVABILITY`; suite compartida de backends |
| entrypoints, CLI, importadors i eines | `ENTRYPOINT`, `CLI` o `DEV_TOOL` |

## 5. Delta de la fase 05: slice vertical d'estrelles

| Fitxer creat/mogut/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `scene/plans/stars.py` | — | `MODEL` | selecció, culling, Alt/Az, projecció, magnitud, color i batches | cap Qt, cache de sprites i paint | AST sense Qt/UI/runtime; batches immutables i picks estables |
| `scene/resources.py` | — | `MODEL` | handle de vistes de catàleg sense còpia | lifecycle físic del memmap | identitat de arrays i `allow_pickle=False` del loader existent |
| `scene/{optics,photometry,photometry_math}.py` | lògica sota `widgets/` | `MODEL` | equacions òptiques/fotomètriques i límit visual | cap widget o render | imports del planner/model sense `widgets` |
| `render/qpainter/stars.py` | — | `VIEW` | cache de `QImage`/`QPixmap` i dibuix per lots | culling, fotometria, colors i decisions de sprite | paritat de píxel contra legacy |
| `render/stars_renderer.py` | `MIXED` | `MIXED` transitori | façade de capability i rollback legacy | el camí `scene` no fa càlcul ni import de widgets | capability `legacy/scene`, paritat i rollback coberts |
| `application/star_rendering.py` | — | `CONTROLLER` | resolució validada del flag de capability | qualsevol fórmula o paint | default `scene`, error explícit i rollback `legacy` |
| `widgets/{optica_telescopica,physical_math,visual_magnitude_engine}.py` | `VIEW`/`MODEL` dispers | `MODEL` de compatibilitat | superfície d'import pública històrica | implementació científica activa | proves de magnitud i telescopi mantenen la API |

La façana legacy de `render/stars_renderer.py` continua classificada com a
`MIXED` només per permetre rollback fins a la fase 06. El camí seleccionat per
defecte (`scene`) consumeix exclusivament el planner `MODEL` i l'adapter
`VIEW`; no augmenta el recompte de deute `MIXED`.

## 6. Delta de la fase 06: Sistema Solar, eclipsis i traces

| Fitxer creat/mogut/modificat | Rol abans | Rol despres | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `astro/apparent.py` | — | `MODEL` | context UTC, fallback complet, separacio, refraccio, overlap i contrast lunar | Qt, QPainter i estat de canvas | snapshot coherent/stale, transmissio i refraccio numerics |
| `astro/photometry.py` | — | `MODEL` | magnitud i color nominal planetari | labels i rasteritzat | magnitud resolta abans del plan |
| `scene/plans/bodies.py` | — | `MODEL` | discs, fase, corona, planetes, picks i polylines de trails immutables | `QPainterPath`, `QImage` i qualsevol API Qt | AST sense Qt; fase monotona, pick i cache de trails |
| `render/qpainter/bodies.py` | — | `VIEW` | gradients, paths, labels i cache de la imatge de trails | efemerides, fotometria, culling o geometria astronomica | consumeix exclusivament `CelestialBodiesPlan`/`TrailPlan` |
| `application/celestial_rendering.py` | — | `CONTROLLER` | resolucio del flag reversible | ciencia i paint | default `scene`, validacio i rollback `legacy` |
| `runtime/offscreen_renderer.py` | `MIXED` transitori | `MIXED` transitori | wiring RenderState -> planners -> adapter i registre de picks | la ruta activa no calcula fallback, eclipsi, refraccio, fase, magnitud o trails | metadades de pipeline, scene/legacy i proves funcionals |
| `astro/ephemeris_coordinator.py` | `MIXED` transitori | `MIXED` transitori | scheduling Qt i publicacio de snapshots | fallback analitic i fotometria duplicada | usa `astro.apparent` i `astro.photometry` |
| `ui/frame_presenter.py` | `MIXED` transitori | `MIXED` transitori | presenta la metadada de debug | seleccio de capability | HUD mostra els tres pipelines |

La ruta activa de fase 06 es controla amb
`TERRALAB_CELESTIAL_RENDERING_PIPELINE=scene|legacy`; el valor per defecte es
`scene`. Les funcions de compatibilitat del renderer antic es mantenen nomes
per al rollback temporal. La planificacio seleccionada no depen de Qt ni de
widgets, i la cache de trails es propietat de l'adapter de Vista.

## 6.1 Delta de la fase 07: Via Lactia i cel profund

| Fitxer creat/mogut/modificat | Rol abans | Rol despres | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `scene/plans/deep_sky.py` | — | `MODEL` | transforms, sampling, extincio, `MilkyWayPlan`, `DeepSkyBatch`, labels i picks | paths, Qt, QImage i paint | AST sense Qt; seam/pols/GC, dust, NGC i picks tipats |
| `data/sky_resources.py` | — | `OUTSIDE_MVC/DATA_ADAPTER` | path, hash, decode, CSV→records i lifecycle mmap | politica de visibilitat i paint | `allow_pickle=False`, `mmap_mode="r"`, warm handle identic |
| `application/ports/sky_resources.py` | — | `CONTROLLER` | contracte de resources versionats | I/O fisic i ciencia | Protocol sense Qt/I/O |
| `application/deep_sky_rendering.py` | — | `CONTROLLER` | flags reversibles independents | planejament i QPainter | default scene, legacy independent, valor invalid explicit |
| `render/qpainter/deep_sky.py` | — | `VIEW` | composicio QImage, shapes i labels QPainter | files, coordenades, Bortle/dust | adapter sense `open`, `Path`, `np.load` o ciencia |
| `runtime/offscreen_renderer.py` | `MIXED` transitori | `MIXED` transitori | wiring de frame, resources, plans, adapter i rollback | la ruta `scene` no fa I/O ni calculs de Via Lactia/NGC | dispatch scene/legacy i metadades per capability |

La ruta activa de fase 07 es controla de manera independent amb
`TERRALAB_MILKYWAY_RENDERING_PIPELINE=scene|legacy` i
`TERRALAB_DEEP_SKY_RENDERING_PIPELINE=scene|legacy`; els dos defaults son
`scene`. Les facanes legacy romanen com a rollback temporal. El recompte de
`MIXED` no augmenta.

## 6.2 Delta de la fase 08: grid, bruixola, labels i HUD

| Fitxer creat/mogut/modificat | Rol abans | Rol despres | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `scene/plans/labels.py` | — | `MODEL` | projeccio del grid, ticks, contingut HUD, candidats, prioritat, clip i col·lisio | Qt, metriques concretes i paint | AST sense Qt/runtime; plans i no-overlap provats amb port fals |
| `application/ports/font_metrics.py` | — | `CONTROLLER` | contracte de mesura de font i revisio de cache | API Qt i politica de layout | Protocol Qt-free; el Model rep nomes metriques |
| `application/overlay_rendering.py` | — | `CONTROLLER` | resolucio del flag reversible | ciencia, layout i paint | default `scene`, rollback `legacy` i valor invalid |
| `render/qpainter/labels.py` | — | `VIEW` | fallback de font Qt i materialitzacio de paths/text/caixes | estat de canvas, cataleg, projeccio i col·lisio | source sense canvas/astro/data; consumeix nomes plans |
| `render/qpainter/{bodies,deep_sky}.py` | `VIEW` | `VIEW` | discs i glyphs resolts | labels, metriques i col·lisio | labels centralitzats a `TextBatch` |
| `render/grid_renderer.py` | `MIXED` | `VIEW` | seam de compatibilitat de callback | formula i paint del grid actiu | cap Qt o ciencia; el Model activa el grid |
| `runtime/offscreen_renderer.py` | `MIXED` transitori | `MIXED` transitori | wiring state→plans→adapter, diagnostics i rollback | la ruta scene no usa `QRectF` per labels ni calcula fonts en paint | metadata de pipeline i tests scene/legacy |
| `ui/frame_presenter.py` | `VIEW` | `VIEW` | presenta el frame de proces | HUD duplicat sobre el frame | un unic HUD dins el frame produït |

La ruta activa de fase 08 es controla amb
`TERRALAB_OVERLAY_RENDERING_PIPELINE=scene|legacy`; el valor per defecte es
`scene`. El recompte de `MIXED` baixa de 54 a 53.

## 7. Regles de noms i revisions

- Un fitxer `*_controller.py` és `CONTROLLER`; no es permet usar «controller»
  per a un widget, provider o renderer.
- Un fitxer sota `ui/` o `render/<backend>/` és `VIEW`.
- Un fitxer sota `application/` és `CONTROLLER`, inclosos els ports que posseeix.
- `model`, `domain`, `scene/plans`, matemàtica i ciència són `MODEL`.
- `adapter`, `provider`, `persistence`, `runtime`, `bootstrap`, `cli` i `tools`
  no s'etiqueten artificialment com MVC: són `OUTSIDE_MVC`.
- Els noms `manager`, `service`, `helper`, `coordinator` i `utils` no determinen
  rol. Necessiten una entrada explícita al manifest i s'han d'evitar en fitxers
  nous si amaguen més d'una responsabilitat.

Cada PR o fase ha d'incloure al seu informe una taula:

| Fitxer creat/mogut/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |

La revisió falla si una cel·la queda buida, si apareix `UNKNOWN`, si un fitxer
té dos rols finals o si el codi contradiu l'etiqueta.
