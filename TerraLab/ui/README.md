# Interfície d'usuari

Composició de l'aplicació Qt.

`AstronomicalWidget` i `AstroCanvas` són les úniques classes canòniques del giny
principal i el llenç. Les seves responsabilitats s'organitzen en mixins amb noms
semàntics. Els workers de Qt viuen a `ui/workers`; el terreny se sol·licita
exclusivament a través de `TerrainCoordinator`.

La importació dels mòduls no crea un `QApplication`, arxius ni temporitzadors.
