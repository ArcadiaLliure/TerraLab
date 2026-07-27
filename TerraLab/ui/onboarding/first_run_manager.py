"""Persistència i política de versions de l'onboarding."""

from __future__ import annotations

from TerraLab.common.utils import get_config_value, set_config_value

CURRENT_ONBOARDING_VERSION = 1
APPLICATION_VERSION = "0.1.0"
_MISSING = object()


class FirstRunManager:
    """Decideix quan cal mostrar el primer viatge i en desa el resultat."""

    def __init__(self, current_version: int = CURRENT_ONBOARDING_VERSION):
        self.current_version = max(1, int(current_version))

    def prepare_config(self) -> None:
        """Afegeix els camps de primer inici sense sobreescriure l'usuari."""

        if get_config_value("version", _MISSING) is _MISSING:
            set_config_value("version", APPLICATION_VERSION)
        if get_config_value("first_run", _MISSING) is _MISSING:
            set_config_value("first_run", True)
        set_config_value("onboarding_version", self.current_version)

    def should_show_on_startup(self) -> bool:
        """Retorna si l'usuari encara no ha vist la versió actual."""

        raw_version = get_config_value("last_onboarding_seen", 0)
        if isinstance(raw_version, (str, int, float)):
            try:
                last_seen = int(raw_version)
            except (TypeError, ValueError):
                last_seen = 0
        else:
            last_seen = 0
        return last_seen < self.current_version

    def mark_completed(self) -> None:
        """Registra la finalització; la versió vista s'escriu al final."""

        set_config_value("first_run", False)
        set_config_value("onboarding_version", self.current_version)
        set_config_value("last_onboarding_seen", self.current_version)
