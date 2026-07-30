# FASE 02 — Contractes de render i selector de backend

## Rol i missió

Executa només la fase 02. Introdueix el port de render, contractes mínims, un
registre/factory i una selecció de backend, connectant l'actual QPainter com a
adapter legacy sense alterar cap píxel.

Aquest prompt és autònom. Redescobreix el repositori.

## Prerequisits

- fase 01 `passed` al ledger;
- baseline, goldens i benchmarks accessibles;
- suite/lint/type gates en l'estat aprovat;
- branca i working tree verificats;
- lectura completa d'arquitectura, pla, ADRs i `AGENTS.md`.

Si la fase 01 no és inequívocament passada, no implementis.

## Regla arquitectònica central

El Model no importa ni coneix `RendererBackend`. El port pertany a
`TerraLab.application`. Els contractes no poden importar PyQt, QPainter,
Three.js, OpenGL, Vulkan, `ui` o implementacions concretes.

## Objectius

- definir lifecycle i capacitats d'un backend;
- definir inputs/outputs/errors mínims i tipats;
- crear un registre sense `if backend == ...` dispersos;
- introduir `render.backend` i `TERRALAB_RENDER_BACKEND`;
- encapsular l'actual `OffscreenSceneRenderer` com a `qpainter` legacy;
- seleccionar backend només al composition root;
- conservar protocol, shared frame pool i presenter actuals.

## Fitxers previstos

Nous, subjectes a validar noms contra el repositori:

- `TerraLab/application/__init__.py`
- `TerraLab/application/ports/__init__.py`
- `TerraLab/application/ports/rendering.py`
- `TerraLab/scene/contracts.py`
- `TerraLab/render/registry.py`
- `TerraLab/bootstrap/__init__.py`
- `TerraLab/bootstrap/settings.py`
- `TerraLab/bootstrap/composition.py`
- `TerraLab/render/qpainter/backend.py`

Existents:

- `TerraLab/runtime/render_service.py`
- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/runtime/protocol.py`
- `TerraLab/ui/frame_presenter.py`
- `TerraLab/__main__.py`
- `TerraLab/common/performance/flags.py` com a precedent, no com a lloc obligat
- `tests/architecture/test_boundaries.py`

## Contracte mínim

El disseny ha de representar:

- `backend_id`;
- `RenderCapability`;
- `start(output_port)`;
- `submit(frame/request)`;
- `request_pick`;
- `close` idempotent;
- outputs raster/hosted/recording discriminats;
- errors tipats;
- declaració de compatibilitat presenter/backend.

No anticipis primitives de totes les fases amb `Any`. Defineix només el
necessari i permet extensió per schemas versionats.

## Configuració

Precedència:

1. override explícit de test/CLI;
2. `TERRALAB_RENDER_BACKEND`;
3. preferència d'usuari validada;
4. `qpainter`.

Un ID desconegut ha de fallar de manera accionable. No fallback silenciós.
El backend legacy s'anomena clarament com a adapter temporal intern; l'ID
públic estable és `qpainter`.

## Procediment

1. Caracteritza construcció i lifecycle actuals.
2. Escriu tests de contracte/registry/config abans del wiring.
3. Implementa DTOs frozen/slots sense Qt.
4. Implementa registry explícit i injectable; no auto-registre per side effect
   d'import.
5. Implementa settings purs.
6. Encapsula `OffscreenSceneRenderer` sense moure'n lògica.
7. Canvia `render_service` perquè rebi/construeixi el backend des de composition.
8. Mantén QPainter per defecte.
9. Prova error d'ID, capacitat absent, start/close/restart i presenter
   incompatible.
10. Afegeix barreres AST d'imports.
11. Compara totes les escenes baseline píxel a píxel o amb tolerància zero:
    aquesta fase no justifica cap diferència.
12. Mesura startup, frame P50/P95, RSS i IPC. L'overhead de factory ha de ser
    irrellevant després de startup.

## Proves obligatòries

- unit tests de settings, registry, duplicate ID i missing backend;
- contractes importables en un entorn sense PyQt;
- runtime process isolation;
- render worker restart;
- entrypoint amb default i env override;
- arquitectura: Model no importa application rendering ni Qt;
- suite completa i goldens.

## Reversió

El wiring antic ha de poder-se recuperar amb revert del commit. No mantinguis
dues factories permanents. Si uses un flag intern per comparar, elimina'l
abans de passar la fase perquè el selector estable ja és el mecanisme.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest contra el filesystem abans
  d'editar.
- Classifica cada fitxer tocat amb un únic rol abans i després.
- En aquesta fase, ports i registre són `CONTROLLER`; backend/presenter són
  `VIEW`; settings/composition són `OUTSIDE_MVC/BOOTSTRAP`; contractes d'escena
  purs són `MODEL`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla la fase si hi ha paths absents/duplicats, `UNKNOWN`, rol doble o augment
  de `MIXED`.

## Criteris d'acceptació

- una sola línia de composició decideix el backend;
- `render_service` no instancia directament `OffscreenSceneRenderer`;
- contractes zero-Qt i zero-UI;
- QPainter legacy passa totes les capabilities declarades;
- selector desconegut falla clarament;
- paritat visual exacta;
- cap regressió >5 %, cap augment RSS injustificat;
- suite, arquitectura, Ruff i Pyright gates;
- document/ADR de la decisió;
- ledger actualitzat;
- no s'ha iniciat la fase 03.

## Sortida

Informa fitxers, contracte final, wiring, tests exactes, benchmarks, artifacts
visuals i prova de selecció. Si qualsevol gate falla, deixa `blocked`.
