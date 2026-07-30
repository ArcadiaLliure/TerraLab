# Fase 05 — Slice vertical d'estrelles

## Resultat automàtic

- `StarScenePlanner` resol selecció, índex espacial scope, Alt/Az, projecció,
  magnitud, color, lots i `StarPickBatch` sense Qt, UI, runtime ni render.
- `QPainterStarAdapter` només cacheja recursos Qt i pinta els lots resolts.
- `TERRALAB_STAR_RENDERING_PIPELINE=scene|legacy` és reversible; `scene` és el
  valor per defecte i el metadatum de frame/HUD identifica el pipeline actiu.
- `StarCatalogResource` conserva les vistes del catàleg per referència; el
  `CatalogCache` existent continua carregant amb `mmap_mode="r"` i
  `allow_pickle=False`.
- La fotometria compartida s'ha extret a `scene/`; els mòduls antics sota
  `widgets/` són compatibilitat d'import.

## Paritat i rendiment

`tests/test_stars_vertical_slice.py` comprova píxels, picks, índex RA-wrap,
immutabilitat i rollback. La paritat és exacta per les fixtures deterministes
wide i scope, inclòs el camí d'índex espacial de 250k files.

El benchmark de set repeticions està a
`benchmarks/stars_vertical_slice_20260729.json`. La regressió màxima P50 del
frame `scene` contra `legacy` és **4,530 %**, dins el pressupost de 5 %; el
cas de 1M scope també queda dins el límit. Es mesura igualment el cold start de
l'índex scope (213,788 ms a 1M) i el warm planner (1,630 ms). El delta RSS
mesurat durant cada tram és inferior a 451 KiB i no apareix cap còpia completa
del catàleg.

## Gates executats

- `python -m pytest -q` — 675 passed en 70,46 s.
- `python -m pytest -q tests/architecture/test_mvc_file_roles.py tests/test_stars_vertical_slice.py tests/test_sky_background_slice.py` — 28 passed.
- `python -m ruff check …` i `ruff format --check` de la superfície de fase — passed.
- `python -m compileall -q TerraLab` — passed.
- `python tools/dev/check_pyright_baseline.py` — 2357 errors, 5 warnings;
  sota el baseline documentat de 2362/9.

## Pendent d'acceptació humana

Cal confirmar visualment wide field dia/crepuscle/nit, Bortle 1/5/9, colors
purs, pan/zoom continu, scope petit/gran amb ISO/obertura/exposició i canvi de
catàleg. No s'ha migrat Sol, Lluna, traces ni constel·lacions.
