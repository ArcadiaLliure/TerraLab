# 📝 TerraLab: Pròximes Passes (Full de Ruta)

Aquesta llista recull les millores pendents i les noves funcionalitats previstes per al desenvolupament de **TerraLab**.

---

## 🔭 Prioritat Alta: Simulador de Telescopi

L'objectiu és simular la visió a través d'un instrument òptic real, no només una càmera.

### 1. Motor de Magnitud Visual per a Telescopis

Calcular la magnitud estel·lar assolible ($m_{lim, scope}$) segons:

* **Obertura (Aperture)** ($D$): Capacitat colectora de llum.
* **Magnificació ($M = F / f_{ep}$)**: Com més augments, més "negre" es torna el fons del cel (contrast), fins a cert límit.
* **Pila de sortida (Exit Pupil)**: Relació entre obertura i magnificació. Ha de coincidir amb la pupil·la de l'ull ($7mm$ per a joves, $\approx 5mm$ per a adults).
* **Fórmula Base**:
    $$m_{lim, scope} = m_{lim, eye} + 5 \log_{10}(D / d_{pupil}) - \text{Pèrdues per transmissió i dispersió}$$

### 2. Interfície de Configuració

* Selector de **Telescopis** (Newton, Refractor, Schmidt-Cassegrain).
* Selector d'**Oculars** (1.25", 2" i focals: 30mm, 15mm, 6mm...).
* Cercle de visió (Field Stop) amb **comas** i **aberracions cromàtiques** simulades.

---

## 🎨 Millores Visuals i Atmosfèriques

* [ ] **Objectes de Cel Profund (DSOs)**: Incorporar el catàleg Messier i NGC amb imatges realistes escalables.
* [ ] **Extinció Atmosfèrica segons Altitud**: Implementar la pèrdua de magnitud estel·lar a mesura que ens acostem a l'horitzó per la massa d'aire ($X = \sec(z)$).
* [ ] **Simulació d'Auroras i Zodíac**: Brillantors subtils segons la latitud i l'època.
* [ ] **Refracció Atmosfèrica Real**: Desplaçament vertical dels astres quan estan molt baixos, fent que el Sol "es pongui" més tard del que dicta la geometria pura.

---

## ⚙️ Refactorització Tècnica

* [ ] **Modularitat dels Kernels de LC**: Separar els filtres de contaminació lluminosa en classes independents per permetre diferents models (Gaussià, Airy, etc.).
* [ ] **Optimització de Raycasting**: Implementar ray-marching amb salts de distància (SDF) si la topografia és molt densa.
* [ ] **Traduccions Complertes**: Migrar totes les cadenes de text a `translations.json`.

---

## 📱 Portabilitat i Exportació

* [ ] **Exportador de Mapes de Qualitat de Cel**: Generar imatges GeoTIFF a partir dels càlculs d'SQM fets per TerraLab.
* [ ] **Mode Offline**: Assegurar que totes les coordenades i catàlegs estiguin ben empaquetats per a l'ús al camp sense connexió.

---

## ✅ Millores Recents (Febrer 2026)

* [x] **Unificació de Focal**: Corregida la discrepància entre el HUD i el Toast del zoom. Ara ambdós usen la fórmula de sensor de 36mm.
* [x] **Sincronització Temporal (UTC/Local)**: Solucionat el salt de 24 hores que es produïa a la 1:00 AM. Ara el motor canvia de dia UTC correctament per evitar salts en la posició de la Lluna.
* [x] **HUD de Temps Corregit**: Intercanviades les etiquetes de "Local" i "UT" que estaven invertides.
* [x] **Extinció i Refracció**: Implementat l'efecte de l'atmosfera que "apaga" estrelles baixes i les aixeca òpticament a l'horitzó.
