# FASE 09 — Picking, selecció i mesures

## Rol i missió

Executa només la fase 09. Separa la resolució de hit-test/pick i els casos d'ús
de selecció/mesura de la Vista i del backend. El resultat visual continua
arribant per plans renderer-neutral.

## Prerequisits

- fases 01-08 `passed`;
- totes les capes no interactives migrades;
- baseline de click, hover, pulse, context menu i mesures;
- protocol de generacions estable;
- working tree i gates verificats.

## Evidència actual a revalidar

`AstroCanvas` gestiona events i construeix payloads; `SharedFramePresenter`
envia pick requests. `OffscreenSceneRenderer` conserva l'últim `RenderState`,
visible stars/NGC/QRects, resol pick i també executa `interact`.
`widgets/measurement_tools.py` barreja controlador, geometria i QPainter.

## Objectius

- intencions `PointerPressed/Moved/Released`, `PickRequested`,
  `MeasurementCommand`;
- `PickIndex` immutable associat a `frame_generation`;
- política de selecció al controlador d'aplicació;
- geometria de mesures pura;
- `SelectionPlan` i `MeasurementPlan`;
- backend només presenta i, si cal, accelera query sota contracte equivalent;
- resultats stale descartats;
- context menus traduïts per la Vista, no decidits pel renderer;
- capability reversible.

## Fitxers previstos

- `TerraLab/ui/astro_canvas.py`
- `TerraLab/ui/canvas_mixins/interaction.py`
- `TerraLab/ui/frame_presenter.py`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/widgets/measurement_tools.py`
- nous `TerraLab/application/interaction.py`
- nous `TerraLab/application/commands.py`
- nous `TerraLab/scene/picking.py`
- nous `TerraLab/scene/plans/interaction.py`
- nou adapter QPainter d'overlays interactius
- tests process canvas/offscreen/recovered features

## Regles

- coordenades d'event es normalitzen una vegada amb viewport/DPR;
- un `PickResult` inclou generació i ID estable;
- cap objecte científic mutable o QWidget travessa el contracte;
- la selecció és estat d'aplicació, no estat del renderer;
- el backend no obre menús ni executa `goto`;
- mesures no usen QPainter per calcular distàncies/angles.

## Procediment

1. Caracteritza precedència actual de star/planet/NGC/surface i radis.
2. Defineix IDs, records, índex i tie-breakers.
3. Genera pick records durant planners de fases prèvies.
4. Implementa query pura i resultats versionats.
5. Mou selection pulse/state al controlador; planner produeix visuals.
6. Separa model/commands de mesures del dibuix.
7. Implementa adapter QPainter de selection/measurement primitives.
8. Fes que UI només tradueixi event -> command i result -> menú/view update.
9. Mantén protocol v1 adapter temporal si cal.
10. Enruta amb rollback i compara resultats.
11. Prova moves coalescibles i clicks/releases FIFO.

## Proves obligatòries

- precedència/tie break i radius;
- high DPI i resize entre request/result;
- result stale ignorat;
- star, planet, sun/moon, NGC i surface;
- selection pulse real;
- context goto amb clau canònica;
- ruler/angular/area o eines existents;
- Ctrl/Shift drag/click segons tests actuals;
- restart render entre frames;
- planner/model sense Qt;
- suite completa.

## Rendiment

Mesura build/query de PickIndex, bytes, RSS i P50/P95 de hover. Hover no pot
forçar una reconstrucció completa ni enviar arrays grans. L'índex comparteix
referències als batches del frame.

## Validació visual manual

- hover superfície i objectes;
- seleccionar cada família;
- pulse i context menu;
- goto i entrada a scope;
- mesures, clear i cancel;
- drag/pan amb modificadors;
- resize mentre hi ha una petició pendent.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Geometria i `PickIndex` són `MODEL`; selecció/mesura com a cas d'ús és
  `CONTROLLER`; events i overlays són `VIEW`.
- Assigna un únic rol abans/després a cada fitxer tocat i divideix els mòduls
  `MIXED`, sense etiquetes dobles.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- renderer no posseeix selecció de negoci ni menú;
- `PickIndex` generacional i tipat;
- measurement model sense Qt;
- UI prima en aquest flux;
- paritat de comportament/visual;
- rollback i latest/FIFO verificats;
- rendiment dins gates;
- tests/arquitectura/Ruff/Pyright verds;
- ledger fase 09 `passed`.
