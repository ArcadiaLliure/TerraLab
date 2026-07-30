# Corrección del pipeline categórico S2GLC

## Defecto localizado

La lectura inicial del GeoTIFF no era el origen de las cuñas. El proveedor
usaba vecino más próximo, pero después ocurrían dos pérdidas de semántica:

1. `sample_native_lod` cuantizaba una celda y guardaba solamente el píxel de
   su centro. Un píxel minoritario podía representar una ventana de hasta
   128×128 píxeles.
2. El renderer recuperaba únicamente `RGBA` de `SurfaceSampleCache`, componía
   luz y niebla por vértice e interpolaba ese color final dentro del triángulo.
   `class_id`, `categorical` y `source_index` seguían en caché, pero no
   participaban en la rasterización.

El pipeline anterior era:

`GeoTIFF -> clase nearest/centro LOD -> paleta RGBA -> caché -> RGBA de vértice
-> luz+niebla de vértice -> interpolación RGBA -> imagen`

Por ello una clase presente en un solo vértice se extendía gradualmente y
aparecían colores que no pertenecían a ninguna clase.

## Pipeline corregido

El nuevo recorrido es:

`GeoTIFF -> clase entera nearest/moda LOD -> clase+fuente+paleta en caché ->
TerrainMaterialSamples -> coordenada ENU interpolada sobre la superficie ->
consulta nearest en la malla material -> luz+distancia continuas -> niebla ->
composición final`

`TerrainMaterialSamples` conserva conjuntamente:

- `base_rgba`;
- `valid`;
- `class_ids`;
- `categorical`;
- `source_indices`.

Los remapeos desde las rejillas `visual`, `relief` y `near_patch` se calculan
una vez y se aplican a todos los campos. La identidad categórica es el par
`(source_index, class_id)`, por lo que códigos iguales de leyendas distintas no
se confunden.

## Política de triángulos

- Tres vértices categóricos con la misma fuente y clase: color base constante.
- Clases categóricas distintas: se interpola la coordenada horizontal ENU del
  punto de superficie y se consulta por vecino más próximo la malla material
  precalculada. La decisión ya no depende del vértice dominante en pantalla.
- Tres vértices continuos de la misma fuente: interpolación RGBA anterior.
- Fuentes continuas diferentes o triángulo mixto: consulta discreta en la
  malla material mediante la coordenada de superficie; nunca se mezcla una
  categoría con RGB ni dos fuentes diferentes.
- `flat` sólo afecta magnitudes continuas y nunca promedia materiales
  categóricos.

Las coordenadas de la malla polar se convierten a este/norte antes de
interpolarlas, evitando la discontinuidad 0/360°. Para el parche próximo se
usan directamente sus ejes cartesianos. La consulta se realiza únicamente
contra `TerrainMaterialSamples` ya preparado por el worker: no hay lecturas
Rasterio/GDAL durante el pintado. Además, cualquier caché categórica fuerza la
ruta de z-buffer por triángulos incluso si una configuración antigua conserva
el modo `vertex` o `flat`; así no puede volver al renderer de bandas verticales.

Después de resolver el material se interpolan intensidad y distancia. La
perspectiva atmosférica se calcula desde esa distancia interpolada y
`compose_vertex_rgba` se aplica al color base ya resuelto. El antialiasing
continúa limitado a la cobertura/alfa del horizonte.

## LOD modal

Los factores válidos son 1, 2, 4, 8, 16, 32, 64 y 128.

- Factor 1 devuelve el entero exacto por vecino más próximo.
- Factores mayores reducen la ventana alineada al origen mediante la moda de
  píxeles válidos.
- NoData y máscaras se excluyen.
- Si hay empate y la clase central es modal, gana el centro.
- Si el centro no es válido o no está entre las clases modales, gana el
  `class_id` menor.
- Una ventana sin datos válidos se devuelve inválida.
- En fuentes RGB categóricas se decodifica cada píxel a clase antes de contar;
  no se calculan modas por canal.

Las consultas se deduplican por celda, se agrupan por factor/fila y por
intervalos próximos, y se procesan en ventanas acotadas. El lote máximo de
reducción está limitado aproximadamente a un millón de píxeles. No se carga el
raster continental completo.

Las teselas guardan `class_ids` `int64`, validez, bandas e identidad del
reductor/paleta. El namespace es `categorical-lod-modal-v2` y el esquema de
tesela es 2.

## Densidad visual

El viewport se propaga desde la UI al coordinador, worker y
`SurfaceSamplingRequest`. Para fuentes categóricas:

- se calcula píxeles por radián a partir del ancho y FOV;
- se fuerza LOD 1 cuando un píxel nativo proyecta al menos 0,5 px;
- la huella modal lejana se limita a unos 2 px;
- se subdividen aristas que exceden el objetivo de 8 px;
- los intervalos con mayor exceso, con desempate favorable al campo próximo,
  reciben primero el presupuesto;
- nunca se supera `max_relief_samples`.

Las elevaciones de la rejilla visual siguen interpolándose desde el DEM; la
subdivisión no inventa topografía. La política anterior se conserva para una
cadena exclusivamente RGB.

## Diagnóstico

`terrain_surface_diagnostics_enabled` está desactivado por defecto. Al
activarlo, el renderer conserva:

- imagen de material sin iluminación;
- imagen final;
- histograma visible por `(source_index, class_id)`;
- triángulos con uno, dos o tres materiales categóricos;
- triángulos mixtos;
- tiempo de resolución de materiales.

El diagnóstico bajo cursor devuelve, sin abrir el raster, clase, nombre,
fuente, leyenda, tipo de material, factor LOD, fila/columna nativa y origen
`exact`, `modal`, `source_fallback` o `terrain_fallback`.

## Invalidaciones

- `SURFACE_CACHE_POLICY_VERSION`: 3 -> 4.
- Esquema persistente de `SurfaceSampleCache`: 3 -> 4.
- `LAND_COVER_PALETTE_VERSION`: 2 -> 3.
- Geometría: `terrain-geometry-v4-categorical-materials`.
- Teselas LOD: `categorical-lod-modal-v2`, esquema 2.
- Material: `terrain-material-v3-surface-space`.
- Luz de vértices: `vertex-light-v2-continuous`.
- Triángulos: `triangles-v2-categorical-grid`.
- Z-buffer/imagen: `zbuffer-v5-surface-materials`.

La variación procedural S2GLC se ha desactivado. La paleta devuelve el color
canónico u override exacto y la variación visual procede únicamente de luz,
sombra y atmósfera aplicadas después de resolver la clase.

## Pruebas

Se añadieron pruebas para:

- conservación de enteros mayores de `2**24`;
- nearest, CRS y NoData existentes;
- factores 1, 2, 4 y 8;
- mayoría 83 con centro 105;
- desempate por centro y por clase menor;
- ventanas NoData y bordes recortados;
- decodificación RGB antes de la moda;
- persistencia modal y rechazo de teselas esquema 1;
- serialización de clase, fuente, máscara categórica y diagnóstico;
- rechazo de `SurfaceSampleCache` esquema 3;
- triángulos categóricos sin colores intermedios;
- selección de clase por coordenada del terreno aunque otro vértice domine en
  pantalla;
- invariancia del mapa categórico al cambiar la diagonal de triangulación;
- detección de cachés categóricas para impedir el renderer de bandas;
- RGB continuo suave;
- triángulos mixtos y fuentes diferentes;
- iluminación continua sobre una clase base constante;
- color canónico S2GLC independiente de coordenadas.

Resultados finales:

- pruebas dirigidas de superficie/render: `70 passed`;
- suite completa: `458 passed, 1 warning`;
- Ruff crítico (`E9,F63,F7,F82`) sobre los archivos del cambio: sin errores.
  La ejecución sobre todo `TerraLab/` sigue notificando `F821` preexistentes
  en módulos helper que inyectan dependencias mediante `globals().update`;
  quedan fuera de esta corrección.

## Rendimiento

Escena: Torroja del Priorat, radio 30 km, 48×120 muestras, S2GLC oficial de
404.618×412.612 píxeles, sin overviews. El baseline se ejecutó en un worktree
detached de `HEAD`; la medida corregida usó exactamente la misma cuadrícula y
un directorio de caché temporal aislado.

| Métrica | Pipeline anterior | Pipeline corregido | Cambio |
|---|---:|---:|---:|
| Muestreo frío | 0,936 s | 1,391 s | +48,6 % |
| Caché tras reinicio | 2,004 s | 0,576 s | -71,3 % |
| Bytes de ventanas retenidas | 2.578.382 | 34.888 | -98,6 % |
| Celdas LOD únicas | 4.239 | 4.239 | igual |
| Aciertos LOD en caliente | 4.239 | 4.239 | igual |
| Delta RSS frío | 419 MB | 547 MB | +30,4 % |

La fase fría es más lenta porque lee todos los píxeles válidos de cada ventana
y calcula una moda, no un único centro. La agrupación y el límite de lote
mantienen el trabajo acotado; la nueva tesela `int64` reduce marcadamente el
tiempo de recuperación en caliente. El pico RSS incluye inicialización
Rasterio/GDAL y cachés internas del proceso, por lo que no equivale al tamaño
de las ventanas, que fue 34,9 KB en esta escena.

El proxy de doce rotaciones para un viewport 960×540 usa la resolución
interactiva real 480×270:

- interpolación RGBA anterior, mediana: 17,6 ms;
- resolución dominante+composición categórica anterior, mediana: 42,1 ms;
- consulta de superficie+composición, mediana: 35,4 ms;
- p95 estable corregido: 49,4 ms;
- rendimiento mediano corregido: 28,3 FPS.

El resolver de superficie usa un bucle compilado y cacheable para la ruta
categórica completa. Su primera activación en un proceso requiere un
calentamiento JIT medido de 344 ms; no se repite durante las rotaciones. Si
Numba no está disponible se conserva la implementación NumPy vectorizada,
funcional pero más lenta.

Los resultados y capturas se generan con:

`python -m TerraLab.tools.benchmark_categorical_pipeline --output-dir
docs/benchmarks/categorical-s2glc`

## Archivos modificados

- `TerraLab/terrain/providers.py`: lectura entera y LOD modal persistente.
- `TerraLab/terrain/surface.py`: semántica/diagnóstico, caché esquema 4,
  viewport y densidad adaptativa.
- `TerraLab/terrain/overlay.py`: materiales discretos, composición por píxel y
  diagnóstico.
- `TerraLab/terrain/render_pipeline.py`: opción diagnóstica.
- `TerraLab/terrain/terrain_coordinator.py`, `TerraLab/terrain/worker.py` y
  `TerraLab/ui/sky_widget_impl.py`: propagación del viewport.
- `TerraLab/terrain/land_cover/legends/s2glc.py`: paleta canónica.
- `TerraLab/tools/benchmark_categorical_pipeline.py`: benchmark reproducible.
- `tests/test_surface_lod_cache.py`, `tests/test_terrain_surface_render.py` y
  `tests/test_s2glc_land_cover.py`: regresiones.

## Limitaciones

- La moda exacta es inevitablemente más costosa en frío que leer un centro.
- El benchmark de rotación aísla el pipeline NumPy con la resolución
  interactiva; no incluye coste de estrellas, Qt, compositor del escritorio ni
  latencia del monitor.
- El diagnóstico se conserva en memoria sólo cuando se activa. No se añadió
  un control visible nuevo; está disponible para benchmark y herramientas.
- No se usan overviews salvo que en el futuro se pueda demostrar mediante
  metadatos que fueron construidos con reducción categórica modal.
- La frontera discreta sigue limitada por la resolución y presupuesto de la
  malla. La política adaptativa reduce esa limitación, pero no reconstruye
  polígonos vectoriales subpíxel.
