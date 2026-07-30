# FASE 16 — Controlador d'aplicació i Vista Qt prima

## Rol i missió

Executa només la fase 16. Completa MVC: mou orquestració, estat i casos d'ús
d'`AstronomicalWidget`, `AstroCanvas` i mixins a un controlador d'aplicació.
La Vista Qt només construeix controls, tradueix events/values i presenta
outputs.

## Prerequisits

- fases 01-15 `passed`;
- ports de render/compute/data/terrain complets;
- planners i interacció independents;
- baseline UI, entrypoints i lifecycle;
- working tree/gates verificats.

## Evidència actual a revalidar

`AstronomicalWidget`, `AstroCanvas`, builders i mixins encara concentren estat
i orquestració. L'auditoria comptava 30 dependències `ui -> data`, 17
`ui -> widgets` i nombrosos `getattr`. `SkyController` existia però no era
consumer de la ruta activa.

## Objectius

- `ApplicationController` framework-neutral;
- store/state immutable o mutació encapsulada;
- commands/use cases per temps, ubicació, capes, scope, assets i terreny;
- lifecycle/start/stop/retry centralitzat;
- adapters Qt per events/signals/dialog results;
- `AstroCanvas` només input/presenter;
- `AstronomicalWidget` només composició visual/binding;
- eliminar lògica científica, NumPy i concrete services de `ui`;
- reduir mixins per responsabilitat, no fragmentar mecànicament.

## Fitxers previstos

- nous `TerraLab/application/controller.py`, `state.py`, `lifecycle.py`
- `TerraLab/ui/astro_canvas.py`
- `TerraLab/ui/astronomical_widget.py`
- `TerraLab/ui/sky_controller.py`
- `TerraLab/ui/widget_mixins/*`
- `TerraLab/ui/widget_*helpers.py`
- `TerraLab/ui/application_window.py`
- `TerraLab/widgets/*` residuals
- nous `TerraLab/adapters/qt/*`
- tests UI/process/lifecycle/architecture

## Regles

- controlador no importa QWidget/QObject/pyqtSignal;
- Vista no importa NumPy, rasterio, skyfield, `TerraLab.render`,
  `TerraLab.terrain` concret o `data.catalogs`;
- events es converteixen a commands tipats;
- controlador no conté fórmules: delega Model/planners;
- dialogs són ports de Vista o request/result, no crides des de Model;
- no substitueixis mixins per un altre monòlit.

## Procediment

1. Inventaria estat i ownership actual per camp/callback.
2. Defineix `ApplicationState` i commands.
3. Connecta ports de fases 14-15.
4. Migra un flux cada vegada: lifecycle, temps, ubicació, capes, terreny,
   assets, scope/interacció.
5. Converteix signals Qt a adapter bindings.
6. Fes que frame builder consumeixi state/control snapshots del controller.
7. Redueix `AstroCanvas` a events + `SharedFramePresenter`.
8. Redueix `AstronomicalWidget` a controls/bindings.
9. Elimina getattr/setattr dinàmic de fronteres migrades.
10. Afegeix tests AST estrictes de Vista.
11. Prova import/start/close/rebuild i worker restart.
12. Audita classes/mètodes grans després; no imposis límit arbitrari si són
    layout declaratiu cohesiu.

## Proves obligatòries

- controller pur per cada command/use case;
- bindings Qt amb fakes;
- time/location/layers/scope/terrain/assets;
- entrypoint i onboarding;
- canvas interactions;
- no QThread/calculation loop a UI;
- no imports prohibits;
- import safety sense crear QApplication;
- close/rebuild/retry;
- suite completa i goldens.

## Rendiment

Mesura startup, first frame, event-to-frame P50/P95, idle CPU, resize i RSS.
L'arquitectura no ha d'introduir una còpia completa d'ApplicationState per cada
mouse move si pot usar structural sharing/version.

## Validació manual

Recorre tota l'aplicació:

- onboarding/arrencada;
- controls de temps i ubicació;
- totes les capes;
- scope, selecció, mesures, constel·lacions;
- data library/DEM/surface/assets;
- resize, minimitzar/restaurar;
- fallada/retry workers;
- tancar/reobrir.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Tot `application/*.py` és `CONTROLLER`; widgets/presenters/bridges Qt són
  `VIEW`; ciència/plans són `MODEL`; processos i I/O són `OUTSIDE_MVC`.
- Cap controlador final pot quedar dins `ui/`; assigna un únic rol abans/després
  a cada fitxer tocat i separa `AstroCanvas`/widgets `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- MVC observable i verificat per imports/tests;
- controller framework-neutral i sense ciència;
- Vista sense concrete model/render/data dependencies;
- una sola font d'estat;
- paritat funcional/visual;
- rendiment dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 16 `passed`.
