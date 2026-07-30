# Informe d'Execució — Fase 09: Picking, Selecció i Mesures

## 1. Resum Executiu

La **Fase 09** ha separat de manera neta la resolució de hit-test/picking, la política de selecció d'aplicació i la geometria de les eines de mesura de la capa de Vista i del processador gràfic de render.

Key Accomplishments:
- **`PickIndex` Renderer-Neutral (`MODEL`):** Es produeix un índex espacial `PickIndex` immutable per a cada generació de frame, que resol queries de picking amb la precedència canònica (Cossos Celestials > NGC > Estrelles > Superfície/Cel).
- **`ApplicationInteractionController` (`CONTROLLER`):** L'estat de selecció i la gestió de peticions obsoletes (`generation < current`) viuen a la capa d'aplicació.
- **Geometria de Mesures Pura (`MODEL`):** Càlculs de distàncies, àrees, polígons i handles realitzats sense cap import de Qt o `QPainter`.
- **Adaptador QPainter de Presentació (`VIEW`):** Presentador dedicat `render/qpainter/interaction.py` que rep `SelectionPlan` i `MeasurementPlan` i els rasteritza com a overlays.
- **Routing Reversible:** Selector de pipeline `TERRALAB_PICKING_PIPELINE=scene|legacy` amb valor per defecte `scene`.

---

## 2. Mapa Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat conservada | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/scene/picking.py` | — | `MODEL` | `PickIndex` espacial i desprojecció renderer-neutral | cap API Qt, QPainter o estat | AST sense Qt/runtime; prioritat de pick provada per test d'unitat |
| `TerraLab/scene/plans/interaction.py` | — | `MODEL` | DTOs de `SelectionPlan` i `MeasurementPlan`, geometria pura | cap API Qt o presentació | AST sense Qt/runtime; distància i àrees provades |
| `TerraLab/application/interaction.py` | — | `CONTROLLER` | `ApplicationInteractionController`, estat de selecció, descarte de peticions stale i selector de pipeline | cap dibuix ni càlcul científic | provat descarte de generació antiga i routing |
| `TerraLab/application/commands.py` | `CONTROLLER` | `CONTROLLER` | Extensió de DTOs de commands (`PointerPressed`, `MeasurementCommand`, etc.) | cap | comprovat schema immutables |
| `TerraLab/render/qpainter/interaction.py` | — | `VIEW` | Rasterització QPainter de `SelectionPlan` i `MeasurementPlan` | cap càlcul de distància, àrea o decisió de selecció | consumeix només plans |
| `TerraLab/runtime/offscreen_renderer.py` | `MIXED` | `MIXED` transitori | Generació de `PickIndex` associat a frame; wiring | la ruta `scene` no calcula decisions de selecció a la vista | paritat de pick i tests funcionals |
| `TerraLab/widgets/measurement_tools.py` | `MIXED` | `MIXED` / `VIEW` façade | Interfície de compatibilitat per al widget Qt | delega geometria i plans a `MODEL` | provades eines de mesura |

---

## 3. Resultats de les Proves i Validació

1. **Unit tests de la Fase 09 (`tests/test_picking_selection_measurements.py`):** 7/7 passats.
2. **Arquitectura (`tests/architecture`):** 17/17 passats.
3. **Suite Completa (`python -m pytest -q`):** 733/733 passats en 107.32s.
4. **Ruff Check & Format:** Tots els checks i formatats d'acord amb la normativa.
5. **Pyright Baseline:** Pyright 0 errors en els fitxers de la fase 09.
