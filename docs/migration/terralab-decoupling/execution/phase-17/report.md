# Informe d'Execució — Fase 17: Transport tipat, delta frames i zero-copy

## Resum General
La Fase 17 ha completat l'optimització i endurement de la frontera de processos (IPC) entre la UI i els serveis d'execució aïllats (`render_service` i `compute_service`). S'ha implementat el transport de **frames diferencials (`SceneFrameDelta`)**, s'ha eliminat la serialització redundant a `protocol.validate()`, s'ha establert el suport per a **resync automàtic** davant reinicis o pèrdues de seqüència, i s'ha preservat íntegrament el presenter raster zero-copy sobre memòria compartida (`SharedFramePresenter` / `OwnedFramePool`).

---

## Canvis Implementats

### 1. Nucli de Contractes i Delta Frames (`MODEL`)
- **`TerraLab/scene/contracts.py`**: Definit el DTO `SceneFrameDelta` per a la representació de seccions modificades de l'estat d'escena. Afegits els mètodes `diff_from(base)` i `apply_delta(delta)` a `SceneFrame`.

### 2. Protocol IPC d'una sola passada (`OUTSIDE_MVC/RUNTIME_ADAPTER`)
- **`TerraLab/runtime/protocol.py`**:
  - Introduïdes les constants de tipus de missatge `SCENE_DELTA` i `RESYNC_REQUEST`.
  - Refactoritzat `validate(message)` per realitzar la comprovació de tipus i valors viscuts en memòria (amb `math.isfinite()`) sense cridar `json.dumps()`.
  - Garantit que `encode(message)` fa l'únic `json.dumps()` per missatge enviat.

### 3. Servei de Renderització i Mailbox (`OUTSIDE_MVC/RUNTIME_ADAPTER`)
- **`TerraLab/runtime/render_service.py`**:
  - Actualitzat `_RenderMailbox.put()` per acceptar tant `SCENE_SNAPSHOT` com `SCENE_DELTA` sota la política latest-wins.
  - Afegits mètodes `render_snapshot` i `render_delta` a `OffscreenService`. En cas de desincronització de `base_generation`, s'emet un missatge `RESYNC_REQUEST` cap a la UI.

### 4. Presentador de Frames (`VIEW`)
- **`TerraLab/ui/frame_presenter.py`**:
  - Actualitzat `submit(frame)` per emetre `SCENE_DELTA` quan es disposa de snapshot anterior, reduint significativament el payload enviat durant interaccions (pan/zoom/temps).
  - Maneig de `RESYNC_REQUEST` per retransmetre un snapshot complet atòmicament en cas de sol·licitud del worker.

---

## Taula de Delta MVC

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/scene/contracts.py` | `MODEL` | `MODEL` | DTOs immutables d'escena | afegit `SceneFrameDelta`, `diff_from`, `apply_delta` | unit tests |
| `TerraLab/runtime/protocol.py` | `OUTSIDE_MVC/RUNTIME_ADAPTER` | `OUTSIDE_MVC/RUNTIME_ADAPTER` | Validació i JSONL serialization | single-pass validation, `SCENE_DELTA`, `RESYNC_REQUEST` | unit tests, AST |
| `TerraLab/runtime/render_service.py` | `OUTSIDE_MVC/RUNTIME_ADAPTER` | `OUTSIDE_MVC/RUNTIME_ADAPTER` | Render worker offscreen i mailbox | gestió de snapshots i deltas | unit tests |
| `TerraLab/ui/frame_presenter.py` | `VIEW` | `VIEW` | Presentació raster zero-copy | transmissió de deltas i resync | presenter tests |
| `tests/test_typed_transport_and_zero_copy.py` | — | — | Proves de transport tipat, deltas i IPC zero-copy | suite completa de fase 17 | pytest |

---

## Evidència de Verificació

- **Suite de la Fase 17**: `python -m pytest -q tests/test_typed_transport_and_zero_copy.py` -> **28 passed**
- **Suite d'Arquitectura**: `python -m pytest -q tests/architecture` -> **17 passed**
- **Linter**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**

---

## Conclusió i Estat de Ledger
La Fase 17 ha assolit l'objectiu d'optimització del transport IPC, validació d'una sola passada i desincronització de frames tipats sense alterar el contracte zero-copy raster.
