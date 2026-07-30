# FASE 22 — Retirada legacy i hardening final

## Rol i missió

Executa només la fase 22, l'última. Elimina rutes legacy, flags temporals,
adapters de protocol antics i codi duplicat amb evidència. Endureix fronteres,
documentació, packaging, tests i benchmarks. No facis noves features ni canvis
visuals.

## Prerequisits

- fases 01-21 `passed`, sense waivers temporals pendents;
- QPainter, Recording i Three.js passen conformitat completa;
- paritat visual humana de Three.js aprovada;
- baselines i ledger complets;
- working tree verificat.

Si falta qualsevol prerequisit, bloqueja; no eliminis legacy.

## Objectius

- una sola arquitectura application/model/scene;
- backends QPainter/Recording/Three.js només com adapters;
- retirar `OffscreenSceneRenderer` monolític o reduir-lo a adapter sense
  duplicació, segons estat real;
- retirar renderers/mixins/wrappers sense consumers;
- retirar protocol v1 i capability migration flags;
- eliminar `Any/extras/getattr` de fronteres estables;
- regles AST/import-linter estrictes;
- zero cicles de mòdul i paquet;
- docs/ADRs/READMEs/packaging actualitzats;
- validació externa completa.

## Fitxers a auditar

- `TerraLab/runtime/offscreen_renderer.py`
- `TerraLab/render/*renderer.py`
- `TerraLab/terrain/overlay.py`, `overlay_mixins/*`
- `TerraLab/widgets/*` migrats
- `TerraLab/ui/*` residuals
- `TerraLab/runtime/protocol.py` adapters antics
- `common/performance/flags.py` i settings de migració
- tests/architecture, docs, pyproject/package data
- inventaris/deprecation registry

No pressuposis que tots s'han d'esborrar: demostra references, consumers i
equivalència.

## Procediment obligatori

1. Genera inventari actual i llista candidats legacy/duplicats.
2. Per cada candidat, registra:
   - definició;
   - references estàtiques;
   - accessos dinàmics/entrypoints;
   - consumers migrats;
   - test protector;
   - mecanisme de revert.
3. Elimina en grups petits i executa tests després de cada grup.
4. Retira flags temporals; només queda `render.backend` estable i flags de
   rendiment justificats.
5. Retira protocol/adapters antics després de prova de version mismatch clara.
6. Endureix fronteres:
   - Model sense PyQt/render/UI;
   - UI sense science/concrete data/render;
   - adapters sense ciència;
   - render/runtime sense `widgets`;
   - zero cicles de paquet;
   - ports/contracts sense `Any/extras`.
7. Executa vulture/ruff/pyright/inventari i investiga, no esborres
   automàticament per heurística.
8. Actualitza ADRs, READMEs, diagrams, changelog i guia de nou backend.
9. Construeix wheel/sdist, instal·la en venv extern i prova entrypoints.
10. Executa matrix completa de tests, visuals i benchmarks.
11. Compara mètriques finals amb fase 01 i explica diferències.
12. Prova reversió mitjançant revert del commit de retirada en un entorn segur,
    mai amb reset destructiu.

## Gates finals

```powershell
python -m compileall -q TerraLab
python -m pytest -q tests/architecture
python -m pytest -q
python -m ruff check TerraLab scripts tests benchmarks tools/dev
python tools/dev/check_pyright_baseline.py
python -m vulture TerraLab scripts --min-confidence 70
python tools/dev/code_inventory.py --output-dir <artifact-final>
python -m build
```

Afegeix:

- conformance QPainter/Recording/Three.js;
- CLI headless sense PyQt;
- install wheel extern;
- tests offline Three.js;
- benchmark optimization/terrain/scope/frame/IPC;
- screenshot matrix i smoke manual.

## Rendiment i memòria

Cap regressió respecte fase 21. Reporta també comparació fase 01 -> final:

- startup/first frame;
- scene P50/P95;
- scope cold/warm;
- terrain cold/warm;
- encode/decode/bytes IPC;
- RSS/UI/render/compute;
- draw calls/uploads;
- cache hits;
- copies justificades.

## Validació manual final

Executa checklist completa amb QPainter i Three.js, i CLI Recording:

- onboarding i data library;
- temps/ubicació/càmera;
- totes les capes i escenes;
- scope, selection, measurements, constellations;
- DEM/surface/assets;
- workers fail/retry;
- resize/DPI/offline;
- close/reopen;
- canvi de backend entre arrencades.

Cal confirmació humana explícita.

## Gate MVC final per fitxer

- Llegeix `docs/migration/terralab-decoupling/mvc-file-map.md` i compara el manifest exactament amb
  `TerraLab/**/*.py`.
- Exigeix cobertura del 100%, unicitat de path i rol, zero `UNKNOWN` i zero
  `MIXED`.
- Verifica que `MODEL` no importa Qt/render/runtime; `VIEW` no conté ciència o
  casos d'ús; `CONTROLLER` no pinta ni calcula; `OUTSIDE_MVC` declara subrol i
  implementa ports cap endins.
- Publica el mapa final complet i la taula de tots els paths legacy retirats o
  moguts, amb `replaces` i test de frontera.
- No tanquis la migració si el nom, directori, manifest i contingut real d'un
  sol fitxer es contradiuen.

## Criteris d'acceptació finals

- Model executa sense PyQt;
- Vista només input/presentació;
- Controller sense ciència/Qt;
- `render.backend` canvia QPainter/Three.js sense diff al Model;
- Recording valida consola;
- cap ruta legacy/flag temporal/protocol antic;
- zero cicles i fronteres estrictes;
- cap duplicació científica entre backends;
- arrays/resources amb ownership i còpies justificades;
- suite, Ruff, Pyright gate, vulture investigat, build/install verds;
- benchmarks dins pressupost;
- paritat visual manual;
- docs actuals;
- ledger fases 01-22 `passed`.

Només ara pots declarar completada la migració. Informa SHA/diff, evidències,
artifacts, mètriques abans/després i qualsevol risc residual no temporal.
