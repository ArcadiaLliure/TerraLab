from __future__ import annotations

from pathlib import Path

from TerraLab.ui.onboarding import first_run_manager as manager_module
from TerraLab.ui.onboarding.first_run_manager import FirstRunManager


def test_first_run_manager_records_the_seen_version(monkeypatch) -> None:
    config: dict[str, object] = {}

    monkeypatch.setattr(
        manager_module,
        "get_config_value",
        lambda key, default=None: config.get(key, default),
    )
    monkeypatch.setattr(
        manager_module,
        "set_config_value",
        lambda key, value: config.__setitem__(key, value),
    )

    manager = FirstRunManager(current_version=4)
    assert manager.should_show_on_startup()

    manager.prepare_config()
    assert config["first_run"] is True
    assert config["onboarding_version"] == 4

    manager.mark_completed()
    assert config["first_run"] is False
    assert config["last_onboarding_seen"] == 4
    assert not manager.should_show_on_startup()


def test_onboarding_document_uses_qwebchannel_and_local_audio() -> None:
    html_path = (
        Path(manager_module.__file__).resolve().parent
        / "assets"
        / "onboarding.html"
    )
    html = html_path.read_text(encoding="utf-8")

    assert "qwebchannel.js" in html
    assert "onboardingBridge" in html
    assert 'src="audio/fading-away.mp3"' in html
    assert "{{" not in html
