# Análisis del pipeline de relieve 2.5D

Estado analizado: rama `refactor`, antes de la implementación del nuevo
sombreado, muestreo y antialiasing solicitados.

## Resumen ejecutivo

TerraLab no genera hoy una única malla durante el raycast. El subprocess de
`bake_process.py` obtiene primero un **perfil científico por bandas** mediante
`HorizonBaker.bake_progressive()` y después construye una **malla polar visual**
independiente mediante `HorizonBaker.build_view_mesh()`. El primero conserva,
para cada azimut y banda de distancia, el punto de mayor elevación aparente y
el último punto válido de la banda; la segunda conserva una rejilla rectangular
`distancia × azimut` con altura, elevación aparente, validez, visibilidad y
normal.

La malla llega serializada en un `HorizonProfile` al proceso GUI. Allí
`HorizonOverlay` prepara arrays inmutables, selecciona las columnas visibles,
proyecta cada vértice a pantalla y, en el camino activo anterior a este trabajo,
convierte cada anillo de distancia en una franja de `QPolygonF`. El color de la
franja se aproxima con un gradiente horizontal de `QPainter`. Existe ya un
rasterizador de triángulos con z-buffer y coordenadas baricéntricas, pero no
está conectado al camino de renderizado.

También existe iluminación parcial basada en normales y una mezcla de bruma
por distancia, pero sus constantes están fijadas en `overlay.py`, no exponen el
contrato configurable solicitado y la ruta activa interpola sólo un factor a
lo largo de cada franja. El supuesto muestreo adaptativo sólo aumenta el paso
radial por tramos; no evalúa error de interpolación, cambio de pendiente,
proyección ni oclusión.

## Recorrido completo de los datos

### 1. Selección y acceso al DEM

1. La UI construye un job de horizonte con observador, representación, número
   de bandas, paso angular, cámara y radio visible.
2. `HorizonWorker.request_bake()` se ejecuta en un `QThread`, transforma el job
   en argumentos primitivos mediante `_build_subprocess_command()` y lanza
   `python -m TerraLab.terrain.bake_process`.
3. `bake_process.main()` crea el proveedor con `_create_provider()`. Los
   proveedores de `terrain/providers.py` normalizan las fuentes ASC/NPY o
   GeoTIFF y exponen `get_elevation()` y, cuando es posible,
   `sample_elevation()`/`sample_elevations()` vectorizados.
4. Las coordenadas de entrada del observador son WGS84. El proveedor las
   transforma al sistema cartesiano interno `EPSG:25831`; todos los avances de
   los rayos se hacen en metros en ese plano.

La altura absoluta del ojo usada por el raycast es:

```text
h_eye_abs = DEM_observer + observer_offset + eye_height
```

`eye_height` vale actualmente 1,7 m. `observer_offset` representa el
desplazamiento configurable sobre el DEM.

### 2. Generación de muestras de cada rayo

Las muestras científicas se generan en `TerraLab/terrain/engine.py`, clase
`HorizonBaker`:

- `_adaptive_distances()` materializa las distancias del raycast.
- `_next_step_distance()` usa 0,5 m como inicio y un paso por tramos: paso base
  hasta 3 km, ×2 hasta 15 km, ×4 hasta 50 km y ×8 después.
- `_sample_azimuth_chunk_vectorized()` calcula matrices X/Y para lotes de hasta
  64 azimuts, pide las elevaciones al proveedor y reduce los resultados por
  banda.
- `_sample_single_azimuth()` es el fallback escalar equivalente.
- `bake_progressive()` ordena los rayos dando prioridad al FOV actual y publica
  previews parciales.

Para una distancia `d`, azimut `az`, observador `(x0, y0)` y terreno `h`:

```text
x = x0 + d * sin(az)
y = y0 + d * cos(az)
earth_drop = d² / (2 * R_effective)
apparent_elevation_rad = atan2(h - earth_drop - h_eye_abs, d)
```

`R_effective` incluye el factor de radio terrestre efectivo sólo si está
habilitada la refracción atmosférica en la configuración de alcance. Esta
ecuación es la referencia que deberá compartir el test geodésico Morell → Puig
d'en Cama; cualquier test que omita `observer_offset`, `eye_height`, curvatura o
refracción compararía magnitudes distintas.

Cada banda guarda seis arrays alineados con `HorizonProfile.azimuths`:

- `angles`, `dists`, `heights`: ángulo máximo aparente de la banda y distancia
  y cota DEM del punto que lo produce.
- `surface_angles`, `surface_dists`, `surface_heights`: último punto válido de
  la banda, empleado para representar su superficie distal.

### 3. Construcción de vértices de la malla

La malla visual se construye en `HorizonBaker.build_view_mesh()` después del
perfil. `_mesh_distance_rings()` produce hasta unas 395 coronas con alta
densidad hasta 5 km y densidad progresivamente menor en cuatro zonas lejanas.
El eje angular usa el paso científico, salvo que un paso inferior a 0,05° se
agrupe en un múltiplo alineado.

Cuando el proveedor soporta lotes, el raycast vectorizado une temporalmente las
distancias científicas y las coronas visuales. Retiene estas últimas en
`PolarElevationField`, de forma que `build_view_mesh()` puede reutilizar las
elevaciones sin consultar otra vez el DEM. El fallback escalar vuelve a
muestrear cada vértice.

La malla serializada es un diccionario de arrays NumPy:

| Campo | Forma | Significado |
| --- | --- | --- |
| `azimuths` | `(A,)` | azimut de cada columna, grados |
| `distances` | `(D,)` | distancia real de cada corona, metros |
| `elevations` | `(D,A)` | cota del DEM, metros |
| `altitudes` | `(D,A)` | elevación aparente proyectable, grados |
| `normal_x/y/z` | `(D,A)` | normal ENU local unitaria |
| `valid` | `(D,A)` | el DEM aportó muestra válida |
| `visible` | `(D,A)` | la muestra alcanza la envolvente angular del rayo |

La posición local 3D no se almacena explícitamente porque se reconstruye sin
pérdida como `(d*sin(az), d*cos(az), elevation)`.

### 4. Normales existentes

`compute_polar_mesh_normals()` calcula diferencias radiales centralizadas (o
hacia delante/atrás en bordes) y diferencias angulares con wrap 0°/360°. Las
derivadas radial/tangencial se rotan al marco Este-Norte y forman la normal
`(-dz/dx, -dz/dy, 1)`, que se normaliza. Los puntos inválidos reciben
`(0,0,1)`.

Para mallas angulares extremadamente pequeñas o gruesas,
`build_view_mesh()` usa `_sample_normal()`, que consulta cuatro puntos
cartesianos alrededor de cada vértice. En el resto de casos las normales se
precalculan por bloques antes de serializar el perfil. La GUI sólo las
reconstruye si carga una malla legacy sin normales válidas.

### 5. Conectividad y triangulación

Hay dos conectividades en `HorizonOverlay`:

1. La ruta activa llama `_build_terrain_surface_spans()`. Cada corona visible
   genera una franja limitada por su curva proyectada y la envolvente ya pintada
   de coronas más lejanas. `_terrain_polygons_for_geometry()` la transforma en
   un `QPolygonF`.
2. `_terrain_triangles_for_view()` forma dos triángulos por celda rectangular
   polar, reutilizando índices `(fila, columna)` exactos. Descarta celdas con un
   vértice inválido, saltos angulares, triángulos degenerados o aristas
   patológicas. También añade dos triángulos por segmento para cerrar la corona
   próxima hasta el borde inferior. Esta ruta existe pero no estaba invocada.

La segunda conectividad es la adecuada para interpolación baricéntrica y para
evitar que dos triángulos compartidos calculen normales o colores distintos.

### 6. Cálculo de color anterior

Los colores base proceden de dos vías:

- `SurfaceSamplingService` (`terrain/surface.py`) muestrea capas RGB o
  categóricas en las posiciones del perfil y en un subconjunto de la malla. El
  resultado `SurfaceSampleCache` contiene RGBA, máscaras, fuente efectiva e
  índices de fila/columna; el worker lo adjunta al `HorizonProfile` después de
  cargar el NPZ, para no enviar proveedores ni objetos Qt al subprocess.
- Si no existe cobertura superficial, `_terrain_surface_color()` escoge la
  paleta por distancia/noche.

La composición actual de una muestra es:

```text
color base -> _apply_terrain_light() -> _apply_terrain_atmosphere()
```

`_terrain_light_factor()` implementa Lambert con la normal de vértice, atenúa
el contraste con la distancia y opcionalmente multiplica la visibilidad solar.
Sus límites `0.84..1.10`, ambiente y fuerza directa son constantes internas.
`_apply_terrain_atmosphere()` mezcla el color iluminado con una variante del
cielo mediante `_distance_haze_factor()`, también fija y sin controles de
desaturación/contraste/brillo independientes.

La ruta de franjas crea como máximo 18 paradas en un `QLinearGradient`. Por
tanto no es interpolación 2D dentro de cada triángulo y puede mostrar planos o
variaciones incorrectas cuando el gradiente principal no es horizontal.

### 7. Proyección y pintado con QPainter

`ui/canvas_runtime_helpers.py` llama `HorizonOverlay.draw()` después del cielo,
estrellas, objetos y meteorología, y antes de brújula, marcadores, HUD y demás
overlays. La proyección escalar y NumPy llegan desde
`AstroCanvas.project_universal_stereo[_numpy]`.

`_terrain_geometry_for_view()` selecciona columnas angulares, llama la
proyección para toda la rejilla y construye franjas. `_draw_terrain_surface_2d()`
desactiva antialiasing durante el relleno para evitar costuras de cielo entre
franjas, obtiene un brush por franja y llama `QPainter.drawPolygon()`. Si las
cachés están activas, pinta primero sobre un `QImage` y reutiliza esa imagen.

El rasterizador alternativo `_rasterize_terrain_triangles()` ya produce por
píxel `triangle_id`, profundidad y dos pesos baricéntricos. La función
`_paint_terrain_triangles()` interpola iluminación y distancia con NumPy,
compone una paleta cuantizada y crea un `QImage`, con supersampling ×2. Como no
estaba conectada, todavía no aportaba calidad a la aplicación.

### 8. Procesos, workers e hilos

- **GUI / hilo principal:** proyección, geometría dependiente de cámara,
  composición de color, rasterización/pintado y overlays. No se envían objetos
  `QPainter`, `QImage` o `QColor` al subprocess.
- **`HorizonWorker` en `QThread`:** resolución de fuentes ligeras, lanzamiento
  y supervisión del subprocess, lectura de JSONL/NPZ, muestreo de cobertura y
  emisión de señales con perfiles Python/NumPy.
- **Subprocess `bake_process`:** apertura pesada del DEM, preparación de región,
  raycast, perfil, malla, normales y serialización. El límite mantiene Qt fuera
  del IPC.
- **Thread Python auxiliar:** drena `stderr` del subprocess para impedir bloqueo
  de pipes.
- **Backends internos declarados:** `HorizonBaker` reconoce `single`, `threads`
  y `processes`, pero el camino vectorizado efectivo es actualmente
  `single_vectorized`; el paralelismo de rayos no es el camino por defecto.
- **Duplicidad arquitectónica:** `TerrainCoordinator` posee su propio
  `HorizonWorker/QThread`, mientras que el bootstrap legacy crea otro. El flujo
  normal de UI hornea mediante el legacy y puentea sus resultados al
  coordinador. No debe agravarse esta duplicidad en este cambio.

## Cachés e invalidación disponibles

| Cambio | Reutilizable | Debe recalcularse |
| --- | --- | --- |
| Sólo luz | DEM, muestras, malla, normales, proyección | intensidad/color e imagen |
| Sólo atmósfera | DEM, malla, normales, proyección, Lambert | color final e imagen |
| Tamaño de ventana | DEM, malla, normales y colores de vértice | proyección, z-buffer e imagen |
| Observador | nada geométrico dependiente del perfil | raycast completo y todo lo posterior |
| Azimut/FOV | DEM, malla, normales e iluminación | selección/proyección, z-buffer e imagen |
| DEM | sólo parámetros de cámara/luz | muestreo, perfil, malla y derivados |
| Capa de color | geometría, normales, iluminación y proyección | muestras de superficie y color final |

Las cachés existentes separan razonablemente asset independiente de cámara,
geometría proyectada, raster baricéntrico, sombreado e imagen final. Las claves
deben ampliarse con la nueva configuración, sin invalidar la malla al cambiar
sólo luz o atmósfera.

## Cuellos de botella observados

1. Acceso al proveedor DEM y transformación/interpolación de grandes matrices.
2. Doble conjunto de distancias (perfil y malla), aunque el campo polar reduce
   la duplicación en el backend vectorizado.
3. Proyección completa de las coronas/columnas visibles cuando cambia cámara o
   tamaño.
4. Construcción Python de `QPolygonF` y un `drawPolygon()` por franja.
5. El rasterizador de referencia contiene bucles por píxel; Numba lo acelera si
   está instalado, pero Numba no es dependencia obligatoria y Python 3.13 puede
   no disponer del backend compatible.
6. La composición del raster llama a `QColor` una vez por combinación
   cuantizada de distancia/luz, no por píxel; aun así la creación repetida de
   paletas debe medirse.
7. El supersampling completo ×2 del prototipo triangular cuadruplica z-buffer,
   pesos y RGBA, contrario al requisito de limitar el coste al horizonte.

## Riesgos técnicos

- Una rejilla polar con distancias distintas por azimut deja de ser rectangular.
  Hay que conservar una parametrización de distancias compartida o introducir
  una triangulación zipper explícita. Forzar la unión global de todos los puntos
  refinados eliminaría el ahorro adaptativo.
- El perfil científico y la malla visual cumplen objetivos diferentes. El
  refinamiento de silueta debe aplicarse al raycast del perfil sin degradarlo,
  mientras la malla puede usar un LOD acotado.
- Las elevaciones aparentes deben usar la misma altura ocular y el mismo radio
  terrestre efectivo en producción y tests. La distancia geodésica y azimut
  WGS84 del test no se deben sustituir silenciosamente por una aproximación
  plana distinta de la usada para consultar el DEM.
- Mezclar color en sRGB oscurece más que hacerlo linealmente. Para preservar las
  coberturas existentes se mantendrá una composición sRGB controlada en esta
  iteración, con límites conservadores y pruebas de rango.
- `QPainter` no admite color arbitrario por vértice. El modo interpolado exige
  un `QImage` y una cobertura determinista; el modo `vertex` puede mantener la
  ruta de gradientes como compromiso, y `flat` debe seguir disponible.
- El antialiasing de polígonos independientes crea costuras. La máscara de
  cobertura debe derivarse del z-buffer/contorno geométrico común, antes de
  overlays, y componerse en premultiplicado para evitar halos.
- El DEM real es un recurso externo. El test Morell→Puig d'en Cama debe saltarse
  de forma explícita cuando no exista cobertura, pero fallar (y registrar todas
  las magnitudes) si el DEM está configurado y la desviación excede tolerancia.

## Estrategia concreta de implementación

### Normales e iluminación

Conservar `compute_polar_mesh_normals()` como función NumPy pura, reforzar sus
bordes y orientación mediante tests y almacenar las normales en la malla NPZ.
Introducir una configuración tipada y validada. Calcular un grid Lambert por
vértice fuera del bucle de pintado, con dirección configurable o solar, y
aplicar `ambient + diffuse_strength * diffuse` limitado por brillo mínimo y
máximo. El color base RGBA seguirá siendo la primera entrada de la composición.

### Color por vértice e interpolación

Activar `_terrain_triangles_for_view()` y el raster baricéntrico como modo
`interpolated`. Preparar color final RGBA por vértice (cobertura→luz→atmósfera),
reutilizar exactamente el mismo array mediante sus índices compartidos e
interpolar sus canales con los pesos baricéntricos. `vertex` mantendrá una
aproximación de gradientes y `flat` compondrá un color por cara sólo como
compatibilidad. El z-buffer resolverá solapamientos sin depender del orden de
triangulación.

### Perspectiva atmosférica

Crear funciones NumPy puras para factor exponencial con inicio/fin/densidad,
desaturación, reducción de contraste, ganancia de brillo y mezcla con el color
de horizonte. Las distancias por defecto se expresarán como fracción del radio
visible cuando el autoescalado esté activo. La atmósfera no tocará `valid`,
`visible`, profundidad ni conectividad.

### Muestreo adaptativo

Separar política de distancias uniformes y adaptativas. La fase base crecerá de
`near_step` a `far_step`. El refinamiento consultará puntos medios por lote y
comparará error de elevación, delta de pendiente, curvatura, elevación aparente
y cambio de envolvente visible. Tendrá profundidad, paso mínimo implícito,
máximo de muestras y tolerancias explícitas.

Para no generar agujeros se mantendrá la malla visual sobre coronas compartidas;
el refinamiento desigual se aplicará al **perfil científico por rayo** y sus
resultados se reducirán por bandas. De esta forma los rayos planos no fuerzan
sus puntos adicionales sobre rayos abruptos y la malla sigue siendo compatible.
El modo uniforme conservará exactamente la progresión actual.

### Horizonte subpíxel

El modo interpolado obtendrá cobertura del borde directamente durante la
rasterización triangular. Se aplicará supersampling sólo a una franja alrededor
del primer píxel cubierto de cada columna, o cobertura vertical analítica si el
modo configurado lo solicita. El interior conservará resolución nativa. La
composición será premultiplicada y ocurrirá dentro del `QImage` de terreno antes
de que `canvas_runtime_helpers.py` dibuje brújula, marcadores o texto.

## Instrumentación y criterio de medida

El repositorio ya escribe JSONL mediante `append_perf_event()`, pero no hay un
interruptor global y las fases solicitadas no están separadas. Se añadirá una
instrumentación opt-in que publique tiempos de muestreo base, refinamiento,
malla, normales, color, proyección/raster e imagen total, además de rayos,
muestras, vértices y triángulos. Las pruebas/benchmark sintéticos producirán
una comparación reproducible sin contaminar la salida normal; el escenario DEM
real se registrará aparte cuando el dataset configurado esté disponible.

## Orden objetivo del pipeline

```text
DEM
  -> muestras base por rayo
  -> refinamiento adaptativo
  -> perfil científico + campo polar/malla
  -> conectividad triangular
  -> normales compartidas
  -> Lambert por vértice
  -> color base y atmósfera por vértice
  -> proyección a pantalla
  -> z-buffer + interpolación baricéntrica en QImage
  -> cobertura subpíxel del horizonte
  -> QPainter.drawImage()
  -> etiquetas, marcadores y overlays
```

Este orden conserva la arquitectura 2.5D, mantiene el perfil/renderer legacy
como fallback y no introduce un motor 3D ni objetos Qt en procesos secundarios.
