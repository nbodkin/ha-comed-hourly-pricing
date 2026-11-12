"""Sensors for ComEd Hourly Pricing."""

import asyncio
import logging

# Home Assistant 2026.10 replaced voluptuous with probatio but still answers to
# the old name for custom integrations, so this works before and after.
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_OFFSET, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.hass_dict import HassKey
import voluptuous as vol

from .const import CONF_PRICE_UNIT, DOMAIN, FEEDS, default_name
from .coordinator import COMED_TIME_ZONE, ComedPriceCoordinator

_LOGGER = logging.getLogger(__name__)

# Each feed's coordinator, as the task that creates it and fetches the first
# price. Kept here so every sensor on a feed shares one request, whether it's
# from the UI or from any `platform: comed_hourly_pricing` entry in YAML.
DATA_FEEDS: HassKey[dict[str, asyncio.Task[ComedPriceCoordinator]]] = HassKey(
    f"{DOMAIN}_feeds"
)
# Unique IDs already used, across all `platform: comed_hourly_pricing` entries.
# Sensors from the UI use their entry's ID, so they can't clash.
DATA_UNIQUE_IDS: HassKey[set[str]] = HassKey(f"{DOMAIN}_unique_ids")

CONF_MONITORED_FEEDS = "monitored_feeds"

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_MONITORED_FEEDS): [
            {
                vol.Required(CONF_TYPE): vol.In(list(FEEDS)),
                vol.Optional(CONF_NAME): cv.string,
                # In cents per kWh, even when price_unit is dollars.
                vol.Optional(CONF_OFFSET, default=0.0): vol.Coerce(float),
                # The Energy dashboard reads ¢/kWh as dollars per kWh, making
                # every cost 100 times too high, so it needs dollars.
                vol.Optional(CONF_PRICE_UNIT, default="cents"): vol.In(
                    ["cents", "dollars"]
                ),
            }
        ]
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up ComEd Hourly Pricing sensors from YAML."""
    # Start every feed before waiting on any, so a slow API delays startup once.
    for sensor_config in config[CONF_MONITORED_FEEDS]:
        _feed(hass, sensor_config[CONF_TYPE])

    used_unique_ids = hass.data.setdefault(DATA_UNIQUE_IDS, set())
    entities = []
    for sensor_config in config[CONF_MONITORED_FEEDS]:
        feed = sensor_config[CONF_TYPE]
        coordinator = await _feed(hass, feed)
        in_dollars = sensor_config[CONF_PRICE_UNIT] == "dollars"
        name = sensor_config.get(CONF_NAME) or default_name(feed, in_dollars)
        sensor = ComedHourlyPricingSensor(
            coordinator, feed, name, sensor_config[CONF_OFFSET], in_dollars
        )
        # Home Assistant would drop it anyway, with a confusing error.
        if sensor.unique_id in used_unique_ids:
            _LOGGER.error(
                "Skipping a second %s sensor named %r; give each one a different name",
                feed,
                name,
            )
            continue
        used_unique_ids.add(sensor.unique_id)
        entities.append(sensor)
    async_add_entities(entities)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensor for an entry added in the UI."""
    feed = entry.data[CONF_TYPE]
    async_add_entities(
        [
            ComedHourlyPricingSensor(
                await _feed(hass, feed),
                feed,
                entry.title,
                entry.options[CONF_OFFSET],
                entry.data[CONF_PRICE_UNIT] == "dollars",
                unique_id=entry.entry_id,
            )
        ]
    )


def _feed(hass: HomeAssistant, feed: str) -> asyncio.Task[ComedPriceCoordinator]:
    """Return the task that starts a feed, starting it if needed."""
    starts = hass.data.setdefault(DATA_FEEDS, {})
    if feed not in starts:
        starts[feed] = hass.async_create_task(_async_start_feed(hass, feed))
    return starts[feed]


async def _async_start_feed(hass: HomeAssistant, feed: str) -> ComedPriceCoordinator:
    """Create a feed's coordinator and fetch its first price."""
    api_type, feed_name = FEEDS[feed]
    range_time_zone = None
    if api_type == "5minutefeed":
        # Its requests ask for a range of times, which ComEd reads in its own zone.
        range_time_zone = await dt_util.async_get_time_zone(COMED_TIME_ZONE)
        if range_time_zone is None:
            _LOGGER.warning(
                "Couldn't load the %s time zone, so the 5-minute feed will "
                "download all 24 hours of prices each time",
                COMED_TIME_ZONE,
            )
    coordinator = ComedPriceCoordinator(
        hass, async_get_clientsession(hass), api_type, feed_name, range_time_zone
    )
    # Without a config entry, polling doesn't stop at shutdown unless asked.
    await coordinator.async_register_shutdown()
    # If this fails, the sensors start out unavailable and it keeps trying.
    await coordinator.async_refresh()
    return coordinator


class ComedHourlyPricingSensor(CoordinatorEntity[ComedPriceCoordinator], SensorEntity):
    """A ComEd price, unavailable whenever the feed is down or stalled."""

    _attr_attribution = "Data provided by ComEd Hourly Pricing service"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: ComedPriceCoordinator,
        feed: str,
        name: str,
        offset: float,
        in_dollars: bool,
        unique_id: str | None = None,
    ) -> None:
        """Set up the sensor."""
        super().__init__(coordinator)
        self._attr_name = name
        # Lets the sensor be customized in the UI. From YAML it's based on the
        # name, so renaming a YAML sensor there creates a new entity.
        self._attr_unique_id = unique_id or f"{feed}_{slugify(name)}"
        self._attr_native_unit_of_measurement = "USD/kWh" if in_dollars else "¢/kWh"
        self._offset = offset
        self._in_dollars = in_dollars

    @property
    def native_value(self) -> float:
        """Return the price plus the offset."""
        cents = self.coordinator.data.price + self._offset
        return round(cents / 100, 4) if self._in_dollars else round(cents, 2)
