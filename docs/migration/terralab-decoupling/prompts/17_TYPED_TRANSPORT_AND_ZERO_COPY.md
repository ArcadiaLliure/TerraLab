# FASE 17 — Transport tipat, delta frames i zero-copy

## Rol i missió

Executa només la fase 17. Optimitza i endureix la frontera de processos després
de completar l'arquitectura: missatges de control tipats, una sola validació/
serialització, frames diferencials i recursos grans per handle. Preserva el
triple buffer raster zero-copy i el lifecycle.

## Prerequisits

- fases 01-16 `passed`;
- application controller i `SceneFrame` tipat actius;
- baselines IPC/frame/RSS de fase 01;
- resources tenen versions/ownership;
- working tree/gates verificats.

## Evidència actual a revalidar

`protocol.envelope()` copia/valida amb `json.dumps`; `encode()` torna a validar
i fa el dump final. La Vista envia un snapshot complet; `render_service`
aplica latest-wins. Frame pixels ja passen per `SharedMemory`/QImage sense
còpia entre render i UI. Catalog/terrain es passen principalment per paths i
memmap.

## Objectius

- schemas tipats i versionats per kind;
- validació estructural única;
- JSON només per control petit;
- `SceneFrameDelta` + full snapshot de resync;
- immutable resource handles per catàleg, textures, terreny i batches;
- shared memory/memmap per dades grans quan realment travessen procés;
- acknowledgements i lifecycle de leases;
- preservar latest-wins/FIFO/generacions/restart;
- mètriques bytes/encode/decode/copies.

## Fitxers previstos

- `TerraLab/runtime/protocol.py`
- `TerraLab/runtime/service_io.py`
- `TerraLab/runtime/supervisor.py`
- `TerraLab/runtime/render_service.py`
- `TerraLab/runtime/compute_service.py`
- `TerraLab/runtime/frame_pool.py`
- `TerraLab/ui/frame_presenter.py`
- `TerraLab/scene/contracts.py`, `resources.py`
- `TerraLab/application/controller.py`
- tests runtime isolation/protocol/restart/benchmarks

## Regles

- no introdueixis pickle;
- no substitueixis JSON per una dependència binària sense necessitat i
  autorització;
- no enviïs paths insegurs sense validar ownership/schema;
- no alliberis shared memory mentre hi ha lease;
- un delta perdut/restart força resync complet;
- control actions destructives conserven FIFO;
- frames/moves conserven latest-wins;
- no retiris `.copy()` de QImage/NumPy sense prova de lifetime.

## Procediment

1. Perfila encode/validate/decode i mida per escena.
2. Defineix schemas per message kind i error.
3. Elimina serialitzacions redundants; valida durant encode/decode una vegada.
4. Divideix frame en dynamic state, stable state i resource versions.
5. Implementa full snapshot + deltas amb base generation/hash.
6. Implementa resource registry/leases i reconnect després de restart.
7. Mantén frame pool actual; afegeix mètriques de slots/copies.
8. Migra un recurs gran només si actualment es copia; no compliquis paths/mmap
   que ja són eficients.
9. Prova corrupció, versió desconeguda, NaN, missing resource, out-of-order i
   restart.
10. Executa benchmarks abans/després amb cold/warm.
11. Mantén adapter protocol v1 una fase si cal rollback; elimina'l abans de
    passar o marca data de retirada fase 22.

## Proves obligatòries

- encode/decode round-trip per cada kind;
- invalid version/kind/payload;
- full/delta/resync;
- latest-wins i FIFO interaction;
- frame pool slots no overwrite;
- repaint mateix slot;
- resize/retired pools;
- render/compute restart;
- resource lease close/unlink;
- installed shadow package isolation;
- suite completa.

## Rendiment i còpies

Gate específic:

- un sol `json.dumps` per missatge de control;
- bytes/frame calent disminueixen o no augmenten;
- encode+decode P95 millora o queda dins 5 %;
- zero còpia de píxels render->UI preservada;
- cap array gran en JSON;
- RSS no augmenta >10 %/64 MiB;
- no leak de shared memory després de shutdown.

Usa instrumentació, no recomptes textuals com a única prova.

## Validació manual

- pan/zoom/time contínuu;
- scene updates estàtics/dinàmics;
- resize repetit;
- canviar recursos;
- matar/reiniciar render i compute;
- tancar durant frame/job;
- verificar absència de frames stale/corruptes.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Protocol, shared memory, frame pool i processos són
  `OUTSIDE_MVC/RUNTIME_ADAPTER`; lifecycle abstracte és `CONTROLLER`; presenter
  és `VIEW`; identitat lògica de recursos és `MODEL`.
- Assigna un únic rol abans/després a cada fitxer tocat i divideix qualsevol
  servei `MIXED`.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- transport tipat/versionat;
- una sola validació/serialització;
- delta/resync correcte;
- resources grans per handles;
- frame zero-copy preservat;
- protocol v1 retirat o waiver exclusiu fins fase 22;
- rendiment/lifecycle gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 17 `passed`.
