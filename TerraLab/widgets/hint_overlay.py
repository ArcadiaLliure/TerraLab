# Component HintOverlay — toast HUD contextual per a TerraLab.
# Apareix sobre el canvas durant 2 segons quan l'usuari interactua
# (zoom, canvi de temps, reubicació) i s'esvaeix suaument.

import math
from PyQt5.QtWidgets import QWidget
from PyQt5.QtCore    import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui     import QPainter, QColor, QFont, QPainterPath, QFontMetrics


# ── Constants visuals ─────────────────────────────────────────────────────────
_PAD_H      = 18    # padding horitzontal (px)
_PAD_V      = 10    # padding vertical (px)
_RADIUS     = 14    # radi dels cantons arrodonits (px)
_MARGIN     = 18    # distància al marge inferior del canvas (px)
_FONT_SIZE  = 11    # mida de la font (pt)
_SHOW_MS    = 2000  # temps de vida del toast (ms)
_FADE_MS    = 350   # durada del fade in/out (ms)

# Colors (fons semitransparent fosc + text blanc)
_BG_COLOR   = QColor(15, 15, 30, 210)
_TEXT_COLOR = QColor(255, 255, 255, 255)


class HintOverlay(QWidget):
    """
    Toast HUD transparent. Flota sobre el widget pare.

    Ús:
        self.hint = HintOverlay(parent=self.canvas)
        self.hint.show_hint("FOV 45°  ·  50mm")
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Transparency + no window chrome + click-through
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)

        self._text  = ""
        self._alpha = 0    # opacitat actual 0-255

        # Timer per amagar el toast
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._start_fade_out)

        # Animació de fade in/out via la propietat windowOpacity
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)
        self._anim.finished.connect(self._on_anim_finished)

        # Font
        self._font = QFont("Segoe UI", _FONT_SIZE)
        self._font.setWeight(QFont.Medium)

        self.hide()

    # ── API pública ───────────────────────────────────────────────────────────

    def show_hint(self, text: str):
        """Mostra el toast amb el text indicat (reinicia el timer si ja estava visible)."""
        self._text = text
        self._reposition()

        # Atura qualsevol animació en curs
        self._anim.stop()
        self._hide_timer.stop()

        # Fade in ràpid
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._anim.setDuration(_FADE_MS // 2)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

        # Programa l'auto-hide
        self._hide_timer.start(_SHOW_MS)
        self.update()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _start_fade_out(self):
        self._anim.stop()
        self._anim.setDuration(_FADE_MS)
        self._anim.setStartValue(self.windowOpacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_finished(self):
        if self.windowOpacity() < 0.01:
            self.hide()

    def _reposition(self):
        """Ajusta posició i mida al text actual, centrat horitzontalment al pare."""
        parent = self.parentWidget()
        if not parent:
            return
        fm    = QFontMetrics(self._font)
        tw    = fm.horizontalAdvance(self._text)
        th    = fm.height()
        w     = tw + _PAD_H * 2
        h     = th + _PAD_V * 2
        px    = (parent.width() - w) // 2
        py    = parent.height() - h - _MARGIN
        self.setGeometry(px, py, w, h)

    def paintEvent(self, event):
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Fons — píndola arrodonida
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), _RADIUS, _RADIUS)
        painter.fillPath(path, _BG_COLOR)

        # Text centrat
        painter.setFont(self._font)
        painter.setPen(_TEXT_COLOR)
        painter.drawText(self.rect(), Qt.AlignCenter, self._text)

    def resizeEvent(self, event):
        # Si el pare canvia de mida també cal reposicionar
        self._reposition()
