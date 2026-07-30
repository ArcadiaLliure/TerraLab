# FASE 10 — Scope i constel·lacions editables

## Rol i missió

Executa només la fase 10. Migra els models i casos d'ús de telescopi/scope i
constel·lacions fora de `widgets` i del renderer, conservant interacció,
fotometria, FOV, edició i aparença.

## Prerequisits

- fases 01-09 `passed`;
- stars, picking i measurements ja renderer-neutral;
- baseline visual i tests d'interacció de scope/constel·lacions;
- catàleg scope cold/warm disponible;
- working tree verificat.

## Evidència actual a revalidar

`runtime/offscreen_renderer` importa `TelescopeScopeController`,
`ConstellationDrawingController`, spherical math i visual magnitude des de
`widgets`. `ConstellationDrawingController` és una classe gran que barreja
model editable, commands, pick i dibuix. `StarsRenderer` havia importat
`telescope_runtime`.

## Objectius

- model pur de `ScopeState/InstrumentProfile`;
- casos d'ús activate/deactivate/goto/nudge/zoom/track;
- model pur de constel·lacions, grups, nodes, segments i selecció;
- command handlers per gestos;
- `ScopePlan` i `ConstellationPlan`;
- QPainter adapter sense controladors;
- persistència/import/export separada per port;
- eliminar `runtime/render -> widgets` per aquestes àrees;
- capabilities reversibles.

## Fitxers previstos

- `TerraLab/widgets/telescope_scope_mode.py`
- `TerraLab/widgets/telescope_runtime.py`
- `TerraLab/widgets/optica_telescopica.py`
- `TerraLab/widgets/visual_magnitude_engine.py`
- `TerraLab/widgets/constellation_drawing.py`
- `TerraLab/ui/astro_canvas.py`
- `TerraLab/runtime/offscreen_renderer.py`
- nous `TerraLab/application/scope.py`, `constellations.py`
- nous `TerraLab/scene/plans/scope.py`, `constellations.py`
- nous adapters Qt/QPainter
- tests scope, spherical math, process interactions i offscreen

## Separació

Model:

- estat, invariants, càlcul òptic, RA/DEC/AltAz, nodes/segments.

Controlador:

- què significa click/right/double/shift/ctrl;
- tracking i commands;
- persist/save requests.

Vista:

- events i controls;
- presentació d'estat.

Renderer:

- dibuix de primitives del pla.

## Procediment

1. Escriu tests de caracterització dels gestos i estat.
2. Defineix models immutables o aggregates amb mutació encapsulada.
3. Mou càlcul òptic/fotometria/spherical math a paquets purs.
4. Mou commands de scope al controlador.
5. Separa model/commands de constel·lacions de qualsevol QPainter/Qt.
6. Defineix plans i pick records.
7. Implementa adapters.
8. Substitueix imports de `widgets` a runtime/render per application/scene.
9. Enruta per capability flags i compara.
10. Prova persistència round-trip i paths externs.
11. Prova warmup/cancel·lació del catàleg scope.

## Proves obligatòries

- activate/deactivate i auto-center;
- FOV, focal, sensor, aperture, exposure i ISO;
- slow/fast nudge;
- manual nudge no sobreescrit per tracking;
- RA/DEC directes i wrap;
- scope shape/mask i pick;
- create/select/add/delete/cancel group/node/segment;
- click/right/double i modificadors;
- import/export/round-trip de constel·lacions;
- model importable sense PyQt;
- cap import `widgets` des de render/runtime per scope/constellations;
- suite completa.

## Rendiment

Mesura scope startup cold/warm, first frame, índex, FPS/P95 i RSS. No reobris
catàleg ni reconstrueixis constel·lacions completes per move. Usa versions i
structural sharing.

## Validació visual manual

- entrar/sortir scope des de context goto;
- diversos instruments i zoom/nudge;
- tracking i control manual;
- dibuixar/editar múltiples constel·lacions amb tots els gestos;
- labels/segments/preview;
- save/reload;
- restart backend.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Matemàtica/geometria de scope i constel·lacions és `MODEL`; estat i commands
  són `CONTROLLER`; input, handles i paint són `VIEW`; runtime és
  `OUTSIDE_MVC`.
- Assigna un únic rol abans/després a cada fitxer tocat i redueix `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- render/runtime no importen controladors `widgets`;
- model/casos d'ús provables en consola;
- paritat funcional i visual;
- rollback verificat;
- scope performance dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 10 `passed`;
- no migris encara terreny.
