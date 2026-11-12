"""The ComEd Hourly Pricing integration.

Each sensor is set up either in the UI, as its own config entry, or in YAML
under `sensor:`. Both kinds share one request per feed.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TYPE, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .coordinator import ComedPriceCoordinator, async_get_feed

PLATFORMS = [Platform.SENSOR]

type ComedConfigEntry = ConfigEntry[ComedPriceCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: ComedConfigEntry) -> bool:
    """Set up a sensor added in the UI."""
    try:
        entry.runtime_data = await async_get_feed(hass, entry.data[CONF_TYPE])
    except Exception as err:
        # Starting a feed only fails unexpectedly, so try again later.
        raise ConfigEntryNotReady(f"couldn't start the ComEd feed: {err!r}") from err
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ComedConfigEntry) -> bool:
    """Remove a sensor added in the UI."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
