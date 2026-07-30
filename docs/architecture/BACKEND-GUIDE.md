# How to Implement a New Renderer Backend

This guide explains how to add a new rendering backend (e.g., OpenGL, Vulkan,
WebGPU) to TerraLab.

## Prerequisites

- Familiarity with the `RendererBackend` protocol in
  `TerraLab/application/ports/rendering.py`
- Understanding of `RenderPlanBundle` in
  `TerraLab/core/rendering_contracts/plans.py`
- Conformance suite in `TerraLab/render/conformance.py`

## Step-by-Step

### 1. Create the Backend Module

Create a new directory under `TerraLab/render/<backend_name>/` with:

```
TerraLab/render/<backend_name>/
    __init__.py
    backend.py      # RendererBackend implementation
```

### 2. Implement `RendererBackend`

Your backend class must declare:

- `backend_id: str` — unique lowercase identifier (e.g., `"opengl"`)
- `capabilities: frozenset[RenderCapability]` — the set of capabilities
  your backend supports
- `target_kinds: frozenset[RenderTargetKind]` — the output target kinds

And implement these methods:

- `start(output_port: RenderOutputPort) -> None`
- `render(plan: RenderPlanBundle, target: RenderTarget) -> RenderOutput`
- `request_pick(request: PickRequest, plan: ...) -> PickResult`
- `close() -> None`

### 3. Key Rule: No Science in the Backend

Your backend receives pre-resolved data via `RenderPlanBundle`. It must
**never**:

- Calculate ephemeris, magnitudes, or projections
- Open DEM or raster files directly
- Perform atmospheric or photometric calculations
- Decide visibility, selection, or category assignments

All of these are the responsibility of the Model planners.

### 4. Register in Composition Root

Edit `TerraLab/bootstrap/composition.py`:

```python
def _my_backend_factory() -> RendererBackend:
    from TerraLab.render.my_backend.backend import MyBackend
    return MyBackend()

# Inside create_backend_registry():
registry.register(
    BackendRegistration(
        backend_id="my_backend",
        factory=_my_backend_factory,
        capabilities=frozenset({...}),
        target_kinds=frozenset({RenderTargetKind.HOSTED_SURFACE}),
    )
)
```

### 5. Pass the Conformance Suite

Run the conformance tests from `TerraLab/render/conformance.py` against
your backend. The suite validates lifecycle, frame rendering, pick handling,
and error reporting.

### 6. Update the MVC Manifest

Add your new files to `docs/architecture/mvc_file_roles.json` with
`mvc_role: VIEW` and run:

```bash
python tools/dev/generate_mvc_file_roles.py --output docs/architecture/mvc_file_roles.json
python -m pytest -q tests/architecture
```

### 7. Select Your Backend

```bash
# Environment variable
TERRALAB_RENDER_BACKEND=my_backend python -m TerraLab

# Or CLI argument
python -m TerraLab --render-backend my_backend
```

## Reference Implementations

- **QPainter**: `TerraLab/view/pyqt/backend.py` — shared raster via QImage
- **Recording**: `TerraLab/render/recording/backend.py` — headless command stream
- **Three.js**: `TerraLab/render/threejs/backend.py` — hosted surface via JS bridge
