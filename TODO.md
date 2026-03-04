# TerraLab: Full de Ruta

Aquest document recull l'estat actual del projecte i les properes passes, consolidat i sense duplicats.

---

# ✅ Millores Recents

## Febrer 2026
- [x] Unificació de focal (HUD i Toast amb sensor 36mm).
- [x] Correcció sincronització UTC/Local.
- [x] HUD de temps amb etiquetes correctes.
- [x] Extinció i refracció atmosfèrica implementades.

## Març 2026
- [x] Mode Mira telescòpica interactiu (recentrat, zoom, `Esc` per sortir).
- [x] `Ctrl` + drag mou càmera global en mode mira.
- [x] Modes LENT/RÀPID aplicats també al moviment secundari.
- [x] Sistema de mesures avançat (selecció, moure, redimensionar, rotar, eliminar).
- [x] Cerca amb recenter automàtic (càmera i mira) i coincidència tolerant.
- [x] Correccions de compatibilitat i encoding UI.
- [x] Motor de magnitud visual implementat amb separacio clara entre vista general i vista telescopica.
- [x] HUD de mira amb valors automatics de pupil·la de sortida, airmass, extincio, perdua i transmissio.
- [x] Botó collapse/expand reposicionat de forma desacoblada pero sincronitzada amb la geometria del panell.
- [x] Correccio de densitat estel·lar en vista general per Bortle alt (8-9) sense spillover de la mira.
- [x] Reajust del model de densitat estel·lar: mLim de vista general recalibrat (B1~7.6, B9~3.6) per evitar una retallada excessiva.
- [x] HUD de mira dividit en dos panells laterals (info principal a l'esquerra, telemetria optica/atmosferica a la dreta).
- [x] HUD de mira reanclat estrictament als costats externs de la mirilla (no posicionat a la part inferior).
- [x] Reequilibri de la corba mLim de vista general (B1~7.7, B9~4.1) per evitar infradensitat d'estrelles.
- [x] HUD de mesures i HUD de mira compactats amb mida dinamica segons text (sense caixes fixes sobredimensionades).
- [x] Refactor dels calculs fisics en metodes atomics documentats (airmass, extincio, obertura efectiva, guanys/perdues i mLim).
- [x] Compensacio de render mLim fixada a 0.0 per defecte (prioritzant realisme fisic), mantenint variable i descripcio per calibracio futura.
- [x] Unificacio de calcul fisic en un modul dedicat `physical_math.py` amb classes per responsabilitat (`AtmosphericMath`, `InstrumentOpticsMath`, `VisualPhotometryMath`).
- [x] Integracio Copernicus CDS (CAMS) per AOD + pressio atmosferica en el runtime de la mira telescopica (sense Open-Meteo).
- [x] Cache de metriques atmosferiques de la mira amb TTL ampliat i fallback local.
- [x] Flux de primer us en activar `Clima`: dialeg amb guia de registre Copernicus i entrada de l'API key a configuracio local.
- [x] En desactivar `Clima`, el calcul d'extincio de la mira forca `k_fallback` offline (sense crides de xarxa).
- [x] Integracio de clima real del widget amb MET Norway (`api.met.no`) i cache per cobertura temporal amb fallback procedural.
- [x] Documentacio de `README.md` actualitzada en catala amb atribucions oficials per a Copernicus CAMS/CDS i MET Norway.
- [x] Update de la ubicacio de clima en canvis de lat/lon (sincronitzat amb relocalitzacio).
- [x] Unificacio real de flux entre `Goto` i mira telescopica (Goto com a drecera del mateix comportament).
- [x] Gestos de mira restaurats i consistents: `Ctrl+clic+drag` mou camera, `clic+drag` mou mira.
- [x] Quan `Sol i Lluna` esta ocult, desapareix tambe el halo lunar i el seu impacte de visibilitat.
- [x] En mode mira, la resposta fotometrica de la imatge d'estrelles ara reflecteix ISO/exposicio/obertura (visibilitat + saturacio/cremat).
- [x] En mode mira, els estels febles tambe creixen lleugerament amb exposicions llargues (mida/alpha suaus, sense convertir-los en boles artificials).
- [x] El comptador `STARS` en mode mira ara nomes compta estels dins la mirilla (cercle/rectangle), no tot el viewport.

---

# PENDENT PRIORITARI

# 🔭 Simulador de Telescopi

## Mode Mira Telescòpica
- [x] Overlay amb forat circular/rectangular real.
- [x] Retícula minimalista.
- [x] HUD amb FOV + mode LENT/RÀPID.
- [x] HUD de mira amb coordenades RA/Dec del centre.
- [x] Sensibilitat angular precisa (0.5 arcmin/s lent, 0.5°/s ràpid).
- [x] Bloqueig navegació global (amb excepció `Ctrl` per navegació secundària).
- [x] Relació d'aspecte flexible per a la mira rectangular.
- [x] Evitar HUDs superposats en mode mira (toast de zoom només en mode normal).
- [x] Menú contextual amb `Goto` (clic dret) per activar mira, encarar objecte i seguir-lo.
- [x] `Goto` aplica zoom automàtic cap al destí per defecte.
- [x] En moviment manual amb `Ctrl` + arrossegament dins la mira, la càmera queda lliure però la mira manté el seguiment de l'objecte.
- [x] Sol procedural en mode mira amb taques i granulació estables durant zoom (sense regeneració visual per cada pas de zoom).
- [x] `Goto` reutilitza exactament el mateix pipeline de mira telescopica (sense via paral·lela de gestos o centrament).
- [x] Arrossegament manual de la mira desactiva el lock de reticle al target per evitar recenter forcat.

## Motor de Magnitud Visual
Càlcul físic segons:
- [x] Obertura
- [x] Magnificació
- [x] Pupil·la de sortida
- [x] Pèrdues òptiques
- [x] Extinció atmosfèrica
- [x] Contaminació lumínica (NTL)

\[
m_{lim, scope} = m_{lim, eye} + 5 \log_{10}(D/d_{pupil}) - pèrdues
\]

- [x] Recalcul dinàmic.
- [x] Interdependència magnitud ↔ exposició.
- [x] Ajust visual automàtic d’estrelles.
- [x] Entrada d'obertura dual: diàmetre (mm) o número f/ (mode càmera), amb conversió interna automàtica a obertura efectiva en mm (`D = focal / f`).
- [x] Perfil d'instrument amb `Telescopi`, `APS-C` i `Full Frame`, mostrant camps segons perfil (ocular només en telescopi) i amb càlcul de magnitud diferenciat per telescopi/càmera.
- [x] Topall de `scope_limit_mag` ampliat per evitar saturacio prematura del model (ISO/exposicio amb efecte real a la profunditat).
- [x] Render en mode mira amb guany fotometric visual: increment d'alpha/mida i bloom de saturacio ("cremat") en estrelles brillants segons parametres de captura.

## Configurador d’Instrument
- [ ] Selector telescopi.
- [ ] Selector ocular.
- [x] Càlcul FOV automàtic.
- [ ] Field stop realista.
- [ ] Aberracions subtils.


La idea és que, a part de la simulació visual (pupil·la segons condicions) que ja hem esmentat, sota del botó "Iniciar circumpolar" TAMBÉ hi ha d'haver un botó de "Iniciar sessió de fotografia" i que surti immediatament i que s'obrin les opcions típiques d'un intervalòmetre. L'usuari pot esperar la simulació per veure com progressa l'aparició d'estels i altres objectes de diferents magnituds o pot resoldre-la instantàniament. Cal tenir en compte que s'han de poder "cremar" si se n'abusa del temps, ISO o de l'exposició els estels, planetes i lluna perquè sigui realista. Com no es té els atributs de totes les càmeres, prendrem com a referència:

- APS-C -> Canon 90D (cercar paràmetres)
- Full Frame -> Sony A7S (cercar paràmetres)
- Telescopi -> Refractor, Catadiòptic, Reflector/Newtonià...(així de forma genèrica però distingint el comportament a la llum i visió que té cadascun per separat). Opció de col·locar barlows, calculant els seus efectes.

cadascun amb el seu comportament segons els paràmetres d'obertura, focal, sensibilitat ISO, temps d'exposició, etc...

D'això n'ha de sortir una "foto" que l'usuari podrà emmagatzemar (així la pot comparar) i també en el resultat el nombre de darks i flats que hauria de fer l'usuari per reduir el soroll, etc.

---

# 🎨 Dibuix i Constel·lacions

- [x] Mode dibuix amb bloqueig de càmera.
- [x] Imantació a estrelles visibles.
- [x] Constel·lacions com grups independents.
- [x] Guardat JSON (nodes, ordre, nom).
- [x] Borrador selectiu.
- [x] Etiquetes persistents.

---

# 🌌 Objectes de Cel Profund

## Catàleg
- [ ] Integrar OpenNGC.
- [ ] Importar tipus, AR/Dec, magnitud, mida angular, angle de posició.

## Representació Procedural
- [x] Definida estrategia de fallback: la representacio procedural actual (Sol, Lluna, planetes i futurs objectes) sera el mode de reserva quan hi hagi assets reals a escala.
- [ ] Integrar imatges reals a escala mantenint fallback procedural automatic.
- [ ] Galàxia espiral (el·lipse + gradient + braços suaus).
- [ ] Galàxia el·líptica (difusa amb aplanament).
- [ ] Nebulosa planetària (disc/anell difús).
- [ ] Nebulosa difusa (soroll procedural + difuminat).
- [ ] Escalat segons zoom i mida angular real.
- [ ] Estètica coherent sense dependència obligatòria d’imatges externes, però escalable perquè algun dia s'hi puguin afegir.


## Clima

- [x] El modul de la mira telescopica ja no depen d'Open-Meteo per aerosols/pressio (migrat a Copernicus CDS).
- [x] Cache de dades atmosferiques de la mira amb reutilitzacio temporal (evita peticions repetides).
- [x] Integrar clima operatiu del widget amb API noruega (`api.met.no`) + cache per cobertura de rang i fallback procedural fora de rang.
- [x] Integracio del fetch de clima en un procés separat (worker de `ProcessPoolExecutor`) per no bloquejar render/UI.
- [x] Reduccio de la sobreexposicio de nuvols de proves (`DEBUG_CLOUD_DENSITY` -> 1.0) i ajust de spawn.
- [x] Ajust de pluja/neu per emissio mes realista i limit de particules en pantalla.
- [x] Flags de runtime per activar/desactivar clima remot i cache (`weather_use_remote_metno`, `weather_cache_enabled`) persistits a configuracio.
- [ ] Assegurar-se que: 
    - [x] Es fa una sola consulta per rang en una crida (`api.met.no` compact) i es reutilitza cache mentre el rang cobreix el dia seleccionat i no expira TTL.
    - [ ] Es fa una consulta del temps dels propers 14 dies (si és possible), així l'usuari pot navegar per data. -> Actualment limitat al rang que publica `api.met.no` en `locationforecast` (normalment ~10 dies).
    - [x] Si el dia seleccionat queda fora del rang remot, no es fa crida i s'aplica fallback procedural local.
    - [x] Cal ser flexibles amb les localitzacions, els diagnòstics del temps tenen certa amplitud.

---

# 🌠 Efemèrides i Alertes

## Sistema Automàtic
- [ ] Conjuncions < 1°.
- [ ] Lluna + cúmuls.
- [ ] Ocultacions.
- [ ] Marcadors visuals al cel.
- [ ] Anticipació configurable.

## Alertes Programables
- [ ] Configuració per tipus d’esdeveniment.
- [ ] Llindars angulars.
- [ ] Historial.
- [ ] Activació/desactivació categories.


---

# 📱 Exportació

## Geo
- [ ] GeoTIFF qualitat del cel.
- [ ] Projecció coherent.

## Visual
- [ ] Export imatge FOV telescopi.
- [ ] Resolució configurable.
- [ ] Opció amb o sense HUD.

## Sessions
- [ ] Guardar sessió.
- [ ] Carregar sessió.
- [ ] Export complet configuració.

---

# ⚙️ Arquitectura i Portabilitat

- [ ] Modularitzar kernels de contaminació lumínica.
- [ ] Optimitzar raycasting/ray-marching.
- [ ] Migrar literals a `translations.json`.
- [ ] Mode offline robust amb catàlegs empaquetats.

---

# 🟡 Pendent NO URGENT

## PENDENT NO URGENT

# 🏔️ Topografia Real i Cims

## Horizon Real
- [ ] Opció “Color real del terra” (GeoTIFF RGB).
- [ ] Overlay DEM real.
- [ ] Mode procedural alternatiu per rendiment.

## Validació de Pics
- [ ] Comparació DEM vs base de dades externa.
- [ ] Llindars de distància i diferència d’altitud.
- [ ] Tooltip amb nom, altitud i distància.
- [ ] Ajust manual possible.
- [ ] Vectorització eficient (NumPy).

## Altura Usuari
- [x] Camp per sumar metres addicionals.
- [x] Mostrar altitud base automàtica.

- [ ] Tour onboarding explicatiu. -> Pendent encara a la UI
- [x] Ajust proporcions panells UI.
- [ ] Validació manual regressió UI (mira, mesures, cerca).

# 🎯 Objectiu Final

Construir un simulador astronòmic:

- Físicament coherent.
- Òpticament realista.
- Integrat amb topografia real.
- Basat en dades obertes.
- Modular i escalable.
