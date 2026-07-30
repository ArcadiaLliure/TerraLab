# Fase 03 — paridad visual QPainter

La fase 03 mantiene intacta la ruta de rasterizado efectiva:

```text
SceneFrame tipado -> protocolo JSONL v1 -> runtime/render_service
-> OffscreenSceneRenderer -> QPainter
```

La ejecución de aceptación automática es
`render-service/run-20260729T131300Z/metadata.json`, con 15 repeticiones por
escena a `320x180@1.0 DPR`. Se comparó contra la evidencia de fase 02
(`run-20260729T115708Z/metadata.json`).

- Los PNG completos de las 10 escenas estáticas coinciden exactamente por
  SHA-256: `day`, `twilight`, `night`, `dense_scope`,
  `solar_moon_planets_eclipse`, `milky_way_ngc`, `terrain_profile`,
  `terrain_relief`, `surface_rgb` y `surface_categorical`.
- `selection_measurement_constellation` conserva la variación ya declarada:
  su pulso de selección depende de `time.monotonic()`. Los bytes de entrada
  de esa escena sí permanecen deterministas.
- Los bytes de los 11 snapshots JSONL v1 no cambian frente a fase 02.
- Frente a la ejecución de fase 02 de cinco repeticiones, el mayor aumento
  observado de P50 de rasterizado es `+1,49 %` (`terrain_relief`); no aumenta
  ningún P95. Los demás P50 mejoran o se mantienen dentro de la variación de
  la máquina.

No se actualizó ningún golden, hash esperado ni tolerancia. La decodificación
v1 ocurre antes de iniciar la medición de rasterizado, por lo que `render_ms`
sigue midiendo sólo la parte de backend/QPainter como en la fase anterior.
