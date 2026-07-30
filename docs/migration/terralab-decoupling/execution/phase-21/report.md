# Informe d'Execució — Fase 21: Terreny, Interacció i Paritat Completa de Three.js

## Resum General

La Fase 21 va completar les capacitats de terreny i interacció al backend Three.js (`ThreeJSRendererBackend`), adaptant els plans de geometria (`TerrainGeometryPlan`) i materials (`TerrainMaterialPlan`), la resposta a consultes d'elevació i hover de superfície, i l'opció de selecció estable del backend `threejs` a la composició. Tot el renderitzat i l'ombrejat de terreny (malla de relleu, perfils, ortofoto RGB i cobertura categòrica de sòl) consumeix exclusivament els buffers precalculats pels planners neutrals de domini. No es recalcula cap ciència ni s'efectua cap I/O de DEM des de JavaScript. QPainter es manté complet i operational com a backend per defecte.

---

## Canvis Implementats

### 1. Contractes i Backend Three.js (`MODEL` / `VIEW`)
- **`TerraLab/core/rendering_contracts/contracts.py`**:
  - Afegides les capacitats neutrals `TERRAIN_GEOMETRY` i `TERRAIN_MATERIALS` a l'enum `RenderCapability`.
- **`TerraLab/render/threejs/protocol.py`**:
  - Definició de primitives de terreny i interacció (`terrain_mesh`, `terrain_material`, `interaction_affordance`) i actualització de l'allowlist del protocol.
- **`TerraLab/render/threejs/backend.py`**:
  - Incorporació de `TERRAIN_GEOMETRY`, `TERRAIN_MATERIALS` i `INTERACTION` al conjunt de capacitats de `ThreeJSRendererBackend`.
  - Serialització de plans de terreny a primitives de bridge i actualització de `request_pick` per retornar payloads de superfície/terreny (`kind="surface"`, `surface="ground"`).
- **`TerraLab/render/threejs/assets/threejs_runner.js`**:
  - Ampliació del runner Three.js per processar malles de terreny (BufferGeometry grid) i ombrejats de superfície.

### 2. Composició i Selecció Estable (`OUTSIDE_MVC` / `CONTROLLER`)
- **`TerraLab/bootstrap/composition.py`**:
  - Registre complet de `threejs` amb totes les capacitats requerides per a la construcció d'escenes d'aplicació.
  - Habilitació de la selecció estable de `threejs` com a opció compatible per a superfícies amfitriones (`HostedSurfaceTarget`).
- **`docs/architecture/mvc_file_roles.json`**:
  - Coberta total actualitzada mantenint zero deute `MIXED`.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/core/rendering_contracts/contracts.py` | `MODEL` | `MODEL` | Contractes neutrals de render | afegits enums de terreny | Pyright, unit tests |
| `TerraLab/render/threejs/protocol.py` | `VIEW` | `VIEW` | Protocol de bridge Three.js | primitives de terreny/interacció | Protocol tests |
| `TerraLab/render/threejs/backend.py` | `VIEW` | `VIEW` | Backend Three.js de superfície amfitriona | serialització de terreny i pick | Parity tests |
| `TerraLab/render/threejs/assets/threejs_runner.js` | `VIEW` | `VIEW` | Runner JS Three.js | manegadors de terreny JS | Asset packaging |
| `TerraLab/bootstrap/composition.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Arrel de composició de backends | selecció estable de `threejs` | Composition test |
| `docs/architecture/mvc_file_roles.json` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Manifiest normatiu MVC | coberta total actualitzada | Coverage test |
| `tests/test_threejs_terrain_interaction_parity.py` | — | — | Test suite de terreny i paritat Three.js | — | Pytest |

---

## Evidència de Verificació

- **Tests de la Fase 21**: `python -m pytest -q tests/test_threejs_terrain_interaction_parity.py` -> **5 passed in 0.10s**
- **Suite de Backends Completa**: `python -m pytest -q tests/test_threejs_host_and_primitives.py tests/test_threejs_celestial_capabilities.py tests/test_threejs_terrain_interaction_parity.py tests/test_recording_headless_conformance.py tests/test_render_backend_selection.py` -> **35 passed in 1.96s**
- **Suite d'Arquitectura**: `python -m pytest -q tests/architecture` -> **18 passed in 10.15s**
- **Linter Ruff**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Barrera de Tipus Pyright**: `python tools/dev/check_pyright_baseline.py` -> **2375 errors, 5 warnings (Documented barrier PASSED)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**

---

## Conclusió i Estat de Ledger

La Fase 21 ha assolit la paritat funcional i de capacitats del backend Three.js, permetent la selecció estable del backend sense tocar el Model ni duplicar ciència en JavaScript. QPainter roman complet i intacte per defecte. El ledger de la Fase 21 s'estableix a `passed`.
