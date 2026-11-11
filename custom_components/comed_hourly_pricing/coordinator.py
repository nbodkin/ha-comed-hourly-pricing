"""Fetches prices from the ComEd Hourly Pricing API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
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
    FEED_API_TYPES,
    MAX_PRICE_AGE,
    REQUEST_TIMEOUT,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PriceReading:
    """One price from the ComEd API."""

    price: float
    """Cents per kWh."""
    time: datetime
    """When ComEd published the price, in UTC."""


class ComedPriceCoordinator(DataUpdateCoordinator[PriceReading]):
    """Polls one ComEd feed for every sensor that shows it.

    When a fetch fails, the coordinator marks its sensors unavailable instead of
    leaving the last price on screen. It logs an error once when the feed goes
    down and an info message once when it recovers, rather than every 5 minutes.
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
            update_interval=UPDATE_INTERVAL,
        )
        self._session = session
        self._params = {"type": FEED_API_TYPES[feed]}

    async def _async_update_data(self) -> PriceReading:
        """Fetch the newest price, or raise UpdateFailed."""
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                self._session.get(API_URL, params=self._params) as response,
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

        age = dt_util.utcnow() - reading.time
        if age > MAX_PRICE_AGE:
            raise UpdateFailed(
                f"the newest ComEd price is from {reading.time.isoformat()}, "
                f"{int(age.total_seconds() // 60)} minutes ago; "
                "the feed seems to have stopped updating"
            )
        return reading


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
