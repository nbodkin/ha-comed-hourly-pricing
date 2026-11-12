"""Helpers shared by the test files."""

from datetime import datetime, timedelta
import json
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.comed_hourly_pricing.coordinator import API_URL

DOMAIN = "comed_hourly_pricing"

FIVE_MINUTE_URL = f"{API_URL}?type=5minutefeed"
HOUR_AVERAGE_URL = f"{API_URL}?type=currenthouraverage"
FIVE_MINUTE_ENTITY = "sensor.comed_5_minute_price"
HOUR_AVERAGE_ENTITY = "sensor.comed_current_hour_average_price"

FIVE_MINUTE = {"type": "five_minute"}
HOUR_AVERAGE = {"type": "current_hour_average"}


def utc(text: str) -> datetime:
    """Parse an ISO time in UTC, like 2026-11-11T12:00:00Z."""
    parsed = datetime.fromisoformat(text)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"not a UTC time: {text}")
    return parsed


def price_at(stamp: str, price: float) -> str:
    """Build a one-row API response for a price with the given timestamp."""
    millis = int(utc(stamp).timestamp() * 1000)
    return json.dumps([{"millisUTC": str(millis), "price": str(price)}])


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
    """Set up the YAML platform with the given monitored_feeds entries."""
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


async def advance_to(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    """Move the clock to `when` and run whatever came due."""
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()
