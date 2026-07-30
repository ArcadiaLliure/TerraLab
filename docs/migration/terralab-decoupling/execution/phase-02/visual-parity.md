# Fase 02 — paridad visual QPainter

La ruta de fase 02 conserva el mismo backend efectivo:

```text
runtime/render_service -> QPainterLegacyBackend -> OffscreenSceneRenderer -> QPainter
```

La ejecución de cinco repeticiones está en
`render-service/run-20260729T115708Z/metadata.json`. Se comparó con el
baseline de fase 01, usando el SHA-256 de cada PNG completo:

- coincidencia exacta: `day`, `twilight`, `night`, `dense_scope`,
  `solar_moon_planets_eclipse`, `milky_way_ngc`, `terrain_profile`,
  `terrain_relief`, `surface_rgb` y `surface_categorical`;
- `selection_measurement_constellation` no se usa como golden exacto porque
  el pulso de selección depende deliberadamente de `time.monotonic()`. Esta
  variación ya se declaró en el metadata de fase 01 y no se ha cambiado en la
  fase 02.

No se ha actualizado ningún golden, hash esperado ni tolerancia. Los snapshots
IPC mantienen exactamente los mismos bytes por escena. Con cinco repeticiones,
ningún P50 de render aumenta más del 5 % frente al baseline; el mayor aumento
observado es +3,8 % en `milky_way_ngc`.
