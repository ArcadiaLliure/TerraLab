"""Tokens visuals compartits del llenguatge cinematogràfic de TerraLab."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TerraLabPalette:
    """Paleta semàntica inspirada en el Primer viatge."""

    void: str = "#02040a"
    chrome: str = "#050811"
    surface: str = "#080c16"
    surface_raised: str = "#0d111c"
    surface_hover: str = "#151b28"
    border: str = "#252c3b"
    border_strong: str = "#3b4559"
    gold: str = "#d8b26a"
    gold_bright: str = "#f1cd88"
    cyan: str = "#4fd8c4"
    violet: str = "#9184e0"
    text: str = "#f3f5fa"
    text_dim: str = "#aab1c2"
    text_muted: str = "#70798d"
    success: str = "#79d9b4"
    warning: str = "#e7bc72"
    error: str = "#ef8f89"


PALETTE = TerraLabPalette()


def onboarding_widget_theme() -> dict:
    """Tema pla compatible amb ``CustomWidgetBase``."""

    p = PALETTE
    return {
        "widget_background_gradient": [p.void, p.chrome],
        "widget_background": p.void,
        "widget_border_color": p.border,
        "widget_border_radius": 0,
        "title_bar_gradient": [p.chrome, p.surface],
        "title_bar_bg": p.chrome,
        "title_text_color": p.gold_bright,
        "control_button_bg": p.surface_raised,
        "control_button_border": p.border_strong,
        "control_button_hover": p.surface_hover,
        "control_button_pressed": p.gold,
        "control_button_text_color": p.text,
        "close_button_bg": p.surface_raised,
        "close_button_border": p.error,
        "close_button_hover": "#3a1e25",
        "close_button_pressed": "#5a2830",
        "close_button_text_color": p.error,
        "content_bg": p.void,
    }

