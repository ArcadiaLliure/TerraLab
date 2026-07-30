# FASE 01 — Baseline i caracterització executable

## Rol i missió

Ets un enginyer principal treballant exclusivament en la fase 01 de la migració
MVC/render de TerraLab. Has de convertir l'estat actual en una línia base
reproduïble i una xarxa de regressió. No extreguis encara cap renderer, Model o
controlador.

El prompt és autònom. No assumeixis context de converses anteriors.

## Context conegut que has de revalidar

En redactar aquest prompt, l'arbre auditat era el d'`origin/refactor`
`4352e35f588a75b1e0fea8b71e3fce6801be53bf`, idèntic al merge
`1fbcf088a0bfc1f832fc0f2a8ba2808e3e783a7d`. Hi havia 628 proves amb una
fallada a `test_silent_generic_exception_debt_does_not_increase`, quinze errors
Ruff i Pyright 2.237/5 sota baseline 2.362/9. El problema de pytest provenia de
`TerraLab/tools/download_gaia_tap.py`. El checkout va canviar externament de
`refactor` a `fromcpu_togpu` durant l'auditoria; verifica sempre la branca real.

No donis aquests valors per vigents. Mesura'ls.

## Prerequisits

1. Llegeix tots els `AGENTS.md`.
2. Llegeix:
   - `docs/migration/terralab-decoupling/architecture.md`
   - `docs/migration/terralab-decoupling/migration-plan.md`
   - ADRs existents.
3. Verifica branca, `HEAD`, status i diffs.
4. Inventaria fitxers previs de l'usuari i no els alteris.
5. Si hi ha una fase anterior al ledger, atura't: aquesta ha de ser la primera.

## Objectius

- establir test/lint/type/build baseline honest;
- resoldre fallades actuals o registrar una excepció temporal aprovada;
- capturar inventari, graf, Qt coupling i grans símbols;
- crear caracterització visual del pipeline actiu;
- mesurar frame/IPC/RSS a més dels benchmarks CPU existents;
- documentar l'execució manual;
- no canviar comportament científic o visual.

## Fora d'abast

- introduir `RendererBackend`;
- moure lògica fora d'`OffscreenSceneRenderer`;
- canviar format de frames o protocol;
- reestructurar `TerraLab/tools` més enllà del mínim per restaurar baseline;
- “millorar” colors, magnitud, càmera o terreny.

## Fitxers a inspeccionar

- `pyproject.toml`
- `tests/architecture/*`
- `tests/test_runtime_process_isolation.py`
- `tests/test_offscreen_functional_regressions.py`
- `tests/test_astro_rendering.py`
- `tests/test_milky_way.py`
- `tests/test_terrain_surface_render.py`
- `benchmarks/*`
- `tools/dev/code_inventory.py`
- `docs/architecture/*baseline*`
- `TerraLab/tools/*`

Pots crear artifacts sota
`docs/migration/terralab-decoupling/execution/baseline/` i tests de
caracterització amb noms semàntics. No reescriguis artifacts històrics.

## Procediment obligatori

1. Executa i registra:

```powershell
python -m pytest --collect-only -q
python -m pytest -q
python -m pytest -q tests/architecture
python -m ruff check TerraLab scripts tests benchmarks tools/dev
python tools/dev/check_pyright_baseline.py
python -m compileall -q TerraLab
```

2. Reprodueix inventari en un directori temporal i extreu:
   mòduls, LOC, símbols, arestes, cicles, dependències Qt, funcions grans,
   captures àmplies i supressions.
3. Diagnostica qualsevol fallada amb evidència. Si és codi reintroduït, decideix
   entre restaurar la frontera ja acceptada o actualitzar explícitament la
   decisió; no augmentis baseline silenciosament.
4. Crea fixtures deterministes per aquestes escenes:
   - dia, crepuscle, nit;
   - scope dens;
   - Sol/Lluna/planetes/eclipsi;
   - Via Làctia/NGC;
   - terreny perfil i relleu;
   - superfície RGB i categòrica;
   - selecció/mesura/constel·lació.
5. Captura imatges offscreen de la ruta `render_service` real, no d'una funció
   auxiliar inactiva. Desa metadata completa i hash.
6. Afegeix una eina/test de benchmark que mesuri:
   - snapshot bytes i temps encode/decode;
   - temps P50/P95 de frame per escena;
   - paint/present ms;
   - pic RSS UI/render;
   - recompte de còpies de frame observable;
   - hits/misses de caches rellevants.
7. Executa `benchmark_optimization.py` amb almenys tres repeticions.
8. Documenta passos manuals: arrencada, pan/zoom, temps, scope, toggles, terreny,
   resize, tancament i restart.
9. Torna a executar tots els gates.

## Restriccions de tests visuals

- No aprovis una imatge només perquè s'ha generat.
- No apliquis actualització massiva de goldens.
- Les captures han de ser petites i deterministes; dades grans queden com a
  artifacts externs amb manifest.
- Fonts i DPI han de quedar registrats.
- Les propietats científiques continuen tenint assertions numèriques.

## Gate MVC per fitxer

- Llegeix completament `docs/migration/terralab-decoupling/mvc-file-map.md`.
- Crea `docs/architecture/mvc_file_roles.json` amb una entrada única per cada
  `TerraLab/**/*.py` i el seu `mvc_role` i `technical_role`.
- Afegeix una prova que compari exactament manifest i filesystem i falli per
  paths absents, duplicats, inexistents, `UNKNOWN` o rols dobles.
- Publica recompte i llista de `MODEL`, `VIEW`, `CONTROLLER`, `OUTSIDE_MVC` i
  `MIXED`; no intentis ocultar el deute actual reetiquetant-lo.
- Inclou la taula fitxer/rol abans/rol després/prova de frontera.

## Criteris d'acceptació

- pytest complet sense fallades inesperades;
- arquitectura verda o waiver explícit aprovat per l'usuari, amb caducitat a
  fase 02 com a màxim;
- Ruff verd o baseline de lint formal i decreixent aprovat; és preferible
  corregir els quinze errors sense canvi funcional;
- Pyright no empitjora;
- inventari i benchmarks reproduïbles;
- mínim una captura per família d'escena;
- P50/P95, RSS i IPC mesurats;
- checklist manual executada i confirmada;
- cap canvi visual/científic;
- ledger fase 01 complet.

## Verificació final

Executa com a mínim:

```powershell
python -m pytest -q tests/architecture
python -m pytest -q tests/test_runtime_process_isolation.py
python -m pytest -q tests/test_offscreen_functional_regressions.py
python -m pytest -q
python -m ruff check TerraLab scripts tests benchmarks tools/dev
python tools/dev/check_pyright_baseline.py
python -m compileall -q TerraLab
```

Informa resultats exactes, artifacts, diff i qualsevol waiver. No comencis la
fase 02.
