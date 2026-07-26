# Terreny de TerraLab: dades, càlcul i representació

Aquest document descriu el subsistema de terreny complet: com es configura la
biblioteca, com s'incorpora un DEM, com es calcula l'horitzó i el relleu
tridimensional, com se superposa una cobertura del sòl independent i per
què cada etapa està separada. La idea central és senzilla:

> El DEM decideix la geometria i les elevacions; la cobertura del sòl decideix
> l'aparença. Tots dos es troben mitjançant coordenades del món, mai per
> coincidència accidental d'índexs de píxel.

## 1. Mapa de l'arquitectura

| Responsabilitat | Implementació |
| --- | --- |
| Biblioteca triada per l'usuari | `TerraLab/common/data_library.py` |
| Catàleg tipat, selecció i migració | `TerraLab/terrain/data_sources.py` |
| Gestor unificat de capes | `TerraLab/data/layer_manager.py` |
| Importació, descàrrega i registre | `TerraLab/data/assets_manager.py` |
| Descàrrega HTTP reprenable | `TerraLab/data/resumable_download.py` |
| Inspecció de fonts | `TerraLab/terrain/source_inspection.py` |
| Proveïdors DEM i accés ràster | `TerraLab/terrain/providers.py` |
| Proveïdors de cobertura i mostreig | `TerraLab/terrain/surface.py` |
| Llegenda semàntica S2GLC | `TerraLab/terrain/land_cover/legends/s2glc.py` |
| Raycast, perfil i malla polar | `TerraLab/terrain/engine.py` |
| Procés de càlcul aïllat | `TerraLab/terrain/bake_process.py` |
| Coordinació amb la interfície | `TerraLab/terrain/worker.py` |
| Il·luminació, atmosfera i fórmules | `TerraLab/terrain/render_pipeline.py` |
| Rasterització de triangles | `TerraLab/terrain/overlay.py` |
| Diàleg avançat de fonts | `TerraLab/ui/data_layers_dialog.py` |
| Targetes del gestor de capes | `TerraLab/ui/layer_configurator.py` |
| Preparació, còpia i progrés | `TerraLab/ui/onboarding_dialogs.py` |

El catàleg no obre ràsters ni coneix Qt. Els proveïdors no coneixen la
interfície. La pintura no fa E/S de GDAL. El treball de xarxa, descompressió,
indexació i mostreig es realitza fora del fil d'interfície.

## 2. Biblioteca de dades i persistència

L'arrel de dades es tria des de TerraLab o mitjançant `TERRALAB_DATA_ROOT`. La
preferència que apunta a aquesta arrel viu al directori d'estat de la
plataforma. Les dades científiques es guarden dins de la biblioteca:

```text
<biblioteca>/
├── terralab-data.json                  # estat de recursos i d'instal·lacions
├── config/data_sources.json           # catàleg geoespacial tipat
├── data/earth/elevation/               # DEM administrats
├── data/earth/surface/                 # cobertures administrades
├── data/earth/light-pollution/
├── cache/terrain/
├── downloads/                          # ZIP complets conservats
├── downloads/.partial/                 # .part i .part.json
└── tmp/
```

Els JSON es publiquen mitjançant una escriptura temporal i una substitució
atòmica. El catàleg utilitza identificadors estables derivats del tipus i la
ruta, conserva les fonts externes sense copiar-les i desa:

- tipus semàntic, format, CRS, resolució detectada, `bounds` natius i cobertura
  WGS84;
- prioritat, habilitació, estat, procedència, atribució i nota de llicència;
- empremta de la font i metadades tècniques de cada ràster;
- selecció automàtica o manual de DEM, ortofoto, cobertura i contaminació
  lumínica;
- mode de superfície `orthophoto` o `land_cover`, amb preferències de font
  independents;
- mode de representació `profile` o `relief`.

El catàleg v3 distingeix `orthophoto_rgb`, `land_cover_rgb` i
`land_cover_categorical`. La migració reconeix S2GLC RGB com a cobertura,
ràsters antics d'una banda com a cobertura categòrica i RGB/RGBA externs sense
llegenda com a ortofoto. L'antiga capa visible «Tipus de sòl» migra a
«Superfície». Una ruta local existent no s'esborra ni es mou durant la
migració.

## 3. Configurar un DEM

El gestor accepta GeoTIFF i els formats històrics ASC/TXT/NPY, individualment o
com a mosaic d'una carpeta. Hi ha tres operacions diferents:

1. **Enllaçar un fitxer o una carpeta**: el catàleg apunta a les dades de
   l'usuari; TerraLab no s'apropia del fitxer.
2. **Copiar a la biblioteca**: es copia a un directori de preparació, es valida
   i després es publica atòmicament sota `data/earth/elevation`.
3. **Descarregar EU-DEM**: utilitza el flux comú de descàrrega i preparació.

Durant la inspecció s'obren descriptors i blocs, no pas el ràster sencer. Es
registren les bandes, el `dtype`, les dimensions, el CRS, la transformació afí,
els límits, la resolució, les dades nodata, els blocs i les piràmides. Els ASC
s'indexen pel seu rectangle i poden materialitzar una memòria cau NPY per
evitar processar el text en cada càlcul.

Si un DEM acabat d'importar conté cobertura geogràfica vàlida, TerraLab pot
centrar l'observador en la unió de les seves tessel·les i transformar aquest
centre a EPSG:4326. L'altura de l'observador és:

```text
h_ojo = h_DEM(x_observador, y_observador) + offset_usuario + 1,7 m
```

`offset_usuario` permet representar un terrat, una torre o una posició elevada.
No modifica el DEM.

### Cadena de proveïdors DEM

La selecció produeix una cadena ordenada. Primer es prova una font manual
vàlida; en mode automàtic, les fonts habilitades aplicables s'ordenen per
prioritat, resolució i estabilitat. Les dades nodata d'una font permeten
continuar amb la següent. El proveïdor GeoTIFF llegeix finestres o blocs i
utilitza una LRU limitada per bytes; el proveïdor ASC utilitza un índex espacial
i una memòria cau de tessel·les.

La interpolació d'elevació contínua és bilineal. En una cel·la amb fraccions
`u` i `v`:

```text
h(u,v) = (1-v)[(1-u)h00 + u h01] + v[(1-u)h10 + u h11]
```

Si manca qualsevol de les mostres necessàries, es conserva nodata o es recorre
a una altra font; no s'inventa cap elevació.

## 4. Sistemes de coordenades

- Posició de l'usuari i cobertura del catàleg: EPSG:4326.
- Sistema mètric actual del motor: ETRS89 / UTM 31N, EPSG:25831.
- DEM i cobertura: el seu CRS natiu, que pot ser qualsevol d'interpretable per
  Rasterio/PROJ.

`CoordinateTransformService` manté transformadors PROJ a la memòria cau i
accepta vectors NumPy. Per a un azimut veritable `a`, una convergència meridiana
`γ` i una distància horitzontal `d`, el punt del raig en el sistema del terreny
és:

```text
a_grid = a - γ
x = x_obs + d sin(a_grid)
y = y_obs + d cos(a_grid)
```

La convergència evita fer girar el relleu quan el nord de la quadrícula no
coincideix amb el nord veritable. Cada proveïdor transforma per lots `(x,y)` des
del sistema de geometria al seu propi CRS abans de consultar-ne la transformació
afí.

## 5. Radi visible i curvatura terrestre

El radi pot ser manual o automàtic. El càlcul automàtic suma la distància a
l'horitzó de l'observador i la d'una cota objectiu. Per a una altura no negativa
`h` i un radi efectiu `R`:

```text
d_horizonte(h,R) = sqrt(2 R h + h²)
```

Per defecte, `R = 6 371 000 m`. Amb la refracció atmosfèrica activada s'utilitza
el model de radi terrestre efectiu `R_eff = (7/6)R`. El resultat està limitat per
`minimum_radius_km` i `maximum_radius_km` (màxim validat: 530 km). La càrrega
immediata propera continua tenint un radi separat per no materialitzar a la
resolució màxima tot un disc continental.

En un punt del terreny amb una elevació `h_t` i una distància `d`, el descens per
curvatura i l'angle aparent són:

```text
caída(d) ≈ d² / (2 R_eff)
α = atan2(h_t - caída(d) - h_ojo, d)
```

La mateixa funció s'utilitza en el raycast i en la malla, cosa que evita
discrepàncies entre la silueta científica i el relleu pintat.

## 6. Raycast de l'horitzó

El nombre de rajos és `ceil(360 / Δazimut)`. El pas està validat entre 0,005° i
5°; el valor normal és 0,5°. Els rajos s'agrupen i consulten el DEM mitjançant
lots NumPy.

La distància de mostreig adaptatiu comença normalment en 25 m, creix fins a
400 m i pot refinar els intervals si supera algun d'aquests errors:

- error vertical estimat: 8 m;
- error projectat: 0,75 píxels;
- canvi de pendent: 4°;
- profunditat màxima: 6;
- màxim per raig: 4096 mostres.

Les distàncies també s'agrupen en bandes. Per a cada banda i azimut es conserva
el punt amb un angle aparent més gran —la silueta oclusora— i l'últim punt vàlid
—la superfície visible d'aquella profunditat—. Vuit absències consecutives
indiquen que el raig ha sortit de la cobertura; es conserva la informació ja
calculada.

El càlcul pesant viu a `bake_process.py`, un procés separat. `HorizonWorker`
l'inicia, consumeix el progrés i la previsualització, permet la cancel·lació i
lliura un `HorizonProfile` serialitzable. Així, la GUI no queda bloquejada i
GDAL no treballa dins de `paintEvent`.

## 7. Perfil i relleu tridimensional

`profile` dibuixa les bandes o siluetes de l'horitzó. `relief` afegeix una malla
2,5D composta per un pedaç cartesià local i una malla polar exterior. La malla
polar utilitza anells radials i columnes d'azimut:

- 40 m–5 km: fins a 150 anells;
- 5–25 km: fins a 100;
- 25–100 km: fins a 75;
- 100–250 km: fins a 45;
- fins al radi final: fins a 25.

Els anells són geomètrics i respecten la resolució nominal del DEM sense excedir
un pressupost limitat. La geometria utilitza l'elevació DEM real. Es calculen
normals ENU mitjançant productes vectorials de tangents radials i angulars, o
mitjançant diferències centrals en malles molt petites:

```text
dz/dx ≈ [h(x+s,y)-h(x-s,y)]/(2s)
dz/dy ≈ [h(x,y+s)-h(x,y-s)]/(2s)
n = normalize(-dz/dx, -dz/dy, 1)
```

Una cel·la vàlida produeix dos triangles. La visibilitat es determina amb el
màxim acumulat de l'angle aparent en cada azimut. La càmera projecta la malla,
el rasteritzador manté la profunditat i les coordenades baricèntriques, i els
valors per vèrtex s'interpolen dins del triangle. Això evita una cara de color
pla per triangle.

El camp proper no es tanca amb un ventall polar ni amb geometria inventada a
l'espai de pantalla. Es mostreja un pedaç ENU cartesià de `160 × 160 m`, centrat
en l'observador. Els seus eixos tenen un pas de 0,5 m al costat del nadir i
l'augmenten progressivament fins a 8 m a la vora. Aquestes mostres addicionals
són consultes o interpolacions de la superfície DEM contínua: no creen
freqüències topogràfiques que el DEM no tingui. El pedaç quadrat i la primera
corona polar de 40 m se superposen àmpliament; tots dos es projecten i es resolen
al mateix z-buffer.

Aquesta topologia evita dos defectes anteriors. Estendre la corona de 40 m fins
a la vora inferior produïa plafons verticals de color; fer convergir tots els
azimuts en un centre polar produïa un embut en mirar cap avall. A la quadrícula
cartesiana, cada cel·la té quatre veïns espacials ben definits, inclosa la que
conté la coordenada exacta de l'observador. La geometria de perfil és la versió
3; un perfil persistent amb una malla anterior s'invalida i es torna a calcular.

La il·luminació és Lambert:

```text
I = clamp(ambiente + difusa · max(dot(n,l),0), I_min, I_max)
```

Després s'aplica perspectiva atmosfèrica exponencial segons la distància, amb
dessaturació, reducció del contrast i aproximació al color de l'horitzó. El
contorn utilitza cobertura subpíxel o supermostreig configurable.

## 8. S2GLC Europe 2017

El gestor agrupa la superfície i exposa dues codificacions de la mateixa
cobertura semàntica:

| Capa | Tipus intern | Descàrrega aproximada | Font |
| --- | --- | ---: | --- |
| Cobertura del sòl — categòrica | `land_cover_categorical` | 8 GB | `S2GLC_Europe_2017_v1.2_grey.zip` |
| Cobertura del sòl — RGB | `land_cover_rgb` | 16,2 GB | `S2GLC_Europe_2017_v1.2_RGB.zip` |

Tots dos són mosaics GeoTIFF europeus amb una resolució nominal de 10 m. No
obstant això, la resolució que queda al catàleg és la detectada al fitxer
instal·lat: un GeoTIFF local alternatiu pot tenir una altra resolució.

La categòrica és l'opció recomanada: ocupa aproximadament la meitat i requereix
menys E/S, memòria i CPU. La RGB es descodifica amb la paleta S2GLC per produir
els mateixos codis de classe. Es poden instal·lar totes dues; la selecció manual
o automàtica es desa al catàleg i l'altra codificació pot cobrir zones nodata.
La preferència no canvia el mode `orthophoto`/`land_cover` de l'interruptor
principal.

### Validació

No es confia en el nom:

- una ortofoto exigeix tres bandes interpretables inequívocament com a R/G/B,
  amb alfa opcional;
- una cobertura RGB exigeix, a més, una paleta coneguda o una llegenda
  explícita;
- una cobertura categòrica exigeix exactament una banda de `dtype` enter;
- es capturen les dimensions, el CRS, la transformació afí, els `bounds`, la
  resolució X/Y, nodata, les escales, els desplaçaments, la interpretació del
  color, la taula de colors, les etiquetes, els blocs i les vistes generals;
- la font només s'habilita després que s'hagi pogut obrir i inspeccionar;
- una importació no vàlida queda catalogada, inhabilitada i diagnosticable.

### Descàrrega, pausa i represa

`ResumableDownloader` no depèn de Qt. El diàleg existent l'executa al seu worker
i mostra la fase, els bytes, el total, el percentatge, la velocitat i l'ETA. Les
fases d'instal·lació són: connectar, descarregar, verificar, extreure,
indexar/registrar i completar.

Abans d'obrir la xarxa s'exigeix espai per a:

```text
ZIP restante + GeoTIFF extraído + max(1 GiB, 10 % de ZIP+GeoTIFF)
```

La descàrrega s'escriu a `downloads/.partial/<nom>.part`. Al costat hi ha un
`.part.json` amb l'URL, la destinació, els bytes, la mida prevista, l'ETag, el
Last-Modified, l'estat i la data. Cada actualització de les metadades és atòmica.

En reprendre, s'envien `Range: bytes=N-` i `If-Range`. Només es concatena una
resposta `206` en què `Content-Range` comenci exactament per `N`. Si el servidor
ignora Range, es conserva provisionalment el parcial anterior i es reinicia la
transferència des de zero. Si canvien l'ETag, el Last-Modified o la mida, el
parcial s'aparta com a `stale` i no es barreja mai amb la versió nova. Hi ha
reintents limitats per als errors transitoris.

El ZIP es valida completament —estructura, rutes i CRC— abans d'extreure'l.
L'extracció és per blocs, cancel·lable i protegida contra les rutes que surtin
de la destinació. El resultat s'inspecciona a l'àrea de preparació i es publica
mitjançant un canvi de nom atòmic. La cancel·lació no esborra el `.part` ni el
ZIP complet. Si es cancel·la durant la verificació o l'extracció, l'execució
següent reutilitza el ZIP ja descarregat. Després d'una instal·lació vàlida,
l'usuari pot eliminar el ZIP sense afectar el GeoTIFF.

## 9. Superposició geogràfica sobre el DEM

Per a cada punt visual:

1. es parteix de la seva coordenada del món `(x,y)`, derivada de l'observador,
   la distància i l'azimut;
2. es transforma al CRS de la cobertura;
3. s'aplica la inversa de la seva transformació afí per trobar la fila i la
   columna;
4. es mostreja la cobertura a la seva pròpia quadrícula;
5. el color resultant s'associa a la malla, l'altura de la qual procedeix
   exclusivament del DEM.

La igualtat de resolució no és suficient per a un camí directe. Dues quadrícules
només estan alineades si comparteixen CRS i vectors de píxel, i si la diferència
d'origen és un nombre enter de píxels. `raster_grids_aligned` comprova aquests
criteris. Si no es compleixen, se segueix el camí general de coordenades i
transformació.

### Cobertura més precisa que el DEM (10 m sobre 30 m)

La malla científica continua sent la del DEM de 30 m.
`SurfaceSamplingService` calcula quant detall visual cap entre els anells i els
azimuts, i crea una malla visual subdividida, limitada pel pressupost. Les
elevacions i els angles nous són interpolacions de la malla DEM; no contenen
relleu topogràfic nou. La cobertura sí que es consulta a les coordenades noves,
de manera que una carretera de 10 m no queda obligatòriament reduïda a un color
per vèrtex DEM.

### Cobertura menys precisa que el DEM (10 m sobre 5 m)

Es conserva tota la geometria DEM de 5 m. L'aparença es consulta per als vèrtexs
o píxels de representació:

- RGB: veí més pròxim;
- categories: veí més pròxim, sempre.

L'elevació no es degrada per ajustar-la a la cobertura.

### La mateixa resolució (30 m sobre 30 m)

Si el CRS, la base afí i l'origen dels centres estan alineats, es pot utilitzar
la correspondència ràpida. Un desplaçament de mig píxel o un CRS diferent obliga
a transformar o remostrejar; no se suposa que `[fila,columna]` coincideixi.

## 10. Suavització sense corrompre les categories

### RGB

Els tres canals es consulten pel veí més pròxim a la quadrícula nativa; no
s'aplica interpolació bilineal RGB, tampoc al pedaç proper. El color RGBA ja
triat per a cada vèrtex es pot interpolar baricèntricament durant la
rasterització del triangle. Aquesta segona operació és ombrejat visual en
pantalla, no crea cap mostra ràster intermèdia ni altera el producte instal·lat.
La cobertura de la vora i el supermostreig redueixen les vores dentades.

### Categòrica

El codi es consulta pel veí més pròxim i es desa per separat a
`near_patch_class_ids`, `profile_class_ids`, `relief_class_ids` o
`visual_class_ids`. Un 82 i un 102 no produeixen mai un 92. Només després de
resoldre el codi es consulta la llegenda i s'obté un RGBA visual. Aquest RGBA
derivat pot participar en la rasterització, la il·luminació, la cobertura
subpíxel i la transició controlada. El codi consultable no canvia.

L'aigua i nodata tenen prioritat semàntica i no es difuminen com a valors de
classe. La variació procedimental és determinista segons el codi, la coordenada
i la llavor; no parpelleja entre fotogrames.

## 11. Registre S2GLC i extensió futura

L'única declaració de categories es troba a
`land_cover/legends/s2glc.py`. Els codis i els colors procedeixen del fitxer de
llegenda inclòs al ZIP oficial:

| Codi | Clau | Classe oficial | RGB base |
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
| 255 | `nodata` | No data | transparent |

Cada `LandCoverStyle` té un codi, una clau estable, una clau traduïble,
etiquetes, un grup, un color, un estil o una parametrització procedimental, una
prioritat de transició i espais reservats per a `material_id`, `texture_id` i
`object_generator_id`.

Per afegir una textura futura, s'assigna `texture_id` a l'estil i es registra
aquesta textura al futur resolutor de materials. Per generar arbres, roques o
edificis, s'assigna `object_generator_id`. El proveïdor continuarà retornant el
mateix codi; no cal afegir `if code == ...` al descarregador ni al renderitzador.
El pendent, l'altitud i l'orientació ja formen part de la signatura visual
perquè un material futur els pugui utilitzar sense canviar el contracte.

## 12. Rendiment i memòria

- Mai no es carrega el mosaic europeu complet.
- Rasterio obre el descriptor una vegada per proveïdor o worker.
- Les consultes són vectoritzades i agrupades per bloc. Quan el bloc natiu és
  raonable, es conserva; en canvi, els S2GLC oficials estan comprimits en
  franges d'una fila que ocupen tota l'amplada europea. En aquest cas, el
  proveïdor crea finestres virtuals locals (fins a 65.536 píxels d'amplada i
  uns 16 MiB) per amortitzar milers de crides a GDAL sense carregar Europa.
- Les finestres inclouen el marge necessari per resoldre correctament les seves
  vores sense llegir el mosaic complet.
- La memòria cau de blocs és LRU i està limitada per bytes.
- Si la màscara GDAL equival únicament a `nodata` o `all_valid`, la validesa es
  calcula a partir dels valors ja llegits i no se sol·licita el mateix bloc una
  segona vegada mitjançant `read_masks`.
- El catàleg desa les vistes generals disponibles; la lectura manté l'accés per
  finestres i permet que la política visual limiti la densitat sol·licitada.
- Els lots i les matrius temporals tenen pressupostos explícits.
- La malla visual se subdivideix com a màxim per un factor de 4 i respecta
  `max_relief_samples`.
- Els descriptors, els executors i les memòries cau es tanquen explícitament
  amb `close()`.
- A Windows s'utilitza un lector protegit; en altres sistemes hi pot haver un
  descriptor Rasterio per worker per a una lectura concurrent segura.
- L'actualització de la cobertura entra al worker mitjançant un senyal Qt
  encuat. La interfície només publica la memòria cau acabada, manté visible
  l'anterior i rep el percentatge i la fase per cada grup de finestres. Una
  selecció nova o el tancament cancel·len cooperativament entre lectures
  acotades.

Una memòria cau categòrica reprojectada haurà de conservar els enters i el veí
més pròxim. Una memòria cau visual derivada pot contenir RGBA, però mai no
substitueix el GeoTIFF categòric original.

## 13. Configuració rellevant

La interfície és el mètode recomanat. Les claus següents expliquen què es manté
de manera persistent i serveixen per al diagnòstic:

| Clau | Valor normal | Efecte |
| --- | ---: | --- |
| `observer_lat`, `observer_lon` | ubicació desada | observador WGS84 |
| `observer_offset` | 0 m | altura addicional sobre el DEM |
| `horizon_ray_step_deg` | 0,5° | precisió angular del raycast |
| `horizon_quality` | 20 | nombre històric de bandes |
| `terrain_display_radius_km` | calculat | profunditat mostrada |
| `terrain_visibility_range` | mode automàtic | límits, refracció i radi |
| `terrain.raycast_backend` | `auto` | backend del raycast |
| `ui.visibility.earth.surface` | `true` | visibilitat de la cobertura |
| `ui.visibility.relleu_tridimensional` | `true` | malla 3D o perfil |

`TerrainSamplingSettings` conté les vuit opcions adaptatives descrites a la
secció 6. `TerrainRenderSettings` conté la llum (azimut de 315°, elevació de 35°,
ambient/difusa), la brillantor, l'ombrejat `flat|vertex|interpolated`,
l'atmosfera i l'antialiàsing `coverage|supersample|off`. Tots els valors es
validen abans d'entrar al procés o al render.

## 14. Estats i diagnòstic

Una cobertura pot estar sense configurar, parcial, en descàrrega, en pausa, en
extracció, preparada, no vàlida o amb error. Si TerraLab es tanca amb un
`.part`, l'inici següent el detecta i mostra «Reprendre descàrrega». Un error
conserva el parcial vàlid.

Si una capa no apareix:

1. comproveu-ne l'estat i la ruta activa al gestor;
2. obriu el diàleg avançat i reviseu el CRS, les bandes, la resolució i l'error
   d'inspecció;
3. confirmeu que la cobertura desitjada consta com a activa;
4. comproveu que «Superfície»/cobertura i topografia siguin visibles;
5. per a nodata, reviseu l'extensió WGS84 i la taula nodata del fitxer;
6. per als desplaçaments, reviseu el CRS i la transformació afí: canviar el nom
   d'un fitxer no en corregeix la georeferenciació.

L'absència total de DEM produeix un perfil pla explícit. Aquesta alternativa pot
rebre una aparença de cobertura, però mai no s'etiqueta com a elevació real.

## 15. Crèdits, citació i condicions

Producto: **S2GLC Land Cover Map of Europe 2017**.

Autoria: **CBK PAN, Space Research Centre of the Polish Academy of Sciences**.
Projecte finançat per l'ESA.

Font oficial: <https://s2glc.cbk.waw.pl/extension>

Citació: Malinowski et al. (2020), *Automated Production of a Land Cover/Use Map
of Europe Based on Sentinel-2 Imagery*, Remote Sensing 12(21), 3523.
<https://doi.org/10.3390/rs12213523>

La font ofereix una descàrrega gratuïta. Aquest document i la interfície no
afirmen que hi hagi una llicència Creative Commons ni una autorització oberta
de redistribució, perquè la pàgina oficial no ho declara expressament. Qui
redistribueixi les dades ha de consultar les condicions del proveïdor.

El WMS GeoVille/CLC+ conservat com a punt d'extensió és un altre producte i un
altre proveïdor. No és S2GLC, no substitueix els seus ZIP i no s'utilitza per
simular una descàrrega europea S2GLC.

## 16. Proves

Els casos específics es troben a `tests/test_s2glc_download.py` i
`tests/test_s2glc_land_cover.py`. Cobreixen una descàrrega nova, Range, un
servidor que l'ignora, un ETag canviat, un reinici, una cancel·lació, un ZIP
malmès, la manca d'espai, RGB/categories en relacions 10/30, 10/5 i 30/30,
orígens desalineats, CRS, nodata, vores, classes desconegudes i metadades
detectades. Les suites generals comproven el catàleg, la UI, els proveïdors, el
worker i el render.

```powershell
python -m pytest tests/test_s2glc_download.py tests/test_s2glc_land_cover.py -q
python -m pytest -q
```
