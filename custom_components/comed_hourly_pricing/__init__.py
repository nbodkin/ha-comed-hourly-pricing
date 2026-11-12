"""The ComEd Hourly Pricing integration.

Each sensor is set up either in the UI, as its own config entry, or in YAML
under `sensor:`. Both kinds share one request per feed.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a sensor added in the UI."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove a sensor added in the UI."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
