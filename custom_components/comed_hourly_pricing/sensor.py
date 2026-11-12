"""Sensors for ComEd Hourly Pricing."""

import logging

# Home Assistant 2026.10 replaced voluptuous with probatio but still answers to
# the old name for custom integrations, so this works before and after.
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, CONF_OFFSET, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify
import voluptuous as vol

from .coordinator import ComedPriceCoordinator

_LOGGER = logging.getLogger(__name__)

CONF_MONITORED_FEEDS = "monitored_feeds"
CONF_PRICE_UNIT = "price_unit"

# Each `type` option's API feed and default name. The names match the built-in
# integration's, so entity IDs stay the same.
FEEDS = {
    "five_minute": ("5minutefeed", "ComEd 5 Minute Price"),
    "current_hour_average": ("currenthouraverage", "ComEd Current Hour Average Price"),
}

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
    session = async_get_clientsession(hass)
    coordinators: dict[str, ComedPriceCoordinator] = {}  # One per feed.
    entities: dict[str, ComedHourlyPricingSensor] = {}  # By unique ID.
    for sensor_config in config[CONF_MONITORED_FEEDS]:
        feed = sensor_config[CONF_TYPE]
        api_type, feed_name = FEEDS[feed]
        if feed not in coordinators:
            coordinator = ComedPriceCoordinator(hass, session, api_type, feed_name)
            coordinators[feed] = coordinator
            # Without a config entry, polling doesn't stop at shutdown unless asked.
            await coordinator.async_register_shutdown()
            # If this fails, the sensors start out unavailable and it keeps trying.
            await coordinator.async_refresh()
        in_dollars = sensor_config[CONF_PRICE_UNIT] == "dollars"
        # A dollars sensor gets its own default name, so it can sit beside a
        # cents one on the same feed.
        name = sensor_config.get(CONF_NAME) or (
            f"{feed_name} USD" if in_dollars else feed_name
        )
        sensor = ComedHourlyPricingSensor(
            coordinators[feed], feed, name, sensor_config[CONF_OFFSET], in_dollars
        )
        # Home Assistant would drop it anyway, with a confusing error.
        if sensor.unique_id in entities:
            _LOGGER.error(
                "Skipping a second %s sensor named %r; give each one a different name",
                feed,
                name,
            )
            continue
        entities[sensor.unique_id] = sensor
    async_add_entities(entities.values())


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
    ) -> None:
        """Set up the sensor."""
        super().__init__(coordinator)
        self._attr_name = name
        # Lets the sensor be customized in the UI. It's based on the name, so
        # renaming the sensor in YAML creates a new entity.
        self._attr_unique_id = f"{feed}_{slugify(name)}"
        self._attr_native_unit_of_measurement = "USD/kWh" if in_dollars else "¢/kWh"
        self._offset = offset
        self._in_dollars = in_dollars

    @property
    def native_value(self) -> float:
        """Return the price plus the offset."""
        cents = self.coordinator.data.price + self._offset
        return round(cents / 100, 4) if self._in_dollars else round(cents, 2)
