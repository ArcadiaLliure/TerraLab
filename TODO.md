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

---

# 🟡 Pendent immediat

- [ ] Tour onboarding explicatiu.
- [ ] Ajust proporcions panells UI.
- [ ] Validació manual regressió UI (mira, mesures, cerca).

---

# 🔭 Simulador de Telescopi

## Mode Mira Telescòpica
- [x] Overlay amb forat circular/rectangular real.
- [x] Retícula minimalista.
- [x] HUD amb FOV + mode LENT/RÀPID.
- [x] Sensibilitat angular precisa (0.5 arcmin/s lent, 0.5°/s ràpid).
- [x] Bloqueig navegació global (amb excepció `Ctrl` per navegació secundària).

## Motor de Magnitud Visual
Càlcul físic segons:
- [ ] Obertura
- [ ] Magnificació
- [ ] Pupil·la de sortida
- [ ] Pèrdues òptiques
- [ ] Extinció atmosfèrica
- [ ] Contaminació lumínica (NTL)

\[
m_{lim, scope} = m_{lim, eye} + 5 \log_{10}(D/d_{pupil}) - pèrdues
\]

- [ ] Recalcul dinàmic.
- [ ] Interdependència magnitud ↔ exposició.
- [ ] Ajust visual automàtic d’estrelles.

## Configurador d’Instrument
- [ ] Selector telescopi.
- [ ] Selector ocular.
- [x] Càlcul FOV automàtic.
- [ ] Field stop realista.
- [ ] Aberracions subtils.

---

# 🌌 Objectes de Cel Profund

## Catàleg
- [ ] Integrar OpenNGC.
- [ ] Importar tipus, AR/Dec, magnitud, mida angular, angle de posició.

## Representació Procedural
- [ ] Galàxia espiral (el·lipse + gradient + braços suaus).
- [ ] Galàxia el·líptica (difusa amb aplanament).
- [ ] Nebulosa planetària (disc/anell difús).
- [ ] Nebulosa difusa (soroll procedural + difuminat).
- [ ] Escalat segons zoom i mida angular real.
- [ ] Estètica coherent sense dependència obligatòria d’imatges externes.

---

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

# 🎨 Dibuix i Constel·lacions

- [ ] Mode dibuix amb bloqueig de càmera.
- [ ] Imantació a estrelles visibles.
- [ ] Constel·lacions com grups independents.
- [ ] Guardat JSON (nodes, ordre, nom).
- [ ] Borrador selectiu.
- [ ] Etiquetes persistents.

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

# 🎯 Objectiu Final

Construir un simulador astronòmic:

- Físicament coherent.
- Òpticament realista.
- Integrat amb topografia real.
- Basat en dades obertes.
- Modular i escalable.
