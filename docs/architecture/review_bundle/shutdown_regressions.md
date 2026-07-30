# Shutdown cooperativo

No hay llamadas a `QThread.terminate()` en el producto. El cierre normal marca
cancelación, interrumpe procesos propios, encola la liberación de recursos en
el thread propietario, ejecuta `quit()` desde el worker y espera con un límite
de 15 segundos y diagnóstico progresivo.

| Escenario | Prueba | Tiempo observado | Recursos comprobados | Resultado |
|---|---|---:|---|---|
| Sin trabajo activo | `test_terrain_coordinator_shutdown_never_leaves_worker_thread_running` | 0.01 s | QThread detenido; cierre idempotente | PASS |
| Durante bake | `test_terrain_coordinator_closes_during_bake_and_reaps_subprocess` | 0.02 s | QThread detenido; subprocess terminado y esperado | PASS |
| Durante lectura GeoTIFF | `test_terrain_coordinator_closes_geotiff_resource_after_cancelled_read` | 0.02 s | QThread detenido; servicio/dataset cerrado | PASS |
| Durante muestreo | `test_terrain_coordinator_closes_during_surface_sampling` | 0.02 s | Cancelación observada; QThread detenido | PASS |
| Durante generación de caché | `test_close_during_surface_cache_generation` | 0.03 s | QThread detenido; fichero cerrable, renombrable y eliminable | PASS |
| Durante preview | `test_close_during_preview_file_generation` | 0.02 s | QThread detenido; fichero cerrable, renombrable y eliminable | PASS |
| Reconstrucción del widget | `test_astronomical_widget_can_close_and_rebuild_offscreen` | 4.55 s total | Dos instancias; timers activos en vida; threads Qt y Python a cero tras cierre | PASS |
| Dos cierres instalados consecutivos | `installed_widget_smoke.py` | 0.00193 s y 0.00216 s | Sin threads Python; terrain/canvas QThreads detenidos; configuración y datos aislados | PASS |

La batería principal de shutdown dio `8 passed in 4.99s`; los dos probes
adicionales de caché/preview dieron `2 passed in 0.29s`. El primer intento de
los probes terminó con un error nativo porque el helper no conservaba viva la
instancia de `QCoreApplication`; el test se corrigió y se repitió. Se
conservan ambos logs (`12b_*` y `12c_*`) para no ocultar esa incidencia del
instrumento de prueba.
