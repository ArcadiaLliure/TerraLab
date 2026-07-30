# FASE 03 — Builder tipat de `SceneFrame`

## Rol i missió

Executa només la fase 03. Extreu de la Vista la construcció i les decisions del
snapshot de frame, substituint el `dict[str, Any]` intern per DTOs immutables.
Mantén un encoder compatible amb el protocol actual. No migris encara cap capa
visual.

## Prerequisits

- fases 01 i 02 `passed`;
- backend `qpainter` seleccionat pel registre i visualment equivalent;
- baseline/goldens disponibles;
- working tree inspeccionat;
- lectura de `architecture.md`, `migration-plan.md`, ADRs i instruccions locals.

## Evidència actual a revalidar

`AstroCanvas._process_scene_snapshot` llegeix widgets/atributs, decideix capes,
resol Bortle/magnitud, prepara scope, terreny, temps, selecció, mesures,
constel·lacions i Via Làctia. `SharedFramePresenter.submit` rep un dict.
`OffscreenSceneRenderer.render` torna a normalitzar i mutar parts del payload.

## Objectius

- definir DTOs petits per viewport, temps, observador, càmera, layers, scope,
  recursos, terreny, weather i selecció;
- introduir un `SceneFrameBuilder` pur a application;
- moure jerarquia de capes, Bortle i magnitud fora del QWidget;
- representar absència/valors per defecte explícitament;
- validar valors finits, enums i versions una sola vegada;
- mantenir adapter `SceneFrame -> protocol v1 dict`;
- reduir `getattr` i mappings dinàmics a la frontera Qt.

## Fitxers previstos

- `TerraLab/application/scene_builder.py`
- `TerraLab/application/commands.py`
- `TerraLab/scene/contracts.py`
- `TerraLab/scene/render_state.py`
- `TerraLab/ui/astro_canvas.py`
- `TerraLab/ui/astronomical_widget.py`
- `TerraLab/ui/frame_presenter.py`
- `TerraLab/runtime/protocol.py`
- `TerraLab/runtime/offscreen_renderer.py` només per adapter/decoder mínim
- tests nous de builder/schema i tests actuals d'UI/processos

## Disseny obligatori

- dataclasses `frozen=True, slots=True`;
- enums o literals validats per capes/modes;
- `schema_version` i `generation`;
- resources per handle/path/version, no arrays copiats;
- cap QObject/QWidget/QColor als DTOs;
- cap callable a `extras`;
- constructors explícits, no lectura arbitrària d'un widget;
- un adapter Qt recull valors de controls i crea una intenció simple;
- el builder combina intenció, snapshots del Model i configuració.

No converteixis tot en un únic DTO de cent camps. Agrupa per estabilitat i
responsabilitat. No mantinguis `extras` com a via d'escapament.

## Procediment

1. Escriu tests de caracterització del payload actual, incloent valors per
   defecte i casos incomplets.
2. Defineix DTOs i invariants.
3. Defineix inputs del builder: `UserViewState`, `ModelSnapshots`,
   `ResourceVersions`, `Viewport`.
4. Mou, sense alterar fórmules:
   - `resolve_earth_layer_visibility`;
   - normalització de contaminació;
   - resolució Bortle/magnitud;
   - layer order;
   - paràmetres scope i terreny.
5. Crea encoder protocol v1 determinista i decoder a runtime.
6. Fes que AstroCanvas delegui; elimina decisions migrades del widget.
7. Evita reconstruir grups/recursos estàtics si la versió no canvia, però no
   implementis encara delta transport.
8. Prova hash/equivalència del payload abans/després.
9. Compara goldens, bytes i temps de snapshot.
10. Afegeix tests AST perquè UI no torni a calcular Bortle/magnitud.

## Proves obligatòries

- builder pur sense QApplication;
- immutabilitat i rebuig de NaN/inf;
- round-trip DTO -> v1 -> decoder;
- layer hierarchy i modes de llum;
- scope, terrain, selection i constellation schemas;
- UI process boundaries;
- runtime process isolation;
- offscreen functional regressions;
- suite completa.

## Rendiment i còpies

Registra:

- temps P50/P95 de construcció i encode;
- bytes del payload per escena;
- allocations si són mesurables;
- que catàleg, DEM i textures continuen per referència/path;
- que no es fa `deepcopy`.

Una dataclass immutable pot compartir tuples, arrays read-only i handles. Una
còpia petita per encode és admissible; justifica-la.

## Verificació visual manual

Amb backend QPainter:

- canvia hora, ubicació, Bortle i capes;
- activa/desactiva horizon/topography/surface;
- entra/surt de scope;
- edita selecció/constel·lació;
- comprova que cada canvi actualitza exactament el frame corresponent.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- `scene_builder.py` és `CONTROLLER`; contracts/plans purs són `MODEL`; el codi
  Qt que només emet commands/presenta és `VIEW`; protocol és `OUTSIDE_MVC`.
- Classifica tots els fitxers tocats amb un únic rol abans/després.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Criteris d'acceptació

- `_process_scene_snapshot` desapareix o queda com a adapter prim sense
  decisions;
- Model snapshots i intencions entren al builder tipat;
- no hi ha `Any/extras` nou a frontera;
- protocol v1 continua compatible;
- paritat visual;
- temps/bytes no empitjoren >5 %;
- tests, arquitectura, Ruff i Pyright verds;
- rollback documentat;
- ledger fase 03 `passed`;
- no s'ha migrat fons/estrelles encara.
