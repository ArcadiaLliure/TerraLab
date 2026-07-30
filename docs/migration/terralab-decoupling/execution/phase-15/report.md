# Informe d'Execució — Fase 15: Data and Terrain Application Ports

## Resum General
La Fase 15 ha completat el desacoblament de la capa de dades i terreny del motor gràfic i de PyQt, introduint interfícies d'aplicació d'alta nivell (`application/ports/terrain.py`, `application/ports/assets.py`), adaptadors d'execució purs (`adapters/runtime/terrain.py`) i adaptadors de presentació Qt (`adapters/qt/terrain.py`). A més, s'ha eliminat la dependència de paquet bidireccional entre `data` i `terrain`.

---

## Canvis Implementats

### 1. Ports d'Aplicació i DTOs (`CONTROLLER` / `APPLICATION_SERVICE`)
- **`TerraLab/application/ports/terrain.py`**: Interfícies pures i DTOs (`ElevationQuery`, `BakeJobRequest`, `SurfaceRefreshContext`, `BortleEstimateRequest`, `TerrainProgress`, `ElevationPort`, `TerrainBakePort`, `SurfaceSamplingPort`) 100% lliures de PyQt.
- **`TerraLab/application/ports/assets.py`**: Interfícies pures (`AssetCatalogPort`, `SourceCatalogPort`, `LayerManagerPort`, `AssetOperationReport`) per a la gestió de catàleg i capes.

### 2. Eliminació de la Dependència de Paquet Bidireccional (`data <-> terrain`)
- **`TerraLab/data/source_catalog.py`**: Mòdul canònic per als tipus de catàleg de dades.
- **`TerraLab/data/crs.py`**: Mòdul pur de transformació CRS pyproj traslladat a dades.
- **`TerraLab/data/source_inspection.py`**: Mòdul pur d'inspecció de descriptors traslladat a dades.
- **`TerraLab/data/asc_cache_builder.py`**: Mòdul pur de materialització ASC traslladat a dades.
- **`TerraLab/data/dem_tiles.py`**: Indexació de rajoles DEM traslladada a dades.
- **`TerraLab/terrain/crs.py`**, **`TerraLab/terrain/source_inspection.py`**, **`TerraLab/terrain/asc_cache_builder.py`**: Convertits en shims de compatibilitat que re-exporten des de `TerraLab.data.*`.
- Actualitzats tots els imports de `TerraLab/data/layer_manager.py` i `TerraLab/data/assets/*` per importar directament des de `TerraLab.data.*` en comptes de `TerraLab.terrain.*`.

### 3. Adaptadors d'Execució i Presentació
- **`TerraLab/adapters/runtime/terrain.py`**: Adaptador d'execució pur `RuntimeTerrainAdapter` que implementa `ElevationPort`, `TerrainBakePort` i `SurfaceSamplingPort` amb callbacks purs i sense PyQt5.
- **`TerraLab/adapters/qt/terrain.py`**: Adaptador de presentació Qt `QtTerrainCoordinatorAdapter` que envolta l'adaptador pur i enllaça els seus esdeveniments amb senyals Qt per a la capa de vista Qt.

### 4. Manifest d'Arquitectura i Tests
- Actualitzats `docs/architecture/mvc_file_roles.json` i `tools/dev/generate_mvc_file_roles.py` registrant els nous fitxers en Fase 15.
- Creat `tests/test_data_and_terrain_ports.py` per validar el desacoblament de ports, la gestió de callbacks, el bridge Qt i l'absència de dependències de paquet o de PyQt a les interfícies d'aplicació.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/application/ports/terrain.py` | — | `CONTROLLER` | Protocols ElevationPort, TerrainBakePort, SurfaceSamplingPort | cap PyQt | AST sense Qt, pytest |
| `TerraLab/application/ports/assets.py` | — | `CONTROLLER` | Protocols AssetCatalogPort, LayerManagerPort, SourceCatalogPort | cap PyQt | AST sense Qt, pytest |
| `TerraLab/adapters/runtime/terrain.py` | — | `OUTSIDE_MVC` | Execució pura DEM, bake subprocess, surface sampling | cap PyQt | unit tests adapters |
| `TerraLab/adapters/qt/terrain.py` | — | `VIEW` | Senyals Qt i bridge de presentació | delegació a adapter pur | bridge tests |
| `TerraLab/data/crs.py` | — | `OUTSIDE_MVC` | Transformació CRS pyproj | cap dependència de terrain | AST sense terrain import |
| `TerraLab/data/source_inspection.py` | — | `OUTSIDE_MVC` | Inspecció de descriptors de dades | cap dependència de terrain | AST sense terrain import |
| `TerraLab/data/asc_cache_builder.py` | — | `OUTSIDE_MVC` | Materialització ASC | cap dependència de terrain | AST sense terrain import |
| `TerraLab/data/dem_tiles.py` | — | `OUTSIDE_MVC` | Indexació rajoles DEM | cap dependència de terrain | AST sense terrain import |
| `TerraLab/terrain/crs.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Re-exportació de compatibilitat | mogut a data | imports verds |
| `TerraLab/terrain/source_inspection.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Re-exportació de compatibilitat | mogut a data | imports verds |
| `TerraLab/terrain/asc_cache_builder.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Re-exportació de compatibilitat | mogut a data | imports verds |

---

## Evidència de Verificació

- **Suite de ports de Fase 15**: `python -m pytest -q tests/test_data_and_terrain_ports.py` -> **6 passed**
- **Suite d'arquitectura**: `python -m pytest -q tests/architecture` -> **17 passed**
- **Linter**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**

---

## Conclusió i Estat de Ledger
La Fase 15 s'ha completat satisfent tots els requisits de desacoblament gràfic, aïllament de PyQt a la capa de ports i eliminació de cicles de paquet `data <-> terrain`.
