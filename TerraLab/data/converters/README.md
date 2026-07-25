# Conversores de datos

Conversores explícitos de productos científicos a formatos de runtime.

Las dependencias pesadas, como Astropy para FITS, se cargan al invocar la
conversión y no al importar `TerraLab`. Los resultados deben ser deterministas,
versionados y seguros de leer sin pickle.
