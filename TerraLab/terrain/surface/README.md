# Superficie

Muestreo de materiales georreferenciados independiente del DEM.

Soporta ortofoto RGB, cobertura RGB y cobertura categórica, además de
contaminación lumínica. `service.py` prepara cachés alineadas con el perfil o
la malla; el render recibe arrays inmutables y no realiza E/S de GDAL.

Desactivar una superficie limpia tanto la selección semántica como la imagen
cacheada, evitando tooltips sin representación visible.
