"""Gedeelde fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Laat Home Assistant de custom integration in custom_components/ vinden."""
    yield
