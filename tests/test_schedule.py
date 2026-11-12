"""Tests for when the integration checks for new prices, and what it asks for."""

from datetime import datetime, timedelta
from email.utils import format_datetime
import json
from typing import Any

import aiohttp
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.comed_hourly_pricing.coordinator import (
    API_URL,
    LATE_RETRY_INTERVAL,
    POSTING_DELAY,
    PUBLISH_INTERVAL,
    comed_time,
    next_poll_delay,
)

FIVE_MINUTE_URL = f"{API_URL}?type=5minutefeed"
HOUR_AVERAGE_URL = f"{API_URL}?type=currenthouraverage"
HOUR_AVERAGE_ENTITY = "sensor.comed_current_hour_average_price"


def utc(text: str) -> datetime:
    """Parse an ISO time in UTC, like 2026-11-11T12:00:00Z."""
    parsed = datetime.fromisoformat(text)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"not a UTC time: {text}")
    return parsed


def price_at(stamp: str, price: float) -> str:
    """Build a one-row API response."""
    millis = int(utc(stamp).timestamp() * 1000)
    return json.dumps([{"millisUTC": str(millis), "price": str(price)}])


async def setup_feed(hass: HomeAssistant, feed: str) -> None:
    """Set up one sensor on the given feed."""
    assert await async_setup_component(
        hass,
        "sensor",
        {
            "sensor": {
                "platform": "comed_hourly_pricing",
                "monitored_feeds": [{"type": feed}],
            }
        },
    )
    await hass.async_block_till_done()


async def advance_to(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    """Move the clock to `when` and run whatever came due."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


PRICE_TIME = utc("2026-11-11T12:00:00Z")
NEXT_PRICE_DUE = PRICE_TIME + PUBLISH_INTERVAL + POSTING_DELAY


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(PRICE_TIME + POSTING_DELAY, PUBLISH_INTERVAL, id="just_got_it"),
        pytest.param(
            NEXT_PRICE_DUE - timedelta(seconds=30),
            timedelta(seconds=30),
            id="next_price_almost_due",
        ),
        pytest.param(
            NEXT_PRICE_DUE + timedelta(seconds=10),
            LATE_RETRY_INTERVAL,
            id="next_price_late",
        ),
        pytest.param(
            NEXT_PRICE_DUE + timedelta(minutes=6),
            PUBLISH_INTERVAL,
            id="next_price_very_late",
        ),
        pytest.param(
            PRICE_TIME - timedelta(hours=2),
            PUBLISH_INTERVAL + POSTING_DELAY,
            id="price_stamped_after_now",
        ),
    ],
)
def test_next_poll_delay(now: datetime, expected: timedelta) -> None:
    """The next check is timed to when ComEd should post the next price."""
    assert next_poll_delay(PRICE_TIME, now) == expected


@pytest.mark.parametrize(
    ("now", "datestart", "dateend"),
    [
        pytest.param("2026-11-11T23:37:00Z", "202611111537", "202611111837", id="CST"),
        pytest.param("2026-07-01T17:07:00Z", "202607011007", "202607011307", id="CDT"),
    ],
)
async def test_five_minute_feed_asks_for_a_few_hours(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    now: str,
    datestart: str,
    dateend: str,
) -> None:
    """The 5-minute feed asks for a few hours around now, in Chicago time."""
    freezer.move_to(now)
    stamp = (utc(now) - timedelta(minutes=7)).isoformat()
    aioclient_mock.get(FIVE_MINUTE_URL, text=price_at(stamp, 4.1))
    await setup_feed(hass, "five_minute")

    url = aioclient_mock.mock_calls[0][1]
    assert url.query["datestart"] == datestart
    assert url.query["dateend"] == dateend


async def test_hour_average_needs_no_range(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The hour-average feed returns one row, so it isn't given a range."""
    aioclient_mock.get(HOUR_AVERAGE_URL, exc=aiohttp.ClientError())
    await setup_feed(hass, "current_hour_average")

    assert dict(aioclient_mock.mock_calls[0][1].query) == {"type": "currenthouraverage"}


async def test_checks_right_after_next_price_is_posted(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a price arrives, the next request waits until the next one is due."""
    freezer.move_to("2026-11-11T23:37:00Z")
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:30:00Z", 5.2))
    await setup_feed(hass, "current_hour_average")
    assert aioclient_mock.call_count == 1

    due = utc("2026-11-11T23:35:00Z") + POSTING_DELAY
    await advance_to(hass, freezer, due - timedelta(seconds=5))
    assert aioclient_mock.call_count == 1

    aioclient_mock.clear_requests()
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:35:00Z", 4.0))
    await advance_to(hass, freezer, due + timedelta(seconds=1))
    assert aioclient_mock.call_count == 1
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == "4.0"


async def test_slow_response_doesnt_delay_next_check(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The next check is timed from when the response arrives."""
    freezer.move_to("2026-11-11T23:37:00Z")

    async def slow_response(
        method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        freezer.tick(timedelta(seconds=20))
        return AiohttpClientMockResponse(
            method, url, text=price_at("2026-11-11T23:30:00Z", 5.2)
        )

    aioclient_mock.get(HOUR_AVERAGE_URL, side_effect=slow_response)
    await setup_feed(hass, "current_hour_average")

    aioclient_mock.clear_requests()
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-11T23:35:00Z", 4.0))
    due = utc("2026-11-11T23:35:00Z") + POSTING_DELAY
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
    await setup_feed(hass, "current_hour_average")
    assert aioclient_mock.call_count == 1

    await advance_to(hass, freezer, start + PUBLISH_INTERVAL - timedelta(seconds=5))
    assert aioclient_mock.call_count == 1

    await advance_to(hass, freezer, start + PUBLISH_INTERVAL + timedelta(seconds=1))
    assert aioclient_mock.call_count == 2


async def test_clock_behind_still_checks(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without a Date header, an outage still shows up if the clock is behind."""
    start = utc("2026-11-11T23:37:00Z")
    freezer.move_to(start)
    aioclient_mock.get(HOUR_AVERAGE_URL, text=price_at("2026-11-12T01:30:00Z", 5.2))
    await setup_feed(hass, "current_hour_average")
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == "5.2"

    aioclient_mock.clear_requests()
    aioclient_mock.get(HOUR_AVERAGE_URL, status=503)
    longest_wait = PUBLISH_INTERVAL + POSTING_DELAY
    await advance_to(hass, freezer, start + longest_wait + timedelta(seconds=1))
    assert aioclient_mock.call_count == 1
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == STATE_UNAVAILABLE


DATE = "Thu, 12 Nov 2026 01:58:21 GMT"


@pytest.mark.parametrize(
    ("date", "age", "expected"),
    [
        pytest.param(DATE, None, "2026-11-12T01:58:21Z", id="gmt"),
        pytest.param(
            "Thu, 12 Nov 2026 01:58:21 -0000",
            None,
            "2026-11-12T01:58:21Z",
            id="no_zone",
        ),
        pytest.param(None, None, "2026-11-12T03:00:00Z", id="missing"),
        pytest.param("not a date", None, "2026-11-12T03:00:00Z", id="unreadable"),
        pytest.param(DATE, "120", "2026-11-12T02:00:21Z", id="from_a_cache"),
        pytest.param(DATE, "soon", "2026-11-12T03:00:00Z", id="unreadable_age"),
        pytest.param(DATE, "-60", "2026-11-12T01:58:21Z", id="negative_age"),
        pytest.param(DATE, "9" * 30, "2026-11-12T03:00:00Z", id="huge_age"),
        pytest.param(None, "120", "2026-11-12T03:00:00Z", id="age_without_date"),
        pytest.param(
            "Fri, 31 Dec 9999 23:59:59 GMT",
            "120",
            "2026-11-12T03:00:00Z",
            id="date_too_late_to_add_age",
        ),
    ],
)
def test_comed_time(
    freezer: FrozenDateTimeFactory, date: str | None, age: str | None, expected: str
) -> None:
    """ComEd's time is Date plus any Age, or Home Assistant's clock if unreadable."""
    freezer.move_to("2026-11-12T03:00:00Z")
    assert comed_time(date, age) == utc(expected)


# Measured: each price appears 5:17 after its timestamp.
POSTED_AFTER = timedelta(minutes=5, seconds=17)
SKEWS = pytest.mark.parametrize(
    "skew",
    [timedelta(0), timedelta(hours=2), -timedelta(minutes=35)],
    ids=["clock_right", "clock_2h_behind", "clock_35min_ahead"],
)


def newest_stamp(comed_now: datetime) -> datetime:
    """Return the timestamp of the newest price ComEd has posted."""
    posted = comed_now - POSTED_AFTER
    return posted.replace(
        minute=posted.minute - posted.minute % 5, second=0, microsecond=0
    )


def simulated_comed(
    skew: timedelta, stops_at: datetime | None = None
) -> tuple[Any, list[datetime]]:
    """Return a mock of ComEd's hour-average feed, and the timestamps it served.

    ComEd's clock is Home Assistant's plus `skew`. It posts a price every 5
    minutes, 5:17 after each timestamp, and puts its time in a Date header.
    With `stops_at`, it posts nothing newer than that.
    """
    served: list[datetime] = []

    async def respond(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        comed_now = dt_util.utcnow() + skew
        stamp = newest_stamp(comed_now)
        if stops_at is not None:
            stamp = min(stamp, stops_at)
        served.append(stamp)
        body = price_at(stamp.isoformat(), stamp.minute / 10)
        return AiohttpClientMockResponse(
            method,
            url,
            text=body,
            headers={"Date": format_datetime(comed_now, usegmt=True)},
        )

    return respond, served


async def run_for(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, duration: timedelta
) -> None:
    """Let time pass in 20-second steps, running whatever comes due."""
    end = dt_util.utcnow() + duration
    while dt_util.utcnow() < end:
        freezer.tick(timedelta(seconds=20))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


@SKEWS
async def test_every_price_once_whatever_the_clock(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    skew: timedelta,
) -> None:
    """However far off Home Assistant's clock is, each price is fetched once."""
    freezer.move_to("2026-11-11T23:37:00Z")
    respond, served = simulated_comed(skew)
    aioclient_mock.get(HOUR_AVERAGE_URL, side_effect=respond)
    await setup_feed(hass, "current_hour_average")
    await run_for(hass, freezer, timedelta(hours=1))

    # The price at startup, then the 12 posted during the hour, each with one
    # request: none missed, none fetched before it was posted.
    assert len(set(served)) == 13
    assert len(served) == 13
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state != STATE_UNAVAILABLE


@SKEWS
async def test_stalled_feed_goes_unavailable_whatever_the_clock(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    skew: timedelta,
) -> None:
    """However far off Home Assistant's clock is, a stalled feed is caught."""
    start = utc("2026-11-11T23:37:00Z")
    freezer.move_to(start)
    respond, _ = simulated_comed(skew, stops_at=newest_stamp(start + skew))
    aioclient_mock.get(HOUR_AVERAGE_URL, side_effect=respond)
    await setup_feed(hass, "current_hour_average")
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state != STATE_UNAVAILABLE

    # The last price is stamped 7 minutes before startup, so it's 30 minutes
    # old 23 minutes in. Allow for the 5-minute checks once it's late.
    await run_for(hass, freezer, timedelta(minutes=30))
    assert hass.states.get(HOUR_AVERAGE_ENTITY).state == STATE_UNAVAILABLE
