# Informe d'Execució — Fase 18: Backend Recording/Headless i Suite de Conformitat

## Resum General

La Fase 18 va implementar un backend de gravació sense API gràfica (`RecordingRendererBackend`), una primera suite de conformitat compartida (`BackendConformanceSuite`) i la CLI headless de construcció d'escenes (`TerraLab/cli/render_scene.py`). L'evidència demostra el camí `SceneFrame` i el manifest de recording sense PyQt; no demostra que tots els planners científics, terreny, meteorologia o la ruta de producció QPainter estiguin lliures de dependències gràfiques.

---

## Canvis Implementats

### 1. Backend de Recording / Headless (`VIEW`)
- **`TerraLab/render/recording/__init__.py`**: Exportació pública de `RecordingRendererBackend`.
- **`TerraLab/render/recording/backend.py`**:
  - Implementat `RecordingRendererBackend` zero-graphics que valida `SceneFrame`, ordena capes, quantifica primitives i calcula resums de recursos.
  - Produeix manifests JSON deterministes i hashes SHA256 per cada frame registrat.
  - Implementa les operacions `start`, `submit`, `request_pick`, `close` i verifica les condicions de cicle de vida.

### 2. Suite de Conformitat Compartida (`OUTSIDE_MVC/OBSERVABILITY`)
- **`TerraLab/render/conformance.py`**:
  - Implementada la classe `BackendConformanceSuite` que valida el contracte uniforme per qualsevol backend (`RecordingRendererBackend`, `QPainterLegacyBackend`, futurs backends Three.js / Vulkan / OpenGL).
  - Comprova transicions de cicle de vida, rebuig de valors o esquemes invàlids, determinisme de hash i respostes de picking.

### 3. Eina Headless CLI (`OUTSIDE_MVC/CLI`)
- **`TerraLab/cli/render_scene.py`**:
  - Executable de consola headless per construir escenes reproduïbles (`minimal` i `full`), executar la gravació i generar manifests deterministes.
  - Admet el flag `--block-pyqt` que immobilitza els mòduls Qt a `sys.modules` per garantir la independència absoluta de PyQt.

### 4. Composició i Ports (`CONTROLLER` / `MODEL`)
- **`TerraLab/application/ports/rendering.py`**: Actualitzada la signatura de `RenderOutputPort.frame_ready` per acceptar `RenderOutput` (incloent `RecordingOutput`).
- **`TerraLab/bootstrap/composition.py`**: Registrat el backend `"recording"` al registre de backend per a selecció dinàmica per composició.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/render/recording/__init__.py` | — | `VIEW` | Mòdul d'exportació de backend recording | — | AST |
| `TerraLab/render/recording/backend.py` | — | `VIEW` | Backend zero-graphics de gravació i manifestos | — | Conformance suite, AST |
| `TerraLab/render/conformance.py` | — | `OUTSIDE_MVC` | Suite d'asserció i comprovació de conformitat | — | Pytest suite |
| `TerraLab/cli/render_scene.py` | — | `OUTSIDE_MVC` | CLI headless de renderització d'escena | — | Subprocess CLI test |
| `TerraLab/application/ports/rendering.py` | `MODEL` | `MODEL` | Interfície i contracte de render port | actualitzada la unió `RenderOutput` | Pyright, unit tests |
| `TerraLab/bootstrap/composition.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Arrel de composició de backends | registrat `"recording"` backend | Composition test |
| `TerraLab/render/qpainter/backend.py` | `VIEW` | `VIEW` | Presentador legacy QPainter | linter/lifecycle guard | Conformance test |
| `tests/test_recording_headless_conformance.py` | — | — | Test suite de conformitat, CLI i zero-Qt AST | — | Pytest |

---

## Evidència de Verificació

- **Tests de Conformitat Fase 18**: `python -m pytest -q tests/test_recording_headless_conformance.py` -> **6 passed in 0.92s**
- **Suite d'Arquitectura**: `python -m pytest -q tests/architecture` -> **17 passed**
- **Linter Ruff**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Barrera de Tipus Pyright**: `python tools/dev/check_pyright_baseline.py` -> **2372 errors, 5 warnings (Documented barrier PASSED)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**
- **CLI Execució Manual**: `python TerraLab/cli/render_scene.py --scene minimal --block-pyqt` -> **JSON Manifest generat correctament amb SHA256**

---

## Conclusió i Estat de Ledger

La Fase 18 va validar el subconjunt recording/headless. El desacoblament total i la planificació headless completa (incloent terreny i meteorologia) queden fora de la seva evidència i s'han de verificar en el hardening final.
