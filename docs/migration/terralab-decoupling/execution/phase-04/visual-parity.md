# Fase 04 — paridad visual del fondo de cielo

La ruta `scene` es ahora el valor por defecto y se puede revertir por proceso
con `TERRALAB_SKY_BACKGROUND_PIPELINE=legacy`. Ambas rutas terminan en el mismo
`OffscreenSceneRenderer` y QPainter; sólo cambia la capability de fondo:

```text
MODEL scene/plans/sky.py -> SkyBackgroundPlan
VIEW  render/qpainter/sky.py -> QImage pequeña escalada
```

## Semántica canónica

La ruta ejecutable anterior (`OffscreenSceneRenderer._draw_background`) no
dibujaba una silueta de suelo básica: el terreno se compone después como una
capa distinta. Por tanto, el plan conserva esa semántica y expresa una máscara
de proyección/clip de viewport. La silueta de terreno y el antiguo ground mask
de canvas permanecen explícitamente fuera de este slice para no migrar terreno
ni alterar el frame.

## Resultado automático

La ejecución final de `scene` es
`render-service/scene-20260729T142600Z/metadata.json` (15 repeticiones por
escena) y el rollback se capturó en
`render-service/legacy-20260729T142300Z/metadata.json`.

- Coinciden por SHA-256 los PNG completos de las 10 escenas estáticas:
  `day`, `twilight`, `night`, `dense_scope`,
  `solar_moon_planets_eclipse`, `milky_way_ngc`, `terrain_profile`,
  `terrain_relief`, `surface_rgb` y `surface_categorical`.
- `selection_measurement_constellation` conserva la variación declarada del
  pulso dependiente de `time.monotonic()`; no se ha actualizado ningún golden,
  hash ni tolerancia.
- Los payloads v1 de cuatro fixtures de recursos cambian sólo porque la
  herramienta de caracterización inserta paths absolutos bajo cada directorio
  de salida (`.../phase-03/...` frente a `.../phase-04/...`). El protocolo y
  los datos científicos no cambiaron; las imágenes estáticas correspondientes
  son exactas.

El benchmark específico final está en
`benchmarks/sky_background_20260729T141600Z.json` (1.000 repeticiones,
320×180): la ruta `scene` mide `+4,931 %` P50 en idle y `+4,733 %` en
interacción frente a `legacy`; ambos P95 son inferiores. El buffer nuevo es de
9.216 B en idle y 400 B en interacción, frente a un frame de 230.400 B: no se
crea una copia adicional del frame completo.

Las métricas de frame completo de escenas nocturnas muestran variación de
`renderer_overlays` entre procesos independientes. La capa migrada se mide por
separado en el benchmark anterior y permanece dentro del presupuesto; la
variación se conserva en los tres artefactos de servicio, no se oculta ni se
usa para cambiar el baseline.
