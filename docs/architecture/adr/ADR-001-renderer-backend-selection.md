# ADR-001: Renderer Backend Selection

## Status

**Accepted** — Phase 22 (final hardening)

## Context

TerraLab's graphics rendering was originally hard-wired to QPainter via
`OffscreenSceneRenderer`. The decoupling migration (Phases 01–21) introduced
a neutral `RendererBackend` protocol, a `BackendRegistry`, and a composition
root that selects one backend at startup.

The system must support multiple rendering backends (QPainter, Three.js,
future OpenGL/Vulkan) without altering Model or scientific code.

## Decision

### Selection Mechanism

The backend is resolved with the following precedence:

1. **Explicit test/CLI override** (`explicit_backend` parameter)
2. **Environment variable** `TERRALAB_RENDER_BACKEND`
3. **User preference** (stored settings)
4. **Default**: `qpainter`

### Registry & Validation

- `BackendRegistry` holds factories, capabilities, and target kinds.
- `build_render_backend()` in `bootstrap/composition.py` is the sole
  composition root.
- An unknown backend ID raises `BackendNotFoundError` with an actionable
  message listing available backends.
- A capability mismatch raises `PresenterIncompatibleError`.

### Stable Backends

| Backend ID | Target Kind       | Status  |
| ---------- | ----------------- | ------- |
| `qpainter` | `shared_raster`   | Default |
| `recording`| `command_stream`  | Stable  |
| `threejs`  | `hosted_surface`  | Stable  |

### Constraints

- No implicit fallback to QPainter when a requested backend is missing.
- The Model never imports or references any backend.
- Backends consume pre-resolved `RenderPlanBundle` data; they do not
  perform scientific calculations.

## Consequences

- Adding a new backend requires implementing `RendererBackend`, registering
  it in `composition.py`, and passing the conformance suite.
- Changing backends between runs requires only `TERRALAB_RENDER_BACKEND=<id>`
  or the CLI argument — no code changes to Model files.
- The conformance suite (`render/conformance.py`) validates lifecycle,
  frame submission, and pick handling for any backend.
