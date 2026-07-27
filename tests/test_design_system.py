from __future__ import annotations

from TerraLab.common.design_tokens import PALETTE, onboarding_widget_theme
from TerraLab.ui.design_system import GLOBAL_STYLESHEET


def _relative_luminance(hex_color: str) -> float:
    channels = [
        int(hex_color[index : index + 2], 16) / 255.0
        for index in (1, 3, 5)
    ]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    light, dark = sorted(
        (_relative_luminance(first), _relative_luminance(second)),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


def test_core_palette_has_accessible_text_contrast() -> None:
    assert _contrast(PALETTE.text, PALETTE.void) >= 7.0
    assert _contrast(PALETTE.text_dim, PALETTE.void) >= 4.5
    assert _contrast(PALETTE.gold_bright, PALETTE.void) >= 4.5
    assert _contrast(PALETTE.void, PALETTE.gold) >= 4.5


def test_global_theme_covers_the_primary_widget_families() -> None:
    for selector in (
        "QMenuBar",
        "QPushButton",
        "QLineEdit",
        "QCheckBox",
        "QSlider",
        "QTabWidget",
        "QTreeView",
        "QScrollBar",
        "QToolTip",
    ):
        assert selector in GLOBAL_STYLESHEET


def test_custom_widget_theme_uses_the_shared_tokens() -> None:
    theme = onboarding_widget_theme()

    assert theme["widget_background"] == PALETTE.void
    assert theme["title_bar_bg"] == PALETTE.chrome
    assert theme["title_text_color"] == PALETTE.gold_bright
    assert theme["content_bg"] == PALETTE.void
