# FASE 19 — Host Three.js i primitives fonamentals

## Rol i missió

Executa només la fase 19. Crea el host/bridge local de Three.js, lifecycle,
transport de recursos i adapters de primitives bàsiques. Demostra un frame
diagnòstic visible de punta a punta, però mantén QPainter com a backend per
defecte i impedeix seleccionar Three.js per una escena completa fins que
declari totes les capabilities.

## Prerequisits

- fases 01-18 `passed`;
- suite de conformitat Recording/QPainter completa;
- contracte `HostedSurfaceOutput` o equivalent definit;
- decisió de dependències web/packaging autoritzada;
- working tree/gates verificats.

## Objectius

- backend `threejs` registrat amb manifest de capabilities parcial;
- host Qt/WebEngine o host web seleccionat al composition root, mai al Model;
- assets Three.js locals i versionats, sense CDN obligatori;
- bridge tipat per lifecycle/frame/resources/pick/error;
- primitives: color, point/sprite, line/polyline, triangle mesh, image, text
  bàsic, clip/blend mínim;
- reconnect/restart/resize/DPR;
- escena diagnòstica visible i testable;
- backend complet rebutjat de forma accionable mentre faltin capabilities.

## Fitxers previstos

- nous `TerraLab/render/threejs/backend.py`, `bridge.py`, `protocol.py`
- nous `TerraLab/render/threejs/assets/*`
- nou presenter/host a `TerraLab/adapters/qt/`
- `TerraLab/render/registry.py`
- `TerraLab/bootstrap/composition.py`, `settings.py`
- `pyproject.toml` package data/dependencies si és necessari
- tests bridge/packaging/conformance parcial

## Restriccions

- no descarreguis Three.js en runtime;
- no referenciïs CDN com a únic camí;
- no habilitis `threejs` com a backend complet si falta terreny o interacció;
- no dupliquis fórmules/planners en JavaScript;
- no enviïs arrays grans com JSON lists;
- evita `eval` i codi dinàmic; aplica CSP/bridge allowlist;
- qualsevol dependència nova requereix justificació, llicència i packaging.

## Procediment

1. Decideix host i output amb ADR: pros/contres de Qt WebEngine, navegador
   extern o altre host compatible.
2. Verifica llicència i packaging local dels assets.
3. Defineix protocol bridge versionat i binari/handles per buffers grans.
4. Implementa lifecycle start/ready/submit/ack/error/close/restart.
5. Implementa resource registry JS amb versions i dispose.
6. Implementa primitives bàsiques contra fixtures de conformitat.
7. Implementa presenter host i resize/DPR.
8. Renderitza escena diagnòstica independent de l'app completa.
9. Registra capability manifest parcial i prova que full app selection falla
   amb llista exacta de mancances.
10. Mesura startup, transfer i frame ack.
11. Prova build/wheel/install extern.

## Proves obligatòries

- bridge encode/decode/version/errors;
- local assets/package data;
- no network required;
- lifecycle/restart/dispose;
- cada primitive bàsica;
- resource reuse/version invalidation;
- resize/DPR;
- security: no eval, allowlisted messages, malformed input;
- capability rejection;
- QPainter/Recording no regressen;
- suite completa.

## Validació visual manual

- obrir host Three.js;
- veure escena diagnòstica amb punts, línies, mesh, imatge i text;
- pan/resize/DPR del host de prova;
- simular restart;
- executar offline;
- confirmar que l'aplicació normal continua QPainter.

## Rendiment

Registra startup cold/warm, bytes bridge, upload time, frame ack P50/P95, GPU/
RSS si disponible i dispose. Cap buffer immutable es torna a pujar sense canvi
de versió.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Backend, bridge, presenter i JavaScript Three.js són `VIEW`; registry/selecció
  són `CONTROLLER`; host process és `OUTSIDE_MVC`; contracts són `MODEL`.
- Assigna un únic rol abans/després a cada fitxer tocat; JavaScript/assets també
  declaren propietari `VIEW` al ledger encara que el manifest executable sigui
  de `.py`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- host/bridge local i segur;
- primitives base conformes;
- escena diagnòstica visible;
- backend parcial no es pot seleccionar erròniament per full app;
- QPainter default intacte;
- packaging i offline verificats;
- tests/gates verds;
- ledger fase 19 `passed`;
- no implementis encara escenes celestes completes.
