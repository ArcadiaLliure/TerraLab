# Informe d'Execució — Fase 10: Scope i Constel·lacions Editables

## Resum General
La Fase 10 ha completat la migració dels models de domini, càlculs astronòmics/òptics/esfèrics i casos d'ús de **Telescope Scope** i **Constel·lacions Editables** fora de `TerraLab/widgets` i de la capa de renderitzat QPainter directa.

---

## Canvis Implementats

### 1. Model Esfèric i de Domini (`MODEL`)
- **`TerraLab/scene/spherical_math.py`**: mòdul pur de matemàtica esfèrica (`sky_to_vector`, `vector_to_sky`, `angular_distance`, `slerp_arc_points`, `destination_point`, `ra_dec_to_alt_az`, `altaz_to_ra_dec`).
- **`TerraLab/widgets/spherical_math.py`**: convertit en façana de compatibilitat que re-exporta des de `TerraLab.scene.spherical_math`.
- **`TerraLab/scene/scope.py`**: estat i paràmetres de forma (cercle/rectangle), presets de sensors, càlcul de FOV i operacions de nudge/tracking.
- **`TerraLab/scene/constellations.py`**: model pur de `ConstellationNode`, `ConstellationGroup`, estat d'edició i persistència JSON pura sense dependències de PyQt.

### 2. DTOs de Plans d'Escena (`MODEL`)
- **`TerraLab/scene/plans/scope.py`**: DTO `ScopePlan` immutable per descriure la màscara, reticle, creu de centrat i mètriques del HUD.
- **`TerraLab/scene/plans/constellations.py`**: DTO `ConstellationPlan` immutable per descriure linies geodèsiques interpolades (slerp), nodes, seleccions actives, preview de dibuix i etiquetes.

### 3. Controladors d'Aplicació (`CONTROLLER`)
- **`TerraLab/application/scope.py`**: `ScopeController` per a casos d'ús de scope (activació, nudge, tracking, FOV) i resolució del flag `TERRALAB_SCOPE_PIPELINE`.
- **`TerraLab/application/constellations.py`**: `ConstellationController` per a gestos de dibuix/edició, selecció, esborrat de nodes/segments, desfer/refer i resolució del flag `TERRALAB_CONSTELLATION_PIPELINE`.

### 4. Presentació QPainter (`VIEW`)
- **`TerraLab/render/qpainter/scope.py`**: adaptador pur `render_qpainter_scope_plan` sense lògica de domini.
- **`TerraLab/render/qpainter/constellations.py`**: adaptador pur `render_qpainter_constellation_plan` sense I/O ni decisions de negoci.

### 5. Desacoblament de Runtime i Widgets
- Actualitzat **`TerraLab/runtime/offscreen_renderer.py`** eliminant imports directes de `TerraLab/widgets` per a scope, constel·lacions i spherical math.
- Actualitzat el manifest **`docs/architecture/mvc_file_roles.json`** via `tools/dev/generate_mvc_file_roles.py`.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/scene/spherical_math.py` | — | `MODEL` | trigonometria esfèrica pura | cap Qt | AST pur, unit tests |
| `TerraLab/widgets/spherical_math.py` | `MODEL` | `MODEL` compatibilitat | re-export públic | implementació traslladada | re-exports verds |
| `TerraLab/scene/scope.py` | — | `MODEL` | estat de scope, presets, FOV, nudge | cap Qt o paint | unit tests purs |
| `TerraLab/scene/constellations.py` | — | `MODEL` | nodes, grups, JSON persistence | cap PyQt/QPainter | round-trip JSON pur |
| `TerraLab/scene/plans/scope.py` | — | `MODEL` | DTO ScopePlan | cap paint | AST sense Qt |
| `TerraLab/scene/plans/constellations.py` | — | `MODEL` | DTO ConstellationPlan | cap paint | AST sense Qt |
| `TerraLab/application/scope.py` | — | `CONTROLLER` | casos d'ús de scope | cap paint o Qt import | unit tests controllers |
| `TerraLab/application/constellations.py` | — | `CONTROLLER` | casos d'ús i gestos de constel·lacions | cap paint o Qt import | unit tests controllers |
| `TerraLab/render/qpainter/scope.py` | — | `VIEW` | dibuix de reticle, màscara i HUD | cap càlcul | adapter pur |
| `TerraLab/render/qpainter/constellations.py` | — | `VIEW` | dibuix de segments, nodes i etiquetes | cap I/O o càlcul | adapter pur |

---

## Evidència de Verificació

- **Suite d'unitat de Fase 10**: `python -m pytest -q tests/test_scope_and_constellations_slice.py` -> **8 passed**
- **Suite d'integració i offscreen**: `python -m pytest -q tests/test_scope_and_constellations_slice.py tests/test_process_canvas_interactions.py tests/test_offscreen_functional_regressions.py tests/test_render_backend_selection.py tests/architecture` -> **81 passed**
- **Linter i format**: `ruff check` i `ruff format` -> **0 errors**
- **Pyright Baseline**: `python tools/dev/check_pyright_baseline.py` -> **2384 errors (dins la barrera 2395)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**
- **Suite completa**: `python -m pytest -q` -> **741 passed**

---

## Conclusió i Estat de Ledger
La Fase 10 ha passat totes les verificacions automàtiques, de linter, d'arquitectura i de paritat funcional. El ledger de fases (`phase-state.json`) s'ha actualitzat a la Fase 10 amb estat `passed`.
