# ADR-006: Composición explícita del backend de render

## Status

Accepted — phase 02; amended in phase 22.

## Context

`runtime/render_service.py` constructed `OffscreenSceneRenderer` directly.
The process, the shared-memory transport, and the QPainter renderer were
therefore one inseparable implementation, with no validated backend identity
or presenter compatibility check.

## Decision

The stable renderer contract now lives in
`TerraLab/core/rendering_contracts`. A backend consumes an immutable
`RenderPlanBundle` and a neutral `RenderTarget`, then returns a discriminated
`RenderOutput`. `SceneFrame` remains a Qt-free application input; planning
resolves geometry, materials, sprites, text, resources, picking and
interaction before the View adapter runs. `TerraLab/bootstrap/composition.py`
is the sole place that registers concrete backends.

The stable public backend ID is `qpainter`. It is selected with this order:

1. explicit CLI/test override;
2. `TERRALAB_RENDER_BACKEND`;
3. validated user preference;
4. `qpainter`.

The selected QPainter adapter is `TerraLab/view/pyqt/backend.py`; it alone
creates Qt paint objects. Runtime is toolkit-neutral and supplies a
`SharedRasterTarget`. Recording uses a `CommandStreamTarget`; future Three.js
uses the same family, while OpenGL and Vulkan require a hosted surface or
swapchain target. Unsupported or uninstalled IDs (`threejs`, `opengl`,
`vulkan`) fail explicitly and never fall back to QPainter.

## Consequences

Core, application and infrastructure do not import Qt or a concrete renderer.
The runtime obtains a backend only from composition and does not create a Qt
application or paint device. Compatibility imports remain outside the active
path while legacy visual parity is retired capability by capability.

## Reversal

Reverting the phase-02 change restores the direct legacy construction. No
persistent data, protocol version, or frame format is changed.
