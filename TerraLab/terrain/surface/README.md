# Superfície

Mostreig de materials georeferenciats independent del DEM.

Admet ortofoto RGB, cobertura RGB i cobertura categòrica, a més de contaminació
lumínica. `service.py` prepara memòries cau alineades amb el perfil o la malla;
el render rep matrius immutables i no efectua E/S de GDAL.

Desactivar una superfície neteja tant la selecció semàntica com la imatge
emmagatzemada a la memòria cau, cosa que evita indicadors de funció sense
representació visible.
