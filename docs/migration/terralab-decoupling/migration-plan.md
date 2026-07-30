# Pla incremental de migració

## 1. Política general

Les vint-i-dues fases són seqüencials. Una fase és petita en superfície de canvi, però
completa en una capacitat observable. No es permet deixar codi que només
compila sense estar connectat a la ruta executable.

Cada fase:

- comença verificant branca, `HEAD`, working tree i prerequisits;
- conserva els canvis previs de l'usuari;
- registra baseline abans de canviar;
- afegeix tests abans o al mateix commit que el comportament;
- introdueix rollback quan substitueix una ruta;
- executa tests dirigits, arquitectura, suite completa, Ruff i Pyright;
- compara rendiment i memòria;
- inclou una prova visual manual reproduïble;
- actualitza el mapa MVC per a cada fitxer creat, mogut, eliminat o modificat;
- actualitza el ledger de migració;
- acaba amb un commit reversible i cap placeholder.

El mapa canònic és [`mvc-file-map.md`](mvc-file-map.md) i l'execució manté el
manifest `docs/architecture/mvc_file_roles.json`. Abans i després de cada fase
s'ha de publicar aquesta taula:

| Fitxer | Rol abans | Rol després | Responsabilitat conservada | Responsabilitat extreta | Test de frontera |
| --- | --- | --- | --- | --- | --- |

No s'accepten files buides, rols dobles, `UNKNOWN` ni fitxers
`TerraLab/**/*.py` sense entrada. `MIXED` només pot disminuir; si una fase toca
un fitxer `MIXED`, ha de
resoldre almenys la responsabilitat que està migrant o justificar per què el
deute roman fins a una fase concreta.

Un canvi de `HEAD`, tests fallits aliens, dades locals absents o dependències
externes no autoritzen a inventar resultats. L'orquestrador decideix si
rebaselinar, bloquejar o continuar amb una excepció documentada.

## 2. Seqüència i dependències

| Fase | Resultat vertical | Depèn de |
| ---: | --- | --- |
| 01 | baseline actual verd o excepcions explícites | cap |
| 02 | contractes, registre i selector de backend | 01 |
| 03 | builder tipat de frame fora de Qt | 02 |
| 04 | fons de cel i màscara d'horitzó via scene pipeline | 03 |
| 05 | estrelles completes via planner + QPainter | 04 |
| 06 | Sol, Lluna, planetes, eclipses i traces | 05 |
| 07 | Via Làctia i cel profund | 06 |
| 08 | grid, brúixola, labels i HUD | 07 |
| 09 | selecció, pick i mesures | 08 |
| 10 | scope i constel·lacions editables | 09 |
| 11 | geometria de terreny renderer-neutral | 10 |
| 12 | materials, llum i atmosfera de terreny purs | 11 |
| 13 | adaptador QPainter de terreny i hit-test | 12 |
| 14 | ports de càlcul astronòmic, estrelles i clima | 13 |
| 15 | ports de DEM, superfície, catàlegs i assets | 14 |
| 16 | controlador d'aplicació i Vista Qt prima | 15 |
| 17 | transport tipat, delta frames i còpies acotades | 16 |
| 18 | Recording/Headless renderer i conformitat completa | 17 |
| 19 | host Three.js i primitives bàsiques | 18 |
| 20 | capacitats celestes Three.js | 19 |
| 21 | terreny, interacció i paritat Three.js | 20 |
| 22 | retirada legacy, flags i hardening final | 21 |

## 3. Fitxa de cada fase

### Fase 01 — Baseline i xarxa de seguretat

Objectiu: fer reproduïble l'estat actual abans de tocar arquitectura.

Fitxers principals:

- `tests/architecture/test_boundaries.py`
- `docs/architecture/silent_exception_baseline.json`
- `TerraLab/tools/*`
- `pyproject.toml`
- `benchmarks/*`
- nous `tests/visual/` i `docs/migration/.../ledger.md`

Treball:

- resoldre o aprovar explícitament la prova fallida i els quinze errors Ruff;
- capturar test count, Pyright, graf, dependències Qt i benchmarks;
- crear `mvc_file_roles.json` amb cobertura exacta de `TerraLab/**/*.py`, rol
  únic, subrol tècnic i deute `MIXED`;
- afegir un test que falli per path absent/duplicat/desconegut i un resum per
  `MODEL`, `VIEW`, `CONTROLLER`, `OUTSIDE_MVC` i `MIXED`;
- crear escenes de caracterització del render actiu;
- afegir mètriques de P50/P95, RSS i mida/temps del snapshot IPC;
- documentar com executar la verificació manual.

Riscos: “arreglar” comportament durant caracterització o actualitzar un
baseline per ocultar una regressió.

Acceptació: baseline explicat i repetible; cap fallada inesperada; snapshots
visuals produïts per la ruta real; cap canvi de comportament; el 100% de
`TerraLab/**/*.py` classificat una sola vegada al manifest MVC.

### Fase 02 — Contractes i selecció de backend

Objectiu: poder seleccionar QPainter a través d'un port sense canviar el frame.

Fitxers previstos:

- nous `TerraLab/application/ports/rendering.py`
- nous `TerraLab/scene/contracts.py` i `resources.py`
- nous `TerraLab/render/registry.py`
- nous `TerraLab/bootstrap/settings.py` i `composition.py`
- `TerraLab/runtime/render_service.py`
- `TerraLab/__main__.py`
- tests de contracte/registre

Treball:

- definir IDs, capabilities, lifecycle, output i errors;
- afegir `render.backend`/`TERRALAB_RENDER_BACKEND`;
- registrar un adapter legacy QPainter que encapsula l'offscreen actual;
- validar valors desconeguts i compatibilitat presenter/backend;
- deixar `qpainter` com a default.

Risc: crear una interfície que filtri QPainter. Acceptació: cap tipus Qt als
contractes, backend concret construït només al composition root i paritat
total amb la ruta anterior.

### Fase 03 — `SceneFrameBuilder` tipat

Objectiu: treure decisions de `_process_scene_snapshot` del `QWidget`.

Fitxers previstos:

- nous `TerraLab/application/scene_builder.py`, `commands.py`
- `TerraLab/scene/contracts.py`
- `TerraLab/ui/astro_canvas.py`
- `TerraLab/ui/astronomical_widget.py`
- `TerraLab/runtime/protocol.py`
- tests de snapshots i UI

Treball:

- definir DTOs per temps, observador, càmera, capes, scope i recursos;
- traslladar Bortle, magnitud i jerarquia de capes al builder pur;
- fer que la UI emeti intencions/estat de control mínim;
- mantenir un encoder v1 temporal compatible;
- provar immutabilitat, valors finits i schema.

Acceptació: la UI ja no calcula Bortle/magnitud ni reconstrueix estructura
científica; el frame visual és idèntic.

### Fase 04 — Fons de cel i horitzó

Objectiu: primera capacitat visible completa del nou pipeline.

Fitxers previstos:

- nous `TerraLab/scene/plans/sky.py`
- nous `TerraLab/render/qpainter/sky.py`
- `TerraLab/render/sky_renderer.py`
- `TerraLab/runtime/offscreen_renderer.py`
- tests de color, projecció i golden de dia/crepuscle/nit

Treball:

- separar `sky_color_phys` de `QColor`;
- generar colors/primitives renderer-neutral;
- adaptar-los a QImage/QPainter;
- incloure màscara de sòl/horizon bàsica;
- flag reversible de capability.

Acceptació: cap fórmula de cel a l'adapter, paritat visual documentada i temps
de capa no pitjor que baseline més 5 % sense justificació.

### Fase 05 — Estrelles

Objectiu: migrar índex, culling, magnitud, color i batches d'estrelles.

Fitxers previstos:

- nous `scene/plans/stars.py`
- nous `render/qpainter/stars.py`
- `render/stars_renderer.py`
- `widgets/telescope_runtime.py`
- `data/catalogs/*`
- tests de stars, scope startup i regressió offscreen

Treball:

- separar `StarScenePlanner` del dibuix;
- conservar memmap, índexs i caches;
- fer batches immutables de sprites/punts;
- moure fotometria fora de `widgets`;
- mantenir pick records renderer-neutral.

Acceptació: no es copia el catàleg per frame; paritat de recompte, color,
magnitud i scope; FPS/P95 i RSS dins pressupost.

### Fase 06 — Sistema solar, eclipses i traces

Objectiu: treure tota la ciència celeste de l'offscreen renderer.

Fitxers previstos:

- `astro/engine.py`, nous mòduls purs si cal
- `astro/ephemeris_coordinator.py`
- nous `scene/plans/bodies.py`
- nous `render/qpainter/bodies.py`
- `runtime/offscreen_renderer.py`
- tests d'eclipsi, refracció, fases i planetes

Acceptació: cap fallback d'efemèrides, separació angular, magnitud o geometria
física dins QPainter; Torroja 2026 i les fases existents passen visualment i
numèricament.

### Fase 07 — Via Làctia i cel profund

Objectiu: separar recursos, sampling i dibuix.

Fitxers previstos:

- `render/sky/milkyway_overlay.py`
- `astro/ngc_catalog.py`
- nous repositoris/ports de textura i NGC
- nous `scene/plans/deep_sky.py`
- nous adapters QPainter
- tests de Via Làctia, NGC i memmap

Acceptació: l'adapter no obre fitxers ni transforma coordenades
astronòmiques; resources versionats; cost de reload només quan canvia la
versió.

### Fase 08 — Grid, brúixola, labels i HUD

Objectiu: convertir overlays informatius a batches.

Fitxers previstos:

- `render/grid_renderer.py`, `render/overlays_renderer.py`
- `runtime/offscreen_renderer.py`
- nous `scene/plans/labels.py`
- nous `render/qpainter/labels.py`
- tests de col·lisió, fonts, DPI i coordenades

Acceptació: política de labels resolta al planner; QPainter només presenta;
paritat a resize i DPI.

### Fase 09 — Selecció, picking i mesures

Objectiu: independitzar interacció del backend.

Fitxers previstos:

- `widgets/measurement_tools.py`
- `ui/astro_canvas.py`
- `runtime/offscreen_renderer.py`
- nous `application/interaction.py`
- nous `scene/picking.py`
- tests de click, hover, pulse i mesures

Acceptació: pick usa un índex renderer-neutral associat a la generació del
frame; events obsolets es descarten; backend no decideix l'acció de negoci.

### Fase 10 — Scope i constel·lacions

Objectiu: separar model editable, gestos i pintura de dues àrees molt
acoblades.

Fitxers previstos:

- `widgets/constellation_drawing.py`
- `widgets/telescope_scope_mode.py`
- `widgets/telescope_runtime.py`
- `ui/astro_canvas.py`
- nous use cases/plans/adapters
- tests de gestos, nudge, FOV i selecció

Acceptació: cap controlador de `widgets` importat pel procés de render; scope i
constel·lacions es poden provar sense Qt; edició visual completa.

### Fase 11 — Geometria de terreny

Objectiu: produir buffers de geometria, raster i pick sense Qt.

Fitxers previstos:

- `terrain/render/geometry.py`, `triangle_raster.py`, `overlay_types.py`
- `terrain/overlay_mixins/projection_geometry.py`
- nous `scene/plans/terrain_geometry.py`
- tests de z-buffer, seams, nadir, NoData i visibilitat

Acceptació: cap `QPointF`, `QPolygonF` o `QImage` a la geometria; arrays
read-only i cache keys tipades; benchmark dins pressupost.

### Fase 12 — Materials, llum i atmosfera de terreny

Objectiu: fer renderer-neutral tot el color final del terreny.

Fitxers previstos:

- `terrain/render/palette.py`, `materials.py`, `lighting.py`, `atmosphere.py`
- mixins de materials/light/triangle paint
- nous `scene/plans/terrain_materials.py`
- tests extensos de `test_terrain_surface_render.py`

Acceptació: colors RGBA numèrics, no `QColor/QBrush`; identitat categòrica,
llum, lluna, boira, bloom i cache invariants preservats.

### Fase 13 — Adapter QPainter de terreny

Objectiu: presentar el pla complet de terreny i mantenir hit-test.

Fitxers previstos:

- `terrain/overlay.py` i `overlay_mixins/*`
- nous `render/qpainter/terrain.py`
- `runtime/offscreen_renderer.py`
- tests offscreen/golden i benchmark de terreny

Acceptació: adapter només converteix buffers a QImage/primitives; ownership
segur; perfil, relleu, RGB i categòric funcionen visualment.

### Fase 14 — Ports de càlcul astronòmic, estrelles i clima

Objectiu: retirar QObject/futures del Model.

Fitxers previstos:

- `astro/ephemeris_coordinator.py`
- `data/star_data_coordinator.py`
- `weather/system.py`
- `runtime/clients.py`, `compute_service.py`
- nous ports i adapters runtime/Qt

Acceptació: Model importable sense PyQt; coordinadors framework-specific a
adapters; lifecycle i cancel·lació passen.

### Fase 15 — Ports de dades, DEM, superfície i assets

Objectiu: invertir dependències de `data`/`terrain` i desacoblar workers Qt.

Fitxers previstos:

- `terrain/terrain_coordinator.py`, `worker.py`, `surface/service.py`
- `terrain/providers/*`, `data/assets/*`, `data/source_catalog.py`
- ports de repositori i adapters
- tests de data sources, worker, cache i shutdown

Acceptació: sense dependència de paquet bidireccional, owner únic, providers
substituïbles i cap Qt al servei científic.

### Fase 16 — Controlador d'aplicació i Vista Qt prima

Objectiu: completar MVC.

Fitxers previstos:

- `ui/astro_canvas.py`, `astronomical_widget.py`, mixins i builders
- `ui/sky_controller.py`
- nous `application/controller.py`, `lifecycle.py`
- nous `adapters/qt/*`
- tests de UI, entrypoint i lifecycle

Acceptació: widgets no importen NumPy, terrain, render ni catàlegs concrets; no
calculen; només tradueixen events i presenten.

### Fase 17 — Transport tipat i zero-copy

Objectiu: eliminar payload complet redundant i còpies evitables.

Fitxers previstos:

- `runtime/protocol.py`, `supervisor.py`, `render_service.py`, `frame_pool.py`
- `ui/frame_presenter.py`
- `scene/resources.py`
- tests d'IPC, shared memory, restart i benchmark

Acceptació: una sola serialització per missatge; control JSON petit; batches
grans per handles; delta frames/versioning; cap regressió de lifecycle.

### Fase 18 — Backend Recording/Headless i conformitat

Objectiu: provar que el sistema funciona completament sense API gràfica.

Fitxers previstos:

- nous `render/recording/*`, `render/conformance.py`
- CLI headless de diagnòstic
- tests de totes les capabilities

Acceptació: es pot construir una escena i registrar totes les primitives des
de consola sense PyQt; QPainter i Recording passen el mateix contracte.

### Fase 19 — Host Three.js i primitives bàsiques

Objectiu: validar un segon backend real sense canviar el Model.

Fitxers previstos:

- nous `render/threejs/*` i assets empaquetats
- composition/settings i presenter host
- tests de protocol, lifecycle, càmera i primitives bàsiques

Acceptació: seleccionar `threejs` en proves arrenca el host, tradueix càmera,
recursos i primitives bàsiques, i no introdueix cap diff al Model/planners.

### Fase 20 — Capacitats celestes Three.js

Objectiu: portar al segon backend les capes celestes amb paritat mesurable.

Fitxers previstos:

- adaptadors celestes a `render/threejs/*`
- fixtures visuals i tests de conformitat per capability
- benchmarks de CPU, GPU, memòria i transport

Acceptació: les capabilities celestes acordades passen el contracte compartit,
els llindars visuals i els pressupostos de rendiment.

### Fase 21 — Terreny, interacció i paritat Three.js

Objectiu: completar l'abast funcional del backend sense duplicar ciència.

Fitxers previstos:

- adaptadors Three.js de geometria i materials de terreny
- picking, selecció, mesures, resize i recuperació d'errors
- matriu final de capabilities i escenes de paritat

Acceptació: Three.js és estable per a l'abast acordat; terreny i interacció
passen conformitat i cap càlcul científic s'ha traslladat a l'adapter.

### Fase 22 — Retirada legacy i hardening

Objectiu: deixar una sola arquitectura.

Fitxers:

- `runtime/offscreen_renderer.py` i renderers legacy
- flags temporals
- tests/READMEs/ADRs/packaging

Treball:

- eliminar rutes duplicades amb evidència;
- prohibir imports Qt al Model i imports `widgets` des de render/runtime;
- eliminar `Any/extras` de fronteres;
- exigir zero `MIXED`, zero `UNKNOWN` i cobertura exacta del manifest MVC;
- confirmar zero cicles de mòdul i de paquet;
- suite, Ruff, Pyright incremental, build, benchmarks, smoke visual i
  instal·lació externa.

Acceptació: tots els criteris finals d'`architecture.md`, cap waiver temporal i
una assignació inequívoca Model/Vista/Controlador/fora de MVC per a cada fitxer.

## 4. Gates de rendiment comuns

Per defecte, una fase no pot:

- empitjorar P50 o P95 de la seva escena més d'un 5 %;
- augmentar pic RSS més d'un 10 % o 64 MiB, el menor;
- materialitzar un catàleg/DEM complet abans memmap/out-of-core;
- duplicar un buffer de frame complet sense justificació;
- augmentar bytes IPC estàtics per frame;
- reduir cache hit rate sense explicar una invalidació correcta.

Si una diferència és sorollosa, s'augmenten repeticions i es reporten mostres.
Una regressió només s'accepta amb causa, impacte, alternativa, rollback i
aprovació explícita de l'orquestrador.

## 5. Reversibilitat

Fases 04-13 tenen un flag temporal de capability. La reversió consisteix a
tornar a `legacy` sense canviar schema de Model o dades. Fases 14-17 mantenen
adapters de compatibilitat versionats durant una fase. Fases 18-20 afegeixen
implementacions, no canvien el default. La fase 21 estabilitza la paritat
completa. La fase 22 canvia la reversibilitat: només elimina legacy després que
QPainter i Three.js passin conformitat; la recuperació és el revert del commit
de fase, no un flag etern.
