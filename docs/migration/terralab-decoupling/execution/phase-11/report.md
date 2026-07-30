# Informe d'Execució — Fase 11: Pla de Geometria de Terreny Renderer-Neutral

## Resum General
La Fase 11 ha completat el desacoblament de tota la projecció, selecció de malles, rasterització z-buffer, càlculs de coordenades baricèntriques i hit geometry del terreny respecte de PyQt i QPainter. Es manté la façana de compatibilitat i el rollback a la fase 10 mitjançant el flag `TERRALAB_TERRAIN_GEOMETRY_PIPELINE` (`scene` per defecte, `legacy` per a rollback).

---

## Canvis Implementats

### 1. DTOs i Planner pur (`MODEL`)
- **`TerraLab/scene/plans/terrain_geometry.py`**: DTOs immutables `TerrainGeometryPlan` i `TerrainHitRecord`, i el planner pur `build_terrain_geometry_plan()` / `build_terrain_triangles_geometry()` per calcular posicions/índexs/validitat/depth/baricèntriques de manera renderer-neutral sense cap import de Qt.
- **`TerraLab/terrain/render/geometry.py`**: Mantingut com a mòdul pur de càlcul numèric de malles i spans sense dependències de presentació.
- **`TerraLab/terrain/render/triangle_raster.py`**: Rasteritzador numèric pur de z-buffer i resolució baricèntrica.

### 2. Controlador d'Aplicació (`CONTROLLER`)
- **`TerraLab/application/terrain_geometry.py`**: Controlador de capability `resolve_terrain_geometry_pipeline()` per validar i resoldre el flag de la pipeline de geometria de terreny (`TERRALAB_TERRAIN_GEOMETRY_PIPELINE`).

### 3. Adaptadors i Mixins de Vista (`VIEW` / `MIXED` Transitori)
- **`TerraLab/terrain/overlay_mixins/projection_geometry.py`**: Actualitzat per consumir el pla pur de geometria quan la pipeline `scene` està activa.
- **`TerraLab/terrain/overlay_mixins/category_hit_test.py`**: Suport directament afegit per a la consulta espacial des de `TerrainGeometryPlan`.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/scene/plans/terrain_geometry.py` | — | `MODEL` | DTO TerrainGeometryPlan, hit records i planner pur | cap Qt o paint | AST sense Qt, unit tests purs |
| `TerraLab/application/terrain_geometry.py` | — | `CONTROLLER` | resolució del flag reversible `TERRALAB_TERRAIN_GEOMETRY_PIPELINE` | cap càlcul o paint | default `scene`, validació i rollback `legacy` |
| `TerraLab/terrain/render/geometry.py` | `MODEL` | `MODEL` | càlculs de malles, horizons i spans purs | cap Qt | AST pur |
| `TerraLab/terrain/render/triangle_raster.py` | `MODEL` | `MODEL` | rasterització z-buffer i baricèntriques | cap Qt | AST pur |
| `TerraLab/terrain/overlay_mixins/projection_geometry.py` | `MIXED` transitori | `MIXED` transitori | façana de rendering QPainter | la ruta `scene` delega la geometria al planner pur | capability router i visual parity |
| `TerraLab/terrain/overlay_mixins/category_hit_test.py` | `MIXED` transitori | `MIXED` transitori | cerca de categories sobre malles i spans | hit testing directament des de TerrainGeometryPlan | unit tests de hit query |

---

## Evidència de Verificació

- **Suite d'unitat de Fase 11**: `python -m pytest -q tests/test_terrain_geometry_plan.py` -> **4 passed**
- **Suites de terreny i integració**: `python -m pytest -q tests/test_terrain_geometry_plan.py tests/test_terrain_surface_render.py tests/test_terrain_representation.py tests/test_terrain_visibility_range.py tests/test_astro_rendering.py tests/test_offscreen_functional_regressions.py tests/test_process_canvas_interactions.py` -> **182 passed**
- **Linter i format**: `ruff check` i `ruff format` -> **0 errors**
- **Pyright Baseline**: `python tools/dev/check_pyright_baseline.py` -> **2368 errors (dins la barrera 2395)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**
- **Tests d'Arquitectura i Manifest MVC**: `python -m pytest -q tests/architecture` -> **17 passed**

---

## Conclusió i Estat de Ledger
La Fase 11 ha complert tots els requeriments d'abstracció de geometria de terreny renderer-neutral. El manifest `mvc_file_roles.json` i el ledger `phase-state.json` queden actualitzats amb estat `passed`.
