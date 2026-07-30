# Informe final de auditoría y refactorización arquitectónica

## 1. Resumen ejecutivo

La intervención sustituye las rutas paralelas de UI, terreno, assets y
herramientas por una arquitectura canónica y comprobable. El grafo productivo
ha pasado de tres componentes cíclicos a cero; Ruff y Vulture quedan limpios;
los tests de caracterización y arquitectura protegen los límites, los imports
sin efectos secundarios, los entry points, la superficie y el relieve.

Los cambios permanecen únicamente en el working tree local. No se ha creado
ningún commit ni se ha realizado ningún push, por petición expresa del usuario.

Resultado cuantitativo principal:

- 554 tests finales frente a 522 iniciales.
- 86.473 → 84.222 LOC productivas.
- 34 → 29 módulos productivos por encima de 800 líneas.
- 3 → 0 componentes cíclicos.
- 690 → 627 capturas genéricas.
- 237 → 0 capturas genéricas silenciosas.
- 27 → 0 hallazgos de Vulture con confianza mínima del 70 %.
- 5/5 JSON que eran punteros LFS quedan normalizados como texto.
- 0 módulos numerados y 0 copias dinámicas de namespaces.
- 38/38 paquetes Python documentados con `README.md`.

La intervención no convierte en “resuelta” la deuda que aún existe. Pyright
mantiene 2.362 errores históricos y quedan 627 capturas genéricas, aunque
ninguna vuelve a ser silenciosa. El smoke test de PyInstaller agotó diez
minutos sin producir ejecutable, por lo que ese método de distribución se
retiró explícitamente. Estas limitaciones están registradas y cuentan con
barreras incrementales donde es posible.

## 2. SHA inicial y SHA final

| Concepto | Valor |
| --- | --- |
| Rama verificada | `refactor` |
| SHA inicial | `eaa51474032327ed29c054921a89eafd882a016e` |
| SHA final | `eaa51474032327ed29c054921a89eafd882a016e` |
| Motivo de SHA idéntico | No se realizaron commits |
| Estado entregado | Working tree local con cambios sin publicar |

## 3. Estado inicial de tests

La línea base se ejecutó con Python 3.13.7 en Windows 11:

- 522 tests recogidos y aprobados.
- 27,16 s de ejecución.
- Una advertencia.
- Sin cobertura consolidada disponible en la línea base.

La validación final recoge 554 tests. La cobertura instrumentada de producto es
44 %: 18.157 de 41.672 statements cubiertos. El JSON reproducible está en
`docs/architecture/refactor_coverage.json`.

## 4. Inventario de entropía

Los artefactos se generan con `tools/dev/code_inventory.py`:

- `refactor_file_inventory.csv`: fichero, paquete, tamaño, responsabilidad y
  señales de riesgo.
- `refactor_symbol_inventory.json`: clases, funciones, métodos, complejidad,
  capturas y referencias dinámicas.
- `refactor_dependency_graph.json`: nodos, aristas y ciclos.
- `refactor_dead_code_candidates.json`: candidatos clasificados; nunca se usan
  como orden automática de borrado.

Inventario final:

| Métrica | Valor |
| --- | ---: |
| Ficheros inventariados | 399 |
| Módulos Python | 300 |
| Símbolos AST | 3.711 |
| Aristas internas | 818 |
| Componentes cíclicos | 0 |
| Candidatos heurísticos pendientes | 937 |

Los 937 candidatos del inventario no equivalen a 937 elementos eliminables.
Incluyen slots Qt, callbacks, entry points, métodos accedidos dinámicamente y
API pública. Vulture, con confianza mínima de 70 %, sí queda en cero.

## 5. Dependencias cíclicas encontradas

En el SHA inicial se encontraron tres componentes fuertemente conexos:

1. `TerraLab.terrain.engine` ↔ `TerraLab.terrain.providers`.
2. `TerraLab.ui.astro_canvas` ↔ `TerraLab.ui.sky_widget_impl`.
3. `TerraLab.ui.layer_configurator` ↔ `TerraLab.ui.onboarding_dialogs`.

El grafo final contiene 818 aristas y cero ciclos. Las correcciones fueron:

- contratos y backends de raster fuera del motor de raycast;
- `AstroCanvas` y `AstronomicalWidget` canónicos, sin runtime legacy;
- construcción de diálogos desacoplada mediante llamadas explícitas;
- eliminación de copias de namespace y de módulos agregadores de runtime.

## 6. Código muerto eliminado

La tabla agrupa eliminaciones con el mismo tipo de evidencia. “Commit” conserva
el literal requerido, aunque no exista commit.

| Símbolo o fichero | Motivo | Evidencia estática | Evidencia dinámica | Consumidores migrados | Test protector | Commit |
| --- | --- | --- | --- | --- | --- | --- |
| `HorizonBaker.bake`, bloque posterior al `return` | 190 líneas inalcanzables duplicaban el algoritmo escalar | AST y Vulture: `unreachable code after return` | Paridad escalar/vectorizada y suite de terreno verdes | El fallback escalar ya vive en `bake_progressive` | `tests/terrain/test_vectorized_raycast.py` | Sin commit (cambio local por petición del usuario) |
| `bake_process._legacy_main` | Segundo entry point de 298 líneas sin consumidor | Búsqueda de símbolos: sólo definición | El worker invoca `main`; tests de subprocess verdes | `HorizonWorker` y `python -m TerraLab.terrain.bake_process` | `test_terrain_representation.py`, `test_worker_data_sources.py` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/ui/sky_widget.py` | Alias interno innecesario | Único consumidor: `TerraLab.__main__` | Entry point `--help` verde tras migración | `TerraLab.__main__` importa `astronomical_widget` | `tests/cli/test_entrypoints.py` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/widgets/sky_widget.py` | Shim duplicado | Imports migrados; cero referencias restantes | Suite UI completa | Importadores internos | Contrato de bridges eliminados | Sin commit (cambio local por petición del usuario) |
| `TerraLab/ui/sky_widget_impl.py` | Implementación paralela de UI | Definiciones canónicas únicas comprobadas por AST | Tests de frame, selección, goto y cierre | `AstronomicalWidget`, `AstroCanvas` y mixins semánticos | `test_canonical_runtime_types_have_one_definition` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/widgets/sky_legacy_components.py` | Runtime legacy sin contrato externo | Cero referencias y registro de deprecación aprobado | Suite UI y render verde | UI canónica | Contrato de bridges eliminados | Sin commit (cambio local por petición del usuario) |
| `TerraLab/widgets/scope_runtime_cache.py` | Caché paralela del telescopio | Consumidores migrados a catálogo canónico | Tests de catálogo y telescopio verdes | `data.catalogs.scope_cache` | Tests de Gaia/scope | Sin commit (cambio local por petición del usuario) |
| `TerraLab/common/runtime_cache.py` | Segunda implementación de caché y cancelación | Definiciones únicas comprobadas por AST | Tests concurrentes verdes | `common.cache`, `common.cancellation` | `tests/common/test_cache.py`, `test_cancellation.py` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/common/performance.py` | Módulo agregado sustituido | Imports migrados | Benchmarks y suite completa | `common.performance.{budget,flags,memory}` | `test_performance_budget.py` | Sin commit (cambio local por petición del usuario) |
| Wrappers `función`/`función_impl` de render | Duplicación sin semántica | Contrato AST prohíbe `_impl` paralelo | Tests de render verdes | Funciones públicas canónicas | `test_renderers_do_not_expose_parallel_impl_functions` | Sin commit (cambio local por petición del usuario) |
| Copias `globals().update(...)` y `globals()[...]` | Ocultaban dependencias reales | Búsqueda AST final: cero | Import safety y suite completa | Imports directos en mixins | `test_packages_do_not_use_numbered_fragments_or_dynamic_namespace_copying` | Sin commit (cambio local por petición del usuario) |
| `overlay_runtime.py` y `sky_runtime.py` transitorios | Bridges creados durante la migración y después retirados | Ficheros ausentes y cero imports | Suite completa | Mixins con imports explícitos | Contrato de namespace y ciclos | Sin commit (cambio local por petición del usuario) |
| `tests/test_performance_architecture.py` | Test monolítico de 585 líneas | Casos trasladados uno a uno | 23/23 tests pasan en los nuevos destinos | `tests/common`, `tests/data`, `tests/terrain` | Recolección completa de pytest | Sin commit (cambio local por petición del usuario) |

## 7. Código obsoleto eliminado

| Símbolo o fichero | Motivo | Evidencia estática | Evidencia dinámica | Consumidores migrados | Test protector | Commit |
| --- | --- | --- | --- | --- | --- | --- |
| `TerraLab/terrain/engine.py` | Monolito y ciclo con providers | Ciclo confirmado; responsabilidades mezcladas | Equivalencia de perfiles, malla y NoData | `domain`, `raycast`, `mesh`, `persistence` | Suite de terreno | Sin commit (cambio local por petición del usuario) |
| `TerraLab/terrain/providers.py` | Cinco backends y factories en un fichero | Grafo y análisis de clases | Tests ASC, GeoTIFF, chain y ventanas | `terrain/providers/*` | `test_tile_index.py`, `test_raster_windows.py` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/terrain/render_pipeline.py` | Render, configuración y raster en una unidad | 2.075 líneas y acoplamiento | Tests visuales y de pipeline | `terrain/render/*` | `test_terrain_render_pipeline.py` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/terrain/surface.py` | Servicio, geometría, RGB y categorías mezclados | 3.200 líneas | Tests RGB/categórico, tooltip y modos | `terrain/surface/*` | `test_surface_mode_switch.py`, `test_terrain_surface_render.py` | Sin commit (cambio local por petición del usuario) |
| Cuerpo antiguo de `TerraLab/terrain/overlay.py` | Overlay de 9.203 líneas | Responsabilidades detectadas por AST | Tests de hit-test, tooltip y render | `terrain/overlay_mixins/*` | Tests de superficie y render | Sin commit (cambio local por petición del usuario) |
| `TerraLab/data/copernicus_orthophoto.py` | Cliente, planificación, descarga y mosaico juntos | 2.496 líneas |  Tests de reintento, mosaico y diálogo | `data/copernicus/*` | Tests Copernicus | Sin commit (cambio local por petición del usuario) |
| `TerraLab/tools/download_gaia_tap.py` y `download_gaia_tiles.py` | Lógica productiva dentro de tools | Registro aprobado y consumidores conocidos | Pipeline Gaia en proceso spawn verde | `cli.gaia`, `data.gaia_downloader` | `test_gaia_process_pipeline.py` | Sin commit (cambio local por petición del usuario) |
| `TerraLab/tools/*` de análisis y benchmarks | Herramientas dentro del paquete distribuible | No existen imports productivos a `TerraLab.tools` | CLI e inventario ejecutados desde raíz | `tools/`, `benchmarks/` | Contrato `TerraLab` no importa tools | Sin commit (cambio local por petición del usuario) |
| `setup.py`, `requirements.txt`, `environment.yml` | Tres fuentes divergentes de dependencias | Metadatos duplicados | Instalación editable `.[dev]` correcta | `pyproject.toml` | Tests de entry points | Sin commit (cambio local por petición del usuario) |
| `TerraLab.spec` | Build standalone no validado y divergente | PyInstaller agotó 600 s sin producir ejecutable | Instalación Python y entry points verdes | Distribución soportada mediante `pyproject.toml` | `tests/cli/test_entrypoints.py` | Sin commit (cambio local por petición del usuario) |
| `de421.bsp` raíz y `TerraLab/data/stars/de421.bsp` | Binario duplicado dentro del código | Dos entradas Git confirmadas | Runtime resuelve/descarga el asset desde la biblioteca | `DataLibrary` y `AssetManager` | Tests de assets e import safety | Sin commit (cambio local por petición del usuario) |

## 8. Rutas inalcanzables eliminadas

Se eliminaron explícitamente:

- el algoritmo escalar colocado después del retorno de `HorizonBaker.bake`;
- `_legacy_main`, que no era invocado por el módulo ni por el worker;
- el borrador de “constellation eraser” sin ruta de activación;
- guards de dependencias obligatorias que nunca podían ejecutar un backend
  soportado distinto;
- branches de UI que reentraban en implementaciones heredadas;
- aliases internos sin consumidores;
- imports y variables que quedaron sin uso tras la extracción.

Vulture final con `--min-confidence 70`: cero hallazgos.

## 9. Ficheros movidos

| Origen | Destino lógico |
| --- | --- |
| `TerraLab.spec` | Retirado tras fallar el smoke test; no hay spec soportado |
| `test_get_elevation_by_gps.py` | servicio `terrain/services/elevation_query.py`, CLI y tests |
| scripts raíz DVNL/SQM/calibración | `TerraLab/cli/*` |
| `TerraLab/tools/benchmark_*` | `benchmarks/*` |
| herramientas cognitivas | `tools/dev/code_inventory.py` y artefactos en `docs/architecture` |
| `scene/render_context.py` | `render/qt/context.py` |
| `common/runtime_cache.py` | `common/cache.py` y `common/cancellation.py` |
| `common/performance.py` | `common/performance/*` |
| `terrain/engine.py` | `terrain/domain`, `raycast`, `mesh`, `persistence` |
| `terrain/providers.py` | `terrain/providers/*` |
| `terrain/render_pipeline.py` | `terrain/render/*` |
| `terrain/surface.py` | `terrain/surface/*` |
| `data/copernicus_orthophoto.py` | `data/copernicus/*` |
| `data/assets_manager.py` (implementación) | `data/assets/*`; queda como fachada canónica |
| `tests/test_performance_architecture.py` | siete suites semánticas en `tests/common`, `tests/data` y `tests/terrain` |

## 10. Arquitectura resultante

```text
TerraLab
├── astro                 cálculo astronómico y efemérides
├── cli                   entry points soportados
├── common                paths, biblioteca, caché, cancelación, presupuesto
├── data
│   ├── assets            registro, descubrimiento, instalación y validación
│   ├── catalogs          Gaia, estrellas y caché de scope
│   ├── converters        conversiones offline
│   └── copernicus        cliente, planificación, mosaico y manifiesto
├── scene                 estado, cámara y controlador, sin UI
├── render
│   ├── qt                adaptación Qt
│   ├── sky               capas astronómicas
│   └── workers           render asíncrono
├── terrain
│   ├── domain            bandas, curvatura y perfil
│   ├── infrastructure    teselas DEM y caché
│   ├── providers         ASC, GeoTIFF, dataset y chain
│   ├── raycast           cálculo de horizonte
│   ├── mesh              campo y normales
│   ├── persistence       schema NPZ
│   ├── render            geometría/materiales sin I/O
│   ├── surface           RGB, categórico, caché y servicio
│   └── services          casos de uso
└── ui                    widget, canvas, diálogos, mixins y workers
```

`TerrainCoordinator` es el único propietario runtime de `HorizonWorker`.
`AstronomicalWidget` y `AstroCanvas` tienen una sola definición cada uno.

## 11. Contratos de dependencias

`tests/architecture` hace fallar la suite si:

- dominio importa Qt, UI, rasterio o requests;
- `data`, `scene` o terrain core importan UI;
- runtime importa `TerraLab.tools`, scripts o herramientas;
- render core abre ficheros;
- reaparece un ciclo;
- reaparece una definición paralela de tipos canónicos;
- se construye `HorizonWorker` fuera del coordinador;
- reaparecen módulos `part_N.py`;
- se copia un namespace dinámicamente;
- reaparecen bridges UI eliminados;
- se escribe o crea `QApplication` durante imports;
- aparece cualquier `except Exception` cuyo único cuerpo sea `pass`.

## 12. Migraciones de configuración y datos

- `pyproject.toml` es la única fuente de dependencias, extras y entry points.
- `DataLibrary` separa preferencias de aplicación y datasets científicos.
- El layout versionado contiene manifiesto, descargas parciales, efemérides,
  catálogos, rasters, derivados y cachés.
- Las escrituras JSON son atómicas y no ocurren durante imports.
- Gaia usa lectura out-of-core y un índice HEALPix sin `allow_pickle=True`.
- El perfil de terreno persiste con schema explícito y lectura segura.
- `de421.bsp` ya no se distribuye dentro del árbol fuente; el asset manager lo
  instala en `data/sky/solar-system`.
- `.gitattributes` reserva LFS para binarios científicos. Configuración,
  fuentes, Markdown, TOML, YAML y JSON son texto Git normal.

## 13. Compatibilidad preservada

- Se conservan los nombres de consola declarados en `pyproject.toml`.
- Se conservan las claves de configuración y las variables de rollback de
  rendimiento.
- El lector NPZ acepta perfiles históricos sin `visible`, pero escribe el
  schema canónico.
- Se mantienen los modos RGB y categórico, el tooltip y la selección.
- Desmarcar superficie limpia render e interacción.
- Al retirar relieve 3D, la superficie vuelve a dibujarse y continúa siendo
  consultable.
- La caché de catálogo mantiene migración del formato plano histórico.
- Los datos grandes siguen siendo seleccionados/instalados por el usuario.

## 14. Tests añadidos

Se añadieron o reorganizaron pruebas para:

- límites de dependencias, ciclos y ownership;
- imports sin escrituras, threads, procesos ni `QApplication`;
- ausencia de fragmentos numerados y copias de namespace;
- ausencia de bridges UI retirados;
- caché por bytes y cancelación concurrente;
- presupuesto de memoria y flags de rollback;
- equivalencia escalar/vectorizada, NoData, previews, progreso y malla;
- índice espacial, pinning y presupuesto de teselas;
- ventanas GeoTIFF sin materializar el ROI completo;
- pipeline Gaia e índice HEALPix en proceso spawn;
- materialización ASC en workers acotados;
- colores de estrellas;
- entry points con `--help`;
- selección/deselección de superficie y transición relieve/superficie.

El antiguo test monolítico se repartió en:

- `tests/common/test_cache.py`
- `tests/common/test_performance_budget.py`
- `tests/terrain/test_vectorized_raycast.py`
- `tests/terrain/test_tile_index.py`
- `tests/terrain/test_raster_windows.py`
- `tests/data/test_gaia_process_pipeline.py`
- `tests/terrain/test_asc_cache.py`

## 15. Benchmarks antes/después

Máquina común: Windows 11, Python 3.13.7, 12 CPU lógicas. El resultado final
usa tres repeticiones y mediana; la línea base histórica sólo conservaba una
medición. Por tanto, los porcentajes son indicativos y reproducibles, no una
prueba estadística completa.

| Caso | Antes (s) | Después, mediana (s) | Cambio |
| --- | ---: | ---: | ---: |
| Terreno 30 m / 150 km / 0,5° | 0,236622 | 0,215903 | -8,8 % |
| Terreno 5 m / 150 km / 0,5° | 0,449594 | 0,403556 | -10,2 % |
| Terreno 1 m / 150 km / 0,5° | 1,454046 | 1,352714 | -7,0 % |
| Stream 62.000 estrellas | 0,011091 | 0,001023 | -90,8 % |
| Stream 1.000.000 estrellas | 0,017653 | 0,017530 | -0,7 % |
| Stream 43.600.000 estrellas | 0,888169 | 0,857710 | -3,4 % |
| Stream 157.700.000 estrellas | 3,267341 | 2,954029 | -9,6 % |

El pico RSS observado pasa de 172.544.000 a 124.829.696 bytes (-27,7 %).
Los resultados completos y muestras crudas están en
`benchmarks/performance_current.json`.

## 16. Métricas de tamaño antes/después

### Producto (`TerraLab`)

| Métrica | Antes | Después |
| --- | ---: | ---: |
| Ficheros Python | 137 | 222 |
| LOC | 86.473 | 84.222 |
| Clases | 236 | 248 |
| Funciones y métodos | 2.545 | 2.403 |
| Módulos >800 LOC | 34 | 29 |
| Capturas `Exception` | 690 | 627 |
| `except Exception: pass` | 237 | 0 |

### Repositorio Python

| Métrica | Antes | Después |
| --- | ---: | ---: |
| Ficheros Python | 195 | 300 |
| LOC | 104.823 | 105.933 |
| Clases | 315 | 331 |
| Funciones y métodos | 3.418 | 3.380 |
| Módulos >800 LOC | 39 | 35 |
| Capturas `Exception` | 697 | 637 |
| `except Exception: pass` | 238 | 0 |

### Arquitectura y estática

| Métrica | Antes | Después |
| --- | ---: | ---: |
| Componentes cíclicos | 3 | 0 |
| Candidatos Vulture ≥70 % | 27 | 0 |
| Candidatos heurísticos de inventario | 948 | 937 |
| Shims/bridges UI y scope catalogados | 5 | 0 |
| Módulos `part_N.py` | 0 | 0 |
| Errores Ruff, primer barrido raíz completo | 1.599 | 0 |

El aumento de ficheros no fue un objetivo: responde a límites de dominio y a
tests semánticos. Las LOC productivas bajan 2.251 líneas.

## 17. Riesgos restantes

1. **Pyright:** 2.362 errores y 9 warnings. La mayoría procede de mixins Qt
   dinámicos y stubs científicos. `tools/dev/check_pyright_baseline.py` impide
   que aumenten, pero no los resuelve.
2. **Capturas genéricas:** quedan 627. Todas las supresiones best-effort
   registran contexto y traceback en DEBUG, pero todavía deben estrecharse a
   excepciones concretas por subsistema.
3. **Cobertura:** 44 % del producto. Los kernels críticos están cubiertos, pero
   quedan rutas UI y de integración externa poco ejercitadas.
4. **Distribución standalone:** PyInstaller agotó 600 s en un child aislado y
   no produjo `TerraLab.exe`. Los procesos, spec y artefactos parciales se
   retiraron. La única distribución soportada es la instalación Python.
5. **Candidatos dinámicos:** 937 candidatos del inventario necesitan evidencia
   individual antes de cualquier borrado.
6. **Servicios externos:** descargas reales de Copernicus, Gaia, MET Norway,
   CAMS y datasets multigigabyte no se ejecutaron de extremo a extremo.
7. **Working tree:** contiene archivos locales previos del usuario
   (`TODO_*`, notas y datasets). Se preservaron deliberadamente.

## 18. Deuda técnica residual justificada

No se introdujo ningún monolito nuevo mediante fragmentos numerados. Aun así,
quedan 29 módulos productivos mayores de 800 líneas:

- `terrain/raycast/baker.py` (1.910): kernel numérico cohesivo, protegido por
  tests de equivalencia y benchmark. La extracción futura debe preservar la
  vectorización.
- `terrain/render/config.py` y `palette.py`: configuración y tablas
  declarativas; separarlas por tamaño empeoraría la localización semántica.
- `terrain/providers/raster_dataset.py` (1.009): backend de ventanas y caché;
  candidato a separar lifecycle de dataset e interpolación.
- `terrain/surface/service.py` (1.205): deuda de orquestación; siguiente
  candidato funcional.
- `onboarding_dialogs.py`, `widget_controls_builder.py`,
  `widget_bootstrap_helpers.py`, `canvas_runtime_helpers.py` y otros módulos UI
  siguen por encima del objetivo. Deben reducirse mediante controladores con
  contratos, no con más mixins mecánicos.
- renderizadores de estrellas, overlays, Vía Láctea y constelaciones contienen
  algoritmos y dibujo acoplados; requieren caracterización visual antes de otra
  división.

La prioridad siguiente debe ser: rediseñar el packaging standalone, tipar fronteras de
mixins, reducir capturas silenciosas por subsistema y elevar cobertura de UI.
La arquitectura activa, sin embargo, ya es única: no quedan runtimes legacy,
shims internos ni bridges paralelos para esas áreas.
