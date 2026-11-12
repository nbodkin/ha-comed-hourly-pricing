"""Tests for setting up sensors from the UI."""

from datetime import timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_FRIENDLY_NAME, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.comed_hourly_pricing.config_flow import USER_SCHEMA
from custom_components.comed_hourly_pricing.coordinator import POSTING_DELAY

from .common import (
    DOMAIN,
    FIVE_MINUTE_ENTITY,
    FIVE_MINUTE_URL,
    HOUR_AVERAGE_ENTITY,
    HOUR_AVERAGE_URL,
    advance_to,
    feed,
    next_poll,
    price_at,
    utc,
)

TRANSLATIONS = (
    Path(__file__).parent.parent
    / "custom_components"
    / DOMAIN
    / "translations"
    / "en.json"
)


@pytest.fixture(autouse=True)
def healthy_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Serve normal responses for both feeds."""
    aioclient_mock.get(FIVE_MINUTE_URL, text=feed(3.7))
    aioclient_mock.get(HOUR_AVERAGE_URL, text=feed(4.3))
    return aioclient_mock


async def submit(hass: HomeAssistant, **user_input: Any) -> dict[str, Any]:
    """Fill in the setup screen and return what it does next."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"type": "five_minute", "price_unit": "cents", "offset": 0, **user_input},
    )


async def add_sensor(hass: HomeAssistant, **user_input: Any) -> ConfigEntry:
    """Go through the setup screen and return the entry it creates."""
    result = await submit(hass, **user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def change_offset(hass: HomeAssistant, entry: ConfigEntry, offset: float) -> None:
    """Change a sensor's offset with its Configure screen."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"offset": offset}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


async def test_defaults(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """With nothing changed, the sensor matches the YAML default's."""
    entry = await add_sensor(hass)

    assert entry.title == "ComEd 5 Minute Price"
    assert entry.data == {
        "type": "five_minute",
        "price_unit": "cents",
        "name": "ComEd 5 Minute Price",
    }
    assert entry.options == {"offset": 0}
    state = hass.states.get(FIVE_MINUTE_ENTITY)
    assert state.state == "3.7"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "¢/kWh"
    assert entity_registry.async_get(FIVE_MINUTE_ENTITY).unique_id == entry.entry_id


async def test_dollars_offset_and_name(hass: HomeAssistant) -> None:
    """The unit, offset and name are all used."""
    entry = await add_sensor(
        hass, price_unit="dollars", offset=1.5, name="  Supply plus delivery  "
    )

    assert entry.title == "Supply plus delivery"
    state = hass.states.get("sensor.supply_plus_delivery")
    assert state.state == "0.052"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD/kWh"


async def test_default_dollars_name(hass: HomeAssistant) -> None:
    """A dollars sensor's default name gets USD, like in YAML."""
    entry = await add_sensor(hass, type="current_hour_average", price_unit="dollars")

    assert entry.title == "ComEd Current Hour Average Price USD"
    assert (
        hass.states.get("sensor.comed_current_hour_average_price_usd").state == "0.043"
    )


async def test_change_offset(hass: HomeAssistant) -> None:
    """The offset can be changed later, and takes effect right away."""
    entry = await add_sensor(hass, offset=1)
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "4.7"

    await change_offset(hass, entry, 2.25)

    assert entry.options == {"offset": 2.25}
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "5.95"


async def test_remove(hass: HomeAssistant) -> None:
    """Deleting the entry removes its sensor."""
    entry = await add_sensor(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(FIVE_MINUTE_ENTITY) is None


async def test_same_feed_twice(
    hass: HomeAssistant,
    healthy_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two sensors on one feed both appear, and share its requests."""
    await add_sensor(hass)
    await add_sensor(hass, name="Supply plus delivery", offset=8.5)

    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "3.7"
    assert hass.states.get("sensor.supply_plus_delivery").state == "12.2"
    assert healthy_api.call_count == 1

    await next_poll(hass, freezer)
    assert healthy_api.call_count == 2


async def test_ui_and_yaml_share_a_feed(
    hass: HomeAssistant,
    healthy_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A sensor from the UI and one from YAML on the same feed share its requests."""
    assert await async_setup_component(
        hass,
        "sensor",
        {
            "sensor": {
                "platform": DOMAIN,
                "monitored_feeds": [{"type": "five_minute"}],
            }
        },
    )
    await hass.async_block_till_done()
    await add_sensor(hass, price_unit="dollars")

    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "3.7"
    assert hass.states.get(f"{FIVE_MINUTE_ENTITY}_usd").state == "0.037"
    assert healthy_api.call_count == 1

    await next_poll(hass, freezer)
    assert healthy_api.call_count == 2


async def test_name_in_use(hass: HomeAssistant) -> None:
    """A second sensor with the same name is asked for a different one."""
    await add_sensor(hass)

    result = await submit(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "name_in_use"}
    assert result["description_placeholders"] == {"entity_id": FIVE_MINUTE_ENTITY}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"type": "five_minute", "price_unit": "cents", "offset": 0, "name": "Other"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_name_taken_by_old_yaml_sensor(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """A YAML sensor's leftover entity still holds its name, so it's asked for."""
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "five_minute_comed_5_minute_price",
        suggested_object_id="comed_5_minute_price",
    )

    result = await submit(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"name": "name_in_use"}


async def test_sensor_added_to_idle_feed_gets_a_fresh_price(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A sensor added after its feed went unused doesn't show the old price."""
    entry = await add_sensor(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    freezer.tick(timedelta(hours=3))
    aioclient_mock.clear_requests()
    aioclient_mock.get(FIVE_MINUTE_URL, text=feed(9.9))
    await add_sensor(hass)

    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "9.9"
    assert aioclient_mock.call_count == 1


async def test_changing_offset_keeps_the_next_check(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reloading for a new offset doesn't put off the next price."""
    freezer.move_to("2026-11-11T23:37:00Z")
    aioclient_mock.clear_requests()
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:30:00Z", 5.2))
    entry = await add_sensor(hass, type="current_hour_average")
    assert aioclient_mock.call_count == 1

    await advance_to(hass, freezer, utc("2026-11-11T23:40:00Z"))
    await change_offset(hass, entry, 1)
    assert aioclient_mock.call_count == 1

    aioclient_mock.clear_requests()
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:35:00Z", 4.0))
    due = utc("2026-11-11T23:35:00Z") + POSTING_DELAY
    await advance_to(hass, freezer, due + timedelta(seconds=1))
    assert aioclient_mock.call_count == 1
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == "5.0"


async def test_failed_feed_start_is_retried(hass: HomeAssistant) -> None:
    """If a feed can't start, the entry retries instead of loading empty."""
    with patch(
        "homeassistant.util.dt.async_get_time_zone", side_effect=OSError("disk error")
    ):
        entry = await add_sensor(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.states.get(FIVE_MINUTE_ENTITY) is None

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "3.7"


async def test_renaming_entry_keeps_sensor_name(hass: HomeAssistant) -> None:
    """The entry's title is just its label; the sensor keeps its own name."""
    entry = await add_sensor(hass)
    hass.config_entries.async_update_entry(entry, title="Supply price")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(FIVE_MINUTE_ENTITY)
    assert state.attributes[ATTR_FRIENDLY_NAME] == "ComEd 5 Minute Price"


def test_every_field_and_choice_has_text() -> None:
    """The setup screen has a label for each field and each choice."""
    strings = json.loads(TRANSLATIONS.read_text())
    step = strings["config"]["step"]["user"]
    fields = {str(key) for key in USER_SCHEMA.schema}

    assert set(step["data"]) == fields
    assert set(step["data_description"]) == fields
    for key, selector in USER_SCHEMA.schema.items():
        config = selector.config
        if "translation_key" in config:
            choices = strings["selector"][config["translation_key"]]["options"]
            assert set(choices) == set(config["options"]), key
