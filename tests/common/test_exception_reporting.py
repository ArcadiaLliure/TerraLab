from __future__ import annotations

import logging

from TerraLab.common.exception_reporting import log_suppressed_exception


def test_suppressed_exception_is_observable_with_operation_context(caplog):
    with caplog.at_level(logging.DEBUG, logger=__name__):
        try:
            raise ValueError("diagnostic")
        except ValueError:
            log_suppressed_exception(__name__, "catalog.cleanup")

    record = caplog.records[-1]
    assert record.getMessage().endswith("catalog.cleanup")
    assert record.exc_info is not None
    assert record.exc_info[0] is ValueError
