# FASE 18 — Backend Recording/Headless i suite de conformitat

## Rol i missió

Executa només la fase 18. Demostra que Model, controlador, builders i planners
funcionen completament en consola sense PyQt mitjançant un backend
`recording/headless` que registra plans i una suite de conformitat compartida.

## Prerequisits

- fases 01-17 `passed`;
- totes les capabilities representades per plans tipats;
- transport/resources versionats;
- QPainter passa baseline complet;
- working tree/gates verificats.

## Objectius

- `RecordingRendererBackend` zero-gràfic;
- CLI headless que construeix una escena real i produeix manifest;
- suite de conformitat de lifecycle, capabilities, frame, pick i errors;
- assertions semàntiques per totes les primitives;
- prova d'import/execució amb PyQt bloquejat;
- QPainter i Recording executen la mateixa suite;
- detectar qualsevol ciència o decisió que encara viu en QPainter.

## Fitxers previstos

- nous `TerraLab/render/recording/__init__.py`, `backend.py`
- nou `TerraLab/render/conformance.py`
- `TerraLab/render/registry.py`
- nou `TerraLab/cli/render_scene.py` o equivalent
- `pyproject.toml` només si s'afegeix entrypoint autoritzat
- tests de conformance/headless/import safety
- fixtures de totes les capabilities

## Contracte del Recording backend

Ha de:

- acceptar `SceneFrame` i validar schema/capabilities;
- registrar ordre, batches, counts, resource IDs, hashes i pick records;
- no rasteritzar ni importar API gràfica;
- produir output determinista JSON segur, sense serialitzar arrays complets;
- implementar start/submit/pick/close/restart;
- fallar si una primitive o capability queda sense tractar.

No és un mock permissiu. És una implementació de referència semàntica.

## Procediment

1. Enumera totes les capabilities actives a QPainter.
2. Defineix tests parametrizats de conformitat, no tests copiats per backend.
3. Implementa Recording i manifest determinista.
4. Implementa CLI que usa composition amb fake/local data ports i escena
   reproduïble.
5. Executa CLI en subprocess amb imports PyQt bloquejats.
6. Compara counts/order/pick/resources entre Recording i QPainter instrumentation.
7. Si una decisió només existeix a QPainter, mou-la al planner corresponent
   dins de l'abast mínim i torna a provar.
8. Prova errors de capability/resource/schema.
9. Mesura overhead de Recording i builder, no FPS gràfic.
10. Documenta com un nou backend adopta la suite.

## Proves obligatòries

- totes les capabilities celestes, interactives i de terreny;
- lifecycle/restart;
- unsupported capability;
- missing/stale resource;
- full/delta frame;
- pick results;
- deterministic manifest/hash;
- console import sense PyQt;
- QPainter conformance;
- suite completa, arquitectura, Ruff i Pyright.

## Validació manual

Executa la CLI headless amb:

- escena mínima;
- escena completa;
- recursos absents opcionals;
- perfil i relleu;
- Bortle/temps/scope;
- output manifest inspeccionat.

No cal judici de píxels nou, però les escenes QPainter baseline continuen
verdes.

## Rendiment

El CLI no ha de materialitzar catàlegs/DEM complets. Registra temps, RSS i mida
del manifest. Arrays es representen per hash/shape/dtype/resource ID.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- El backend Recording és `VIEW`, encara que sigui headless; contractes/plans
  són `MODEL`; port/dispatch són `CONTROLLER`; conformance és
  `OUTSIDE_MVC/OBSERVABILITY`.
- Assigna un únic rol abans/després a cada fitxer tocat.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- Model/application/scene funcionen completament sense PyQt;
- Recording tracta totes les capabilities;
- una suite de conformitat compartida passa per QPainter i Recording;
- cap ciència queda només a l'adapter;
- CLI determinista i bounded-memory;
- tests/gates verds;
- ledger fase 18 `passed`;
- no implementis Three.js encara.
