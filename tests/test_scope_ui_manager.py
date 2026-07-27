from TerraLab.widgets.scope_ui_manager import ScopeUIManager


class _Value:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _Combo:
    def __init__(self, text):
        self._text = text

    def currentText(self):
        return self._text


class _Button:
    def __init__(self):
        self.enabled = None

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)


class _WidgetStub:
    def __init__(self):
        self.scope_ra_h_spin = _Value(5)
        self.scope_ra_m_spin = _Value(30)
        self.scope_ra_s_spin = _Value(0.0)
        self.scope_dec_sign_combo = _Combo("-")
        self.scope_dec_d_spin = _Value(12)
        self.scope_dec_m_spin = _Value(15)
        self.scope_dec_s_spin = _Value(0.0)
        self.btn_scope_activate = _Button()
        self.btn_scope_exit = _Button()
        self.coordinate_inputs = None
        self.goto_request = None

    def _set_scope_coord_inputs(self, ra, dec):
        self.coordinate_inputs = (ra, dec)

    def request_scope_goto(self, ra, dec):
        self.goto_request = (ra, dec)


def test_scope_ui_manager_delegates_coordinate_conversion_to_compute():
    widget = _WidgetStub()
    ScopeUIManager(widget).goto_radec()

    assert widget.coordinate_inputs == (82.5, -12.25)
    assert widget.goto_request == (82.5, -12.25)


def test_scope_ui_manager_updates_local_button_state():
    widget = _WidgetStub()
    manager = ScopeUIManager(widget)

    manager.sync_ui_state(True)
    assert widget.btn_scope_activate.enabled is False
    assert widget.btn_scope_exit.enabled is True

    manager.sync_ui_state(False)
    assert widget.btn_scope_activate.enabled is True
    assert widget.btn_scope_exit.enabled is False
