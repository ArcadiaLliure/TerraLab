"""
Classe base per a tots els widgets personalitzats de l'escriptori.
Proporciona funcionalitats comunes com arrossegar, redimensionar i controls
de finestra.

Aquesta implementació incorpora suport de tematització. Els colors i estils
de la interfície es deriven del tema actual emmagatzemat a
`self.current_theme`. Aquest tema es pot configurar amb el mètode
`set_theme()`, que accepta tant formats simplificats com temes complets de
Studio Ghibli (amb seccions `colors`, `gradients` i `effects`).

El mètode `apply_styles()` genera dinàmicament el full d'estils QSS segons
el tema actiu.
"""

from PyQt5.QtCore import QEvent, QPoint, Qt, pyqtSignal
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .utils import get_config_value


# =============================================================================
# Utilitats de color i contrast
# =============================================================================

def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Converteix un color en format '#RRGGBB' a una tupla (r, g, b)."""
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _rgb_to_hex(red: int, green: int, blue: int) -> str:
    """Converteix components RGB a un color hexadecimal '#rrggbb'."""
    return f"#{red:02x}{green:02x}{blue:02x}"


def lighten_color(color: str, factor: float = 0.1) -> str:
    """
    Aclareix un color barrejant-lo amb blanc.

    El factor ha d'estar entre 0 i 1. Un factor de 0.0 no canvia el color,
    i 1.0 el converteix en blanc.
    """
    red, green, blue = _hex_to_rgb(color)
    return _rgb_to_hex(
        min(255, int(red   + (255 - red)   * factor)),
        min(255, int(green + (255 - green) * factor)),
        min(255, int(blue  + (255 - blue)  * factor)),
    )


def darken_color(color: str, factor: float = 0.1) -> str:
    """
    Enfosqueix un color multiplicant cada component per (1 - factor).

    El factor ha d'estar entre 0 i 1. Un factor de 0.0 no canvia el color.
    """
    red, green, blue = _hex_to_rgb(color)
    return _rgb_to_hex(
        max(0, int(red   * (1 - factor))),
        max(0, int(green * (1 - factor))),
        max(0, int(blue  * (1 - factor))),
    )


def get_contrast_color(color: str) -> str:
    """
    Determina un color de text (negre o blanc) que contrasti amb el fons.

    Es calcula la lluminositat perceptiva del color per escollir el més llegible.
    """
    red, green, blue = _hex_to_rgb(color)
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    return "#000000" if luminance > 128 else "#FFFFFF"


# =============================================================================
# Servei de tema i refresc de widgets
# =============================================================================

class ThemeLifecycleService:
    """Servei de suport per a operacions de tema i refresc de widgets."""

    @staticmethod
    def update_all_widgets_theme(widget_instances, theme_dict: dict):
        """Aplica el tema nou a totes les instàncies de widget registrades."""
        for widget_instance in list(widget_instances):
            try:
                widget_instance.set_theme(theme_dict)
            except Exception:
                continue

    @staticmethod
    def refresh_widget(widget_instance, deep: bool = True, reapply_theme: bool = True):
        """
        Refresca la interfície d'un widget.

        Paràmetres:
            widget_instance: El widget a refrescar.
            deep: Si és True, reconstrueix el contingut cridant setup_content().
            reapply_theme: Si és True, torna a aplicar el full d'estils.
        """
        if reapply_theme:
            try:
                widget_instance.apply_styles()
            except Exception:
                pass

        retranslate = getattr(widget_instance, "retranslate_ui", None)
        if callable(retranslate):
            try:
                retranslate(deep=deep)
            except Exception:
                pass

        if deep:
            try:
                while widget_instance.content_layout.count():
                    item = widget_instance.content_layout.takeAt(0)
                    child = item.widget()
                    if child is not None:
                        child.setParent(None)
                widget_instance.setup_content()
            except Exception:
                pass

        try:
            widget_instance.updateGeometry()
            widget_instance.repaint()
        except Exception:
            pass


# =============================================================================
# Servei d'interacció de finestra
# =============================================================================

class WindowInteractionService:
    """Servei de suport per al càlcul de vores i cursor de redimensionament."""

    @staticmethod
    def check_resize_area(widget_instance, position, resize_margin: int) -> int:
        """
        Calcula quines vores toca el cursor i retorna una màscara de bits.

        Bits: 1=esquerra, 2=dalt, 4=dreta, 8=baix.
        """
        resize_edges = 0
        w, h = widget_instance.width(), widget_instance.height()
        if position.x() < resize_margin:          resize_edges |= 1
        if position.x() > w - resize_margin:      resize_edges |= 4
        if position.y() < resize_margin:          resize_edges |= 2
        if position.y() > h - resize_margin:      resize_edges |= 8
        return resize_edges

    @staticmethod
    def update_cursor(widget_instance, resize_edges: int):
        """Assigna el cursor corresponent a la combinació de vores activa."""
        cursors = {
            0: Qt.ArrowCursor,
            1: Qt.SizeHorCursor,
            4: Qt.SizeHorCursor,
            2: Qt.SizeVerCursor,
            8: Qt.SizeVerCursor,
            3: Qt.SizeFDiagCursor,
            12: Qt.SizeFDiagCursor,
            6: Qt.SizeBDiagCursor,
            9: Qt.SizeBDiagCursor,
        }
        widget_instance.setCursor(cursors.get(resize_edges, Qt.ArrowCursor))


# =============================================================================
# Servei d'estat i persistència de finestra
# =============================================================================

class WindowStateService:
    """
    Servei de suport per a l'estat de finestra i la seva persistència.

    Centralitza minimització, restauració, maximització, tancament i
    serialització de la geometria/estat visual del widget.
    """

    @staticmethod
    def minimize_widget(widget_instance):
        """Oculta el widget i emet el senyal widget_minimized."""
        widget_instance.hide()
        widget_instance.widget_minimized.emit(widget_instance)

    @staticmethod
    def restore_widget(widget_instance):
        """Restaura el widget a la mida i posició anteriors o per defecte."""
        if widget_instance.is_maximized:
            if widget_instance.old_geometry:
                widget_instance.setGeometry(widget_instance.old_geometry)
            else:
                widget_instance.resize(*widget_instance.default_size)
                widget_instance.move(*widget_instance.default_position)
            widget_instance.is_maximized = False
            widget_instance.widget_restored.emit(widget_instance)
            return

        widget_instance.resize(*widget_instance.default_size)
        widget_instance.move(*widget_instance.default_position)

    @staticmethod
    def maximize_widget(widget_instance):
        """Maximitza el widget dins l'àrea disponible de la pantalla activa."""
        if widget_instance.is_maximized:
            return

        widget_instance.old_geometry = widget_instance.geometry()

        app = QApplication.instance()
        screen = (
            app.screenAt(widget_instance.frameGeometry().center())
            if app and hasattr(app, "screenAt") else None
        ) or QApplication.primaryScreen()

        target = (
            screen.availableGeometry()
            if screen
            else QApplication.desktop().availableGeometry(widget_instance)
        )

        widget_instance.setGeometry(target)
        widget_instance.is_maximized = True
        widget_instance.widget_maximized.emit(widget_instance)

    @staticmethod
    def close_widget(widget_instance):
        """Emet el senyal widget_closed i tanca el widget."""
        widget_instance.widget_closed.emit(widget_instance)
        widget_instance.close()

    @staticmethod
    def get_state(widget_instance) -> dict:
        """Serialitza la geometria i l'estat visual del widget."""
        return {
            "title":        widget_instance.title,
            "x":            widget_instance.x(),
            "y":            widget_instance.y(),
            "width":        widget_instance.width(),
            "height":       widget_instance.height(),
            "is_maximized": widget_instance.is_maximized,
            "visible":      widget_instance.isVisible(),
        }

    @staticmethod
    def set_state(widget_instance, state: dict):
        """Restaura la geometria i l'estat visual del widget des d'un dict."""
        if "x" in state and "y" in state:
            widget_instance.move(state["x"], state["y"])
        if "width" in state and "height" in state:
            widget_instance.resize(state["width"], state["height"])
        if state.get("is_maximized"):
            widget_instance.maximize_widget()
        if "visible" in state:
            widget_instance.setVisible(state["visible"])


# =============================================================================
# Classe base de widget personalitzat
# =============================================================================

# Tema per defecte aplicat quan no hi ha cap tema desat.
_DEFAULT_THEME_COLORS = {
    "error_color":    "#c8553d",
    "control_bg":     "#948465",
    "secondary":      "#634311",
    "surface":        "#f1dfbe",
}


def _build_default_theme() -> dict:
    """
    Construeix el diccionari de tema per defecte a partir dels colors base.

    Centralitza la definició del tema inicial perquè sigui fàcil de canviar
    sense tocar __init__.
    """
    c = _DEFAULT_THEME_COLORS
    error  = c["error_color"]
    ctrl   = c["control_bg"]
    sec    = c["secondary"]
    surf   = c["surface"]
    return {
        "widget_background_gradient": [surf, ctrl],
        "widget_background":          surf,
        "widget_border_color":        sec,
        "widget_border_radius":       10,
        "title_bar_gradient":         ["#a3a85e", "#a3a85e"],
        "title_bar_bg":               "#a3a85e",
        "title_text_color":           "#000000",
        "control_button_bg":          ctrl,
        "control_button_border":      sec,
        "control_button_hover":       sec,
        "control_button_pressed":     sec,
        "control_button_text_color":  get_contrast_color(ctrl),
        "close_button_bg":            error,
        "close_button_border":        darken_color(error, 0.15),
        "close_button_hover":         lighten_color(error, 0.15),
        "close_button_pressed":       darken_color(error, 0.15),
        "close_button_text_color":    get_contrast_color(error),
        "content_bg":                 surf,
    }


class CustomWidgetBase(QWidget):
    """
    Classe base per a widgets personalitzats de l'escriptori.

    Proporciona funcionalitat de finestra sense marc amb controls personalitzats,
    arrossegament, redimensionament i suport de tematització.
    """

    widget_minimized = pyqtSignal(object)
    widget_maximized = pyqtSignal(object)
    widget_restored  = pyqtSignal(object)
    widget_closed    = pyqtSignal(object)

    _instances = []
    _RESIZE_MARGIN = 8

    def __init__(self, title: str = "Widget", parent=None, frameless=True):
        super().__init__(parent)

        self._init_state(title, frameless)
        self._init_flags(frameless)
        self.current_theme = _build_default_theme()

        self.setup_ui()
        self.apply_styles()

        CustomWidgetBase._instances.append(self)
        self._load_saved_theme()

    # -------------------------------------------------------------------------
    # Inicialització (extreta de __init__ per claredat)
    # -------------------------------------------------------------------------

    def _init_state(self, title: str, frameless: bool):
        """
        Inicialitza els atributs d'estat intern del widget.

        Paràmetres:
            title: Títol que es mostrarà a la barra de títol.
            frameless: Si és True, el widget no té marc del sistema operatiu.
        """
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setMouseTracking(True)

        self.title        = title
        self.is_frameless = frameless
        self.is_maximized = False
        self.old_pos      = QPoint()
        self.old_geometry = None

        self._resizing        = False
        self._resize_drag_pos = QPoint()
        self._resize_edges    = 0

        self.default_size     = (400, 300)
        self.default_position = (100, 100)
        self.resize(*self.default_size)
        self.move(*self.default_position)

    def _init_flags(self, frameless: bool):
        """
        Configura els flags de finestra de Qt.

        Paràmetres:
            frameless: Si és True, activa FramelessWindowHint i WindowStaysOnTopHint.
        """
        if frameless:
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

    def _load_saved_theme(self):
        """
        Intenta carregar i aplicar el tema desat a la configuració.

        Si no hi ha tema desat o es produeix qualsevol error, es manté
        el tema per defecte sense llançar cap excepció.
        """
        try:
            saved_theme = get_config_value("theme", None)
            if not saved_theme:
                return
            from theme.theme_service import ThemeManager
            theme_dict = ThemeManager().get_theme(saved_theme)
            if theme_dict:
                self.set_theme(theme_dict)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Construcció d'interfície
    # -------------------------------------------------------------------------

    def setup_ui(self):
        """Configura l'estructura principal de la interfície del widget."""
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        if self.is_frameless:
            self.create_title_bar()
            self.main_layout.addWidget(self.title_bar)

        self.content_frame = QFrame()
        self.content_frame.setObjectName("contentFrame")
        self.content_layout = QVBoxLayout(self.content_frame)
        self.content_layout.setContentsMargins(10, 10, 10, 10)
        self.content_frame.setMouseTracking(True)
        self.content_frame.installEventFilter(self)

        self.main_layout.addWidget(self.content_frame, 1)
        self.setup_content()

    def create_title_bar(self):
        """Crea la barra de títol personalitzada amb botons de control."""
        self.title_bar = QFrame()
        self.title_bar.setObjectName("titleBar")
        self.title_bar.setFixedHeight(30)

        layout = QHBoxLayout(self.title_bar)
        layout.setContentsMargins(10, 5, 5, 5)

        self.title_label = QLabel(self.title)
        self.title_label.setObjectName("titleLabel")
        layout.addWidget(self.title_label)
        layout.addStretch()

        self.minimize_btn = self._make_control_button("−", self.minimize_widget)
        self.restore_btn  = self._make_control_button("□", self.restore_widget)
        self.maximize_btn = self._make_control_button("□", self.maximize_widget)

        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("closeButton")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.clicked.connect(self.close_widget)

        for btn in (self.minimize_btn, self.restore_btn, self.maximize_btn, self.close_btn):
            layout.addWidget(btn)

    def _make_control_button(self, label: str, callback) -> QPushButton:
        """
        Crea un botó de control estàndard per a la barra de títol.

        Paràmetres:
            label: Text o símbol que mostrarà el botó.
            callback: Funció que es cridarà quan es faci clic.

        Retorna:
            Un QPushButton configurat i connectat.
        """
        btn = QPushButton(label)
        btn.setObjectName("controlButton")
        btn.setFixedSize(20, 20)
        btn.clicked.connect(callback)
        return btn

    def setup_content(self):
        """
        Mètode pensat per ser sobreescrit per les subclasses.

        Aquí cada subclasse ha de configurar el contingut específic del widget.
        """
        pass

    # -------------------------------------------------------------------------
    # Tematització — punt d'entrada públic
    # -------------------------------------------------------------------------

    def set_theme(self, theme_dict: dict):
        """
        Estableix un tema de la interfície del widget.

        Accepta dos formats:
        - Tema complet (Studio Ghibli): dict amb claus 'colors', 'gradients', 'effects'.
        - Tema simplificat: dict pla amb les claus internes del widget.

        Paràmetres:
            theme_dict: Diccionari que descriu el tema a aplicar.
        """
        self._apply_color_scheme(theme_dict)
        self._apply_font_settings(theme_dict)

    # -------------------------------------------------------------------------
    # Tematització — generació de QSS per seccions
    # -------------------------------------------------------------------------

    def apply_styles(self):
        """Aplica els estils CSS generant el QSS des del tema actiu."""
        sections = [
            self._style_widget(),
            self._style_title_bar(),
            self._style_control_buttons(),
            self._style_close_button(),
            self._style_content_frame(),
        ]
        self.setStyleSheet("\n".join(sections))

    def _style_widget(self) -> str:
        """
        Genera el bloc QSS per al cos principal del widget.

        Retorna:
            String amb les regles CSS per a CustomWidgetBase.
        """
        theme = self.current_theme
        gradient = theme.get("widget_background_gradient")
        if gradient:
            bg = (
                f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                f"stop:0 {gradient[0]}, stop:1 {gradient[-1]});"
            )
        else:
            bg = f"background-color: {theme.get('widget_background', '#ffffff')};"

        border_color  = theme.get("widget_border_color", "#cccccc")
        border_radius = theme.get("widget_border_radius", 8)
        return (
            f"CustomWidgetBase {{ {bg} border: 2px solid {border_color}; "
            f"border-radius: {border_radius}px; }}"
        )

    def _style_title_bar(self) -> str:
        """
        Genera el bloc QSS per a la barra de títol i l'etiqueta de títol.

        Retorna:
            String amb les regles CSS per a #titleBar i #titleLabel.
        """
        theme  = self.current_theme
        bg     = theme.get("title_bar_bg", theme.get("control_button_bg", "#e0e0e0"))
        border = theme.get("widget_border_color", "#cccccc")
        radius = theme.get("widget_border_radius", 8)
        color  = theme.get("title_text_color", "#333333")
        return (
            f"#titleBar {{ background-color: {bg}; border-bottom: 1px solid {border}; "
            f"border-top-left-radius: {radius}px; border-top-right-radius: {radius}px; }}\n"
            f"#titleLabel {{ font-weight: bold; color: {color}; }}"
        )

    def _style_control_buttons(self) -> str:
        """
        Genera el bloc QSS per als botons de control (minimitzar, restaurar, maximitzar).

        Retorna:
            String amb les regles CSS per a #controlButton i els seus estats.
        """
        theme   = self.current_theme
        bg      = theme.get("control_button_bg", "#d0d0d0")
        border  = theme.get("control_button_border", bg)
        color   = theme.get("control_button_text_color", get_contrast_color(bg))
        hover   = theme.get("control_button_hover", border)
        pressed = theme.get("control_button_pressed", darken_color(hover, 0.1))
        return (
            f"#controlButton {{ background-color: {bg}; border: 1px solid {border}; "
            f"border-radius: 3px; font-weight: bold; color: {color}; }}\n"
            f"#controlButton:hover {{ background-color: {hover}; }}\n"
            f"#controlButton:pressed {{ background-color: {pressed}; }}"
        )

    def _style_close_button(self) -> str:
        """
        Genera el bloc QSS per al botó de tancament.

        Retorna:
            String amb les regles CSS per a #closeButton i els seus estats.
        """
        theme   = self.current_theme
        bg      = theme.get("close_button_bg", "#ff6b6b")
        border  = theme.get("close_button_border", darken_color(bg, 0.15))
        color   = theme.get("close_button_text_color", get_contrast_color(bg))
        hover   = theme.get("close_button_hover", lighten_color(bg, 0.15))
        pressed = theme.get("close_button_pressed", darken_color(bg, 0.15))
        return (
            f"#closeButton {{ background-color: {bg}; border: 1px solid {border}; "
            f"border-radius: 3px; font-weight: bold; color: {color}; }}\n"
            f"#closeButton:hover {{ background-color: {hover}; }}\n"
            f"#closeButton:pressed {{ background-color: {pressed}; }}"
        )

    def _style_content_frame(self) -> str:
        """
        Genera el bloc QSS per al marc de contingut interior.

        Retorna:
            String amb les regles CSS per a #contentFrame.
        """
        theme  = self.current_theme
        bg     = theme.get("content_bg", theme.get("widget_background", "#ffffff"))
        radius = theme.get("widget_border_radius", 8)
        return (
            f"#contentFrame {{ background-color: {bg}; "
            f"border-bottom-left-radius: {radius}px; "
            f"border-bottom-right-radius: {radius}px; }}"
        )

    # -------------------------------------------------------------------------
    # Tematització — aplicació de colors i fonts
    # -------------------------------------------------------------------------

    def _apply_color_scheme(self, theme_dict: dict):
        """
        Aplica els colors del tema al widget i actualitza current_theme.

        Detecta automàticament si el diccionari és un tema complet (Studio
        Ghibli, amb claus 'colors'/'gradients'/'effects') o un tema
        simplificat (format pla).

        Paràmetres:
            theme_dict: Diccionari que descriu el tema a aplicar.
        """
        if not isinstance(theme_dict, dict):
            return

        is_advanced = "colors" in theme_dict and "gradients" in theme_dict
        if is_advanced:
            partial = self._normalize_advanced_theme(theme_dict)
        else:
            # Tema simplificat: actualitzem directament i normalitzem.
            self.current_theme.update(theme_dict)
            partial = self.current_theme

        self.current_theme.update(self._resolve_theme_defaults(partial))
        self.apply_styles()

    def _normalize_advanced_theme(self, theme_dict: dict) -> dict:
        """
        Converteix un tema complet (Studio Ghibli) al format intern del widget.

        Extreu les seccions 'colors', 'gradients' i 'effects' i calcula els
        valors derivats (contrast, enfosquiment, aclariment) per a cada clau.

        Paràmetres:
            theme_dict: Dict amb seccions 'colors', 'gradients' i 'effects'.

        Retorna:
            Diccionari parcial en format intern, llest per a _resolve_theme_defaults.
        """
        colors    = theme_dict.get("colors", {})
        gradients = theme_dict.get("gradients", {})
        effects   = theme_dict.get("effects", {})

        primary   = colors.get("primary")
        secondary = colors.get("secondary", primary)
        accent    = colors.get("accent", secondary)
        error     = colors.get("error", accent)

        ctrl_bg = primary or self.current_theme.get("control_button_bg")

        title_bg = (
            gradients["title_bar"][0]
            if gradients.get("title_bar")
            else colors.get("title_bar_bg", colors.get("primary", self.current_theme.get("title_bar_bg")))
        )
        close_bg = error or self.current_theme.get("close_button_bg", "#ff6b6b")

        return {
            # Widget
            "widget_background_gradient": gradients.get("widget_background"),
            "widget_background":          colors.get("background", colors.get("surface", self.current_theme.get("widget_background"))),
            "widget_border_color":        colors.get("secondary", self.current_theme.get("widget_border_color")),
            "widget_border_radius":       effects.get("border_radius", self.current_theme.get("widget_border_radius", 8)),
            # Barra de títol
            "title_bar_gradient":         gradients.get("title_bar"),
            "title_bar_bg":               title_bg,
            "title_text_color":           colors.get("text_primary", self.current_theme.get("title_text_color")),
            # Botons de control
            "control_button_bg":          ctrl_bg,
            "control_button_border":      secondary or ctrl_bg,
            "control_button_hover":       accent or secondary or ctrl_bg,
            "control_button_pressed":     darken_color(accent or secondary or ctrl_bg, 0.1),
            "control_button_text_color":  get_contrast_color(ctrl_bg),
            # Botó de tancament
            "close_button_bg":            close_bg,
            "close_button_border":        darken_color(close_bg, 0.15),
            "close_button_hover":         lighten_color(close_bg, 0.15),
            "close_button_pressed":       darken_color(close_bg, 0.15),
            "close_button_text_color":    get_contrast_color(close_bg),
            # Contingut
            "content_bg":                 colors.get("surface", colors.get("background", self.current_theme.get("content_bg"))),
            # Aliases semàntics
            "accent":    accent,
            "primary":   primary,
            "secondary": secondary,
        }

    def _resolve_theme_defaults(self, partial: dict) -> dict:
        """
        Omple les claus absents o None d'un diccionari de tema parcial.

        Utilitza una cadena de fallbacks per garantir que totes les claus
        requerides sempre tinguin un valor vàlid, fins i tot si el tema
        d'entrada és incomplet.

        Paràmetres:
            partial: Diccionari amb els valors ja calculats (pot tenir Nones).

        Retorna:
            Diccionari complet amb totes les claus necessàries resoltes.
        """
        def get(key, fallback=None):
            """Retorna el valor de 'partial' si existeix i no és None."""
            val = partial.get(key)
            return val if val is not None else (fallback() if callable(fallback) else fallback)

        ctrl_bg     = get("control_button_bg", "#d0d0d0")
        ctrl_border = get("control_button_border", ctrl_bg)
        ctrl_hover  = get("control_button_hover", ctrl_border)
        close_bg    = get("close_button_bg", ctrl_hover)
        content_bg  = get("content_bg", get("widget_background", "#ffffff"))
        title_bg    = get("title_bar_bg", ctrl_bg)

        return {
            "widget_background_gradient": get("widget_background_gradient", [content_bg, ctrl_bg]),
            "widget_background":          get("widget_background", content_bg),
            "widget_border_color":        get("widget_border_color", ctrl_border),
            "widget_border_radius":       get("widget_border_radius", 8),
            "title_bar_gradient":         get("title_bar_gradient"),
            "title_bar_bg":               title_bg,
            "title_text_color":           get("title_text_color", lambda: get_contrast_color(title_bg)),
            "control_button_bg":          ctrl_bg,
            "control_button_border":      ctrl_border,
            "control_button_hover":       ctrl_hover,
            "control_button_pressed":     get("control_button_pressed", lambda: darken_color(ctrl_hover, 0.1)),
            "control_button_text_color":  get("control_button_text_color", lambda: get_contrast_color(ctrl_bg)),
            "close_button_bg":            close_bg,
            "close_button_border":        get("close_button_border", lambda: darken_color(close_bg, 0.15)),
            "close_button_hover":         get("close_button_hover", lambda: lighten_color(close_bg, 0.15)),
            "close_button_pressed":       get("close_button_pressed", lambda: darken_color(close_bg, 0.15)),
            "close_button_text_color":    get("close_button_text_color", lambda: get_contrast_color(close_bg)),
            "content_bg":                 content_bg,
        }

    def _apply_font_settings(self, theme_dict: dict):
        """
        Punt d'extensió per aplicar ajustos tipogràfics del tema.

        Les subclasses poden sobreescriure aquest mètode per modificar fonts.
        La implementació base no fa cap canvi per mantenir el comportament existent.

        Paràmetres:
            theme_dict: Diccionari del tema (no s'utilitza a la classe base).
        """
        _ = theme_dict

    # -------------------------------------------------------------------------
    # Interacció de ratolí i redimensionament
    # -------------------------------------------------------------------------

    def eventFilter(self, obj, event):
        """Intercepta events del content_frame per gestionar cursor i redimensionament."""
        if obj != self.content_frame:
            return super().eventFilter(obj, event)

        event_type = event.type()

        if event_type in (QEvent.MouseMove, QEvent.HoverMove):
            pos   = self.mapFromGlobal(obj.mapToGlobal(event.pos()))
            edges = self._check_resize_area(pos)
            if edges:
                self._update_cursor(edges)
            elif not self._resizing:
                self.setCursor(Qt.ArrowCursor)
            return False

        if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            pos   = self.mapFromGlobal(obj.mapToGlobal(event.pos()))
            edges = self._check_resize_area(pos)
            if edges:
                self._resizing        = True
                self._resize_edges    = edges
                self._resize_drag_pos = event.globalPos()
                return True

        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        """Inicia arrossegament o redimensionament en prémer el botó esquerre."""
        if event.button() != Qt.LeftButton:
            return
        edges = self._check_resize_area(event.pos())
        if edges and not self.isMaximized():
            self._resizing        = True
            self._resize_edges    = edges
            self._resize_drag_pos = event.globalPos()
        elif hasattr(self, "title_bar") and self.title_bar.geometry().contains(event.pos()):
            self._resizing = False
            self.old_pos   = event.globalPos()

    def mouseMoveEvent(self, event):
        """Gestiona el moviment del ratolí: actualitza cursor, redimensiona o mou."""
        if event.buttons() == Qt.NoButton:
            if not self.isMaximized():
                self._update_cursor(self._check_resize_area(event.pos()))
            return

        if self._resizing:
            self._apply_resize(event.globalPos())
            return

        if event.buttons() == Qt.LeftButton and not self.old_pos.isNull():
            delta = event.globalPos() - self.old_pos
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = event.globalPos()

    def _apply_resize(self, global_pos: QPoint):
        """
        Calcula i aplica la nova geometria durant el redimensionament.

        Respecta les mides mínimes (100×50 píxels) per evitar que el widget
        desaparegui completament.

        Paràmetres:
            global_pos: Posició global actual del cursor.
        """
        delta = global_pos - self._resize_drag_pos
        geo   = self.geometry()

        if self._resize_edges & 1: geo.setLeft(geo.left()     + delta.x())
        if self._resize_edges & 2: geo.setTop(geo.top()       + delta.y())
        if self._resize_edges & 4: geo.setRight(geo.right()   + delta.x())
        if self._resize_edges & 8: geo.setBottom(geo.bottom() + delta.y())

        # Mides mínimes
        if geo.width() < 100:
            geo.setWidth(100)
            if self._resize_edges & 1:
                geo.setLeft(geo.right() - 100)
        if geo.height() < 50:
            geo.setHeight(50)
            if self._resize_edges & 2:
                geo.setTop(geo.bottom() - 50)

        self.setGeometry(geo)
        self._resize_drag_pos = global_pos

    def mouseReleaseEvent(self, event):
        """Reseteja els estats d'arrossegament i redimensionament."""
        self.old_pos       = QPoint()
        self._resizing     = False
        self._resize_edges = 0
        if not self.isMaximized():
            self._update_cursor(0)

    def enterEvent(self, event):
        """Activa el seguiment del ratolí quan entra al widget."""
        self.setMouseTracking(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Restaura el cursor per defecte quan el ratolí surt del widget."""
        if not self._resizing:
            self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def resizeEvent(self, event):
        """Gestiona l'esdeveniment de redimensionament."""
        super().resizeEvent(event)

    def _check_resize_area(self, pos) -> int:
        """Delega al servei de càlcul de vores actives."""
        return WindowInteractionService.check_resize_area(
            widget_instance=self,
            position=pos,
            resize_margin=self._RESIZE_MARGIN,
        )

    def _update_cursor(self, edges: int):
        """Delega al servei d'actualització de cursor."""
        WindowInteractionService.update_cursor(
            widget_instance=self,
            resize_edges=edges,
        )

    # -------------------------------------------------------------------------
    # Controls d'estat de finestra (deleguen a WindowStateService)
    # -------------------------------------------------------------------------

    def minimize_widget(self):
        """Minimitza el widget (l'oculta i emet el senyal corresponent)."""
        WindowStateService.minimize_widget(self)

    def restore_widget(self):
        """Restaura el widget a la mida i la posició anteriors."""
        WindowStateService.restore_widget(self)

    def maximize_widget(self):
        """Maximitza el widget dins l'àrea de treball de la pantalla activa."""
        WindowStateService.maximize_widget(self)

    def close_widget(self):
        """Tanca el widget i emet el senyal corresponent."""
        WindowStateService.close_widget(self)

    # -------------------------------------------------------------------------
    # Persistència i cicle de vida
    # -------------------------------------------------------------------------

    def get_state(self) -> dict:
        """Retorna l'estat actual del widget serialitzat per a persistència."""
        return WindowStateService.get_state(self)

    def set_state(self, state: dict):
        """Restaura l'estat del widget des d'un diccionari desat prèviament."""
        WindowStateService.set_state(self, state)

    @classmethod
    def update_all_widgets_theme(cls, theme_dict: dict):
        """
        Actualitza el tema de totes les instàncies vives de CustomWidgetBase.

        S'ha de cridar des de ThemeManager quan canvia el tema global
        per assegurar que cada widget reflecteix la paleta nova.

        Paràmetres:
            theme_dict: Diccionari del tema (pot ser avançat o simplificat).
        """
        ThemeLifecycleService.update_all_widgets_theme(
            widget_instances=cls._instances,
            theme_dict=theme_dict,
        )

    def refresh(self, deep: bool = True, reapply_theme: bool = True):
        """
        Refresca el widget després de canvis de configuració, idioma o tema.

        Paràmetres:
            deep: Si és True, reconstrueix el contingut cridant setup_content().
            reapply_theme: Si és True, torna a aplicar el full d'estils actual.
        """
        ThemeLifecycleService.refresh_widget(
            widget_instance=self,
            deep=deep,
            reapply_theme=reapply_theme,
        )