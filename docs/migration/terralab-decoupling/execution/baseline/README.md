# Fase 01 — baseline y caracterización ejecutable

Este directorio conserva resultados nuevos de la fase 01. No sustituye los
artefactos históricos existentes en `docs/architecture/`.

## Inventario

Se generó con:

```powershell
python tools/dev/code_inventory.py --root . --output-dir docs/migration/terralab-decoupling/execution/baseline/inventory
```

Los archivos CSV y JSON contienen el inventario completo, símbolos, grafo y
candidatos de revisión. `inventory/summary.json` resume el alcance de producto:
234 módulos Python, 87.555 LOC, 2.855 símbolos, 59 módulos con acoplamiento
Qt y cero componentes cíclicos.

## Manifest MVC

`docs/architecture/mvc_file_roles.json` se genera de forma determinista con:

```powershell
python tools/dev/generate_mvc_file_roles.py --root . --output docs/architecture/mvc_file_roles.json
python -m pytest -q tests/architecture/test_mvc_file_roles.py
```

El manifest cubre exactamente cada `TerraLab/**/*.py`: 58 `MODEL`, 29 `VIEW`,
3 `CONTROLLER`, 88 `OUTSIDE_MVC` y 56 `MIXED`. Es una fotografía normativa de
la deuda actual; no reetiqueta ni resuelve responsabilidades mezcladas.

## Capturas y mediciones automáticas

Las escenas están fijadas en
`tests/fixtures/render_characterization_scenes.json`. La herramienta arranca
el proceso real `TerraLab.runtime.render_service`, intercambia el frame por el
pool de memoria compartida y ejecuta el `paintEvent` de
`SharedFramePresenter` antes de guardar cada PNG.

La ejecución de baseline aceptada es:

```powershell
python tools/dev/characterize_render_service.py --output-dir docs/migration/terralab-decoupling/execution/baseline/render-service/run-20260729T123500Z --repeats 3
```

Su `metadata.json` registra, por escena: payload JSON y hash, P50/P95 de
encode/decode, render, ida y vuelta IPC y presentación; RSS de UI y render
cuando el sistema operativo lo expone; hashes y tamaños de imágenes; DPI,
formato, fuente y los cuatro assets sintéticos de terreno/superficie.

No se aprobó ningún golden ni se cambió ninguna tolerancia. La escena de
selección contiene un pulso intencionadamente dependiente del reloj monotónico:
sus entradas son deterministas, pero su píxel exacto se declara variable en sus
metadatos. Las rutas activas no emiten contadores de hit/miss de caché para
estas fixtures; el metadata conserva mapas vacíos y lo declara explícitamente.

El smoke de la herramienta se ejecuta automáticamente mediante:

```powershell
python -m pytest -q tests/test_render_service_characterization.py
```

## Benchmark de optimización

El benchmark CPU existente se ejecutó con tres repeticiones y se guardó en:

`benchmarks/benchmark_optimization_20260729T121500Z.json`.

## Procedimiento de comprobación manual (pendiente de aceptación humana)

1. Desde la raíz, inicia TerraLab con `python -m TerraLab`.
2. Espera al primer frame completo y verifica que no se muestra un error del
   worker de render.
3. Haz pan y zoom continuos durante al menos diez segundos; comprueba que el
   frame final sigue al último gesto y que la interfaz no queda bloqueada.
4. Cambia hora entre día, crepúsculo y noche; verifica Sol/Luna, estrellas y
   capas de la cuadrícula.
5. Activa el modo scope y cambia FOV, centro y forma; comprueba que el retículo
   y los controles siguen respondiendo.
6. Activa/desactiva capas celestes, Via Láctea/NGC cuando sus datos estén
   instalados, selección y herramienta de medida.
7. Alterna horizonte, perfil/relieve y superficies RGB/categórica con datos
   locales disponibles; comprueba que no hay artefactos, huecos ni colores
   inesperados.
8. Redimensiona la ventana varias veces, cierra la aplicación, vuelve a
   iniciarla y confirma un nuevo frame correcto.
9. Conserva o compara las capturas de
   `render-service/run-20260729T123500Z/images/` y comunica el resultado de
   cada paso. No marques esta lista como aprobada sin observación humana.

Estado de aceptación manual: **PENDIENTE de confirmación del usuario**.
