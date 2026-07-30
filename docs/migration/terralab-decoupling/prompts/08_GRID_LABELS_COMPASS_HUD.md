# FASE 08 — Grid, brúixola, labels i HUD

## Rol i missió

Executa només la fase 08. Migra overlays informatius no interactius a plans
renderer-neutral: grid celeste, brúixola, labels i HUD. Mantén la política de
contingut i col·lisió fora de QPainter.

## Prerequisits

- fases 01-07 `passed`;
- capabilities celestes principals sobre scene pipeline;
- baselines per DPI, resize, wide/scope i labels de planetes/NGC;
- font runtime i fallback caracteritzats;
- working tree/gates verificats.

## Evidència actual a revalidar

`render/grid_renderer.py` i `render/overlays_renderer.py` usen tipus Qt i poden
llegir canvas/state dinàmic. `runtime/offscreen_renderer` conté `_draw_grid`,
`_draw_compass`, `_draw_hud` i manté `_occupied_labels` com `QRectF`.
La selecció de labels i el dibuix estan acoblats.

## Objectius

- `GridPlan`, `CompassPlan`, `TextBatch` i `HudPlan` tipats;
- projecció, ticks, contingut, format i col·lisió resolts al planner;
- font lògica i mètriques amb port explícit;
- bounds renderer-neutral, no `QRectF`;
- adapter QPainter limitat a paths/text;
- capability flags reversibles;
- eliminar rutes duplicades per les capabilities migrades.

## Fitxers previstos

- `TerraLab/render/grid_renderer.py`
- `TerraLab/render/overlays_renderer.py`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/debug/diagnostics.py`
- nous `TerraLab/scene/plans/labels.py`
- nous `TerraLab/application/ports/font_metrics.py`
- nous `TerraLab/render/qpainter/labels.py`
- tests de render, fonts, offscreen i goldens

No migris selecció, mesures, scope o constel·lacions.

## Disseny

Si la col·lisió depèn de mètriques de font, application defineix un port de
mesura. L'adapter Qt pot implementar-lo, però la política de quina etiqueta
guanya i on es col·loca és pura. No codifiquis una suposada mètrica universal.

`TextBatch` ha d'incloure text ja resolt, posició/anchor, estil lògic,
prioritat, clip, z i pick ID opcional. QPainter no consulta objectes
astronòmics per construir-lo.

## Procediment

1. Caracteritza contingut, ordre, fonts i col·lisió actuals.
2. Defineix rectangles/vectors numèrics renderer-neutral.
3. Extreu grid/projecció/ticks i brúixola.
4. Extreu format/selecció/col·lisió de labels.
5. Extreu HUD a un `HudViewModel` produït pel controlador/planner.
6. Implementa `FontMetricsPort` i adapter Qt.
7. Implementa adapter QPainter de línies/text.
8. Enruta capabilities amb flags, compara legacy/scene.
9. Prova DPI 1/1,25/1,5/2, resize i fonts absents.
10. Confirma que diagnostics són dades i el HUD només les presenta.

## Proves obligatòries

- grid RA/DEC, horizon/poles i wrap;
- compass 0/90/180/270 i rotació càmera;
- format de labels, prioritat i no-overlap;
- font registrada/fallback;
- HUD visible/amagat i contingut;
- resize, DPR i scope;
- adapter sense `getattr(canvas)` ni ciència;
- suite offscreen, runtime, arquitectura i completa.

## Rendiment

Mesura recompte de labels, candidates rebutjats, draw calls, P50/P95 i cache
de layout. Un canvi de frame sense canvi de label inputs ha de reutilitzar el
layout quan sigui segur.

## Validació visual manual

- grid on/off i pan 360°;
- labels planetes/NGC a escena densa;
- brúixola durant rotació;
- HUD normal/scope/debug;
- resize ràpid i diversos DPR;
- font absent simulada i restart.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Layout, col·lisions i plans de labels són `MODEL`; paint/font API són `VIEW`;
  toggles i coordinació de casos d'ús són `CONTROLLER`.
- Assigna un únic rol abans/després a cada fitxer tocat i separa els renderers
  `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- QPainter només materialitza línies/text;
- cap `QRectF/QPainterPath/QPen` al planner;
- política de labels provable sense Qt;
- paritat visual aprovada;
- rollback verificat;
- gates de rendiment/qualitat verds;
- ledger fase 08 `passed`;
- no migris interacció.
