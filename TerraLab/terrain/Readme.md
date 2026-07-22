# Terreno de TerraLab: datos, cálculo y representación

Este documento describe el subsistema de terreno completo: cómo se configura la
biblioteca, cómo se incorpora un DEM, cómo se calcula el horizonte y el relieve
tridimensional, cómo se superpone una cobertura del suelo independiente y por
qué cada etapa está separada. La idea central es sencilla:

> El DEM decide la geometría y las elevaciones; la cobertura del suelo decide la
> apariencia. Ambos se encuentran mediante coordenadas del mundo, nunca por
> coincidencia accidental de índices de píxel.

## 1. Mapa de la arquitectura

| Responsabilidad | Implementación |
| --- | --- |
| Biblioteca elegida por el usuario | `TerraLab/common/data_library.py` |
| Catálogo tipado, selección y migración | `TerraLab/terrain/data_sources.py` |
| Gestor unificado de capas | `TerraLab/data/layer_manager.py` |
| Importación, descarga y registro | `TerraLab/data/assets_manager.py` |
| Descarga HTTP reanudable | `TerraLab/data/resumable_download.py` |
| Inspección de fuentes | `TerraLab/terrain/source_inspection.py` |
| Proveedores DEM y acceso ráster | `TerraLab/terrain/providers.py` |
| Proveedores de cobertura y muestreo | `TerraLab/terrain/surface.py` |
| Leyenda semántica S2GLC | `TerraLab/terrain/land_cover/legends/s2glc.py` |
| Raycast, perfil y malla polar | `TerraLab/terrain/engine.py` |
| Proceso de cálculo aislado | `TerraLab/terrain/bake_process.py` |
| Coordinación con la interfaz | `TerraLab/terrain/worker.py` |
| Iluminación, atmósfera y fórmulas | `TerraLab/terrain/render_pipeline.py` |
| Rasterización de triángulos | `TerraLab/terrain/overlay.py` |
| Diálogo avanzado de fuentes | `TerraLab/ui/data_layers_dialog.py` |
| Tarjetas del gestor de capas | `TerraLab/ui/layer_configurator.py` |
| Preparación, copia y progreso | `TerraLab/ui/onboarding_dialogs.py` |

El catálogo no abre rásteres ni conoce Qt. Los proveedores no conocen la
interfaz. La pintura no hace E/S de GDAL. El trabajo de red, descompresión,
indexación y muestreo se realiza fuera del hilo de interfaz.

## 2. Biblioteca de datos y persistencia

La raíz de datos se elige desde TerraLab o mediante `TERRALAB_DATA_ROOT`. La
preferencia que apunta a esa raíz vive en el directorio de estado de la
plataforma. Los datos científicos se guardan dentro de la biblioteca:

```text
<biblioteca>/
├── terralab-data.json                  # estado de recursos y de instalaciones
├── config/data_sources.json           # catálogo geoespacial tipado
├── data/earth/elevation/               # DEM administrados
├── data/earth/surface/                 # coberturas administradas
├── data/earth/light-pollution/
├── cache/terrain/
├── downloads/                          # ZIP completos conservados
├── downloads/.partial/                 # .part y .part.json
└── tmp/
```

Los JSON se publican mediante escritura temporal y reemplazo atómico. El
catálogo usa identificadores estables derivados del tipo y la ruta, conserva
las fuentes externas sin copiarlas y guarda:

- tipo semántico, formato, CRS, resolución detectada, `bounds` nativos y
  cobertura WGS84;
- prioridad, habilitación, salud, procedencia, atribución y nota de licencia;
- huella de la fuente y metadatos técnicos de cada ráster;
- selección automática o manual de DEM, cobertura y contaminación lumínica;
- modo de representación `profile` o `relief`.

Las entradas antiguas `surface_rgb` y `surface_categorical` se leen y se vuelven
a guardar como `land_cover_rgb` y `land_cover_categorical`. La antigua capa
visible «Tipus de sòl» migra al control compartido de cobertura. Una ruta local
existente no se borra ni se mueve durante la migración.

## 3. Configurar un DEM

El gestor acepta GeoTIFF y los formatos históricos ASC/TXT/NPY, individualmente
o como mosaico de carpeta. Hay tres operaciones distintas:

1. **Enlazar archivo/carpeta**: el catálogo apunta al dato del usuario; TerraLab
   no se apropia del archivo.
2. **Copiar a la biblioteca**: se copia a un directorio de staging, se valida y
   después se publica atómicamente bajo `data/earth/elevation`.
3. **Descargar EU-DEM**: utiliza el flujo común de descarga y preparación.

Durante la inspección se abren descriptores y bloques, no el ráster entero. Se
registran bandas, `dtype`, dimensiones, CRS, transformación afín, límites,
resolución, nodata, bloques y pirámides. Los ASC se indexan por su rectángulo y
pueden materializar una caché NPY para evitar parsear texto en cada cálculo.

Si un DEM recién importado contiene cobertura geográfica válida, TerraLab puede
centrar el observador en la unión de sus teselas y transformar ese centro a
EPSG:4326. La altura del observador es:

```text
h_ojo = h_DEM(x_observador, y_observador) + offset_usuario + 1,7 m
```

`offset_usuario` permite representar una azotea, torre o posición elevada. No
modifica el DEM.

### Cadena de proveedores DEM

La selección produce una cadena ordenada. Una fuente manual válida se prueba
primero; en automático se ordenan fuentes habilitadas aplicables por prioridad,
resolución y estabilidad. El nodata de una fuente permite continuar con la
siguiente. El proveedor GeoTIFF lee ventanas/bloques y usa una LRU limitada por
bytes; el proveedor ASC usa índice espacial y caché de teselas.

La interpolación de elevación continua es bilineal. En una celda con fracciones
`u` y `v`:

```text
h(u,v) = (1-v)[(1-u)h00 + u h01] + v[(1-u)h10 + u h11]
```

Si falta cualquiera de las muestras necesarias se conserva nodata o se recurre
a otra fuente; no se inventa una elevación.

## 4. Marcos de coordenadas

- Posición de usuario y cobertura del catálogo: EPSG:4326.
- Marco métrico actual del motor: ETRS89 / UTM 31N, EPSG:25831.
- DEM y cobertura: su CRS nativo, que puede ser cualquiera interpretable por
  Rasterio/PROJ.

`CoordinateTransformService` mantiene transformadores PROJ cacheados y acepta
vectores NumPy. Para un azimut verdadero `a`, convergencia meridiana `γ` y
distancia horizontal `d`, el punto del rayo en el marco del terreno es:

```text
a_grid = a - γ
x = x_obs + d sin(a_grid)
y = y_obs + d cos(a_grid)
```

La convergencia evita rotar el relieve cuando el norte de cuadrícula no coincide
con el norte verdadero. Cada proveedor transforma por lotes `(x,y)` desde el
marco de geometría a su propio CRS antes de consultar su transformación afín.

## 5. Radio visible y curvatura terrestre

El radio puede ser manual o automático. El cálculo automático suma la distancia
al horizonte del observador y la de una cota objetivo. Para altura no negativa
`h` y radio efectivo `R`:

```text
d_horizonte(h,R) = sqrt(2 R h + h²)
```

Por defecto `R = 6 371 000 m`. Con refracción atmosférica activada se usa el
modelo de radio terrestre efectivo `R_eff = (7/6)R`. El resultado se limita por
`minimum_radius_km` y `maximum_radius_km` (máximo validado: 530 km). La carga
inmediata cercana sigue teniendo un radio separado para no materializar a máxima
resolución todo un disco continental.

En un punto de terreno de elevación `h_t` y distancia `d`, el descenso por
curvatura y el ángulo aparente son:

```text
caída(d) ≈ d² / (2 R_eff)
α = atan2(h_t - caída(d) - h_ojo, d)
```

La misma función se usa en el raycast y en la malla, evitando discrepancias
entre la silueta científica y el relieve pintado.

## 6. Raycast del horizonte

El número de rayos es `ceil(360 / Δazimut)`. El paso está validado entre 0,005°
y 5°; el valor normal es 0,5°. Los rayos se agrupan y consultan al DEM mediante
lotes NumPy.

La distancia de muestreo adaptativa empieza normalmente en 25 m, crece hasta
400 m y puede refinar intervalos si supera alguno de estos errores:

- error vertical estimado: 8 m;
- error proyectado: 0,75 píxeles;
- cambio de pendiente: 4°;
- profundidad máxima: 6;
- máximo por rayo: 4096 muestras.

Las distancias se agrupan además en bandas. Para cada banda y azimut se conserva
el punto con mayor ángulo aparente —la silueta oclusora— y el último punto válido
—la superficie visible de esa profundidad—. Ocho faltas consecutivas indican que
el rayo salió de la cobertura; se conserva la información ya calculada.

El cálculo pesado vive en `bake_process.py`, un proceso separado. `HorizonWorker`
lo inicia, consume progreso/preview, permite cancelación y entrega un
`HorizonProfile` serializable. Así la GUI no queda bloqueada y GDAL no trabaja
dentro de `paintEvent`.

## 7. Perfil y relieve tridimensional

`profile` dibuja las bandas/siluetas del horizonte. `relief` añade una malla
2,5D compuesta por un parche cartesiano local y una malla polar exterior. La
malla polar usa anillos radiales y columnas de azimut:

- 40 m–5 km: hasta 150 anillos;
- 5–25 km: hasta 100;
- 25–100 km: hasta 75;
- 100–250 km: hasta 45;
- hasta el radio final: hasta 25.

Los anillos son geométricos y respetan la resolución nominal del DEM sin exceder
un presupuesto acotado. La geometría usa la elevación DEM real. Se calculan
normales ENU mediante productos vectoriales de tangentes radiales y angulares, o
por diferencias centrales en mallas muy pequeñas:

```text
dz/dx ≈ [h(x+s,y)-h(x-s,y)]/(2s)
dz/dy ≈ [h(x,y+s)-h(x,y-s)]/(2s)
n = normalize(-dz/dx, -dz/dy, 1)
```

Una celda válida produce dos triángulos. La visibilidad se determina con el
máximo acumulado del ángulo aparente en cada azimut. La cámara proyecta la malla,
el rasterizador mantiene profundidad y coordenadas baricéntricas, y los valores
por vértice se interpolan dentro del triángulo. Esto evita una cara de color
plano por triángulo.

El campo próximo no se cierra con un abanico polar ni con geometría inventada en
espacio de pantalla. Se muestrea un parche ENU cartesiano de `160 × 160 m`,
centrado en el observador. Sus ejes tienen paso de 0,5 m junto al nadir y lo
aumentan progresivamente hasta 8 m en el borde. Esas muestras adicionales son
consultas/interpolaciones de la superficie DEM continua: no crean frecuencias
topográficas que el DEM no posea. El parche cuadrado y la primera corona polar
de 40 m se solapan ampliamente; ambos se proyectan y resuelven en el mismo
z-buffer.

Esta topología evita dos defectos anteriores. Extender la corona de 40 m hasta
el borde inferior producía paneles verticales de color; hacer converger todos
los azimuts en un centro polar producía un embudo al mirar hacia abajo. En la
rejilla cartesiana cada celda tiene cuatro vecinos espaciales bien definidos,
incluida la que contiene la coordenada exacta del observador. La geometría de
perfil es versión 3; un perfil persistido con malla anterior se invalida y se
vuelve a calcular.

La iluminación es Lambert:

```text
I = clamp(ambiente + difusa · max(dot(n,l),0), I_min, I_max)
```

Después se aplica perspectiva atmosférica exponencial según distancia, con
desaturación, reducción de contraste y aproximación al color del horizonte. El
contorno usa cobertura subpíxel o supersampling configurable.

## 8. S2GLC Europe 2017

El gestor expone dos productos independientes:

| Capa | Tipo interno | Descarga aproximada | Fuente |
| --- | --- | ---: | --- |
| Cobertura del sòl — categòrica | `land_cover_categorical` | 8 GB | `S2GLC_Europe_2017_v1.2_grey.zip` |
| Cobertura del sòl — RGB | `land_cover_rgb` | 16,2 GB | `S2GLC_Europe_2017_v1.2_RGB.zip` |

Ambos son mosaicos GeoTIFF europeos de resolución nominal 10 m. La resolución
que queda en el catálogo es, no obstante, la detectada en el archivo instalado:
un GeoTIFF local alternativo puede tener otra resolución.

La categórica es la opción recomendada para materiales y objetos procedurales.
La RGB da una representación visual inmediata. Se pueden instalar ambas, pero
solo una cobertura está activa; la selección manual/automática se guarda en el
catálogo entre reinicios. No se componen silenciosamente.

### Validación

No se confía en el nombre:

- RGB exige tres bandas interpretables inequívocamente como R/G/B, con alfa
  opcional;
- categórica exige exactamente una banda de `dtype` entero;
- se capturan dimensiones, CRS, afín, bounds, resolución X/Y, nodata, escalas,
  offsets, interpretación de color, tabla de color, etiquetas, bloques y
  overviews;
- la fuente solo se habilita después de poder abrirse e inspeccionarse;
- una importación inválida queda catalogada, deshabilitada y diagnosticable.

### Descarga, pausa y reanudación

`ResumableDownloader` no depende de Qt. El diálogo existente lo ejecuta en su
worker y muestra fase, bytes, total, porcentaje, velocidad y ETA. Las fases de
instalación son: conectar, descargar, verificar, extraer, indexar/registrar y
completar.

Antes de abrir la red se exige espacio para:

```text
ZIP restante + GeoTIFF extraído + max(1 GiB, 10 % de ZIP+GeoTIFF)
```

La descarga se escribe en `downloads/.partial/<nombre>.part`. A su lado hay un
`.part.json` con URL, destino, bytes, tamaño esperado, ETag, Last-Modified,
estado y fecha. Cada actualización de metadatos es atómica.

Al reanudar se envía `Range: bytes=N-` e `If-Range`. Solo se concatena una
respuesta `206` cuyo `Content-Range` empiece exactamente en `N`. Si el servidor
ignora Range, se conserva provisionalmente el parcial anterior y se reinicia la
transferencia a cero. Si cambia ETag, Last-Modified o tamaño, el parcial se
aparta como `stale` y nunca se mezcla con la nueva versión. Hay reintentos
limitados para errores transitorios.

El ZIP se valida completamente —estructura, rutas y CRC— antes de extraer. La
extracción es por bloques, cancelable y protegida contra rutas que escapen del
destino. El resultado se inspecciona en staging y se publica mediante rename
atómico. Cancelar no borra el `.part` ni el ZIP completo. Si se cancela durante
verificación/extracción, la siguiente ejecución reutiliza el ZIP ya descargado.
Tras una instalación válida, el usuario puede eliminar el ZIP sin tocar el
GeoTIFF.

## 9. Superposición geográfica sobre el DEM

Para todo punto visual:

1. se parte de su coordenada del mundo `(x,y)` derivada de observador, distancia
   y azimut;
2. se transforma al CRS de la cobertura;
3. se aplica la inversa de su transformación afín para hallar fila/columna;
4. se muestrea la cobertura en su propia rejilla;
5. el color resultante se asocia a la malla cuya altura procede exclusivamente
   del DEM.

La igualdad de resolución no basta para un camino directo. Dos rejillas están
alineadas solo si comparten CRS, vectores de píxel y su diferencia de origen es
un número entero de píxeles. `raster_grids_aligned` comprueba esos criterios. Si
no se cumplen, se sigue el camino general de coordenadas y transformación.

### Cobertura más precisa que el DEM (10 m sobre 30 m)

La malla científica sigue siendo la del DEM de 30 m. `SurfaceSamplingService`
calcula cuánto detalle visual cabe entre anillos/azimuts y crea una malla visual
subdividida, limitada por presupuesto. Las elevaciones y ángulos nuevos son
interpolaciones de la malla DEM; no contienen relieve topográfico nuevo. La
cobertura sí se consulta en las nuevas coordenadas, por lo que una carretera de
10 m no queda obligatoriamente reducida a un color por vértice DEM.

### Cobertura menos precisa que el DEM (10 m sobre 5 m)

Se conserva toda la geometría DEM de 5 m. La apariencia se consulta para los
vértices/píxeles de representación:

- RGB: vecino más próximo;
- categorías: vecino más próximo, siempre.

No se degrada la elevación para ajustarla a la cobertura.

### Igual resolución (30 m sobre 30 m)

Si CRS, base afín y origen de centros están alineados se puede usar la
correspondencia rápida. Un desplazamiento de medio píxel o un CRS diferente
obliga a transformar/remuestrear; no se supone que `[fila,columna]` coincida.

## 10. Suavizado sin corromper categorías

### RGB

Los tres canales se consultan por vecino más próximo en la rejilla nativa; no se
aplica bilineal RGB, tampoco en el parche próximo. El color RGBA ya elegido para
cada vértice puede interpolarse baricéntricamente durante la rasterización del
triángulo. Esta segunda operación es sombreado visual en pantalla, no crea una
muestra ráster intermedia ni altera el producto instalado. La cobertura del
borde y el supersampling reducen dientes de sierra.

### Categórica

El código se consulta por vecino más próximo y queda guardado aparte en
`near_patch_class_ids`, `profile_class_ids`, `relief_class_ids` o
`visual_class_ids`. Un 82 y un 102 nunca producen 92. Solo después de resolver
el código se consulta la leyenda y se obtiene un RGBA visual. Ese RGBA derivado
puede participar en rasterización, iluminación, cobertura subpíxel y transición
controlada. El código consultable no cambia.

Agua/nodata tienen prioridad semántica y no se difuminan como valores de clase.
La variación procedural es determinista por código, coordenada y semilla; no
parpadea entre fotogramas.

## 11. Registro S2GLC y extensión futura

La única declaración de categorías está en
`land_cover/legends/s2glc.py`. Los códigos y colores proceden del archivo de
leyenda incluido en el ZIP oficial:

| Código | Clave | Clase oficial | RGB base |
| ---: | --- | --- | --- |
| 0 | `clouds` | Clouds | 255,255,255 |
| 62 | `artificial` | Artificial surfaces and constructions | 210,0,0 |
| 73 | `cultivated` | Cultivated areas | 253,211,39 |
| 75 | `vineyards` | Vineyards | 176,91,16 |
| 82 | `broadleaf_trees` | Broadleaf tree cover | 35,152,0 |
| 83 | `coniferous_trees` | Coniferous tree cover | 8,98,0 |
| 102 | `herbaceous` | Herbaceous vegetation | 249,150,39 |
| 103 | `moors_heathland` | Moors and heathland | 141,139,0 |
| 104 | `sclerophyllous` | Sclerophyllous vegetation | 95,53,6 |
| 105 | `marshes` | Marshes | 149,107,196 |
| 106 | `peatbogs` | Peatbogs | 77,37,106 |
| 121 | `natural_material` | Natural material surfaces | 154,154,154 |
| 123 | `permanent_snow` | Permanent snow covered surfaces | 106,255,255 |
| 162 | `water` | Water bodies | 20,69,249 |
| 255 | `nodata` | No data | transparente |

Cada `LandCoverStyle` tiene código, clave estable, clave traducible, etiquetas,
grupo, color, estilo/parametría procedural, prioridad de transición y huecos
para `material_id`, `texture_id` y `object_generator_id`.

Para añadir una textura futura se asigna `texture_id` al estilo y se registra
esa textura en el futuro resolvedor de materiales. Para generar árboles, rocas
o edificios se asigna `object_generator_id`. El proveedor continuará devolviendo
el mismo código; no hay que añadir `if code == ...` al descargador ni al
renderizador. Pendiente/altitud/orientación ya forman parte de la firma visual
para que un material futuro pueda usarlas sin cambiar el contrato.

## 12. Rendimiento y memoria

- Nunca se carga el mosaico europeo completo.
- Rasterio abre el descriptor una vez por proveedor/worker.
- Las consultas son vectorizadas y agrupadas por bloque. Cuando el bloque
  nativo es razonable se conserva; los S2GLC oficiales, en cambio, están
  comprimidos en franjas de una fila por todo el ancho europeo. En ese caso el
  proveedor crea ventanas virtuales locales (hasta 65.536 píxeles de ancho y
  unos 16 MiB) para amortizar miles de llamadas GDAL sin cargar Europa.
- Las ventanas incluyen el margen necesario para resolver correctamente sus
  bordes sin leer el mosaico completo.
- La caché de bloques es LRU y está limitada por bytes.
- Si la máscara GDAL equivale únicamente a `nodata` o `all_valid`, la validez
  se calcula desde los valores ya leídos y no se solicita el mismo bloque una
  segunda vez mediante `read_masks`.
- El catálogo guarda los overviews disponibles; la lectura mantiene el acceso
  por ventanas y permite que la política visual limite la densidad solicitada.
- Los lotes y matrices temporales tienen presupuestos explícitos.
- La malla visual se subdivide como máximo por factor 4 y respeta
  `max_relief_samples`.
- Los handles, ejecutores y cachés se cierran explícitamente con `close()`.
- En Windows se usa un lector protegido; en otros sistemas puede haber un
  handle Rasterio por worker para lectura segura concurrente.
- El refresco de cobertura entra al worker mediante una señal Qt encolada. La
  interfaz solo publica el caché terminado, mantiene visible el anterior y
  recibe porcentaje/fase por cada grupo de ventanas. Una nueva selección o el
  cierre cancelan cooperativamente entre lecturas acotadas.

Una caché reproyectada categórica deberá conservar enteros y vecino más próximo.
Una caché visual derivada puede contener RGBA, pero nunca sustituye al GeoTIFF
categórico original.

## 13. Configuración relevante

La interfaz es el método recomendado. Las claves siguientes explican lo que se
persiste y sirven para diagnóstico:

| Clave | Valor normal | Efecto |
| --- | ---: | --- |
| `observer_lat`, `observer_lon` | ubicación guardada | observador WGS84 |
| `observer_offset` | 0 m | altura adicional sobre el DEM |
| `horizon_ray_step_deg` | 0,5° | precisión angular del raycast |
| `horizon_quality` | 20 | número histórico de bandas |
| `terrain_display_radius_km` | calculado | profundidad mostrada |
| `terrain_visibility_range` | modo auto | límites, refracción y radio |
| `terrain.raycast_backend` | `auto` | backend del raycast |
| `ui.visibility.earth.surface` | `true` | visibilidad de cobertura |
| `ui.visibility.relleu_tridimensional` | `true` | malla 3D o perfil |

`TerrainSamplingSettings` contiene las ocho opciones adaptativas descritas en
la sección 6. `TerrainRenderSettings` contiene luz (azimut 315°, elevación 35°,
ambiente/difusa), brillo, sombreado `flat|vertex|interpolated`, atmósfera y
antialiasing `coverage|supersample|off`. Todos los valores se validan antes de
entrar en el proceso o el render.

## 14. Estados y diagnóstico

Una cobertura puede estar no configurada, parcial, descargando, pausada,
extrayendo, preparada, inválida o en error. Si TerraLab se cierra con un
`.part`, el siguiente inicio lo detecta y muestra «Reprendre descàrrega». Un
error conserva el parcial válido.

Si una capa no aparece:

1. comprobar su estado y ruta activa en el gestor;
2. abrir el diálogo avanzado y revisar CRS, bandas, resolución y error de
   inspección;
3. confirmar que la cobertura deseada figura como activa;
4. comprobar que «Superfície»/cobertura y topografía están visibles;
5. para nodata, revisar la extensión WGS84 y la tabla nodata del archivo;
6. para desplazamientos, revisar CRS y afín: renombrar un archivo no corrige su
   georreferenciación.

La ausencia total de DEM produce un perfil plano explícito. Ese fallback puede
recibir una apariencia de cobertura, pero nunca se etiqueta como elevación real.

## 15. Créditos, cita y condiciones

Producto: **S2GLC Land Cover Map of Europe 2017**.

Autoría: **CBK PAN, Space Research Centre of the Polish Academy of Sciences**.
Proyecto financiado por ESA.

Fuente oficial: <https://s2glc.cbk.waw.pl/extension>

Cita: Malinowski et al. (2020), *Automated Production of a Land Cover/Use Map
of Europe Based on Sentinel-2 Imagery*, Remote Sensing 12(21), 3523.
<https://doi.org/10.3390/rs12213523>

La fuente ofrece descarga gratuita. Este documento y la interfaz no afirman una
licencia Creative Commons ni una autorización abierta de redistribución porque
la página oficial no la declara expresamente. Quien redistribuya los datos debe
consultar las condiciones del proveedor.

El WMS GeoVille/CLC+ conservado como punto de extensión es otro producto y otro
proveedor. No es S2GLC, no sustituye sus ZIP y no se usa para simular una
descarga europea S2GLC.

## 16. Pruebas

Los casos específicos están en `tests/test_s2glc_download.py` y
`tests/test_s2glc_land_cover.py`. Cubren descarga nueva, Range, servidor que lo
ignora, ETag cambiado, reinicio, cancelación, ZIP corrupto, falta de espacio,
RGB/categorías en relaciones 10/30, 10/5 y 30/30, orígenes desalineados, CRS,
nodata, bordes, clases desconocidas y metadatos detectados. Las suites generales
comprueban catálogo, UI, proveedores, worker y render.

```powershell
python -m pytest tests/test_s2glc_download.py tests/test_s2glc_land_cover.py -q
python -m pytest -q
```
