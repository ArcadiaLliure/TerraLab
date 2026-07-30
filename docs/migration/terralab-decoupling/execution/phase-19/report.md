# Informe d'Execució — Fase 19: Host Three.js i Primitives Fonamentals

## Resum General

La Fase 19 va implementar el host i bridge local de Three.js (`ThreeJSRendererBackend`), el protocol tipat de missatges amb control d'allowlist i buffers de memòria binària, els assets empaquetats locals sense dependències externes de CDN, i els adaptadors de primitives fonamentals (color, punts/sprites, línies/polilínies, malla de triangles, imatge, text bàsic, clip/blend). QPainter es manté com a backend per defecte de producció, i el backend `threejs` es registra amb un manifiest de capacitats parcial (`HOSTED_SURFACE`), rebutjant la selecció per a escenes completes d'aplicació amb un missatge d'error accionar mentre faltin capacitats celestes o de terreny.

---

## Canvis Implementats

### 1. Nucli Three.js Render & Bridge (`VIEW`)
- **`TerraLab/render/threejs/__init__.py`**: Exportació pública de `ThreeJSRendererBackend`.
- **`TerraLab/render/threejs/protocol.py`**:
  - Definició de missatges de protocol versionats (`start`, `ready`, `submit`, `ack`, `pick_request`, `pick_result`, `register_resource`, `dispose_resource`, `error`, `close`, `restart`).
  - Codificació i descodificació de manetes binàries de memòria (`encode_binary_handle`, `decode_binary_handle`) per evitar enviar grans arrays com llistes JSON.
  - Guardes de seguretat i allowlists per operacions i primitives.
- **`TerraLab/render/threejs/bridge.py`**:
  - Implementat `ThreeJSBridge` per a la comunicació bidireccional local i control del cicle de vida.
  - Gestor de recursos `ResourceRegistry` amb versionat i descartat explícit (`dispose()`).
- **`TerraLab/render/threejs/backend.py`**:
  - Implementació concreta de `RendererBackend` per a Three.js (`backend_id = "threejs"`).
  - Manifiest de capacitats parcial: `frozenset({RenderCapability.HOSTED_SURFACE})`.
  - Producció d'outputs `HostedSurfaceOutput` i gestió de picking.
- **`TerraLab/render/threejs/diagnostic.py`**:
  - Generador d'escena diagnòstica independent (`build_diagnostic_primitive_manifest`) amb totes les primitives bàsiques per a validació visible.

### 2. Assets empaquetats locals (`VIEW`)
- **`TerraLab/render/threejs/assets/three.min.js`**: Llibreria Three.js empaquetada localment, 100% offline sense crides CDN.
- **`TerraLab/render/threejs/assets/threejs_runner.js`**: Executador JavaScript de bridge, gestió d'escena/càmera, renderitzat de primitives i dispatch de missatges.
- **`TerraLab/render/threejs/assets/diagnostic_runner.html`**: Pàgina HTML contenidor per al runner local de Three.js.

### 3. Presentador Host Qt (`VIEW`)
- **`TerraLab/adapters/qt/threejs_host.py`**:
  - Presentador Qt `ThreeJSWebEngineHostPresenter` que incrusta WebEngine i enllaça el bridge local de Three.js per a superfícies amfitriones (`HostedSurfaceTarget`).

### 4. Composició i Registre (`OUTSIDE_MVC` / `CONTROLLER`)
- **`TerraLab/bootstrap/composition.py`**:
  - Registrat el backend `"threejs"` amb capacitat parcial `HOSTED_SURFACE`.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/render/threejs/__init__.py` | — | `VIEW` | Mòdul d'exportació backend Three.js | — | AST |
| `TerraLab/render/threejs/protocol.py` | — | `VIEW` | Protocol i esquemes tipats de bridge Three.js | — | Unit tests |
| `TerraLab/render/threejs/bridge.py` | — | `VIEW` | Bridge local de cicle de vida i recursos | — | Unit tests |
| `TerraLab/render/threejs/backend.py` | — | `VIEW` | Backend Three.js de superfície amfitriona | — | Conformance test, AST |
| `TerraLab/render/threejs/diagnostic.py` | — | `VIEW` | Escena diagnòstica de primitives | — | Diagnostic test |
| `TerraLab/render/threejs/assets/*` | — | `VIEW` | Assets Three.js i HTML/JS runners locals | — | Packaging test |
| `TerraLab/adapters/qt/threejs_host.py` | — | `VIEW` | Presentador host Qt WebEngine per a Three.js | — | View test |
| `TerraLab/bootstrap/composition.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Arrel de composició de backends | registrat `"threejs"` backend | Composition test |
| `tools/dev/generate_mvc_file_roles.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Generador de manifiest MVC | actualitzat amb noves rutes | Arch test |
| `docs/architecture/mvc_file_roles.json` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Manifiest normatiu MVC | afegits fitxers Fase 19 | Coverage test |
| `tests/test_threejs_host_and_primitives.py` | — | — | Test suite de bridge, protocol, assets i rejeció | — | Pytest |

---

## Evidència de Verificació

- **Tests de la Fase 19**: `python -m pytest -q tests/test_threejs_host_and_primitives.py` -> **9 passed in 0.09s**
- **Suite de Conformitat i Selecció**: `python -m pytest -q tests/test_recording_headless_conformance.py tests/test_render_backend_selection.py` -> **16 passed in 1.87s**
- **Suite d'Arquitectura**: `python -m pytest -q tests/architecture` -> **18 passed in 10.74s**
- **Linter Ruff**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Barrera de Tipus Pyright**: `python tools/dev/check_pyright_baseline.py` -> **2372 errors, 5 warnings (Documented barrier PASSED)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**

---

## Conclusió i Estat de Ledger

La Fase 19 ha completat la integració del host i bridge Three.js, demostrant el renderitzat de les primitives bàsiques sobre l'escena diagnòstica i validant que el backend parcial es troba registrat i es rebutja de forma accionar quan una escena d'aplicació requereix capacitats encara no portades.
