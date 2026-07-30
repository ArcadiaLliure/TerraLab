# Revisión local final de TerraLab

Fecha: 26 de julio de 2026  
Rama: `refactor`  
Python de validación: 3.13.7

## 1. Base Git e índice

- `HEAD`: `98204d4cfd2e16fa6ff39075c72c573d4800cde8`
- `origin/refactor`: `98204d4cfd2e16fa6ff39075c72c573d4800cde8`
- `HEAD` y `origin/refactor` son iguales.
- No se hizo commit, push, merge, rebase ni reset durante esta fase.
- El único cambio staged es:
  `R100 TerraLab/terrain/Readme.md TerraLab/terrain/README.md`.
- La renombración permanece staged porque, en Windows, el índice es necesario
  para conservar un cambio que solo afecta a mayúsculas/minúsculas.
- Los demás cambios de producto siguen unstaged o untracked.
- `git diff --check` y `git diff --cached --check` terminan con código 0. Git
  avisa de futuras normalizaciones CRLF a LF; `.gitattributes` fija el
  contrato para que el parche sea estable.

Las salidas congeladas están en `git/`. El estado final añade únicamente el
bundle de revisión y `dist-modern/` como artefactos untracked.

El `git status --short` final también muestra como modificados
`canvas_runtime_helpers.py`, `widget_controls_builder.py` y
`widget_mixins/layers.py`. En los tres casos `git diff` y `git diff --numstat`
están vacíos y el blob normalizado coincide con `HEAD`: solo cambió la
representación CRLF física al retirar el delta bloqueante, mientras la nueva
`.gitattributes` exige LF. No forman parte del parche ni del delta semántico.
Se dejaron sin stage para respetar el índice del usuario. El estado literal
está en `git/final_status_short.txt`.

## 2. Separación del delta y archivos personales

El parche propuesto contiene exactamente 30 rutas y tiene SHA-256:

`029710EE61DE6B13634AB40F1244CA0732622F9D58F45B899A9B046E150D7592`

No incluye datasets, credenciales, cachés, rutas personales, resultados
científicos, notas o planes privados. Se preservan y excluyen expresamente:

- `TODO_PHOTOMETRIC.md`
- `TODO_light_pollution.md`
- `TODO_refactor.md`
- `TerraLab/NOTES.md`
- `TerraLab/assets/ES.csv`
- `TerraLab/assets/FR.csv`
- `TerraLab/assets/PT.csv`
- `plans/`
- informes, cobertura y benchmarks generados que no son necesarios para el
  producto final
- `TerraLab/assets/horizon_profile.npz`
- `TerraLab/data/light_pollution/C_DVNL 2022.tif`

Los dos últimos binarios estaban ocultos en el estado habitual por atributos
del índice y se detectaron al construir un índice temporal. Se excluyeron
antes de conservar el parche.

Los parches y los artefactos de este bundle no están staged. Sus hashes y
alcance están en `patch_manifest.md`.

## 3. Reproducción en un worktree limpio

Se creó el worktree temporal
`C:\Users\Manel\AppData\Local\Temp\terralab-review-98204d4` directamente desde
`98204d4`. El parche completo se aplicó sin leer ficheros untracked del
checkout original.

En Windows, `git apply --index` no puede materializar directamente la
renombración que solo cambia capitalización. El procedimiento reproducible
fue:

1. aplicar el parche al índice temporal con `git apply --cached`;
2. eliminar únicamente el nombre físico antiguo `Readme.md` del worktree
   temporal;
3. materializar el índice con `git checkout-index --all --force`.

El resultado contiene `TerraLab/terrain/README.md` y no contiene
`TerraLab/terrain/Readme.md`. El diff staged del worktree limpio tuvo
exactamente el mismo hash que el parche completo.

Después de guardar todas las salidas y copiar los artefactos, el worktree
temporal se eliminó con `git worktree remove --force`. Los virtualenvs y
directorios de estado aislado permanecen bajo
`C:\Users\Manel\AppData\Local\Temp\terralab-*`: la política de ejecución
bloqueó su borrado recursivo. No están dentro del repositorio ni intervienen
en el delta propuesto.

## 4. Entorno limpio y matriz completa

Se creó el virtualenv
`C:\Users\Manel\AppData\Local\Temp\terralab-review-venv-98204d4`. Todos los
comandos se ejecutaron desde el worktree limpio; para cada uno se guardaron
comando, directorio, Python, inicio, fin, duración, código de salida, stdout y
stderr en `validation/`.

| Comprobación | Resultado | Duración |
|---|---|---:|
| `pip install --upgrade pip` | exit 0 | 5.639 s |
| `pip install -e ".[dev]"` final | exit 0 | 9.077 s |
| `compileall` | exit 0 | 0.173 s |
| Pytest collect | 566 tests | 3.575 s |
| Pytest completo | 566 passed, 29 warnings | 42.926 s |
| Ruff 0.13.2 | 0 errores | 0.758 s |
| Vulture, confianza 70 | 0 hallazgos | 2.081 s |
| Pyright focalizado, 4 ficheros | 0 errores, 0 warnings | 2.984 s |
| Baseline Pyright global | 2343 errores, 9 warnings; baseline 2362/9 | 23.506 s |
| Inventario | 382 ficheros, 303 módulos, 3744 símbolos, 821 aristas, 0 ciclos | 16.251 s |
| Build sdist y wheel | exit 0 | 15.285 s |

Los 29 warnings son deprecaciones de NumPy 2.5 expuestas por lecturas de
Rasterio; no hubo skips ni xfails.

La primera instalación limpia resolvió Ruff 0.16 debido al rango
`ruff>=0.6,<1` y reprodujo fallos que el entorno local con Ruff 0.13.2 no
mostraba. Ese fue un defecto bloqueante de reproducibilidad. Se corrigió
`pyproject.toml` fijando `ruff==0.13.2` y añadiendo `build==1.5.0` al extra
`dev`. El log fallido `06_ruff.*` y el log verde `06b_ruff_pinned.*` se
conservan.

Las invariantes focalizadas también quedan verdes:

- 0 ciclos arquitectónicos.
- 0 `except Exception: pass`.
- 0 lectores con `allow_pickle=True`.
- 0 módulos `part_N.py`.
- 0 copias dinámicas de namespaces.
- 38/38 paquetes documentables con `README.md` de capitalización exacta.

## 5. Regresiones funcionales de superficie

La batería independiente dio `6 passed in 1.49s`.

- Al desmarcar la superficie se eliminan textura, relleno, interacción,
  tooltip, hit-test, caché visual, materiales y referencia publicada.
- Al desactivar el relieve 3D la superficie continúa pintándose mediante las
  bandas y muestras de perfil 2D. El tooltip y el hit-test categórico siguen
  operativos y no se requiere una malla activa.

La matriz de estados y los tests concretos están en
`surface_regressions.md`.

Durante la ejecución apareció un cambio tardío en cinco ficheros que forzaba
el relieve 3D y bloqueaba el control cuando la superficie estaba visible. Se
reprodujo la violación del contrato anterior y se retiró solo ese cambio
bloqueante mediante edición explícita. No forma parte del parche final y el
resto del working tree se preservó.

## 6. Shutdown cooperativo

No existe `QThread.terminate()` en el producto ni en el cierre ordinario.
Pasaron los escenarios sin trabajo, bake con subprocess, lectura GeoTIFF,
muestreo, generación de caché, preview y dos construcciones/cierres del
widget.

- Batería principal: `8 passed in 4.99s`.
- Probes adicionales de caché y preview: `2 passed in 0.29s`.
- Smoke instalado aislado: dos cierres en 0.00193 s y 0.00216 s.
- Tras el cierre: cero QThreads propios activos, cero threads Python propios,
  cero subprocesses propios y ficheros temporales desbloqueados.
- Los timers pertenecen a widgets cerrados y no quedan callbacks diferidos
  vivos contra ellos.

Detalle y tiempos por escenario: `shutdown_regressions.md`.

## 7. Gaia independiente del CWD

`tests/test_gaia_manifest_resolution.py` se ejecutó desde:

1. el worktree limpio;
2. un directorio temporal vacío;
3. un directorio temporal con un `tile_manifest.json` falso.

Las tres ejecuciones dieron `3 passed` y el mismo resultado. El manifiesto del
CWD nunca se descubre. Los estados son explícitos:

- `catalog_not_configured` cuando no existe biblioteca configurada;
- `catalog_unavailable` para una configuración inválida o no resoluble;
- `catalog_available` solo para el manifiesto de la biblioteca configurada.

## 8. Wheel, sdist e instalación externa

El manifiesto completo está en `artifact_manifest.md`.

| Artefacto | Tamaño | SHA-256 |
|---|---:|---|
| `terralab-0.1.0-py3-none-any.whl` | 800624 | `03C91C3B291FC6111BAE70875E442EDA64A161800A92F72E854D3372BBE92E51` |
| `terralab-0.1.0.tar.gz` | 819663 | `15B470905130E20B8E18D8B41955CCF1398FD661FC303E51F9899A669A929CAA` |

El wheel contiene licencia MIT, `METADATA` y exactamente siete entry points.
No contiene tests, tools, benchmarks, datasets, caches ni rutas del checkout.
El sdist contiene fuentes, documentación pública, tests y metadatos de
construcción, sin datos ni ficheros privados.

Se creó desde cero el virtualenv externo
`C:\Users\Manel\AppData\Local\Temp\terralab-wheel-final-98204d4` y se instaló
exclusivamente el wheel absoluto de `dist-modern/`. La instalación terminó con
exit 0 en 105.097 s. Desde un CWD vacío:

- `TerraLab.__file__` apunta a `Lib/site-packages/TerraLab/__init__.py`;
- los siete comandos responden a `--help` con exit 0;
- no se creó ningún fichero en el CWD;
- `--help` no creó `QApplication` ni solicitó datasets;
- el smoke Qt instalado se ejecutó con `APPDATA`, `LOCALAPPDATA` y
  `TERRALAB_DATA_ROOT` aislados y vacíos;
- la ausencia de DEM y efemérides se trató como disponibilidad ausente, no
  como fallo de importación o cierre.

## 9. README y enlaces

La auditoría encontró exactamente 38 paquetes Python documentables:

- 38/38 tienen `README.md` con capitalización exacta;
- 38/38 contienen texto específico del paquete;
- no hay pares de alta similitud que indiquen plantillas duplicadas;
- no hay enlaces internos rotos;
- no queda ninguna referencia Markdown a `Readme.md`.

El JSON completo `validation/14_readme_audit.stdout.txt` registra por paquete
responsabilidad, API pública, dependencias, propiedad de estado, concurrencia,
persistencia y tests. Muchos README cortos no explicitan todos esos campos:
es una limitación documental conocida, no un enlace roto ni contenido
genérico.

## 10. Delta local propuesto respecto de `98204d4`

| Archivo | Tipo | Problema corregido | Test protector | Riesgo | Estado | Origen | Destino |
|---|---|---|---|---|---|---|---|
| `.gitattributes` | añadido | Normalización reproducible de texto y binarios | aplicación exacta del parche | bajo | untracked | nuevo | commit futuro |
| `CHANGELOG.md` | añadido | Historial público ausente | packaging/link audit | bajo | untracked | nuevo | commit futuro |
| `LICENSE` | añadido | Licencia MIT declarada pero inexistente | `test_packaging_contract` | bajo | untracked | nuevo | commit futuro |
| `README.md` | modificado | Enlaces y estado de calidad/empaquetado | link audit | bajo | unstaged | preexistente | commit futuro |
| `TerraLab/astro/ephemeris_coordinator.py` | modificado | Cancelación y cierre explícitos | lifecycle Qt | medio | unstaged | preexistente | commit futuro |
| `TerraLab/data/assets_manager.py` | modificado | Biblioteca de datos como dependencia explícita | packaging/full suite | bajo | unstaged | preexistente | commit futuro |
| `TerraLab/data/star_data_coordinator.py` | modificado | Estado de carga consistente tras cierre/error | lifecycle/full suite | medio | unstaged | preexistente | commit futuro |
| `TerraLab/render/workers/star_render.py` | modificado | Cancelación cooperativa de render y procesos | lifecycle/full suite | medio | unstaged | preexistente | commit futuro |
| `TerraLab/terrain/README.md` | renombrado | Capitalización portable | `test_packaging_contract` | bajo | staged | preexistente | commit futuro |
| `TerraLab/terrain/terrain_coordinator.py` | modificado | Afinidad de thread, snapshots y shutdown | `test_worker_data_sources` | alto | unstaged | preexistente | commit futuro |
| `TerraLab/terrain/worker.py` | modificado | Cancelación, recursos raster y finalización | `test_worker_data_sources` | alto | unstaged | preexistente | commit futuro |
| `TerraLab/ui/astro_canvas.py` | modificado | Contrato tipado de ciclo de vida/render | Pyright focalizado/lifecycle | medio | unstaged | preexistente | commit futuro |
| `TerraLab/ui/astronomical_widget.py` | modificado | Propiedad explícita, callbacks, Gaia y cierre | Gaia/lifecycle/Pyright | alto | unstaged | preexistente | commit futuro |
| `TerraLab/ui/canvas_mixins/interaction.py` | modificado | Constructor implícito dependiente del MRO | Pyright/full suite | medio | unstaged | preexistente | commit futuro |
| `TerraLab/ui/canvas_selection.py` | modificado | Hit-test coherente con visibilidad superficial | regresión de superficie | medio | unstaged | preexistente | commit futuro |
| `TerraLab/ui/scope_preload_worker.py` | modificado | Cancelación de preload y subprocess | lifecycle/full suite | medio | unstaged | preexistente | commit futuro |
| `TerraLab/ui/widget_bootstrap_helpers.py` | modificado | Callbacks ligados al ciclo de vida | lifecycle Qt | medio | unstaged | preexistente | commit futuro |
| `TerraLab/ui/widget_init_helpers.py` | modificado | Inicialización canónica explícita | lifecycle/Pyright | medio | unstaged | preexistente | commit futuro |
| `TerraLab/ui/widget_mixins/bootstrap_terrain.py` | modificado | Elimina constructor dependiente del MRO | Pyright/full suite | medio | unstaged | preexistente | commit futuro |
| `docs/architecture/refactor_pyright_baseline.json` | añadido | Baseline reproducible | `check_pyright_baseline.py` | bajo | untracked | nuevo | commit futuro |
| `docs/architecture/silent_exception_baseline.json` | añadido | Baseline de excepciones silenciosas | arquitectura | bajo | untracked | nuevo | commit futuro |
| `docs/categorical-surface-pipeline.md` | añadido | Contrato público de superficie | link audit | bajo | untracked | nuevo | commit futuro |
| `docs/deprecations.json` | añadido | Registro tipado de deprecaciones | full suite | bajo | untracked | nuevo | commit futuro |
| `docs/roadmap.md` | añadido | Enlace público declarado | link audit | bajo | untracked | nuevo | commit futuro |
| `pyproject.toml` | modificado | Build/licencia/paquetes; Ruff y build reproducibles | build/packaging/Ruff | medio | unstaged | preexistente | commit futuro |
| `tests/test_data_layers_ui_contract.py` | modificado | Desmarcado superficial y callback de UI | mismo fichero | bajo | unstaged | preexistente | commit futuro |
| `tests/test_gaia_manifest_resolution.py` | añadido | Gaia independiente del CWD | mismo fichero, tres CWD | bajo | untracked | nuevo | commit futuro |
| `tests/test_packaging_contract.py` | añadido | Licencia, enlaces, entry points y paquete | mismo fichero | bajo | untracked | nuevo | commit futuro |
| `tests/test_qt_lifecycle.py` | añadido | Construcción, cierre y reconstrucción offscreen | mismo fichero | bajo | untracked | nuevo | commit futuro |
| `tests/test_worker_data_sources.py` | modificado | Thread affinity, bake, raster, muestreo y shutdown | mismo fichero | bajo | unstaged | preexistente | commit futuro |

`dist-modern/` y `docs/architecture/review_bundle/` son artefactos de revisión
local y no se han añadido al parche propuesto ni al índice.

## 11. Riesgos y limitaciones

1. El baseline global de Pyright sigue en 2343 errores y 9 warnings. No crece
   y el ámbito focalizado está a cero, pero la deuda histórica continúa.
2. Pytest emite 29 deprecaciones NumPy/Rasterio.
3. Los README de todos los paquetes son específicos, aunque muchos no
   explicitan cada aspecto de contrato auditado.
4. El sdist incluye tests y metadatos generados por setuptools; el wheel de
   distribución permanece limpio.
5. La primera versión del smoke instalado escribió su configuración mínima en
   `C:\Users\Manel\AppData\Roaming\TerraLab\config\config.json` antes de que
   se aislara `APPDATA`. TerraLab volvió a persistir en ese fichero sus claves
   de visibilidad y `raster_path`; `data_location.json`, `data_sources.json` y
   los datasets no se modificaron. No existe una copia previa fiable con la
   que restaurar preferencias desconocidas. Las ejecuciones definitivas usan
   rutas temporales aisladas y el helper quedó corregido para no volver a
   tocar el perfil real.

## 12. Decisión

El parche de producto es reproducible, limpio y técnicamente cumple las
condiciones de preparación para commit. Sin embargo, por la incidencia
explícita sobre el fichero de preferencias personal durante el primer smoke,
la entrega requiere revisión humana local antes de considerar cualquier
publicación.

**READY_FOR_LOCAL_REVIEW**

No se ha realizado ningún commit ni push.
