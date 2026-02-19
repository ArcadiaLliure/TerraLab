"""
Clase base para todos los widgets personalizados del escritorio.
Proporciona funcionalidad común como arrastre, redimensionamiento y controles de ventana.

Esta implementación incorpora soporte de tematización. Los colores y estilos de la
interfaz se derivan de un tema actual almacenado en `self.current_theme`.  Este
tema puede ser configurado mediante el método `set_theme()`, que acepta tanto
formatos simplificados como los temas completos de Studio Ghibli (con
secciones `colors`, `gradients` y `effects`).

El método `apply_styles()` genera dinámicamente la hoja de estilos QSS en
función del tema activo.
"""

import os
import json
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QFrame, QSizeGrip, QApplication
)
from PyQt5.QtCore import Qt, QPoint, pyqtSignal, QEvent
from PyQt5.QtGui import QFont, QIcon
from PyQt5.QtWidgets import QSizePolicy
from .utils import resource_path



# -----------------------------------------------------------------------------
# Utilidades de color
def _hex_to_rgb(color: str):
    """Convierte un color en formato '#RRGGBB' a una tupla (r, g, b)."""
    color = color.lstrip('#')
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convierte componentes RGB a un color hexadecimal."""
    return f"#{r:02x}{g:02x}{b:02x}"


def lighten_color(color: str, factor: float = 0.1) -> str:
    """
    Aclara un color mezclándolo con blanco. El factor debe estar entre 0 y 1.
    Un factor de 0.0 no cambia el color, 1.0 lo vuelve blanco.
    """
    r, g, b = _hex_to_rgb(color)
    new_r = min(255, int(r + (255 - r) * factor))
    new_g = min(255, int(g + (255 - g) * factor))
    new_b = min(255, int(b + (255 - b) * factor))
    return _rgb_to_hex(new_r, new_g, new_b)


def darken_color(color: str, factor: float = 0.1) -> str:
    """
    Oscurece un color multiplicando cada componente por (1 - factor).
    El factor debe estar entre 0 y 1. Un factor de 0.0 no cambia el color.
    """
    r, g, b = _hex_to_rgb(color)
    new_r = max(0, int(r * (1 - factor)))
    new_g = max(0, int(g * (1 - factor)))
    new_b = max(0, int(b * (1 - factor)))
    return _rgb_to_hex(new_r, new_g, new_b)


def get_contrast_color(color: str) -> str:
    """
    Determina un color de texto (negro o blanco) que contraste con el fondo.
    Se calcula la luminosidad perceptual del color para escoger el más legible.
    """
    r, g, b = _hex_to_rgb(color)
    # Fórmula de luminosidad percibida según ITU-R BT.601
    luminance = (0.299 * r + 0.587 * g + 0.114 * b)
    return '#000000' if luminance > 128 else '#FFFFFF'


class CustomWidgetBase(QWidget):
    """
    Clase base para widgets personalizados del escritorio.
    Proporciona funcionalidad de ventana frameless con controles personalizados.
    """

    # Señales para comunicación con la ventana principal
    widget_minimized = pyqtSignal(object)  # Emite el widget que se minimizó
    widget_maximized = pyqtSignal(object)  # Emite el widget que se maximizó
    widget_restored = pyqtSignal(object)   # Emite el widget que se restauró
    widget_closed = pyqtSignal(object)     # Emite el widget que se cerró

    # Lista de instancias para la actualización masiva de temas
    _instances = []

    # Marge per detectar el redimensionament
    _RESIZE_MARGIN = 8

    def __init__(self, title: str = "Widget", parent=None, frameless=True):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        # Habilitar el seguiment del ratolí per canviar el cursor a les vores
        self.setMouseTracking(True)
        
        self.title = title
        self.is_maximized = False
        self.old_pos = QPoint()
        self.old_geometry = None  # Para restaurar desde maximizado
        
        # Estat de redimensionament
        self._resizing = False
        self._resize_drag_pos = QPoint()
        self._resize_edges = 0  # Bitmask: Left=1, Top=2, Right=4, Bottom=8
        
        self.is_frameless = frameless

        # Configurar ventana frameless
        if frameless:
            self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            self.setAttribute(Qt.WA_TranslucentBackground, False)
        else:
            # Standard window
            self.setAttribute(Qt.WA_TranslucentBackground, False)

        # Configurar tamaño por defecto
        self.default_size = (400, 300)
        self.default_position = (100, 100)
        self.resize(*self.default_size)
        self.move(*self.default_position)

        # Definir un tema por defecto basado en la paleta Terra
        # Este diccionario puede ser actualizado mediante el método set_theme().
        error_color = '#c8553d'
        control_bg = '#948465'
        secondary_color = '#634311'
        surface_color = '#f1dfbe'
        self.current_theme = {
            # Fondo del widget: degradado de widget_background o color fijo
            'widget_background_gradient': ['#f1dfbe', '#948465'],
            'widget_background': '#f1dfbe',
            'widget_border_color': secondary_color,
            'widget_border_radius': 10,
            # Barra de título
            'title_bar_gradient': ['#a3a85e', '#a3a85e'],
            'title_bar_bg': '#a3a85e',
            'title_text_color': '#000000',
            # Botones de control (minimizar/restaurar/maximizar)
            'control_button_bg': control_bg,
            'control_button_border': secondary_color,
            'control_button_hover': secondary_color,
            'control_button_pressed': secondary_color,
            'control_button_text_color': get_contrast_color(control_bg),
            # Botón de cierre
            'close_button_bg': error_color,
            'close_button_border': darken_color(error_color, 0.15),
            'close_button_hover': lighten_color(error_color, 0.15),
            'close_button_pressed': darken_color(error_color, 0.15),
            'close_button_text_color': get_contrast_color(error_color),
            # Contenido interno
            'content_bg': surface_color,
        }

        # Configurar la interfaz
        self.setup_ui()

        # Aplicar estilos basados en el tema
        self.apply_styles()

        # Registrar la instancia para actualizaciones de tema
        try:
            CustomWidgetBase._instances.append(self)
        except Exception:
            # Si la lista no existe (primera carga), crearla
            CustomWidgetBase._instances = [self]

        # Aplicar el tema persistido al inicializar, si existe una preferencia
        # y la paleta actual del widget aún es la predeterminada. Esto evita
        # que se muestren colores por defecto antes de que el usuario seleccione
        # manualmente un tema.
        try:
            import json
            import os
            # Localizar el archivo de configuración desde la ruta de este módulo
            config_file = resource_path('data/config.json')
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as cfg:
                    conf = json.load(cfg)
                saved_theme = conf.get('theme')
                if saved_theme:
                    # Obtener tema completo utilizando ThemeManager (se crea instancia temporal)
                    try:
                        from theme.theme_service import ThemeManager
                        tmp_tm = ThemeManager()
                        theme_dict = tmp_tm.get_theme(saved_theme)
                        # Establecer el tema en este widget solo si es diferente al actual
                        if theme_dict:
                            self.set_theme(theme_dict)
                    except Exception:
                        pass
        except Exception:
            pass

    def setup_ui(self):
        """Configura la interfaz de usuario del widget."""
        # Layout principal
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Crear la barra de título (Solo si es frameless)
        if self.is_frameless:
            self.create_title_bar()

        # Crear el área de contenido
        self.content_frame = QFrame()
        self.content_frame.setObjectName("contentFrame")
        self.content_layout = QVBoxLayout(self.content_frame)
        self.content_layout.setContentsMargins(10, 10, 10, 10)

        # Añadir al layout principal
        if self.is_frameless:
            self.main_layout.addWidget(self.title_bar)
            
        self.main_layout.addWidget(self.content_frame, 1)
        
        # Instal·lem event filter al frame de contingut per detectar vores fins i tot a sobre dels fills
        self.content_frame.setMouseTracking(True)
        self.content_frame.installEventFilter(self)

        # Configurar el contenido específico del widget
        self.setup_content()

    def eventFilter(self, obj, event):
        """Intercepta moviments per actualitzar cursor a les vores."""
        if obj == self.content_frame:
            if event.type() == QEvent.MouseMove or event.type() == QEvent.HoverMove:
                # Convertim la posició local del fill a la del pare (CustomWidgetBase)
                pos = self.mapFromGlobal(obj.mapToGlobal(event.pos()))
                edges = self._check_resize_area(pos)
                if edges:
                    self._update_cursor(edges)
                    # Opcional: Si estem molt a la vora, podríem voler 'menjar-nos' l'event
                    # o deixar que passi. Normalment canviar el cursor és suficient visualment.
                    return False # Deixem passar, però ja hem canviat el cursor
                elif not self._resizing:
                     # Si no estem a la vora ni redimensionant, restaurem cursor normal
                     self.setCursor(Qt.ArrowCursor)
            
            # També cal gestionar el clic si volem permetre redimensionar iniciant el clic DINS del marge del fill
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                pos = self.mapFromGlobal(obj.mapToGlobal(event.pos()))
                edges = self._check_resize_area(pos)
                if edges:
                    self._resizing = True
                    self._resize_edges = edges
                    self._resize_drag_pos = event.globalPos()
                    return True # Capturem l'event, no deixem que el fill (ex: text input) el rebi
        
        return super().eventFilter(obj, event)

    def create_title_bar(self):
        """Crea la barra de título personalizada."""
        self.title_bar = QFrame()
        self.title_bar.setObjectName("titleBar")
        self.title_bar.setFixedHeight(30)

        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(10, 5, 5, 5)

        # Título
        self.title_label = QLabel(self.title)
        self.title_label.setObjectName("titleLabel")
        title_layout.addWidget(self.title_label)

        title_layout.addStretch()

        # Botones de control
        self.minimize_btn = QPushButton("−")
        self.minimize_btn.setObjectName("controlButton")
        self.minimize_btn.setFixedSize(20, 20)
        self.minimize_btn.clicked.connect(self.minimize_widget)

        self.restore_btn = QPushButton("□")
        self.restore_btn.setObjectName("controlButton")
        self.restore_btn.setFixedSize(20, 20)
        self.restore_btn.clicked.connect(self.restore_widget)

        self.maximize_btn = QPushButton("□")
        self.maximize_btn.setObjectName("controlButton")
        self.maximize_btn.setFixedSize(20, 20)
        self.maximize_btn.clicked.connect(self.maximize_widget)

        self.close_btn = QPushButton("×")
        self.close_btn.setObjectName("closeButton")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.clicked.connect(self.close_widget)

        title_layout.addWidget(self.minimize_btn)
        title_layout.addWidget(self.restore_btn)
        title_layout.addWidget(self.maximize_btn)
        title_layout.addWidget(self.close_btn)

    def setup_content(self):
        """
        Método para ser sobrescrito por las subclases.
        Aquí se debe configurar el contenido específico del widget.
        """
        pass

    def apply_styles(self):
        """Aplica los estilos CSS al widget en función del tema actual."""
        t = self.current_theme
        # Construir estilo del widget y su barra de título
        css_parts = []
        # Estilo para el contenedor principal (CustomWidgetBase)
        if t.get('widget_background_gradient'):
            color0, color1 = t['widget_background_gradient'][0], t['widget_background_gradient'][-1]
            widget_bg = f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {color0}, stop:1 {color1});"
        else:
            widget_bg = f"background-color: {t.get('widget_background', '#ffffff')};"
        # Determinar colores y valores tomando como preferencia los definidos en el tema.
        # Si no existen, derivar de otros valores ya presentes en el tema de manera lógica.
        border_color = t.get('widget_border_color', t.get('control_button_border', t.get('control_button_bg', '#cccccc')))
        border_radius = t.get('widget_border_radius', 8)
        css_parts.append(
            f"CustomWidgetBase {{ {widget_bg} border: 2px solid {border_color}; "
            f"border-radius: {border_radius}px; }}"
        )
        # Estilo de la barra de título
        # (Modificació: Ignorar el gradient per defecte per evitar que es vegi fosc/verd si l'usuari prefereix el color pla)
        # if t.get('title_bar_gradient'):
        #     tb_c0, tb_c1 = t['title_bar_gradient'][0], t['title_bar_gradient'][-1]
        #     title_bar_bg = (
        #         f"background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {tb_c0}, stop:1 {tb_c1});"
        #     )
        # else:
        title_bar_bg = f"background-color: {t.get('title_bar_bg', t.get('control_button_bg', '#e0e0e0'))};"
        # Usar las claves con get y derivar si faltan
        tb_border_color = t.get('widget_border_color', t.get('control_button_border', t.get('control_button_bg', '#cccccc')))
        tb_radius = t.get('widget_border_radius', 8)
        css_parts.append(
            f"#titleBar {{ {title_bar_bg} border-bottom: 1px solid {tb_border_color}; "
            f"border-top-left-radius: {tb_radius}px; "
            f"border-top-right-radius: {tb_radius}px; }}"
        )
        # Estilo del título
        css_parts.append(
            f"#titleLabel {{ font-weight: bold; color: {t.get('title_text_color', t.get('text_primary', '#333333'))}; }}"
        )


        # Estilo de los botones de control (excluyendo el botón de cierre)
        # Para los botones de control usamos un esquema que derive colores de otros valores
        # si alguna clave no existe. De este modo evitamos caer en colores por defecto
        # ajenos al tema (como grises genéricos).
        ctrl_bg = t.get(
            'control_button_bg',
            t.get('widget_border_color', t.get('title_bar_bg', t.get('widget_background', '#d0d0d0')))
        )
        ctrl_border = t.get('control_button_border', t.get('widget_border_color', ctrl_bg))
        # Color de texto de los botones de control
        ctrl_text = t.get('control_button_text_color', get_contrast_color(ctrl_bg))
        ctrl_hover = t.get('control_button_hover', ctrl_border)
        ctrl_pressed = t.get('control_button_pressed', darken_color(ctrl_hover, 0.1))
        css_parts.append(
            f"#controlButton {{ "
            f"background-color: {ctrl_bg}; "
            f"border: 1px solid {ctrl_border}; "
            f"border-radius: 3px; font-weight: bold; "
            f"color: {ctrl_text}; }}"
        )
        css_parts.append(
            f"#controlButton:hover {{ background-color: {ctrl_hover}; }}"
        )
        css_parts.append(
            f"#controlButton:pressed {{ background-color: {ctrl_pressed}; }}"
        )
        # Estilo específico del botón de cierre
        # Estilo específico del botón de cierre
        close_bg = t.get('close_button_bg', ctrl_hover)
        close_border = t.get('close_button_border', darken_color(close_bg, 0.15))
        # Calcular el color de texto contrastado si no viene en el tema
        close_text = t.get('close_button_text_color', get_contrast_color(close_bg))
        close_hover = t.get('close_button_hover', lighten_color(close_bg, 0.15))
        close_pressed = t.get('close_button_pressed', darken_color(close_bg, 0.15))
        css_parts.append(
            f"#closeButton {{ "
            f"background-color: {close_bg}; "
            f"border: 1px solid {close_border}; "
            f"border-radius: 3px; font-weight: bold; "
            f"color: {close_text}; }}"
        )
        css_parts.append(
            f"#closeButton:hover {{ background-color: {close_hover}; }}"
        )
        css_parts.append(
            f"#closeButton:pressed {{ background-color: {close_pressed}; }}"
        )
        # Estilo del contenido interno
        # Estilo del contenido interno
        content_bg = t.get('content_bg', t.get('surface', t.get('widget_background', '#ffffff')))
        c_radius = t.get('widget_border_radius', border_radius)
        css_parts.append(
            f"#contentFrame {{ background-color: {content_bg}; "
            f"border-bottom-left-radius: {c_radius}px; "
            f"border-bottom-right-radius: {c_radius}px; }}"
        )
        # Unir partes y aplicar
        self.setStyleSheet("\n".join(css_parts))

    # ----- Gestión del tema -----
    def set_theme(self, theme_dict):
        """
        Establece un tema para el widget. Puede aceptar tanto un tema
        simplificado (con claves que coinciden con las de `self.current_theme`)
        como un tema avanzado con secciones `colors`, `gradients` y `effects`.

        Args:
            theme_dict (dict): Descripción del tema.
        """
        # Si el tema tiene la estructura de los temas de Ghibli (colors/gradients/effects)
        if isinstance(theme_dict, dict) and 'colors' in theme_dict and 'gradients' in theme_dict:
            colors = theme_dict.get('colors', {})
            gradients = theme_dict.get('gradients', {})
            effects = theme_dict.get('effects', {})
            new_theme = {}
            # Elementos clave del tema
            primary = colors.get('primary')
            secondary = colors.get('secondary', primary)
            accent = colors.get('accent', secondary)
            error_color = colors.get('error', accent)
            # Fondo del widget
            new_theme['widget_background_gradient'] = gradients.get('widget_background')
            new_theme['widget_background'] = colors.get('background', colors.get('surface', self.current_theme.get('widget_background')))
            
            # Guardar colores base para futuras referencias (crucial para hover/accent)
            new_theme['accent'] = accent
            new_theme['primary'] = primary
            new_theme['secondary'] = secondary
            
            # Borde y radio
            new_theme['widget_border_color'] = colors.get('secondary', self.current_theme.get('widget_border_color'))
            new_theme['widget_border_radius'] = effects.get('border_radius', self.current_theme.get('widget_border_radius', 8))
            # Barra de título
            new_theme['title_bar_gradient'] = gradients.get('title_bar')
            if 'title_bar' in gradients and gradients.get('title_bar'):
                new_theme['title_bar_bg'] = gradients['title_bar'][0]
            else:
                new_theme['title_bar_bg'] = colors.get('title_bar_bg', colors.get('primary', self.current_theme.get('title_bar_bg')))
            
            new_theme['title_text_color'] = colors.get('text_primary', self.current_theme.get('title_text_color'))
            # Botones de control
            new_theme['control_button_bg'] = primary or self.current_theme.get('control_button_bg')
            new_theme['control_button_border'] = secondary or new_theme['control_button_bg']
            new_theme['control_button_hover'] = accent or new_theme['control_button_border']
            # Para el botón pulsado, oscurecer el color de hover
            new_theme['control_button_pressed'] = darken_color(new_theme['control_button_hover'], 0.1)
            # Color de texto contrastado para los botones de control
            new_theme['control_button_text_color'] = get_contrast_color(new_theme['control_button_bg'])
            # Botón de cierre
            new_theme['close_button_bg'] = error_color or new_theme['control_button_hover']
            new_theme['close_button_border'] = darken_color(new_theme['close_button_bg'], 0.15)
            new_theme['close_button_hover'] = lighten_color(new_theme['close_button_bg'], 0.15)
            new_theme['close_button_pressed'] = darken_color(new_theme['close_button_bg'], 0.15)
            new_theme['close_button_text_color'] = get_contrast_color(new_theme['close_button_bg'])
            # Contenido interno
            new_theme['content_bg'] = colors.get('surface', colors.get('background', self.current_theme.get('content_bg')))
            # Asegurarse de que todos los atributos esenciales están presentes
            required_keys = [
                'widget_background_gradient', 'widget_background', 'widget_border_color', 'widget_border_radius',
                'title_bar_gradient', 'title_bar_bg', 'title_text_color',
                'control_button_bg', 'control_button_border', 'control_button_hover', 'control_button_pressed', 'control_button_text_color',
                'close_button_bg', 'close_button_border', 'close_button_hover', 'close_button_pressed', 'close_button_text_color',
                'content_bg', 'accent', 'primary', 'secondary'
            ]
            for k in required_keys:
                if k not in new_theme:
                    # Derivar faltantes basándose en otros valores calculados
                    if k == 'widget_background_gradient':
                        # Si no hay gradiente definido, crear uno a partir del fondo y el borde
                        new_theme[k] = gradients.get('widget_background', [new_theme['content_bg'], new_theme['control_button_bg']])
                    elif k == 'widget_border_color':
                        new_theme[k] = new_theme.get('control_button_border', new_theme.get('control_button_bg', '#cccccc'))
                    elif k == 'widget_border_radius':
                        new_theme[k] = effects.get('border_radius', self.current_theme.get('widget_border_radius', 8))
                    elif k == 'title_bar_gradient':
                        new_theme[k] = gradients.get('title_bar')
                    elif k == 'title_bar_bg':
                        new_theme[k] = new_theme.get('control_button_bg', '#e0e0e0')
                    elif k == 'title_text_color':
                        # Calcular color de texto según contraste con barra de título
                        new_theme[k] = get_contrast_color(new_theme.get('title_bar_bg', new_theme.get('control_button_bg', '#333333')))
                    elif k == 'control_button_bg':
                        new_theme[k] = new_theme.get('control_button_hover', new_theme.get('control_button_border', primary))
                    elif k == 'control_button_border':
                        new_theme[k] = new_theme.get('control_button_bg', primary)
                    elif k == 'control_button_hover':
                        new_theme[k] = new_theme.get('control_button_border', new_theme.get('control_button_bg', '#d0d0d0'))
                    elif k == 'control_button_pressed':
                        new_theme[k] = darken_color(new_theme.get('control_button_hover', new_theme.get('control_button_bg', '#d0d0d0')), 0.1)
                    elif k == 'control_button_text_color':
                        new_theme[k] = get_contrast_color(new_theme.get('control_button_bg', '#d0d0d0'))
                    elif k == 'close_button_bg':
                        new_theme[k] = new_theme.get('control_button_hover', new_theme.get('control_button_bg', '#ff6b6b'))
                    elif k == 'close_button_border':
                        new_theme[k] = darken_color(new_theme.get('close_button_bg'), 0.15)
                    elif k == 'close_button_hover':
                        new_theme[k] = lighten_color(new_theme.get('close_button_bg'), 0.15)
                    elif k == 'close_button_pressed':
                        new_theme[k] = darken_color(new_theme.get('close_button_bg'), 0.15)
                    elif k == 'close_button_text_color':
                        new_theme[k] = get_contrast_color(new_theme.get('close_button_bg'))
                    elif k == 'content_bg':
                        new_theme[k] = new_theme.get('widget_background', colors.get('surface', '#ffffff'))
            # Actualizar tema y aplicar
            self.current_theme.update(new_theme)
            self.apply_styles()
            return
        # Si ya viene normalizado, mezclarlo directamente
        elif isinstance(theme_dict, dict):
            # Interpretar el diccionario simplificado como un conjunto de sobrescrituras
            self.current_theme.update(theme_dict)
            # Asegurar que todas las claves esenciales estén presentes. Si faltan, derivar valores lógicos.
            required_keys = [
                'widget_background_gradient', 'widget_background', 'widget_border_color', 'widget_border_radius',
                'title_bar_gradient', 'title_bar_bg', 'title_text_color',
                'control_button_bg', 'control_button_border', 'control_button_hover', 'control_button_pressed', 'control_button_text_color',
                'close_button_bg', 'close_button_border', 'close_button_hover', 'close_button_pressed', 'close_button_text_color',
                'content_bg'
            ]
            t = self.current_theme
            # Variables auxiliares basadas en las claves existentes
            ctrl_bg = t.get('control_button_bg', '#d0d0d0')
            ctrl_border = t.get('control_button_border', ctrl_bg)
            ctrl_hover = t.get('control_button_hover', ctrl_bg)
            ctrl_pressed = t.get('control_button_pressed', darken_color(ctrl_hover, 0.1))
            border_radius = t.get('widget_border_radius', 8)
            # Rellenar claves faltantes
            for k in required_keys:
                if k not in t or t[k] is None:
                    if k == 'widget_background_gradient':
                        t[k] = t.get('widget_background_gradient', [t.get('content_bg', '#ffffff'), ctrl_bg])
                    elif k == 'widget_background':
                        t[k] = t.get('widget_background', t.get('content_bg', '#ffffff'))
                    elif k == 'widget_border_color':
                        t[k] = t.get('widget_border_color', ctrl_border)
                    elif k == 'widget_border_radius':
                        t[k] = border_radius
                    elif k == 'title_bar_gradient':
                        t[k] = t.get('title_bar_gradient')
                    elif k == 'title_bar_bg':
                        t[k] = t.get('title_bar_bg', ctrl_bg)
                    elif k == 'title_text_color':
                        t[k] = t.get('title_text_color', get_contrast_color(t.get('title_bar_bg', ctrl_bg)))
                    elif k == 'control_button_bg':
                        # Si no hay color para el fondo del botón de control, usar el color del borde del widget
                        # o bien el color de la barra de título. Así se mantiene la coherencia con el tema.
                        t[k] = t.get('control_button_bg', t.get('widget_border_color', t.get('title_bar_bg', '#d0d0d0')))
                    elif k == 'control_button_border':
                        # Usar el borde del widget o el fondo del botón de control
                        t[k] = t.get('control_button_border', t.get('widget_border_color', t.get('control_button_bg')))
                    elif k == 'control_button_hover':
                        # Por defecto el hover hereda del borde del botón de control
                        t[k] = t.get('control_button_hover', t.get('control_button_border', t.get('control_button_bg')))
                    elif k == 'control_button_pressed':
                        # Si no se define, oscurecer el color del hover
                        t[k] = t.get('control_button_pressed', darken_color(t.get('control_button_hover', t.get('control_button_bg')), 0.1))
                    elif k == 'control_button_text_color':
                        # Determinar color de texto según contraste con el fondo del botón de control
                        t[k] = t.get('control_button_text_color', get_contrast_color(t.get('control_button_bg', t.get('widget_border_color', '#d0d0d0'))))
                    elif k == 'close_button_bg':
                        # Usar el color de error o en su defecto el color de hover del botón de control
                        t[k] = t.get('close_button_bg', t.get('control_button_hover', t.get('control_button_bg')))
                    elif k == 'close_button_border':
                        # Oscurecer el color de fondo del botón de cierre
                        t[k] = t.get('close_button_border', darken_color(t.get('close_button_bg', t.get('control_button_hover', t.get('control_button_bg'))), 0.15))
                    elif k == 'close_button_hover':
                        # Aclarar el color de fondo del botón de cierre
                        t[k] = t.get('close_button_hover', lighten_color(t.get('close_button_bg', t.get('control_button_hover', t.get('control_button_bg'))), 0.15))
                    elif k == 'close_button_pressed':
                        # Oscurecer el color de fondo del botón de cierre
                        t[k] = t.get('close_button_pressed', darken_color(t.get('close_button_bg', t.get('control_button_hover', t.get('control_button_bg'))), 0.15))
                    elif k == 'close_button_text_color':
                        # Ajustar texto para que contraste con el color del botón de cierre
                        t[k] = t.get('close_button_text_color', get_contrast_color(t.get('close_button_bg', t.get('control_button_hover', t.get('control_button_bg')))))
                    elif k == 'content_bg':
                        # Usar superficie o fondo por defecto
                        t[k] = t.get('content_bg', t.get('widget_background', '#ffffff'))
            # Aplicar estilos con el tema completado
            self.apply_styles()
        else:
            # Formato no reconocido: no se aplica ningún cambio
            return

    # ----- Eventos de interacción y manejo del widget -----
    
    def _check_resize_area(self, pos):
        """Determina a quina vora està el ratolí."""
        edges = 0
        w, h = self.width(), self.height()
        m = self._RESIZE_MARGIN
        
        if pos.x() < m: edges |= 1  # Left
        if pos.x() > w - m: edges |= 4  # Right
        if pos.y() < m: edges |= 2  # Top
        if pos.y() > h - m: edges |= 8  # Bottom
        
        return edges

    def _update_cursor(self, edges):
        """Canvia el cursor segons la vora."""
        if edges == 0:
            self.setCursor(Qt.ArrowCursor)
        elif edges in (1, 4):     # Esquerra o Dreta
            self.setCursor(Qt.SizeHorCursor)
        elif edges in (2, 8):     # Dalt o Baix
            self.setCursor(Qt.SizeVerCursor)
        elif edges in (3, 12):    # TopLeft o BottomRight
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (6, 9):     # TopRight o BottomLeft
            self.setCursor(Qt.SizeBDiagCursor)

    def mousePressEvent(self, event):
        """Maneja el clic per moure o redimensionar."""
        if event.button() == Qt.LeftButton:
            edges = self._check_resize_area(event.pos())
            if edges and not self.isMaximized():
                self._resizing = True
                self._resize_edges = edges
                self._resize_drag_pos = event.globalPos()
            elif self.title_bar.geometry().contains(event.pos()):
                self._resizing = False
                self.old_pos = event.globalPos()

    def mouseMoveEvent(self, event):
        """Maneja el moviment (redimensionament, arrossegament o canvi de cursor)."""
        # 1. Si no estem prement res, actualitzem cursor segons posició
        if event.buttons() == Qt.NoButton:
            if not self.isMaximized():
                edges = self._check_resize_area(event.pos())
                self._update_cursor(edges)
            return

        # 2. Si estem redimensionant
        if self._resizing:
            delta = event.globalPos() - self._resize_drag_pos
            geo = self.geometry()
            
            # Càlcul de nova geometria
            if self._resize_edges & 1: # Left
                geo.setLeft(geo.left() + delta.x())
            if self._resize_edges & 2: # Top
                geo.setTop(geo.top() + delta.y())
            if self._resize_edges & 4: # Right
                geo.setRight(geo.right() + delta.x())
            if self._resize_edges & 8: # Bottom
                geo.setBottom(geo.bottom() + delta.y())
            
            # Aplicar límits mínims (per evitar errors)
            if geo.width() < 100: 
                geo.setWidth(100)
                if self._resize_edges & 1: geo.setLeft(geo.right() - 100)
            if geo.height() < 50:
                geo.setHeight(50)
                if self._resize_edges & 2: geo.setTop(geo.bottom() - 50)

            self.setGeometry(geo)
            self._resize_drag_pos = event.globalPos()
            return

        # 3. Si estem movent (mode arrossegar)
        if event.buttons() == Qt.LeftButton and not self.old_pos.isNull():
            delta = QPoint(event.globalPos() - self.old_pos)
            self.move(self.x() + delta.x(), self.y() + delta.y())
            self.old_pos = event.globalPos()

    def mouseReleaseEvent(self, event):
        """Reseteja estats."""
        self.old_pos = QPoint()
        self._resizing = False
        self._resize_edges = 0
        if not self.isMaximized():
            self._update_cursor(0) # Restaurar cursor per si de cas

    def enterEvent(self, event):
        # Assegurar tracking al entrar
        self.setMouseTracking(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        # Restaurar cursor al sortir
        if not self._resizing:
            self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def resizeEvent(self, event):
        """Maneja el evento de redimensionar."""
        super().resizeEvent(event)

    def minimize_widget(self):
        """Minimiza el widget (lo oculta)."""
        self.hide()
        self.widget_minimized.emit(self)

    def restore_widget(self):
        """Restaura el widget a su tamaño y posición por defecto."""
        if self.is_maximized:
            # Restaurar desde maximizado
            if self.old_geometry:
                self.setGeometry(self.old_geometry)
            else:
                self.resize(*self.default_size)
                self.move(*self.default_position)
            self.is_maximized = False
            self.widget_restored.emit(self)
        else:
            # Restaurar a posición/tamaño por defecto
            self.resize(*self.default_size)
            self.move(*self.default_position)

    def maximize_widget(self):
        """Maximiza el widget dentro del área de trabajo (sin salirse por abajo)."""
        if self.is_maximized:
            return

        # Guardar geometría actual para poder restaurar
        self.old_geometry = self.geometry()

        # Intentamos usar la pantalla donde está el widget
        app = QApplication.instance()
        screen = None
        if app is not None and hasattr(app, "screenAt"):
            # Qt >= 5.10
            screen = app.screenAt(self.frameGeometry().center())

        if screen is None:
            # Fallback: pantalla principal
            screen = QApplication.primaryScreen()

        if screen is not None:
            geom = screen.availableGeometry()   # 👈 área útil (respeta barra de tareas)
        else:
            # Último recurso
            geom = QApplication.desktop().availableGeometry(self)

        self.setGeometry(geom)
        self.is_maximized = True
        self.widget_maximized.emit(self)


    def close_widget(self):
        """Cierra el widget."""
        self.widget_closed.emit(self)
        self.close()

    def get_state(self) -> dict:
        """Retorna el estado actual del widget para persistencia."""
        return {
            'title': self.title,
            'x': self.x(),
            'y': self.y(),
            'width': self.width(),
            'height': self.height(),
            'is_maximized': self.is_maximized,
            'visible': self.isVisible()
        }

    def set_state(self, state: dict):
        """Restaura el estado del widget desde datos guardados."""
        if 'x' in state and 'y' in state:
            self.move(state['x'], state['y'])
        if 'width' in state and 'height' in state:
            self.resize(state['width'], state['height'])
        if 'is_maximized' in state and state['is_maximized']:
            self.maximize_widget()
        if 'visible' in state:
            self.setVisible(state['visible'])

    # ------------------------------------------------------------------
    # Métodos de utilidad para la gestión de temas
    # ------------------------------------------------------------------
    @classmethod
    def update_all_widgets_theme(cls, theme_dict: dict):
        """
        Actualiza el tema de todas las instancias vivas de CustomWidgetBase.

        Este método recorre todas las instancias registradas de la clase y
        aplica el nuevo tema usando `set_theme()`. Debe llamarse desde
        ThemeManager cuando cambia el tema para asegurar que cada widget
        refleje la nueva paleta.

        Args:
            theme_dict (dict): Diccionario del tema (puede ser avanzado o
                               simplificado) que se pasará a `set_theme()`.
        """
        for widget in list(cls._instances):
            try:
                widget.set_theme(theme_dict)
            except Exception:
                continue

    def refresh(self, deep: bool = True, reapply_theme: bool = True):
        """
        Refresca el widget tras cambios de configuración/idioma/tema.

        - reapply_theme: si True, vuelve a aplicar la hoja de estilos actual.
        - deep: si True, reconstruye el contenido del widget (llama a setup_content()).

        Subclases pueden sobreescribir on_refresh() o implementar métodos como:
        retranslate_ui(), retranslate(), update_texts(), update_ui(), refresh_content()
        para actualizar sus textos/estados específicos.
        """
        # 1) Reaplicar estilos del tema activo
        if reapply_theme:
            try:
                self.apply_styles()
            except Exception:
                pass

        # 3) Hook de extensión para subclases
        onr = getattr(self, "retranslate_ui", None)
        if callable(onr):
            try:
                onr(deep=deep)
            except Exception:
                pass

        # 4) Reconstrucción profunda del contenido si se pide
        if deep:
            try:
                # Vaciar el layout de contenido
                while self.content_layout.count():
                    item = self.content_layout.takeAt(0)
                    w = item.widget()
                    if w is not None:
                        w.setParent(None)
                # Volver a construir el contenido específico
                self.setup_content()
            except Exception:
                pass

        # 5) Ajustes finales de layout/repintado
        try:
            self.updateGeometry()
            self.repaint()
        except Exception:
            pass