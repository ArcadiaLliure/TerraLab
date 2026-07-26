"""Sistema visual Qt que replica el llenguatge de l'onboarding."""

from __future__ import annotations

from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import QApplication

from TerraLab.common.design_tokens import PALETTE


GLOBAL_STYLESHEET = """
QWidget {
    color: #f3f5fa;
    font-family: "Segoe UI";
    font-size: 10px;
    selection-background-color: #4b3d27;
    selection-color: #ffffff;
}
QMainWindow, QDialog, QMessageBox, QInputDialog {
    background-color: #02040a;
}
QMainWindow::separator {
    background-color: #252c3b;
    width: 1px;
    height: 1px;
}
QMenuBar {
    background-color: #050811;
    color: #aab1c2;
    border-bottom: 1px solid #252c3b;
    padding: 3px 8px;
    spacing: 3px;
}
QMenuBar::item {
    background: transparent;
    padding: 6px 12px;
    border-radius: 3px;
}
QMenuBar::item:selected {
    background-color: #151b28;
    color: #d8b26a;
}
QMenu {
    background-color: #080c16;
    color: #f3f5fa;
    border: 1px solid #3b4559;
    padding: 5px;
}
QMenu::item {
    padding: 7px 26px 7px 10px;
    border-radius: 3px;
}
QMenu::item:selected {
    background-color: #201c16;
    color: #f1cd88;
}
QMenu::separator {
    background-color: #252c3b;
    height: 1px;
    margin: 5px 8px;
}
QLabel {
    color: #f3f5fa;
    background: transparent;
}
QLabel[muted="true"] {
    color: #aab1c2;
}
QLabel[eyebrow="true"] {
    color: #d8b26a;
    font-family: "Consolas";
    font-size: 9px;
    font-weight: 600;
}
QGroupBox {
    color: #d8b26a;
    background-color: #080c16;
    border: 1px solid #252c3b;
    border-radius: 7px;
    margin-top: 15px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: #d8b26a;
    background-color: #080c16;
}
QFrame[card="true"], QFrame#panel, QFrame#optionsPanel {
    background-color: #080c16;
    border: 1px solid #252c3b;
    border-radius: 8px;
}
QFrame#assetRow {
    background-color: #0d111c;
    border: 1px solid #252c3b;
    border-radius: 7px;
}
QPushButton, QToolButton {
    background-color: #0d111c;
    color: #f3f5fa;
    border: 1px solid #3b4559;
    border-radius: 5px;
    padding: 5px 10px;
    min-height: 18px;
}
QPushButton:hover, QToolButton:hover {
    background-color: #151b28;
    color: #f1cd88;
    border-color: #d8b26a;
}
QPushButton:pressed, QToolButton:pressed {
    background-color: #211c14;
    border-color: #f1cd88;
}
QPushButton:checked, QToolButton:checked {
    background-color: #d8b26a;
    color: #02040a;
    border-color: #f1cd88;
    font-weight: 600;
}
QPushButton:focus, QToolButton:focus {
    border: 1px solid #f1cd88;
}
QPushButton:disabled, QToolButton:disabled {
    background-color: #080c16;
    color: #596176;
    border-color: #202635;
}
QPushButton#primaryButton {
    background-color: #d8b26a;
    color: #02040a;
    border-color: #f1cd88;
    font-weight: 600;
}
QPushButton#primaryButton:hover {
    background-color: #f1cd88;
}
QPushButton#removeLayerDataButton {
    color: #ef8f89;
    border-color: #68343a;
}
QPushButton#removeLayerDataButton:hover {
    background-color: #2d171c;
    border-color: #ef8f89;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox,
QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit, QDateTimeEdit {
    background-color: #080c16;
    color: #f3f5fa;
    border: 1px solid #3b4559;
    border-radius: 5px;
    padding: 4px 7px;
    min-height: 18px;
}
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QComboBox:hover,
QSpinBox:hover, QDoubleSpinBox:hover, QDateEdit:hover, QTimeEdit:hover,
QDateTimeEdit:hover {
    border-color: #6d6250;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QTimeEdit:focus,
QDateTimeEdit:focus {
    background-color: #0d111c;
    border-color: #d8b26a;
}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background-color: #050811;
    color: #596176;
    border-color: #202635;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border-left: 1px solid #3b4559;
    background-color: #0d111c;
}
QComboBox QAbstractItemView {
    background-color: #080c16;
    color: #f3f5fa;
    border: 1px solid #3b4559;
    outline: 0;
    selection-background-color: #211c14;
    selection-color: #f1cd88;
}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    background-color: #0d111c;
    border-left: 1px solid #3b4559;
    width: 16px;
}
QCheckBox, QRadioButton {
    color: #f3f5fa;
    spacing: 7px;
    background: transparent;
}
QCheckBox:disabled, QRadioButton:disabled {
    color: #70798d;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 13px;
    height: 13px;
    background-color: #050811;
    border: 1px solid #596176;
}
QCheckBox::indicator {
    border-radius: 3px;
}
QRadioButton::indicator {
    border-radius: 7px;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: #d8b26a;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background-color: #d8b26a;
    border: 2px solid #f1cd88;
}
QSlider::groove:horizontal {
    background-color: #050811;
    border: 1px solid #252c3b;
    height: 4px;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background-color: #6c5734;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background-color: #d8b26a;
    border: 1px solid #f1cd88;
    width: 12px;
    margin: -5px 0;
    border-radius: 6px;
}
QSlider::handle:horizontal:hover {
    background-color: #f1cd88;
}
QProgressBar {
    background-color: #050811;
    color: #f3f5fa;
    border: 1px solid #252c3b;
    border-radius: 4px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #d8b26a;
    border-radius: 3px;
}
QTabWidget::pane {
    background-color: #080c16;
    border: 1px solid #252c3b;
    border-radius: 0 7px 7px 7px;
}
QTabBar::tab {
    background-color: #050811;
    color: #aab1c2;
    border: 1px solid #252c3b;
    border-bottom: none;
    padding: 7px 14px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #0d111c;
    color: #f1cd88;
    border-color: #6d6250;
}
QTabBar::tab:hover:!selected {
    background-color: #151b28;
    color: #f3f5fa;
}
QTreeView, QTreeWidget, QListView, QListWidget, QTableView, QTableWidget {
    background-color: #050811;
    alternate-background-color: #080c16;
    color: #f3f5fa;
    border: 1px solid #252c3b;
    border-radius: 5px;
    outline: 0;
}
QTreeView::item, QTreeWidget::item, QListView::item, QListWidget::item {
    min-height: 22px;
    padding: 2px 4px;
}
QTreeView::item:hover, QTreeWidget::item:hover,
QListView::item:hover, QListWidget::item:hover {
    background-color: #151b28;
}
QTreeView::item:selected, QTreeWidget::item:selected,
QListView::item:selected, QListWidget::item:selected,
QTableView::item:selected, QTableWidget::item:selected {
    background-color: #211c14;
    color: #f1cd88;
}
QHeaderView::section {
    background-color: #0d111c;
    color: #d8b26a;
    border: none;
    border-right: 1px solid #252c3b;
    border-bottom: 1px solid #252c3b;
    padding: 6px;
    font-weight: 600;
}
QScrollArea, QAbstractScrollArea {
    background-color: transparent;
    border: none;
}
QScrollBar:vertical {
    background-color: #050811;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #3b4559;
    min-height: 28px;
    border-radius: 4px;
}
QScrollBar:horizontal {
    background-color: #050811;
    height: 10px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background-color: #3b4559;
    min-width: 28px;
    border-radius: 4px;
}
QScrollBar::handle:hover {
    background-color: #6d6250;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
}
QStatusBar {
    background-color: #050811;
    color: #aab1c2;
    border-top: 1px solid #252c3b;
}
QToolTip {
    background-color: #151b28;
    color: #f3f5fa;
    border: 1px solid #d8b26a;
    padding: 5px;
}
"""


CONTROL_GROUP_STYLESHEET = """
QGroupBox {
    background-color: #080c16;
    color: #d8b26a;
    border: 1px solid #252c3b;
    border-radius: 7px;
    margin-top: 14px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 6px;
    color: #d8b26a;
    background-color: #080c16;
    font-family: "Consolas";
    font-size: 9px;
    font-weight: 600;
}
QLabel, QCheckBox {
    color: #f3f5fa;
    background: transparent;
    font-size: 10px;
    font-style: normal;
    font-weight: normal;
}
"""


CONTROL_PANEL_STYLESHEET = """
QFrame#controlFrame {
    background-color: rgba(2, 4, 10, 244);
    color: #f3f5fa;
    border-top: 1px solid #3b4559;
    border-left: none;
    border-right: none;
    border-bottom: none;
}
QFrame#controlFrame QFrame {
    color: #f3f5fa;
}
QFrame#controlFrame QLabel {
    color: #f3f5fa;
    background: transparent;
}
QFrame#controlFrame QLineEdit {
    background-color: #080c16;
    color: #f3f5fa;
    border: 1px solid #3b4559;
    border-radius: 4px;
    padding: 3px 6px;
}
QFrame#controlFrame QLineEdit:focus {
    border-color: #d8b26a;
}
QFrame#controlFrame QPushButton {
    background-color: #0d111c;
    color: #f3f5fa;
    border: 1px solid #3b4559;
    border-radius: 5px;
    padding: 4px 8px;
}
QFrame#controlFrame QPushButton:hover {
    background-color: #151b28;
    color: #f1cd88;
    border-color: #d8b26a;
}
QFrame#controlFrame QPushButton:checked {
    background-color: #d8b26a;
    color: #02040a;
    border-color: #f1cd88;
}
QFrame#controlFrame QCheckBox {
    color: #f3f5fa;
}
"""


QUICK_TOOLBAR_STYLESHEET = """
QFrame#quickToolbar {
    background-color: #050811;
    border: none;
    border-bottom: 1px solid #252c3b;
}
QFrame#quickToolbar QPushButton {
    background-color: transparent;
    color: #aab1c2;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 4px 11px;
    min-height: 20px;
    font-size: 10px;
}
QFrame#quickToolbar QPushButton:hover {
    background-color: #151b28;
    color: #f1cd88;
    border-color: #3b4559;
}
QFrame#quickToolbar QPushButton:pressed,
QFrame#quickToolbar QPushButton:checked {
    background-color: #211c14;
    color: #f1cd88;
    border-color: #d8b26a;
}
QFrame#quickToolbar QPushButton:focus {
    color: #f1cd88;
    border-color: #f1cd88;
}
QFrame#quickToolbar QLabel#toolbarBrand {
    color: #d8b26a;
    font-family: "Consolas";
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
}
"""


CONTROL_TABS_STYLESHEET = """
QTabWidget#mainControlTabs {
    background-color: #02040a;
}
QTabWidget#mainControlTabs::pane {
    background-color: #050811;
    border: 1px solid #252c3b;
    border-radius: 0 7px 7px 7px;
}
QTabWidget#mainControlTabs QTabBar::tab {
    min-width: 74px;
    padding: 6px 14px;
    background-color: #050811;
    color: #aab1c2;
    border: 1px solid #252c3b;
    border-bottom: none;
}
QTabWidget#mainControlTabs QTabBar::tab:selected {
    background-color: #0d111c;
    color: #f1cd88;
    border-color: #6d6250;
}
QTabWidget#mainControlTabs QTabBar::tab:hover:!selected {
    background-color: #151b28;
    color: #f3f5fa;
}
QTabWidget#mainControlTabs QScrollArea,
QTabWidget#mainControlTabs QScrollArea > QWidget > QWidget {
    background-color: #050811;
}
"""


CONTROL_DRAWER_STYLESHEET = """
QFrame#controlDrawer {
    background-color: #050811;
    border: none;
    border-left: 1px solid #252c3b;
}
QFrame#drawerHeader {
    background-color: #080c16;
    border: none;
    border-bottom: 1px solid #252c3b;
}
QLabel#drawerTitle {
    color: #f1cd88;
    font-family: "Consolas";
    font-size: 11px;
    font-weight: 600;
}
QPushButton#drawerCloseButton {
    background: transparent;
    color: #aab1c2;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px;
    min-height: 18px;
}
QPushButton#drawerCloseButton:hover,
QPushButton#drawerCloseButton:focus {
    color: #f1cd88;
    border-color: #d8b26a;
    background-color: #151b28;
}
QStackedWidget#controlDrawerStack,
QWidget[drawerPage="true"] {
    background-color: #050811;
    border: none;
}
QWidget[drawerPage="true"] QLabel {
    color: #aab1c2;
    background: transparent;
    font-size: 9px;
}
QWidget[drawerPage="true"] QLabel[sectionTitle="true"] {
    color: #d8b26a;
    font-family: "Consolas";
    font-size: 9px;
    font-weight: 600;
}
QWidget[drawerPage="true"] QPushButton,
QWidget[drawerPage="true"] QLineEdit,
QWidget[drawerPage="true"] QComboBox,
QWidget[drawerPage="true"] QSpinBox,
QWidget[drawerPage="true"] QDoubleSpinBox {
    min-height: 14px;
    padding: 2px 6px;
    font-size: 9px;
}
QWidget[drawerPage="true"] QCheckBox {
    min-height: 14px;
    spacing: 5px;
    font-size: 9px;
}
QWidget[drawerPage="true"] QCheckBox::indicator {
    width: 11px;
    height: 11px;
}
QWidget[drawerPage="true"] QSlider {
    min-height: 12px;
    max-height: 16px;
}
QFrame#drawerRail {
    background-color: #02040a;
    border: none;
    border-left: 1px solid #252c3b;
}
QFrame#drawerRail QPushButton {
    background-color: transparent;
    color: #aab1c2;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 0;
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    font-family: "Segoe UI Symbol";
    font-size: 17px;
}
QFrame#drawerRail QPushButton:hover {
    background-color: #151b28;
    color: #f1cd88;
    border-color: #3b4559;
}
QFrame#drawerRail QPushButton:checked,
QFrame#drawerRail QPushButton:focus {
    background-color: #211c14;
    color: #f1cd88;
    border-color: #d8b26a;
}
"""


COLLAPSE_BUTTON_STYLESHEET = """
QPushButton {
    background-color: #050811;
    color: #d8b26a;
    border: 1px solid #3b4559;
    border-bottom: 2px solid #050811;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
    font-family: "Consolas";
    font-size: 15px;
    font-weight: 600;
    padding-bottom: 2px;
}
QPushButton:hover {
    background-color: #151b28;
    border-color: #d8b26a;
    color: #f1cd88;
}
"""


DIALOG_STYLESHEET = GLOBAL_STYLESHEET + """
QDialog {
    background-color: #02040a;
    color: #f3f5fa;
}
QDialog QLabel#titleLabel, QDialog QLabel#dialogTitle {
    color: #f1cd88;
    font-size: 20px;
    font-weight: 600;
}
QDialog QLabel#subtitleLabel, QDialog QLabel#subtitle {
    color: #aab1c2;
}
QDialog QLabel#assetStatusOk {
    color: #79d9b4;
    font-weight: 600;
}
QDialog QLabel#assetStatusMissing {
    color: #ef8f89;
    font-weight: 600;
}
"""


CALENDAR_STYLESHEET = """
QDialog {
    background-color: #02040a;
    border: 1px solid #3b4559;
    border-radius: 6px;
}
QCalendarWidget QWidget {
    alternate-background-color: #080c16;
    color: #f3f5fa;
}
QCalendarWidget QToolButton {
    color: #d8b26a;
    icon-size: 18px;
}
QCalendarWidget QMenu {
    background-color: #080c16;
    color: #f3f5fa;
}
QCalendarWidget QSpinBox {
    color: #f3f5fa;
    background-color: #0d111c;
    selection-background-color: #4b3d27;
}
QCalendarWidget QAbstractItemView:enabled {
    color: #f3f5fa;
    background-color: #050811;
    selection-background-color: #d8b26a;
    selection-color: #02040a;
}
QCalendarWidget QAbstractItemView:disabled {
    color: #596176;
}
"""


def scoped_dialog_override(object_name: str) -> str:
    """Genera una capa final d'estil per damunt de fulls locals antics."""

    root = f"QDialog#{object_name}"
    return f"""
{root} {{
    background-color: #02040a;
    color: #f3f5fa;
}}
{root} QLabel, {root} QCheckBox, {root} QRadioButton {{
    color: #f3f5fa;
    background: transparent;
}}
{root} QLabel#titleLabel, {root} QLabel#dialogTitle {{
    color: #f1cd88;
}}
{root} QLabel#subtitleLabel, {root} QLabel#subtitle,
{root} QLabel#effectiveSource, {root} QLabel#footerNote {{
    color: #aab1c2;
}}
{root} QGroupBox {{
    color: #d8b26a;
    background-color: #080c16;
    border-color: #252c3b;
}}
{root} QGroupBox::title {{
    color: #d8b26a;
    background-color: #080c16;
}}
{root} QFrame#assetRow, {root} QFrame#optionsPanel {{
    background-color: #0d111c;
    border-color: #252c3b;
}}
{root} QTabWidget::pane {{
    background-color: #080c16;
    border-color: #252c3b;
}}
{root} QTabBar::tab {{
    background-color: #050811;
    color: #aab1c2;
    border-color: #252c3b;
}}
{root} QTabBar::tab:selected {{
    background-color: #0d111c;
    color: #f1cd88;
    border-color: #6d6250;
}}
{root} QTabBar::tab:hover:!selected {{
    background-color: #151b28;
    color: #f3f5fa;
}}
{root} QPushButton {{
    background-color: #0d111c;
    color: #f3f5fa;
    border-color: #3b4559;
}}
{root} QPushButton:hover {{
    background-color: #151b28;
    color: #f1cd88;
    border-color: #d8b26a;
}}
{root} QPushButton:pressed {{
    background-color: #211c14;
    border-color: #f1cd88;
}}
{root} QPushButton:disabled {{
    background-color: #080c16;
    color: #596176;
    border-color: #202635;
}}
{root} QPushButton#primaryButton {{
    background-color: #d8b26a;
    color: #02040a;
    border-color: #f1cd88;
}}
{root} QLineEdit, {root} QTextEdit, {root} QPlainTextEdit,
{root} QComboBox, {root} QSpinBox, {root} QDoubleSpinBox {{
    background-color: #080c16;
    color: #f3f5fa;
    border-color: #3b4559;
}}
{root} QTreeWidget, {root} QTreeView, {root} QListWidget,
{root} QListView, {root} QTableWidget, {root} QTableView {{
    background-color: #050811;
    alternate-background-color: #080c16;
    color: #f3f5fa;
    border-color: #252c3b;
}}
{root} QHeaderView::section {{
    background-color: #0d111c;
    color: #d8b26a;
    border-color: #252c3b;
}}
{root} QScrollBar:vertical, {root} QScrollBar:horizontal {{
    background-color: #050811;
}}
{root} QScrollBar::handle:vertical,
{root} QScrollBar::handle:horizontal {{
    background-color: #3b4559;
}}
{root} QToolTip {{
    background-color: #151b28;
    color: #f3f5fa;
    border-color: #d8b26a;
}}
"""


def apply_onboarding_theme(app: QApplication | None) -> None:
    """Aplica de manera idempotent paleta, font i QSS globals."""

    if app is None:
        return
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(PALETTE.void))
    palette.setColor(QPalette.WindowText, QColor(PALETTE.text))
    palette.setColor(QPalette.Base, QColor(PALETTE.surface))
    palette.setColor(QPalette.AlternateBase, QColor(PALETTE.surface_raised))
    palette.setColor(QPalette.ToolTipBase, QColor(PALETTE.surface_hover))
    palette.setColor(QPalette.ToolTipText, QColor(PALETTE.text))
    palette.setColor(QPalette.Text, QColor(PALETTE.text))
    palette.setColor(QPalette.Button, QColor(PALETTE.surface_raised))
    palette.setColor(QPalette.ButtonText, QColor(PALETTE.text))
    palette.setColor(QPalette.BrightText, QColor(PALETTE.gold_bright))
    palette.setColor(QPalette.Highlight, QColor(PALETTE.gold))
    palette.setColor(QPalette.HighlightedText, QColor(PALETTE.void))
    palette.setColor(
        QPalette.Disabled,
        QPalette.Text,
        QColor(PALETTE.text_muted),
    )
    palette.setColor(
        QPalette.Disabled,
        QPalette.ButtonText,
        QColor(PALETTE.text_muted),
    )
    app.setPalette(palette)
    app.setStyleSheet(GLOBAL_STYLESHEET)
