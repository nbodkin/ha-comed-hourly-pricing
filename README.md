# ComEd Hourly Pricing (custom integration for Home Assistant)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![Validate](https://github.com/nbodkin/ha-comed-hourly-pricing/actions/workflows/validate.yml/badge.svg)](https://github.com/nbodkin/ha-comed-hourly-pricing/actions/workflows/validate.yml)
[![Tests](https://github.com/nbodkin/ha-comed-hourly-pricing/actions/workflows/tests.yml/badge.svg)](https://github.com/nbodkin/ha-comed-hourly-pricing/actions/workflows/tests.yml)
[![License](https://img.shields.io/github/license/nbodkin/ha-comed-hourly-pricing)](LICENSE)

A replacement for Home Assistant's built-in
[ComEd Hourly Pricing](https://www.home-assistant.io/integrations/comed_hourly_pricing)
integration. It uses the same domain and YAML, so your existing config and
entity IDs keep working, and fixes how it handles errors:

| Built-in integration | This one |
| --- | --- |
| When ComEd's API fails, the last price stays on screen and automations act on it. | Sensors go **unavailable** until a fresh price arrives. |
| An empty or malformed response crashes the update with a traceback. | Every bad response (HTML error page, empty list, `null` or `NaN` price, HTTP errors, timeouts) is handled. |
| No check on whether ComEd is still publishing. | A newest price older than 30 minutes counts as an outage. |
| Logs an error every 5 minutes during an outage. | Logs once when the feed goes down and once when it recovers. |
| Each sensor makes its own request. | One request per feed, shared by all sensors on it. |
| Polls every 5 minutes from whenever Home Assistant started, so each new price shows up as much as 5 minutes after ComEd posts it. | Checks right after ComEd should post each new price. [Same number of requests](#how-often-it-checks), but prices show up right away. |
| Downloads 24 hours of 5-minute prices (about 12 KB) on every poll. | Asks for just a few hours (about 1 KB). |
| Prices only in ¢/kWh, which the Energy dashboard reads as **dollars** (costs 100× too high). | Optional `price_unit: dollars` for the Energy dashboard. |
| No long-term statistics; can't be renamed in the UI. | `state_class: measurement` and a unique ID. |

Tested on Home Assistant 2026.8.3, 2026.10.4 and 2026.11.1.

## Install

### HACS

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nbodkin&repository=ha-comed-hourly-pricing&category=integration)

Click the button above, then **Download**, and restart Home Assistant.

Or by hand:

1. In HACS, open ⋮ → **Custom repositories**.
2. Add `https://github.com/nbodkin/ha-comed-hourly-pricing` with category **Integration**.
3. Find **ComEd Hourly Pricing**, click **Download**, and restart Home Assistant.

It isn't in the HACS store's default list, because HACS doesn't list
integrations that replace a built-in one. Adding it as a custom repository is
the supported way to install it.

### Manual

1. Download this repository ([latest release](https://github.com/nbodkin/ha-comed-hourly-pricing/releases/latest),
   or **Code** → **Download ZIP**).
2. Copy its `custom_components/comed_hourly_pricing/` folder into the
   `custom_components/` folder next to your `configuration.yaml`, creating it if needed.
3. Restart Home Assistant.

After restarting, Home Assistant logs a warning that a custom integration is
overriding a built-in one. That's expected.

## Configure

Same YAML as the built-in integration. `price_unit` is new.

```yaml
sensor:
  - platform: comed_hourly_pricing
    monitored_feeds:
      - type: five_minute
      - type: current_hour_average
      # For the Energy dashboard, which needs whole dollars:
      - type: current_hour_average
        name: ComEd Hourly Price USD
        price_unit: dollars
        offset: 8.5
```

| Option | Required | Default | Description |
| --- | --- | --- | --- |
| `type` | yes | | `five_minute` or `current_hour_average` |
| `name` | no | `ComEd 5 Minute Price` / `ComEd Current Hour Average Price` | Sensor name. Renaming it in YAML creates a new entity; to rename without that, use the UI. |
| `offset` | no | `0` | Added to every price, **in ¢/kWh even when `price_unit` is `dollars`**. ComEd's feed is the supply price only, so this is where delivery charges and other per-kWh fees go. |
| `price_unit` | no | `cents` | `cents` reports ¢/kWh like the built-in integration. `dollars` reports USD/kWh. |

### Energy dashboard

Use a sensor with `price_unit: dollars` as the price entity.
The Energy dashboard ignores the currency in a price's unit and treats the number as your
system currency per kWh, so a ¢/kWh sensor makes every cost 100× too high.
Keep your existing cents sensors for automations if you like. Each sensor is independent.

## How often it checks

ComEd posts one price every 5 minutes, stamped on a 5-minute mark (12:00,
12:05, ...). Each price appears in the API about 5 minutes after its
timestamp, right after the next 5-minute mark. The current hour average updates
at the same moment. ComEd's API documentation doesn't say any of this; it comes
from measuring the live API on the evening of November 11, 2026:

- Sampling every 2 seconds at four 5-minute marks, each new price appeared
  16–18 seconds after the mark.
- Sampling every 20 seconds for 16 minutes, every new price appeared 5:00–5:33
  after its timestamp, with the hour average updating at the same time.
- The 24-hour feed had four gaps of 10–35 minutes with no prices.

So instead of polling on a fixed timer, the integration waits after each price
until the next one should be posted (5 minutes 45 seconds after that price's
timestamp) and checks then:

- **Normally:** one request per feed every 5 minutes, the minimum needed to see
  every price. With both sensors that's 24 small requests an hour.
- **If a price is late:** it checks once a minute for up to 5 minutes.
  ComEd's feed does have occasional gaps.
- **If the feed has stalled or requests fail:** every 5 minutes. It never polls
  faster than ComEd posts.

You can watch it decide by turning on debug logging:

```yaml
logger:
  logs:
    custom_components.comed_hourly_pricing: debug
```

## Remove

Delete `custom_components/comed_hourly_pricing/` (or uninstall in HACS) and
restart. Home Assistant goes back to the built-in integration with the same YAML,
except that `price_unit` must be removed because the built-in one rejects it.

## Development

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest
ruff check . && ruff format --check .
```

GitHub Actions run the same checks on every push and pull request, plus Home
Assistant's `hassfest` and the HACS validator. Tests run against both the oldest
supported Home Assistant (2026.8) and the newest the test plugin supports.

To release a version, update `version` in `manifest.json`, then publish a
GitHub release tagged with the same version (for example `v1.0.1`). HACS shows
releases as versions users can pick.

## License

[Apache License 2.0](LICENSE), the same license as Home Assistant, whose
built-in ComEd integration this is based on.
