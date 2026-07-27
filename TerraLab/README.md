# Paquet `TerraLab`

Aquest directori conté tot el codi que es distribueix com el paquet Python
`TerraLab`. Les dades científiques voluminoses, les eines de desenvolupament i
els artefactes de benchmark viuen fora del paquet.

## Mapa del paquet

| Paquet | Responsabilitat |
| --- | --- |
| `astro` | Càlcul analític, catàlegs NGC i cerca |
| `cli` | Entrades de línia de comandes |
| `common` | Rutes, configuració, memòries cau i cancel·lació |
| `data` | Biblioteca de dades, recursos i catàlegs |
| `debug` | Diagnòstic explícit d'execució |
| `layers` | Capes visuals auxiliars |
| `light_pollution` | Modes i processament de lluminositat |
| `render` | Renderitzadors del cel i context Qt |
| `scene` | Càmera, projecció i estat d'escena |
| `terrain` | Càlcul i representació del terreny |
| `ui` | Ginys d'aplicació, diàlegs i workers |
| `util` | Conversions científiques reutilitzables |
| `weather` | Model i presentació meteorològica |
| `widgets` | Controls interactius especialitzats |

## Entrades canòniques

- `python -m TerraLab` crida `TerraLab.__main__:main`.
- `ui.astronomical_widget.AstronomicalWidget` és el giny principal.
- `ui.astro_canvas.AstroCanvas` és el llenç astronòmic.
- `terrain.terrain_coordinator.TerrainCoordinator` és l'únic propietari en
  runtime del worker d'horitzó.
- `data.assets_manager.AssetManager` coordina la biblioteca de recursos.

No s'han d'introduir mòduls numerats, còpies dinàmiques de namespaces ni
implementacions paral·leles d'aquestes entrades. Els contractes són a
`tests/architecture`.
