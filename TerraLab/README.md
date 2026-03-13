# TerraLab

TerraLab és un visualitzador de cel i terreny d'escriptori basat en Python + Qt.

## Milky Way Overlay

TerraLab integra una capa independent de Via Lactia basada en una textura PNG equirectangular amb alpha:

- Fitxer principal: `data/sky/milkyway_overlay.png`
- Resolució esperada: `3600x1800`
- Projecció: equirectangular `360° x 180°`
- Capa separada del catàleg d'estrelles

La conversió RA/Dec -> UV és:

- `u = ((ra_deg + ra_offset_deg) % 360.0) / 360.0`
- `v = 1.0 - ((dec_deg + 90.0) / 180.0)`

S'aplica:

- wrap horitzontal en `u`
- clamp vertical en `v`
- blending configurable (`normal`, `add`, `screen`)

## Render Order

Ordre de render recomanat i aplicat al pipeline modular:

1. fons/base
2. Via Lactia (`MilkyWayOverlay`)
3. estrelles
4. capes astronòmiques addicionals i overlays
5. UI

## Configuration

La capa es configura via `SceneState.extras["milkyway_overlay"]` i, per defecte, amb claus de configuració persistides:

- `milkyway_overlay_enabled` (bool, default `true`)
- `milkyway_overlay_texture_path` (string, default `data/sky/milkyway_overlay.png`)
- `milkyway_overlay_blend_mode` (`normal|add|screen`, default `add`)
- `milkyway_overlay_ra_offset_deg` (float, default `0.0`)
- `milkyway_overlay_opacity` (float, default `0.65`)
- `milkyway_overlay_sample_scale` (float, default `0.5`)

Controls opcionals per mapa de pols derivat:

- `dust_map_enabled` (bool, default `false`)
- `dust_map_path` (string, default `data/sky/derived/planck_dust_opacity_eq_u16.npz`)
- `dust_density_strength` (float, default `0.0`)
- `dust_extinction_strength` (float, default `0.0`)

## Automatic Opacity Model

L'opacitat final de la capa es calcula automàticament amb model simplificat de brillantor de cel:

- Mode Bortle (`is_auto_bortle=true`):
  - `opacity = clamp(1 - 0.18*(bortle - 2), 0, 1)`
- Mode magnitud límit manual (`is_auto_bortle=false`, fórmula literal):
  - `opacity = clamp((7.5 - mag_limit) / 3.5, 0, 1)`

Mode fotografia (actiu quan `scope_enabled=true`):

- `exposure_factor = log2(ISO/800) + log2(exposure_seconds/15) + log2((2.8/f_number)^2)`
- `opacity = clamp(opacity - 0.12*exposure_factor, 0, 1)`

`milkyway_overlay_opacity` actua com multiplicador tècnic final.

## Planck Dust Map (Build-Time)

El FITS de Planck **no es carrega en runtime** per defecte.  
Es converteix en fase de desenvolupament/build a una cache comprimida equirectangular.

Fitxer d'entrada:

- `data/sky/COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits`

Script:

- `tools/convert_planck_dust.py`

Exemple:

```bash
python tools/convert_planck_dust.py --workers 4 --zst --preview-png data/sky/derived/planck_dust_preview.png
```

Sortides:

- Cache principal: `data/sky/derived/planck_dust_opacity_eq_u16.npz`
- Opcional: `data/sky/derived/planck_dust_opacity_eq_u16.npy.zst` (si `zstandard` està disponible)
- Preview opcional: PNG en escala de grisos

## Decisions and Limitations

- La Via Lactia és una capa visual independent, no mesclada amb el catàleg d'estrelles.
- El FITS de ~403 MB es tracta offline per evitar sobrecost i dependències en runtime.
- El mapa de pols derivat és auxiliar i opcional; si falta, TerraLab arrenca igual.
- L'efecte de pols auxiliar és una modulació simple (densitat/extinció), no un model físic complet.

## Attributions

### Gaia-based Milky Way overlay
This project uses a Milky Way all-sky image derived from ESA Gaia sky-map products.  
Credit: ESA/Gaia/DPAC. Acknowledgement: A. Moitinho.  
Licence: CC BY-SA 3.0 IGO (or ESA Standard Licence, depending on the exact source asset used).  
Source reference: ESA Gaia sky map / equirectangular all-sky image products.

### Planck dust opacity map
This project may optionally use a dust-opacity all-sky map derived from Planck data:  
`COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits`  
Mission / archive credit: ESA / Planck Collaboration / Planck Legacy Archive.  
Data product: GNILC dust opacity model, all-sky HEALPix map.  
Use this dataset only as an auxiliary scientific map (e.g. density/extinction modulation), not as the main visual Milky Way texture unless explicitly documented.

Nota pràctica: l'usuari final ha de verificar que l'asset concret distribuït amb TerraLab coincideix amb la llicència i el crèdit exactes del fitxer real inclòs al repositori o al paquet de dades.

## Licences

- Gaia sky-map derived overlay: `CC BY-SA 3.0 IGO` o `ESA Standard Licence` segons l'asset concret utilitzat.
- Planck GNILC dust FITS: ús subjecte a termes de dades i atribució de la missió/col·laboració Planck.
- OpenNGC catalogue: vegeu condicions pròpies del projecte OpenNGC.

## Data sources

- ESA indica que el seu mapa del cel de Gaia és una vista all-sky de la brillantor i color de prop de `1.8` mil milions d'estrelles observades per Gaia, i ofereix també una versió equirectangular apta per a presentacions full-dome.
- Crèdit d'asset Gaia: `ESA/Gaia/DPAC; CC BY-SA 3.0 IGO. Acknowledgement: A. Moitinho.`
- Gaia és una missió de l'ESA per construir el mapa 3D més gran i precís de la Via Lactia.
- `COM_CompMap_Dust-GNILC-Model-Opacity_2048_R2.01.fits` és un producte científic de pols/opacitat de cel complet associat a l'arxiu/missió Planck i s'ha de tractar com a mapa científic auxiliar.

## Additional data credits

- MET Norway Weather API (`api.met.no`): weather forecast data under provider terms.
- Gaia Archive TAP service (`https://gea.esac.esa.int/archive/`): Gaia DR3 query service and catalogue terms apply.
- Milky Way panorama FITS (`mwpan2_RGB_3600.fits`): source by Axel Mellinger.
- Planck GNILC dust opacity FITS: ESA / Planck Collaboration / Planck Legacy Archive.
- EU-DEM mosaic (`EU_DEM_mosaic_5deg.ZIP`): Copernicus/EEA data via GISCO services.
- Light pollution raster (DVNL dataset): Harvard Dataverse DOI `10.7910/DVN/15IKI5`.
- OpenNGC catalogue (`NGC.csv`): OpenNGC by Mattia Verga and contributors.

## Gaia TAP automation

You can generate a Gaia catalog directly from ESA TAP with a magnitude limit:

```bash
python tools/download_gaia_tap.py --mag-limit 15 --yes
```

The script runs a `COUNT(*)` first, prints estimated dataset size, then downloads and builds TerraLab cache files in `%APPDATA%/TerraLab/data/gaia`.
