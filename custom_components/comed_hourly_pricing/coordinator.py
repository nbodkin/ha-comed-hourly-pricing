"""Fetches prices from the ComEd Hourly Pricing API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import logging
import math
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

_LOGGER = logging.getLogger(__name__)

API_URL = "https://hourlypricing.comed.com/api"
REQUEST_TIMEOUT = 30  # seconds

# ComEd posts a price every 5 minutes, stamped on 5-minute marks, and each one
# appears about 5:17 after its timestamp (measured; see README "How often it
# checks"). So the next price is due 5 minutes after that, with some margin.
PUBLISH_INTERVAL = timedelta(minutes=5)
POSTING_DELAY = timedelta(minutes=5, seconds=45)
# A late price is checked for once a minute, for up to 5 minutes.
LATE_RETRY_INTERVAL = timedelta(minutes=1)
LATE_RETRY_WINDOW = timedelta(minutes=5)
# If the newest price is older than this, the feed has stalled.
MAX_PRICE_AGE = timedelta(minutes=30)

# Without a time range, the 5-minute feed returns 24 hours of prices (about
# 12 KB). The range is in ComEd's local time, and wide enough that it still
# includes the newest price if ComEd reads it an hour off around daylight
# saving changes.
COMED_TIME_ZONE = "America/Chicago"
RANGE_BEFORE_NOW = timedelta(hours=2)
RANGE_AFTER_NOW = timedelta(hours=1)


@dataclass(frozen=True)
class PriceReading:
    """One price from the ComEd API."""

    price: float  # cents per kWh
    time: datetime  # ComEd's timestamp for the price, in UTC


class ComedPriceCoordinator(DataUpdateCoordinator[PriceReading]):
    """Fetches one ComEd feed for every sensor that shows it.

    When a fetch fails, its sensors go unavailable, and the failure is logged
    once rather than on every retry.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        api_type: str,
        name: str,
        range_time_zone: ZoneInfo | None = None,
    ) -> None:
        """Set up fetching for one feed.

        With range_time_zone, requests ask for a few hours of prices around now,
        in that zone, instead of everything ComEd returns.
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=None,
            name=name,
            update_interval=PUBLISH_INTERVAL,
        )
        self._session = session
        self._api_type = api_type
        self._range_time_zone = range_time_zone

    async def _async_update_data(self) -> PriceReading:
        """Fetch the newest price, or raise UpdateFailed."""
        # Used if this fetch fails; replaced below if it succeeds.
        self.update_interval = PUBLISH_INTERVAL

        params = {"type": self._api_type}
        if self._range_time_zone is not None:
            local = dt_util.now(self._range_time_zone)
            params["datestart"] = f"{local - RANGE_BEFORE_NOW:%Y%m%d%H%M}"
            params["dateend"] = f"{local + RANGE_AFTER_NOW:%Y%m%d%H%M}"

        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                self._session.get(API_URL, params=params) as response,
            ):
                response.raise_for_status()
                body = await response.text()
                date_header = response.headers.get("Date")
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"can't reach the ComEd API: {err!r}") from err

        reading = parse_newest_price(body)
        # Compared with ComEd's clock, not Home Assistant's, so the stale check
        # and the schedule both work even if Home Assistant's clock is off.
        now = comed_time(date_header)
        if now - reading.time > MAX_PRICE_AGE:
            raise UpdateFailed(
                f"the newest ComEd price is from {reading.time}; "
                "the feed seems to have stalled"
            )

        self.update_interval = next_poll_delay(reading.time, now)
        self.logger.debug(
            "%s: price %s stamped %s; checking again in %s",
            self.name,
            reading.price,
            reading.time,
            self.update_interval,
        )
        return reading


def parse_newest_price(body: str) -> PriceReading:
    """Return the newest price in an API response, or raise UpdateFailed.

    A bad request gets HTTP 200 with an HTML page, so non-JSON is an error too.
    """
    try:
        # The API lists the newest price first, but don't depend on that.
        newest = max(json_loads(body), key=lambda row: int(row["millisUTC"]))
        price = float(newest["price"])
        if not math.isfinite(price):  # Home Assistant rejects NaN and infinity.
            raise ValueError(price)
        time = dt_util.utc_from_timestamp(int(newest["millisUTC"]) / 1000)
    # An out-of-range timestamp raises OverflowError or OSError.
    except (ValueError, KeyError, TypeError, OverflowError, OSError) as err:
        raise UpdateFailed(
            f"unexpected response from the ComEd API: {body[:80]!r}"
        ) from err
    return PriceReading(price, time)


def comed_time(date_header: str | None) -> datetime:
    """Return ComEd's time from a response's Date header.

    Falls back to Home Assistant's clock, read now (after the response), if the
    header is missing or unreadable.
    """
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
        except TypeError, ValueError:
            pass
        else:
            return parsed.replace(tzinfo=parsed.tzinfo or dt_util.UTC)
    return dt_util.utcnow()


def next_poll_delay(price_time: datetime, now: datetime) -> timedelta:
    """Return how long to wait before checking for the price after price_time.

    Waits until the next price should be posted. If it's late, checks once a
    minute for a while, then every 5 minutes so a stalled feed isn't hammered.
    """
    late_by = now - (price_time + PUBLISH_INTERVAL + POSTING_DELAY)
    if late_by < timedelta(0):
        # A price stamped after now would otherwise put off the next check by
        # however far ahead it is. That can only happen if a response had no
        # Date header and Home Assistant's clock is behind ComEd's.
        return min(-late_by, PUBLISH_INTERVAL + POSTING_DELAY)
    return LATE_RETRY_INTERVAL if late_by < LATE_RETRY_WINDOW else PUBLISH_INTERVAL
