"""Tests for the ComEd Hourly Pricing sensors."""

from datetime import timedelta
import json
import logging
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.comed_hourly_pricing.coordinator import API_URL

DOMAIN = "comed_hourly_pricing"

FIVE_MINUTE_URL = f"{API_URL}?type=5minutefeed"
HOUR_AVERAGE_URL = f"{API_URL}?type=currenthouraverage"
FIVE_MINUTE_ENTITY = "sensor.comed_5_minute_price"
HOUR_AVERAGE_ENTITY = "sensor.comed_current_hour_average_price"

FIVE_MINUTE = {"type": "five_minute"}
HOUR_AVERAGE = {"type": "current_hour_average"}

# What the real API sends for a request it doesn't understand: HTTP 200, HTML.
SERVLET_ERROR_PAGE = "Servlet Feed 2.95-SGalertsSync"


def feed(*prices: Any, newest_age: timedelta = timedelta(minutes=5)) -> str:
    """Build an API response like the real one: newest first, 5 minutes apart."""
    newest = dt_util.utcnow() - newest_age
    return json.dumps(
        [
            {
                "millisUTC": str(
                    int((newest - timedelta(minutes=5 * i)).timestamp() * 1000)
                ),
                "price": str(price),
            }
            for i, price in enumerate(prices)
        ]
    )


async def setup_sensors(hass: HomeAssistant, *sensors: dict[str, Any]) -> None:
    """Set up the platform with the given monitored_feeds entries."""
    assert await async_setup_component(
        hass,
        "sensor",
        {"sensor": {"platform": DOMAIN, "monitored_feeds": list(sensors)}},
    )
    await hass.async_block_till_done()


async def next_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Move time forward to the next scheduled fetch."""
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
def healthy_api(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Serve normal responses for both feeds."""
    aioclient_mock.get(FIVE_MINUTE_URL, text=feed(3.7, 3.4, 4.2))
    aioclient_mock.get(HOUR_AVERAGE_URL, text=feed(4.3))
    return aioclient_mock


@pytest.mark.usefixtures("healthy_api")
async def test_both_feeds(hass: HomeAssistant) -> None:
    """Both sensors show their price, with the same entity IDs as the built-in integration."""
    await setup_sensors(hass, FIVE_MINUTE, HOUR_AVERAGE)

    five_minute = hass.states.get(FIVE_MINUTE_ENTITY)
    assert five_minute.state == "3.7"
    assert five_minute.attributes[ATTR_UNIT_OF_MEASUREMENT] == "¢/kWh"
    assert five_minute.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT
    assert five_minute.attributes[ATTR_ATTRIBUTION] == (
        "Data provided by ComEd Hourly Pricing service"
    )

    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == "4.3"


@pytest.mark.usefixtures("healthy_api")
async def test_name_and_offset(hass: HomeAssistant) -> None:
    """A custom name and a cents offset are applied, rounded to 2 places."""
    await setup_sensors(
        hass, {**FIVE_MINUTE, "name": "Supply plus adders", "offset": 1.234}
    )

    assert hass.states.get("sensor.supply_plus_adders").state == "4.93"


@pytest.mark.usefixtures("healthy_api")
async def test_dollars(hass: HomeAssistant) -> None:
    """price_unit: dollars reports USD/kWh for the Energy dashboard; offset stays in cents."""
    await setup_sensors(
        hass,
        {**FIVE_MINUTE, "name": "ComEd USD", "price_unit": "dollars"},
        {
            **FIVE_MINUTE,
            "name": "ComEd USD offset",
            "price_unit": "dollars",
            "offset": 1,
        },
    )

    state = hass.states.get("sensor.comed_usd")
    assert state.state == "0.037"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD/kWh"
    assert hass.states.get("sensor.comed_usd_offset").state == "0.047"


async def test_one_request_per_feed(
    hass: HomeAssistant,
    healthy_api: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Sensors on the same feed share one request per poll."""
    await setup_sensors(
        hass,
        FIVE_MINUTE,
        {**FIVE_MINUTE, "name": "ComEd USD", "price_unit": "dollars"},
        HOUR_AVERAGE,
    )
    assert healthy_api.call_count == 2

    await next_poll(hass, freezer)
    assert healthy_api.call_count == 4


async def test_newest_price_wins(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The newest price is used even if the API stops listing it first."""
    rows = json.loads(feed(3.7, 3.4, 4.2))
    aioclient_mock.get(FIVE_MINUTE_URL, text=json.dumps(rows[::-1]))
    await setup_sensors(hass, FIVE_MINUTE)

    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "3.7"


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"text": SERVLET_ERROR_PAGE}, id="html_with_http_200"),
        pytest.param({"text": "[]"}, id="empty_list"),
        pytest.param({"text": '{"error": "nope"}'}, id="not_a_list"),
        pytest.param(
            {"text": '[{"millisUTC": "1794438000000", "price": null}]'}, id="null_price"
        ),
        pytest.param({"text": feed("NaN")}, id="nan_price"),
        pytest.param({"text": '[{"price": "3.7"}]'}, id="missing_timestamp"),
        pytest.param(
            {"text": feed(3.7, newest_age=timedelta(minutes=40))}, id="stale_feed"
        ),
        pytest.param({"status": 503, "text": "Service Unavailable"}, id="http_503"),
        pytest.param(
            {"exc": aiohttp.ClientError("connection reset")}, id="client_error"
        ),
        pytest.param({"exc": TimeoutError()}, id="timeout"),
    ],
)
async def test_bad_response_makes_sensor_unavailable(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    response: dict[str, Any],
) -> None:
    """Every kind of bad response leaves the sensor unavailable, with no traceback."""
    aioclient_mock.get(FIVE_MINUTE_URL, **response)
    await setup_sensors(hass, FIVE_MINUTE)

    assert hass.states.get(FIVE_MINUTE_ENTITY).state == STATE_UNAVAILABLE
    assert "Error fetching ComEd 5 Minute Price data" in caplog.text
    # The coordinator logs the full error at DEBUG on purpose. An error-level
    # traceback would mean an exception got past the coordinator, which is how
    # the built-in integration fails on an empty response.
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.exc_info
    ]


async def test_outage_and_recovery(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
    freezer: FrozenDateTimeFactory,
) -> None:
    """During an outage the old price is dropped and only one error is logged."""
    caplog.set_level(logging.INFO)
    aioclient_mock.get(FIVE_MINUTE_URL, text=feed(3.7))
    await setup_sensors(hass, FIVE_MINUTE)
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "3.7"

    aioclient_mock.clear_requests()
    aioclient_mock.get(FIVE_MINUTE_URL, exc=aiohttp.ClientError("connection reset"))
    for _ in range(3):
        await next_poll(hass, freezer)
        assert hass.states.get(FIVE_MINUTE_ENTITY).state == STATE_UNAVAILABLE
    assert caplog.text.count("Error fetching ComEd 5 Minute Price data") == 1

    aioclient_mock.clear_requests()
    aioclient_mock.get(FIVE_MINUTE_URL, text=feed(2.5))
    await next_poll(hass, freezer)
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "2.5"
    assert "Fetching ComEd 5 Minute Price data recovered" in caplog.text


async def test_recovers_after_failed_startup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """If the API is down at startup, the sensor appears once it comes back."""
    aioclient_mock.get(FIVE_MINUTE_URL, exc=aiohttp.ClientError("connection refused"))
    await setup_sensors(hass, FIVE_MINUTE)
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == STATE_UNAVAILABLE

    aioclient_mock.clear_requests()
    aioclient_mock.get(FIVE_MINUTE_URL, text=feed(3.7))
    await next_poll(hass, freezer)
    assert hass.states.get(FIVE_MINUTE_ENTITY).state == "3.7"


@pytest.mark.usefixtures("healthy_api")
async def test_unique_id(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Sensors get a unique ID, so they can be renamed and customized in the UI."""
    await setup_sensors(hass, FIVE_MINUTE)

    entry = entity_registry.async_get(FIVE_MINUTE_ENTITY)
    assert entry is not None
    assert entry.unique_id == "five_minute_comed_5_minute_price"


@pytest.mark.usefixtures("healthy_api")
@pytest.mark.parametrize(
    "sensor_config",
    [
        pytest.param({"type": "bogus"}, id="unknown_type"),
        pytest.param({**FIVE_MINUTE, "price_unit": "euros"}, id="unknown_unit"),
    ],
)
async def test_invalid_config(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    sensor_config: dict[str, Any],
) -> None:
    """Invalid YAML is rejected with a config error instead of creating sensors."""
    await setup_sensors(hass, sensor_config)

    assert hass.states.async_entity_ids("sensor") == []
    assert "Invalid config" in caplog.text
