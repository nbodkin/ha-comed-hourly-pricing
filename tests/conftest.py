"""Shared fixtures for the ComEd Hourly Pricing tests."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components/."""
    return
