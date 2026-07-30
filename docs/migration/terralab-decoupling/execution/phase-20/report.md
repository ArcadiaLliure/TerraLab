# Informe d'Execució — Fase 20: Capabilities Celestes Completes a Three.js

## Resum General

La Fase 20 va adaptar i portar al backend Three.js totes les capabilities celestes i informatives existents (`SKY_BACKGROUND`, `STARS`, `EPHEMERIS_BODIES`, `DEEP_SKY`, `GRID`, `LABELS`, `SCOPE`, `CONSTELLATIONS`, `PICKING`, `MEASUREMENTS`), consumint directament els plans numèrics i geometries ja generades pels planners purs de domini. Cap ciència astronòmica o càlcul de magnitud/posició es recalcula en JavaScript; tot el flux es manté 100% renderer-neutral. QPainter es preserva com a backend per defecte de producció. Dado que el terreny queda per a la Fase 21, la selecció del backend `threejs` per a escenes d'aplicació amb terreny es manté rebutjada netament amb missatge d'error accionar per capacitats pendents.

---

## Canvis Implementats

### 1. Contractes i Backend Three.js (`MODEL` / `VIEW`)
- **`TerraLab/core/rendering_contracts/contracts.py`**:
  - Ampliació del tipus enumerat `RenderCapability` amb les etiquetes celestes backend-neutrals (`STARS`, `EPHEMERIS_BODIES`, `DEEP_SKY`, `GRID`, `LABELS`, `SCOPE`, `CONSTELLATIONS`, `MEASUREMENTS`).
- **`TerraLab/render/threejs/protocol.py`**:
  - Definició de primitives celestes al protocol (`sky_gradient`, `star_batch`, `celestial_body`, `milkyway_mesh`, `grid_lines`, `label_text`, `scope_mask`, `constellation_segment`, `measurement_pulse`) i actualització de l'allowlist.
- **`TerraLab/render/threejs/backend.py`**:
  - Declaració de totes les noves capacitats celestes al manifiest de `ThreeJSRendererBackend`.
  - Implementació de `_build_plan_bundle_manifest` per serialitzar els plans celestes de `RenderPlanBundle` a la llista de primitives del bridge.
- **`TerraLab/render/threejs/assets/threejs_runner.js`**:
  - Ampliació del runner JS amb manegadors de renderitzat per a totes les primitives celestes en Three.js.

### 2. Composició i Registre (`OUTSIDE_MVC` / `CONTROLLER`)
- **`TerraLab/bootstrap/composition.py`**:
  - Registre de `threejs` amb el conjunt de capacitats celestes actualitzat.
- **`docs/architecture/mvc_file_roles.json`**:
  - Actualitzat el manifest MVC conservant la cobertura del 100% de fitxers.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/core/rendering_contracts/contracts.py` | `MODEL` | `MODEL` | Contractes de renderitzat neutrals | afegits enums celestes | Pyright, unit tests |
| `TerraLab/render/threejs/protocol.py` | `VIEW` | `VIEW` | Protocol i allowlist de primitives Three.js | afegides primitives celestes | Protocol tests |
| `TerraLab/render/threejs/backend.py` | `VIEW` | `VIEW` | Backend Three.js de superfície amfitriona | serialització de plans celestes | Celestial tests |
| `TerraLab/render/threejs/assets/threejs_runner.js` | `VIEW` | `VIEW` | Runner JS Three.js de primitives | manegadors celestes JS | Asset packaging |
| `TerraLab/bootstrap/composition.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Arrel de composició de backends | capacitats celestes de `threejs` | Composition test |
| `docs/architecture/mvc_file_roles.json` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Manifiest normatiu MVC | cobert totalment | Coverage test |
| `tests/test_threejs_celestial_capabilities.py` | — | — | Test suite de capabilities celestes Three.js | — | Pytest |

---

## Evidència de Verificació

- **Tests de la Fase 20**: `python -m pytest -q tests/test_threejs_celestial_capabilities.py` -> **5 passed in 0.09s**
- **Suite de Backends Completa**: `python -m pytest -q tests/test_threejs_host_and_primitives.py tests/test_threejs_celestial_capabilities.py tests/test_recording_headless_conformance.py tests/test_render_backend_selection.py` -> **30 passed in 1.80s**
- **Suite d'Arquitectura**: `python -m pytest -q tests/architecture` -> **18 passed in 9.92s**
- **Linter Ruff**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Barrera de Tipus Pyright**: `python tools/dev/check_pyright_baseline.py` -> **2375 errors, 5 warnings (Documented barrier PASSED)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**

---

## Conclusió i Estat de Ledger

La Fase 20 ha portat amb èxit totes les capes celestes i informatives al backend Three.js. QPainter roman intacte com a backend per defecte. El ledger de la Fase 20 s'estableix a `passed`.
