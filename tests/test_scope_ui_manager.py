from TerraLab.widgets.scope_ui_manager import ScopeUIManager


class _WidgetStub:
    def __init__(self):
        self.calls = []

    def _sync_scope_instrument_controls_impl(self):
        self.calls.append(("sync_instrument",))
        return "ok-sync"

    def _scope_ui_goto_radec_impl(self):
        self.calls.append(("goto",))
        return "ok-goto"

    def _scope_ui_activate_impl(self):
        self.calls.append(("activate",))
        return "ok-activate"

    def _scope_ui_exit_impl(self):
        self.calls.append(("exit",))
        return "ok-exit"

    def _scope_ui_sync_state_impl(self, enabled: bool):
        self.calls.append(("sync_ui", bool(enabled)))
        return f"ok-ui-{bool(enabled)}"


def test_scope_ui_manager_delegates():
    widget = _WidgetStub()
    manager = ScopeUIManager(widget)

    assert manager.sync_instrument_controls() == "ok-sync"
    assert manager.goto_radec() == "ok-goto"
    assert manager.activate() == "ok-activate"
    assert manager.exit() == "ok-exit"
    assert manager.sync_ui_state(True) == "ok-ui-True"

    assert widget.calls == [
        ("sync_instrument",),
        ("goto",),
        ("activate",),
        ("exit",),
        ("sync_ui", True),
    ]
