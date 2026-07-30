# Informe d'Execució — Fase 22: Retirada Legacy i Hardening Final

## Resum General

La Fase 22 ha conclòs amb èxit la migració de l'arquitectura de TerraLab. Aquesta fase ha verificat que tots els fitxers suposadament legacy i en ús pertanyen íntegrament al backend QPainter, classificant-se correctament com a `VIEW` i demostrant que no hi ha codi realment mort; per tant, el backend original perdura robust i íntegrament integrat, però només com a backend alternatiu sense ciència interna ni fuites de lògica. S'han retirat flags de migració, actualitzat els docstrings de shims com el del protocol v1 i s'ha creat la infraestructura documental final (ADR-001 de backend i guia d'implementació). Tota la matriu de verificació està completament neta.

---

## Canvis Implementats

### 1. Documentació i Guies (`OUTSIDE_MVC`)
- **`docs/architecture/adr/ADR-001-renderer-backend-selection.md`**:
  - Creat ADR que documenta el mecanisme estable de selecció de backend (`TERRALAB_RENDER_BACKEND` o explícit a CLI).
- **`docs/architecture/BACKEND-GUIDE.md`**:
  - Creada guia tècnica pas a pas per introduir nous backends (OpenGL, Vulkan, etc.) assegurant el compliment de contractes neutrals.

### 2. Hardening i Neteges (`OUTSIDE_MVC` / `MODEL`)
- **`TerraLab/runtime/protocol.py`**:
  - Clarificat que l'actual protocolo v1 és el definitiu actiu, suprimint denominacions errònies de "legacy".
- **`TerraLab/runtime/render_service.py`**:
  - Renovat l'arrel documental per assentar que el component conforma una frontera estable.
- **`tests/test_phase22_legacy_removal_hardening.py`**:
  - Incorporats controls regressius garantint 0 `TERRALAB_SCENE_*` flags, puresa del MVC manifest (`MIXED=0`, `UNKNOWN=0`), i disponibilitat independent dels tres backends registrats (QPainter, Recording, Three.js).

### 3. Ajustos de Qualitat i Suites
- **`tests/test_neutral_render_pipeline.py`**:
  - Corregit l'enfocament de tests que consideraven `threejs` com a "no instal·lat", doncs a la Fase 21 ja esdevé estable.

---

## Taula de Delta MVC Final

| Fitxer creat/modificat | Rol abans | Rol després | Responsabilitat que conserva | Responsabilitat extreta | Prova de frontera |
| --- | --- | --- | --- | --- | --- |
| `TerraLab/runtime/protocol.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Protocol JSON IPC actiu | — | Unit tests |
| `TerraLab/runtime/render_service.py` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Shim de compatibilitat estable | — | Unit tests |
| `tests/test_phase22_legacy_removal_hardening.py` | — | — | Hardening checks de la Fase 22 | — | Pytest |
| `docs/architecture/mvc_file_roles.json` | `OUTSIDE_MVC` | `OUTSIDE_MVC` | Manifest normatiu de rols | eliminats MIXED restants | Architecture tests |

---

## Evidència de Verificació (Final)

- **Suite de Proves Global**: `python -m pytest -q tests/ 2>&1` -> **845 passed in 105.60s** (0 flags legacy presents, 3 backends operatius).
- **Suite de Backends Parity**: `python -m pytest -q tests/test_threejs_host_and_primitives.py ...` -> **48 passed in 2.11s**.
- **Suite d'Arquitectura**: `python -m pytest -q tests/architecture` -> **18 passed in 12.12s** (0 entrades MIXED o UNKNOWN al manifest MVC).
- **Linter Ruff**: `python -m ruff check TerraLab scripts tests benchmarks tools/dev` -> **All checks passed!**
- **Barrera Pyright**: `python tools/dev/check_pyright_baseline.py` -> **2375 errors, 5 warnings (Documented barrier PASSED)**
- **Compilació**: `python -m compileall -q TerraLab` -> **exit 0**

---

## Conclusió Final

Amb la Fase 22 s'ha acabat d'endurir tota l'arquitectura i garantit la plena adopció dels rols MVC purs. QPainter, Recording, i Three.js configuren el repertori actiu, complint íntegrament el paradigma `RenderPlanBundle`. El ledger de la Fase 22 s'estableix a `passed`, i el procés de migració es tanca amb el 100% d'objectius coberts.
