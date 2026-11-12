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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.json import json_loads

from .const import DOMAIN, FEEDS

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

# Each feed's coordinator, as the task that creates it and fetches the first
# price. Every sensor on a feed shares it, whether it's from the UI or YAML.
DATA_FEEDS: HassKey[dict[str, asyncio.Task[ComedPriceCoordinator]]] = HassKey(
    f"{DOMAIN}_feeds"
)


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
        self._next_check = dt_util.utcnow()

    async def async_wake(self) -> None:
        """Get ready for a sensor on a feed that no other sensor is using.

        With no sensors, the coordinator stops polling, so its price may be old,
        and it would otherwise wait a full interval from when a sensor comes
        back. So fetch now if the next check is already overdue, and otherwise
        keep that check's time.
        """
        if self._listeners:
            return
        remaining = self._next_check - dt_util.utcnow()
        if remaining <= timedelta(0):
            await self.async_refresh()
        else:
            self.update_interval = remaining

    def _check_again_in(self, delay: timedelta) -> None:
        """Schedule the next check."""
        self.update_interval = delay
        self._next_check = dt_util.utcnow() + delay

    async def _async_update_data(self) -> PriceReading:
        """Fetch the newest price, or raise UpdateFailed."""
        # Used if this fetch fails; replaced below if it succeeds.
        self._check_again_in(PUBLISH_INTERVAL)

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
                age_header = response.headers.get("Age")
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(f"can't reach the ComEd API: {err!r}") from err

        reading = parse_newest_price(body)
        # Compared with ComEd's clock, not Home Assistant's, so the stale check
        # and the schedule both work even if Home Assistant's clock is off.
        now = comed_time(date_header, age_header)
        if now - reading.time > MAX_PRICE_AGE:
            raise UpdateFailed(
                f"the newest ComEd price is from {reading.time}; "
                "the feed seems to have stalled"
            )

        self._check_again_in(next_poll_delay(reading.time, now))
        self.logger.debug(
            "%s: price %s stamped %s; checking again in %s",
            self.name,
            reading.price,
            reading.time,
            self.update_interval,
        )
        return reading


def async_start_feed(
    hass: HomeAssistant, feed: str
) -> asyncio.Task[ComedPriceCoordinator]:
    """Start a feed's coordinator, unless it's started; return the task that does it.

    If starting fails, the task is forgotten, so the next sensor tries again.
    """
    starts = hass.data.setdefault(DATA_FEEDS, {})
    if feed not in starts:
        task = hass.async_create_task(
            _async_create_coordinator(hass, feed), name=f"Start ComEd {feed} feed"
        )

        def forget_if_failed(task: asyncio.Task[ComedPriceCoordinator]) -> None:
            if (task.cancelled() or task.exception()) and starts.get(feed) is task:
                del starts[feed]

        task.add_done_callback(forget_if_failed)
        starts[feed] = task
    return starts[feed]


async def async_get_feed(hass: HomeAssistant, feed: str) -> ComedPriceCoordinator:
    """Return a feed's coordinator, ready for another sensor."""
    coordinator = await async_start_feed(hass, feed)
    await coordinator.async_wake()
    return coordinator


async def _async_create_coordinator(
    hass: HomeAssistant, feed: str
) -> ComedPriceCoordinator:
    """Create a feed's coordinator and fetch its first price."""
    api_type, name = FEEDS[feed]
    range_time_zone = None
    if api_type == "5minutefeed":
        # Its requests ask for a range of times, which ComEd reads in its own zone.
        range_time_zone = await dt_util.async_get_time_zone(COMED_TIME_ZONE)
        if range_time_zone is None:
            _LOGGER.warning(
                "Couldn't load the %s time zone, so the 5-minute feed will "
                "download all 24 hours of prices each time",
                COMED_TIME_ZONE,
            )
    coordinator = ComedPriceCoordinator(
        hass, async_get_clientsession(hass), api_type, name, range_time_zone
    )
    # Without a config entry, polling doesn't stop at shutdown unless asked.
    await coordinator.async_register_shutdown()
    # If this fails, the sensors start out unavailable and it keeps trying.
    await coordinator.async_refresh()
    return coordinator


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


def comed_time(date_header: str | None, age_header: str | None = None) -> datetime:
    """Return ComEd's time from a response's Date and Age headers.

    A cache that serves a stored response keeps its original Date and adds Age,
    the seconds since then. Falls back to Home Assistant's clock, read now
    (after the response), if Date is missing or either header can't be read.
    """
    try:
        made = parsedate_to_datetime(date_header)
        age = timedelta(seconds=max(int(age_header or 0), 0))
        return made.replace(tzinfo=made.tzinfo or dt_util.UTC) + age
    except TypeError, ValueError, OverflowError:
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
