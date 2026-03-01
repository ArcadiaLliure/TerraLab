import pytest
import sys
with open("test_err.txt", "w", encoding="utf-8") as f:
    class MyPlugin:
        def pytest_runtest_logreport(self, report):
            if report.failed:
                f.write(str(report.longrepr) + "\n")
    pytest.main(["tests/test_providers.py"], plugins=[MyPlugin()])
