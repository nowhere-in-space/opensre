"""Let the analytics tests exercise the provider they are about.

This build declares itself offline, so the provider refuses to send anything -
which is the point, and is asserted on its own in
``tests/quality/test_vendor_services_are_not_contacted.py``. These tests cover
the provider's own behaviour (identity, property coercion, group stamping,
shutdown), so they run with the switch lifted.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _vendor_services_on(vendor_services_enabled: None) -> None:
    """Lift the build switch for this whole suite; see tests/conftest.py."""
