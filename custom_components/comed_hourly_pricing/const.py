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

UPDATE_INTERVAL: Final = timedelta(minutes=5)
REQUEST_TIMEOUT: Final = 30  # seconds

# ComEd publishes a new price every 5 minutes. If the newest one is older than
# this, the feed has stopped updating and its price can't be trusted.
MAX_PRICE_AGE: Final = timedelta(minutes=30)

ATTR_PRICE_TIME: Final = "price_time"
