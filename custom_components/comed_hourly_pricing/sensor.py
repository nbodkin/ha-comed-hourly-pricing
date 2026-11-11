"""Sensors for ComEd Hourly Pricing."""

from typing import Any

# Home Assistant 2026.10 replaced voluptuous with probatio and aliases
# `voluptuous` to it for custom integrations, so this import works before and
# after that change.
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, CONF_OFFSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    ATTR_PRICE_TIME,
    ATTRIBUTION,
    CONF_CURRENT_HOUR_AVERAGE,
    CONF_FIVE_MINUTE,
    CONF_MONITORED_FEEDS,
    CONF_PRICE_UNIT,
    CONF_SENSOR_TYPE,
    DOMAIN,
    PRICE_UNIT_CENTS,
    PRICE_UNIT_DOLLARS,
    UNIT_CENTS_PER_KWH,
    UNIT_DOLLARS_PER_KWH,
)
from .coordinator import ComedPriceCoordinator

# The names match the built-in integration's, so entity IDs stay the same.
SENSOR_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    description.key: description
    for description in (
        SensorEntityDescription(key=CONF_FIVE_MINUTE, name="ComEd 5 Minute Price"),
        SensorEntityDescription(
            key=CONF_CURRENT_HOUR_AVERAGE, name="ComEd Current Hour Average Price"
        ),
    )
}

SENSORS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SENSOR_TYPE): vol.In(list(SENSOR_DESCRIPTIONS)),
        vol.Optional(CONF_NAME): cv.string,
        # Always in cents per kWh, even when price_unit is dollars. The range
        # also rejects NaN and infinity.
        vol.Optional(CONF_OFFSET, default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=-100, max=100)
        ),
        vol.Optional(CONF_PRICE_UNIT, default=PRICE_UNIT_CENTS): vol.In(
            [PRICE_UNIT_CENTS, PRICE_UNIT_DOLLARS]
        ),
    }
)

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_MONITORED_FEEDS): [SENSORS_SCHEMA]}
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up ComEd Hourly Pricing sensors from YAML."""
    session = async_get_clientsession(hass)
    # One coordinator per feed, shared by every sensor on that feed, even across
    # separate `- platform: comed_hourly_pricing` blocks.
    coordinators: dict[str, ComedPriceCoordinator] = hass.data.setdefault(DOMAIN, {})

    entities = []
    for sensor_config in config[CONF_MONITORED_FEEDS]:
        description = SENSOR_DESCRIPTIONS[sensor_config[CONF_SENSOR_TYPE]]
        coordinator = coordinators.get(description.key)
        if coordinator is None:
            coordinator = ComedPriceCoordinator(
                hass, session, description.key, str(description.name)
            )
            coordinators[description.key] = coordinator
            # Without a config entry, nothing stops the polling when Home
            # Assistant shuts down unless the coordinator is told to.
            await coordinator.async_register_shutdown()
            # A failed first fetch isn't fatal. The sensors start out unavailable
            # and the coordinator keeps retrying on its normal schedule.
            await coordinator.async_refresh()
        entities.append(
            ComedHourlyPricingSensor(coordinator, description, sensor_config)
        )

    async_add_entities(entities)


class ComedHourlyPricingSensor(CoordinatorEntity[ComedPriceCoordinator], SensorEntity):
    """A ComEd price, in cents or dollars per kWh."""

    _attr_attribution = ATTRIBUTION
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Changes on every update, so keep it out of the history database.
    _unrecorded_attributes = frozenset({ATTR_PRICE_TIME})

    def __init__(
        self,
        coordinator: ComedPriceCoordinator,
        description: SensorEntityDescription,
        sensor_config: ConfigType,
    ) -> None:
        """Set up the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        name = sensor_config.get(CONF_NAME) or str(description.name)
        self._attr_name = name
        # Lets the sensor be renamed and customized from the UI. It's based on
        # the name, so renaming the sensor in YAML creates a new entity.
        self._attr_unique_id = f"{description.key}_{slugify(name)}"
        self._offset: float = sensor_config[CONF_OFFSET]
        self._in_dollars = sensor_config[CONF_PRICE_UNIT] == PRICE_UNIT_DOLLARS
        self._attr_native_unit_of_measurement = (
            UNIT_DOLLARS_PER_KWH if self._in_dollars else UNIT_CENTS_PER_KWH
        )

    @property
    def available(self) -> bool:
        """Return False while the feed is down, so automations don't act on an old price."""
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self) -> float | None:
        """Return the price plus the configured offset."""
        if (reading := self.coordinator.data) is None:
            return None
        cents = reading.price + self._offset
        if self._in_dollars:
            return round(cents / 100, 4)
        return round(cents, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return when ComEd published the price."""
        if (reading := self.coordinator.data) is None:
            return None
        return {ATTR_PRICE_TIME: reading.time.isoformat()}
