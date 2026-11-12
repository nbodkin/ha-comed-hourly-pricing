"""Tests for when the integration checks for new prices, and what it asks for."""

from datetime import datetime, timedelta
import json

import aiohttp
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.comed_hourly_pricing.const import (
    API_URL,
    DOMAIN,
    LATE_RETRY_INTERVAL,
    POSTING_DELAY,
    PUBLISH_INTERVAL,
)
from custom_components.comed_hourly_pricing.coordinator import (
    next_poll_delay,
    request_params,
)

FIVE_MINUTE_URL = f"{API_URL}?type=5minutefeed"
HOUR_AVERAGE_URL = f"{API_URL}?type=currenthouraverage"
HOUR_AVERAGE_ENTITY = "sensor.comed_current_hour_average_price"


def utc(text: str) -> datetime:
    """Parse an ISO time as UTC."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def price_at(stamp: str, price: float) -> str:
    """Build a one-row API response."""
    millis = int(utc(stamp).timestamp() * 1000)
    return json.dumps([{"millisUTC": str(millis), "price": str(price)}])


async def advance_to(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    """Move the clock to `when` and run whatever came due."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


async def setup_hour_average(hass: HomeAssistant) -> None:
    """Set up one current-hour-average sensor."""
    assert await async_setup_component(
        hass,
        "sensor",
        {
            "sensor": {
                "platform": DOMAIN,
                "monitored_feeds": [{"type": "current_hour_average"}],
            }
        },
    )
    await hass.async_block_till_done()


PRICE_TIME = utc("2026-11-11T12:00:00Z")
NEXT_PRICE_DUE = PRICE_TIME + PUBLISH_INTERVAL + POSTING_DELAY


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(
            PRICE_TIME + POSTING_DELAY,
            PUBLISH_INTERVAL,
            id="just_got_it: wait one interval",
        ),
        pytest.param(
            NEXT_PRICE_DUE - timedelta(seconds=30),
            timedelta(seconds=30),
            id="next_price_almost_due: wait until it is",
        ),
        pytest.param(
            NEXT_PRICE_DUE + timedelta(seconds=10),
            LATE_RETRY_INTERVAL,
            id="next_price_late: retry soon",
        ),
        pytest.param(
            NEXT_PRICE_DUE + timedelta(minutes=6),
            PUBLISH_INTERVAL,
            id="next_price_very_late: back to the normal interval",
        ),
        pytest.param(
            PRICE_TIME - timedelta(minutes=30),
            PUBLISH_INTERVAL + POSTING_DELAY,
            id="clock_behind_comed: capped",
        ),
    ],
)
def test_next_poll_delay(now: datetime, expected: timedelta) -> None:
    """The next check is timed to when ComEd should post the next price."""
    assert next_poll_delay(PRICE_TIME, now) == expected


@pytest.mark.parametrize(
    ("now", "datestart", "dateend"),
    [
        pytest.param("2026-11-11T23:30:00Z", "202611111530", "202611111830", id="CST"),
        pytest.param("2026-07-01T17:00:00Z", "202607011000", "202607011300", id="CDT"),
    ],
)
def test_five_minute_feed_asks_for_a_few_hours(
    now: str, datestart: str, dateend: str
) -> None:
    """The 5-minute feed asks for a few hours around now, in Chicago time."""
    assert request_params("five_minute", utc(now)) == {
        "type": "5minutefeed",
        "datestart": datestart,
        "dateend": dateend,
    }


def test_hour_average_needs_no_range() -> None:
    """The current-hour-average feed returns one row, so it gets no range."""
    assert request_params("current_hour_average", utc("2026-11-11T23:30:00Z")) == {
        "type": "currenthouraverage"
    }


async def test_range_is_sent(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The date range actually goes out with the request."""
    freezer.move_to("2026-11-11T23:37:00Z")
    aioclient_mock.get(FIVE_MINUTE_URL, text=price_at("2026-11-11T23:30:00Z", 4.1))
    assert await async_setup_component(
        hass,
        "sensor",
        {"sensor": {"platform": DOMAIN, "monitored_feeds": [{"type": "five_minute"}]}},
    )
    await hass.async_block_till_done()

    url = aioclient_mock.mock_calls[0][1]
    assert url.query["datestart"] == "202611111537"
    assert url.query["dateend"] == "202611111837"


async def test_checks_right_after_next_price_is_posted(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a price arrives, the next request waits until the next one is due."""
    freezer.move_to("2026-11-11T23:37:00Z")
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:30:00Z", 5.2))
    await setup_hour_average(hass)
    assert aioclient_mock.call_count == 1

    due = utc("2026-11-11T23:35:00Z") + POSTING_DELAY
    await advance_to(hass, freezer, due - timedelta(seconds=5))
    assert aioclient_mock.call_count == 1

    aioclient_mock.clear_requests()
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:35:00Z", 4.0))
    await advance_to(hass, freezer, due + timedelta(seconds=1))
    assert aioclient_mock.call_count == 1
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == "4.0"


async def test_failure_waits_a_full_interval(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed request isn't retried any faster than ComEd posts."""
    start = utc("2026-11-11T23:37:00Z")
    freezer.move_to(start)
    aioclient_mock.get(HOUR_AVERAGE_URL, exc=aiohttp.ClientError("connection reset"))
    await setup_hour_average(hass)
    assert aioclient_mock.call_count == 1

    await advance_to(hass, freezer, start + PUBLISH_INTERVAL - timedelta(seconds=5))
    assert aioclient_mock.call_count == 1

    await advance_to(hass, freezer, start + PUBLISH_INTERVAL + timedelta(seconds=1))
    assert aioclient_mock.call_count == 2
