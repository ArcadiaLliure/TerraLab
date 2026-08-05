"""Explicit application-owned registry for render backend selection."""

from __future__ import annotations

from collections.abc import Iterable

from TerraLab.core.rendering_contracts.contracts import (
    BackendNotFoundError,
    BackendRegistration,
    DuplicateBackendError,
    PresenterIncompatibleError,
    PresenterKind,
    RenderCapability,
    RenderTargetKind,
    RendererBackend,
)


class BackendRegistry:
    """Small injectable registry with one validated selection point."""

    def __init__(self) -> None:
        self._registrations: dict[str, BackendRegistration] = {}

    def register(self, registration: BackendRegistration) -> None:
        backend_id = str(registration.backend_id).strip().lower()
        if not backend_id:
            raise ValueError("Render backend ID cannot be empty")
        if backend_id in self._registrations:
            raise DuplicateBackendError(
                f"Render backend {backend_id!r} is already registered"
            )
        self._registrations[backend_id] = registration

    def available_backend_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))

    def registration_for(self, backend_id: str) -> BackendRegistration:
        """Return one explicit registration without constructing its adapter."""

        normalized = str(backend_id).strip().lower()
        registration = self._registrations.get(normalized)
        if registration is None:
            available = ", ".join(self.available_backend_ids()) or "none"
            raise BackendNotFoundError(
                f"Unknown render backend {backend_id!r}. "
                f"Available backends: {available}"
            )
        return registration

    def validate_selection(
        self,
        backend_id: str,
        *,
        target_kind: RenderTargetKind | None = None,
        presenter_kind: PresenterKind | None = None,
        required_capabilities: Iterable[RenderCapability] = (),
    ) -> BackendRegistration:
        """Validate a backend route without constructing its view adapter."""

        if target_kind is None:
            if presenter_kind is None:
                raise TypeError("target_kind is required")
            target_kind = presenter_kind.target_kind
        elif (
            presenter_kind is not None
            and target_kind != presenter_kind.target_kind
        ):
            raise ValueError("target_kind and presenter_kind disagree")
        normalized = str(backend_id).strip().lower()
        registration = self.registration_for(normalized)
        if target_kind not in registration.target_kinds:
            supported = ", ".join(
                kind.value
                for kind in sorted(registration.target_kinds, key=str)
            )
            compatibility_supported = ", ".join(
                kind.value
                for kind in sorted(registration.presenter_kinds, key=str)
            )
            raise PresenterIncompatibleError(
                f"Render backend {normalized!r} does not support render target "
                f"{target_kind.value!r}; supported: {supported}"
                + (
                    f" (legacy presenter names: {compatibility_supported})"
                    if compatibility_supported
                    else ""
                )
            )
        missing = frozenset(required_capabilities).difference(
            registration.capabilities
        )
        if missing:
            values = ", ".join(
                sorted(capability.value for capability in missing)
            )
            raise PresenterIncompatibleError(
                f"Render backend {normalized!r} lacks required capabilities: {values}"
            )
        return registration

    def create(
        self,
        backend_id: str,
        *,
        target_kind: RenderTargetKind | None = None,
        presenter_kind: PresenterKind | None = None,
        required_capabilities: Iterable[RenderCapability] = (),
    ) -> RendererBackend:
        registration = self.validate_selection(
            backend_id,
            target_kind=target_kind,
            presenter_kind=presenter_kind,
            required_capabilities=required_capabilities,
        )
        normalized = str(backend_id).strip().lower()
        backend = registration.factory()
        if backend.backend_id != normalized:
            raise RuntimeError(
                f"Backend factory for {normalized!r} returned "
                f"{backend.backend_id!r}"
            )
        return backend
