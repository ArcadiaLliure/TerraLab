# ADR-003: Biblioteca de datos de runtime

## Status

Accepted

## Context

Los datos científicos, cachés y preferencias se resolvían desde el repositorio
o el directorio de trabajo y algunas importaciones podían crear archivos.

## Decision

`DataLibrary` define una raíz elegida por el usuario o por
`TERRALAB_DATA_ROOT`. Dentro de ella viven el catálogo de fuentes, datos
administrados, descargas, cachés, temporales y logs. El código sólo resuelve
rutas al importar; crea directorios al ejecutar una operación explícita.
Fuentes enlazadas y datos administrados conservan distinta propiedad.

## Consequences

Una instalación puede ser de sólo lectura y los tests pueden aislar el perfil
del usuario. Retirar un recurso elimina únicamente rutas administradas y
desvincula las externas.

## Supersedes

Rutas relativas al CWD, escrituras de registros durante imports y datos
científicos tratados como archivos del paquete.
