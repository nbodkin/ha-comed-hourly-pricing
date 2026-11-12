"""Set up ComEd Hourly Pricing sensors from the UI."""

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME, CONF_OFFSET, CONF_TYPE
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import slugify
import voluptuous as vol

from .const import CONF_PRICE_UNIT, DOMAIN, FEEDS, default_name

# In cents per kWh, even for a dollars sensor, like the YAML option.
OFFSET = NumberSelector(
    NumberSelectorConfig(
        min=-100,
        max=100,
        step="any",
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="¢/kWh",
    )
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TYPE, default="five_minute"): SelectSelector(
            SelectSelectorConfig(
                options=list(FEEDS),
                mode=SelectSelectorMode.LIST,
                translation_key="feed",
            )
        ),
        vol.Required(CONF_PRICE_UNIT, default="cents"): SelectSelector(
            SelectSelectorConfig(
                options=["cents", "dollars"],
                mode=SelectSelectorMode.LIST,
                translation_key="price_unit",
            )
        ),
        vol.Required(CONF_OFFSET, default=0): OFFSET,
        vol.Optional(CONF_NAME): TextSelector(),
    }
)

OPTIONS_SCHEMA = vol.Schema({vol.Required(CONF_OFFSET): OFFSET})


class ComedHourlyPricingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one price sensor. Adding the integration again adds another."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which price to show, and how."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            feed = user_input[CONF_TYPE]
            price_unit = user_input[CONF_PRICE_UNIT]
            name = user_input.get(CONF_NAME, "").strip() or default_name(
                feed, price_unit == "dollars"
            )
            # Another sensor with this name would push this one's entity ID to
            # _2, and the two would be hard to tell apart.
            entity_id = f"sensor.{slugify(name)}"
            if er.async_get(self.hass).async_is_registered(
                entity_id
            ) or self.hass.states.get(entity_id):
                errors[CONF_NAME] = "name_in_use"
                placeholders["entity_id"] = entity_id
            else:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_TYPE: feed,
                        CONF_PRICE_UNIT: price_unit,
                        CONF_NAME: name,
                    },
                    options={CONF_OFFSET: user_input[CONF_OFFSET]},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, user_input or {}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> ComedHourlyPricingOptionsFlow:
        """Return the flow for changing a sensor's offset."""
        return ComedHourlyPricingOptionsFlow()


class ComedHourlyPricingOptionsFlow(OptionsFlowWithReload):
    """Change a sensor's offset, for example when delivery charges change.

    The feed and unit can't be changed here: a new unit would mix cents and
    dollars in the sensor's history. Add another sensor instead.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new offset."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
