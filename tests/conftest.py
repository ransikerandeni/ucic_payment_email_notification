import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import frappe_stub  # noqa: E402

# Install the stub before any `import frappe` inside the app runs.
frappe_stub.build_module()


@pytest.fixture(autouse=True)
def clean_state():
    frappe_stub.STATE.reset()
    yield
