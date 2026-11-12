"""Constants for the ComEd Hourly Pricing integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "comed_hourly_pricing"

API_URL: Final = "https://hourlypricing.comed.com/api"
ATTRIBUTION: Final = "Data provided by ComEd Hourly Pricing service"

CONF_CURRENT_HOUR_AVERAGE: Final = "current_hour_average"
CONF_FIVE_MINUTE: Final = "five_minute"
CONF_MONITORED_FEEDS: Final = "monitored_feeds"
CONF_PRICE_UNIT: Final = "price_unit"
CONF_SENSOR_TYPE: Final = "type"

PRICE_UNIT_CENTS: Final = "cents"
PRICE_UNIT_DOLLARS: Final = "dollars"

UNIT_CENTS_PER_KWH: Final = "¢/kWh"
# The Energy dashboard reads a price as "<currency>/kWh" and ignores the currency
# part, so it needs a price in whole dollars, not cents.
UNIT_DOLLARS_PER_KWH: Final = "USD/kWh"

# Maps each feed to the value of the API's "type" query parameter.
FEED_API_TYPES: Final = {
    CONF_FIVE_MINUTE: "5minutefeed",
    CONF_CURRENT_HOUR_AVERAGE: "currenthouraverage",
}

REQUEST_TIMEOUT: Final = 30  # seconds

# ComEd posts a price every 5 minutes, stamped 12:00, 12:05, ..., and each one
# appears about 5 minutes after its timestamp, just after the next 5-minute
# mark. The integration checks for each new price right after it should appear
# instead of polling on a timer that ignores the posting schedule.
PUBLISH_INTERVAL: Final = timedelta(minutes=5)
# How long after its timestamp a price has reliably appeared, plus a margin.
# See README "How often it checks" for the measurements behind this.
POSTING_DELAY: Final = timedelta(minutes=5, seconds=45)
# If a price hasn't appeared when expected, check this often...
LATE_RETRY_INTERVAL: Final = timedelta(minutes=1)
# ...for this long. After that the feed is probably stalled, so go back to
# checking every PUBLISH_INTERVAL rather than polling faster than ComEd posts.
LATE_RETRY_WINDOW: Final = timedelta(minutes=5)

# The 5-minute feed returns 24 hours of prices (about 12 KB) unless given a
# time range, in ComEd's local time. A few hours around "now" includes the
# newest price even if ComEd interprets the local time an hour differently
# around daylight saving changes.
COMED_TIME_ZONE: Final = "America/Chicago"
RANGE_BEFORE_NOW: Final = timedelta(hours=2)
RANGE_AFTER_NOW: Final = timedelta(hours=1)

# ComEd publishes a new price every 5 minutes. If the newest one is older than
# this, the feed has stopped updating and its price can't be trusted.
MAX_PRICE_AGE: Final = timedelta(minutes=30)

ATTR_PRICE_TIME: Final = "price_time"
