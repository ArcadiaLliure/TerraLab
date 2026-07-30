# FASE 14 — Ports de càlcul astronòmic, estrelles i clima

## Rol i missió

Executa només la fase 14. Retira dependències Qt i coordinació framework-specific
del Model de càlcul astronòmic, catàlegs estel·lars i clima. Conserva processos,
futures, cancel·lació i comportament mitjançant adapters runtime/Qt.

## Prerequisits

- fases 01-13 `passed`;
- totes les capabilities visuals ja consumeixen planners;
- baseline de compute service, star loading, weather i lifecycle;
- working tree/gates verificats.

## Evidència actual a revalidar

- `astro/ephemeris_coordinator.py` importa `QObject/pyqtSignal`.
- `data/star_data_coordinator.py` importa Qt.
- `runtime/clients.py` són QObjects i coneixen protocol concret.
- `weather/system.py` combina estat/model, provider remot, cache i renderable
  snapshot.
- `compute_service.py` construeix serveis concrets.

## Objectius

- ports application per efemèrides, catàleg/search i weather;
- serveis/model purs sense QObject/signals;
- adapters runtime que converteixen resultats a protocol;
- adapters Qt que converteixen callbacks/events a signals només a Vista;
- lifecycle/cancel·lació/generacions tipats;
- injecció de rellotge/executor/provider;
- Model executable en consola sense PyQt;
- eliminar imports Qt d'`astro` i coordinadors científics migrats.

## Fitxers previstos

- `TerraLab/astro/ephemeris_coordinator.py`
- `TerraLab/data/star_data_coordinator.py`
- `TerraLab/weather/system.py`
- `TerraLab/weather/metno_provider.py`
- `TerraLab/runtime/clients.py`
- `TerraLab/runtime/compute_service.py`
- `TerraLab/ui/astronomical_widget.py`
- nous `TerraLab/application/ports/compute.py`
- nous `TerraLab/adapters/runtime/compute.py`
- nous `TerraLab/adapters/qt/compute.py`
- tests d'ephemeris/star/weather/process/lifecycle

## Regles

- ports defineixen necessitats de casos d'ús, no repliquen API concreta;
- Model no emet pyqtSignal;
- adapters no reimplementen fórmules;
- resultats són snapshots immutables i versionats;
- una petició obsoleta no publica;
- providers remots són ports i errors tipats;
- cap bloqueig a UI.

## Procediment

1. Caracteritza cada API, signal, consumer i owner actual.
2. Defineix ports petits: request/snapshot/cancel/close segons necessitat real.
3. Extreu serveis purs, injectant executor/rellotge/provider.
4. Implementa adapters runtime/protocol.
5. Implementa bridge Qt només per consumers que encara requereixen signals.
6. Canvia composition root perquè injecti ports.
7. Migra consumers sense service locator/global.
8. Prova latest-wins, error, cancel, shutdown i restart.
9. Afegeix tests que importen Model amb PyQt bloquejat/no instal·lat.
10. Elimina QObject del paquet científic quan zero consumers antics.

## Proves obligatòries

- efemèrides context/timestamp/stale;
- compute fora del caller process;
- search/deep-sky artifacts;
- star general/deep tile, generations i shutdown;
- weather cache/backoff/offline/fallback;
- error propagation tipada;
- cancel/restart/no leaked futures;
- import safety sense QApplication/PyQt;
- suite completa i arquitectura.

## Rendiment

Mesura request latency P50/P95, throughput latest-wins, startup, RSS i executor
count. No copiïs snapshots/arrays; catàlegs per handle. No afegeixis un thread
per petició.

## Validació manual

- canviar temps/ubicació ràpidament;
- càrrega incremental Gaia;
- cerca/goto;
- weather on/off/offline;
- matar/reintentar compute worker;
- tancar/reobrir aplicació.

## Gate MVC per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i valida el manifest abans d'editar.
- Ports i use cases són `CONTROLLER`; càlcul astronòmic/estrelles/clima és
  `MODEL`; QProcess/IPC/providers són `OUTSIDE_MVC`; bridges Qt són `VIEW`.
- Assigna un únic rol abans/després a cada fitxer tocat i divideix coordinadors
  `MIXED` en fitxers físicament separats.
- Actualitza el manifest i publica la taula de delta amb prova de frontera.
- Falla per path absent/duplicat, `UNKNOWN`, rol doble o augment de `MIXED`.

## Acceptació

- serveis científics importables/executables sense Qt;
- signals confinats a adapter Qt;
- runtime depèn de ports/factories;
- cap fórmula duplicada;
- lifecycle i errors preservats;
- rendiment dins gates;
- tests, arquitectura, Ruff i Pyright verds;
- ledger fase 14 `passed`;
- no migris encara DEM/assets.
