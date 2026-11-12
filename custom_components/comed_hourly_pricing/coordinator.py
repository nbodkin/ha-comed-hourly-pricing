"""Fetches prices from the ComEd Hourly Pricing API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

from .const import (
    API_URL,
    COMED_TIME_ZONE,
    CONF_FIVE_MINUTE,
    FEED_API_TYPES,
    LATE_RETRY_INTERVAL,
    LATE_RETRY_WINDOW,
    MAX_PRICE_AGE,
    POSTING_DELAY,
    PUBLISH_INTERVAL,
    RANGE_AFTER_NOW,
    RANGE_BEFORE_NOW,
    REQUEST_TIMEOUT,
)

# ComEd's date range format: YYYYMMDDhhmm.
_RANGE_FORMAT = "%Y%m%d%H%M"

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriceReading:
    """One price from the ComEd API."""

    price: float
    """Cents per kWh."""
    time: datetime
    """ComEd's timestamp for the price, in UTC, on a 5-minute mark."""


class ComedPriceCoordinator(DataUpdateCoordinator[PriceReading]):
    """Polls one ComEd feed for every sensor that shows it.

    When a fetch fails, the coordinator marks its sensors unavailable instead of
    leaving the last price on screen. It logs an error once when the feed goes
    down and an info message once when it recovers, rather than every 5 minutes.

    After each fetch it schedules the next one for when ComEd should have posted
    the next price; see next_poll_delay.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        feed: str,
        name: str,
    ) -> None:
        """Set up polling for one feed."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,
            name=name,
            update_interval=PUBLISH_INTERVAL,
        )
        self._session = session
        self._feed = feed

    async def _async_update_data(self) -> PriceReading:
        """Fetch the newest price, or raise UpdateFailed."""
        # Applies if this fetch fails. On success it's replaced below.
        self.update_interval = PUBLISH_INTERVAL

        params = request_params(self._feed, dt_util.utcnow())
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                self._session.get(API_URL, params=params) as response,
            ):
                response.raise_for_status()
                body = await response.text()
        except TimeoutError as err:
            raise UpdateFailed(
                f"no response from the ComEd API within {REQUEST_TIMEOUT} seconds"
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"error talking to the ComEd API: {err}") from err

        reading = parse_latest_reading(body)

        now = dt_util.utcnow()
        age = now - reading.time
        if age > MAX_PRICE_AGE:
            raise UpdateFailed(
                f"the newest ComEd price is from {reading.time.isoformat()}, "
                f"{int(age.total_seconds() // 60)} minutes ago; "
                "the feed seems to have stopped updating"
            )

        self.update_interval = next_poll_delay(reading.time, now)
        self.logger.debug(
            "%s: price %s stamped %s; checking again in %s",
            self.name,
            reading.price,
            reading.time.isoformat(),
            self.update_interval,
        )
        return reading


def request_params(feed: str, now: datetime) -> dict[str, str]:
    """Return the API query parameters for a feed."""
    params = {"type": FEED_API_TYPES[feed]}
    if feed == CONF_FIVE_MINUTE:
        local_now = now.astimezone(dt_util.get_time_zone(COMED_TIME_ZONE))
        params["datestart"] = (local_now - RANGE_BEFORE_NOW).strftime(_RANGE_FORMAT)
        params["dateend"] = (local_now + RANGE_AFTER_NOW).strftime(_RANGE_FORMAT)
    return params


def next_poll_delay(price_time: datetime, now: datetime) -> timedelta:
    """Return how long to wait before checking for the price after price_time.

    The next price is stamped PUBLISH_INTERVAL after this one and should appear
    POSTING_DELAY after that. If that's already past, the price is late: check
    every LATE_RETRY_INTERVAL for a while, then fall back to PUBLISH_INTERVAL so
    a stalled feed is never polled faster than ComEd posts.
    """
    next_price_due = price_time + PUBLISH_INTERVAL + POSTING_DELAY
    if next_price_due > now:
        # The cap only matters if this computer's clock is behind ComEd's.
        return min(next_price_due - now, PUBLISH_INTERVAL + POSTING_DELAY)
    if now - next_price_due < LATE_RETRY_WINDOW:
        return LATE_RETRY_INTERVAL
    return PUBLISH_INTERVAL


def parse_latest_reading(body: str) -> PriceReading:
    """Return the newest usable price in an API response.

    Rows that can't be read are skipped. Raises UpdateFailed when nothing usable is
    left. A bad request gets HTTP 200 with a short HTML page, so a body that
    isn't JSON is treated as an error.
    """
    try:
        data = json_loads(body)
    except ValueError as err:
        raise UpdateFailed(
            f"the ComEd API sent something other than JSON: {_preview(body)}"
        ) from err

    if not isinstance(data, list):
        raise UpdateFailed(
            f"the ComEd API sent an unexpected response: {_preview(body)}"
        )

    readings = [reading for row in data if (reading := _parse_row(row)) is not None]
    if not readings:
        raise UpdateFailed(f"the ComEd API sent no usable prices: {_preview(body)}")

    # The API lists the newest price first, but don't depend on that.
    return max(readings, key=lambda reading: reading.time)


def _parse_row(row: Any) -> PriceReading | None:
    """Read one {"millisUTC": "...", "price": "..."} row, or None if it's malformed."""
    if not isinstance(row, dict):
        return None
    try:
        price = float(row["price"])
        time = dt_util.utc_from_timestamp(int(row["millisUTC"]) / 1000)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    if not math.isfinite(price):
        return None
    return PriceReading(price=price, time=time)


def _preview(body: str) -> str:
    """Shorten a response body for a log message."""
    return repr(body[:80] + "…" if len(body) > 80 else body)
