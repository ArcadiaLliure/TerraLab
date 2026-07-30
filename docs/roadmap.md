# Roadmap

Este documento sólo contiene trabajo pendiente. El historial está en
[`CHANGELOG.md`](../CHANGELOG.md) y las decisiones en `architecture/decisions`.

## Antes de 0.2

- Publicar una biblioteca de datos mínima de ejemplo con licencias verificadas.
- Rediseñar y reintroducir empaquetado standalone sólo cuando exista un smoke
  test de PyInstaller estable en Windows para QtWebEngine y dependencias
  científicas.
- Reducir la línea base de Pyright y sustituir capturas genéricas por
  excepciones concretas por subsistema, sin exclusiones globales.
- Completar tests de shutdown con workers y `mmap` activos.
- Convertir los smoke tests visuales de terreno en comparaciones de imagen con
  tolerancia estable entre plataformas.
- Añadir medición continua de tiempo al primer frame y pico RSS.

## Antes de 1.0

- Congelar y documentar los esquemas públicos de perfil y catálogo.
- Definir política de migración de cachés entre versiones.
- Validar accesibilidad y traducciones completas de la interfaz.
- Publicar paquetes firmados y una guía de reproducción de builds.

Las ideas de producto sin alcance, criterio de aceptación o versión objetivo
no se incorporan al roadmap.
