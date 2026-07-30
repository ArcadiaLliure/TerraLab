# Informe d'Execució — Fase 13: Adaptador QPainter de Terreny

## Resum General
La Fase 13 va implementar l'adaptador de presentació QPainter per a `TerrainGeometryPlan` (Fase 11) i `TerrainMaterialPlan` (Fase 12). Aquesta és evidència de l'adaptador de plans; no és evidència que totes les rutes de terreny, hit-test i caches legacy haguessin estat migrades.

---

## Canvis Implementats

### 1. Presentador QPainter Adaptat (`VIEW`)
- **`TerraLab/render/qpainter/terrain.py`**: nou adaptador `QPainterTerrainAdapter` i `render_qpainter_terrain_plan()` que tradueixen malles de geometria i matrius RGBA de materials en polígons QPainter i raster d'imatges.
- **`TerraLab/application/terrain.py`**: controlador de capability `resolve_terrain_pipeline()` per validar i resoldre el flag de la pipeline de terreny (`TERRALAB_TERRAIN_PIPELINE`).

### 2. Suites de Conforma i Tests
- **`tests/test_qpainter_terrain_adapter.py`**: suite d'unitat i conformitat per a l'adaptador QPainter de terreny i resolució de pipeline.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/render/qpainter/terrain.py` | — | `VIEW` | Rasterització QPainter de TerrainGeometryPlan i TerrainMaterialPlan | cap càlcul de materials, llum o culling | consumeix només plans |
| `TerraLab/application/terrain.py` | — | `CONTROLLER` | resolució del flag reversible `TERRALAB_TERRAIN_PIPELINE` | cap dibuix ni càlcul numèric | default `scene`, validació i rollback `legacy` |

---

## Evidència de Verificació

- **Suites de Fase 12 i 13**: `python -m pytest -q tests/test_terrain_material_plan.py tests/test_qpainter_terrain_adapter.py` -> **6 passed**
- **Suite de Terreny**: `python -m pytest -q tests/test_terrain_geometry_plan.py tests/test_terrain_surface_render.py tests/test_terrain_representation.py tests/test_terrain_visibility_range.py` -> **121 passed**
- **Arquitectura i Manifest MVC**: `python -m pytest -q tests/architecture` -> **17 passed**
- **Linter i format**: `ruff check` i `ruff format` -> **0 errors**

---

## Conclusió i Estat de Ledger
La Fase 13 va complir l'objectiu acotat de l'adaptador QPainter de plans. El desacoblament complet de terreny queda subjecte a les proves de frontera i de paritat del hardening final.
