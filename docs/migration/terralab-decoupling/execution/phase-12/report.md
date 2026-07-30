# Informe d'Execució — Fase 12: Materials, Llum i Atmosfera de Terreny Purs

## Resum General
La Fase 12 va introduir planificació i composició pures de materials, paletes, il·luminació celestial (sol/lluna), atmosfera, haze i estils visuals de terreny. La cobertura correspon al nou pipeline de materials; no certifica per si sola l'eliminació de les rutes legacy de terreny que encara coexistien durant la migració.

---

## Canvis Implementats

### 1. DTOs i Planner pur (`MODEL`)
- **`TerraLab/scene/plans/terrain_materials.py`**: DTOs immutables `TerrainMaterialPlan` i `TerrainAtmospherePlan`, i el planner pur `build_terrain_material_plan()` per calcular vertex_rgba/base_rgba, llum solar/lunar i haze d'atmosfera sense cap import de Qt.
- **`TerraLab/terrain/render/palette.py`**: Eliminats els imports i la dependència de `PyQt5.QtGui.QColor`. Operacions de color sobre NumPy i tuples pures.
- **`TerraLab/terrain/render/materials.py`**: Mòdul pur de composició de colors RGBA i color grading.
- **`TerraLab/terrain/render/lighting.py`**: Mòdul pur d'il·luminació celestial i Lambert.
- **`TerraLab/terrain/render/atmosphere.py`**: Mòdul pur de càlcul de fog exponencial i linear depth haze.

### 2. Controlador d'Aplicació (`CONTROLLER`)
- **`TerraLab/application/terrain_materials.py`**: Controlador de capability `resolve_terrain_material_pipeline()` per validar i resoldre el flag de la pipeline de materials de terreny (`TERRALAB_TERRAIN_MATERIAL_PIPELINE`).

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/scene/plans/terrain_materials.py` | — | `MODEL` | DTOs TerrainMaterialPlan, TerrainAtmospherePlan i planner pur | cap Qt | AST sense Qt, unit tests purs |
| `TerraLab/application/terrain_materials.py` | — | `CONTROLLER` | resolució del flag reversible `TERRALAB_TERRAIN_MATERIAL_PIPELINE` | cap càlcul o paint | default `scene`, validació i rollback `legacy` |
| `TerraLab/terrain/render/palette.py` | `MODEL` | `MODEL` | paletes i transformacions numèriques de color | eliminat import inacabat de QColor | AST pur |

---

## Evidència de Verificació

- **Suite d'unitat de Fase 12**: `python -m pytest -q tests/test_terrain_material_plan.py` -> **4 passed**
- **Tests d'Arquitectura i Manifest MVC**: `python -m pytest -q tests/architecture` -> **17 passed**
- **Linter i format**: `ruff check` i `ruff format` -> **0 errors**

---

## Conclusió i Estat de Ledger
La Fase 12 va complir l'objectiu acotat del planner de materials renderer-neutral. El hardening final ha de validar l'absència de rutes mixtes i la paritat de la ruta completa de terreny.
