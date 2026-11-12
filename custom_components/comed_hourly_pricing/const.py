"""Constants shared by the sensor platform and the setup screen."""

DOMAIN = "comed_hourly_pricing"
CONF_PRICE_UNIT = "price_unit"

# Each feed's API name and default sensor name. The names match the built-in
# integration's, so entity IDs stay the same.
FEEDS = {
    "five_minute": ("5minutefeed", "ComEd 5 Minute Price"),
    "current_hour_average": ("currenthouraverage", "ComEd Current Hour Average Price"),
}


def default_name(feed: str, in_dollars: bool) -> str:
    """Return a sensor's name when none is given.

    A dollars sensor gets its own, so it can sit beside a cents one.
    """
    name = FEEDS[feed][1]
    return f"{name} USD" if in_dollars else name
