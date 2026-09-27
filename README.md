# Novafos for Home Assistant

Water (and district heating) consumption from **Novafos** via KMD Easy-Energy, as
Home Assistant sensors and hourly long-term statistics you can put in the Energy
dashboard.

This is a rewrite of [kpoppel/homeassistant-novafos](https://github.com/kpoppel/homeassistant-novafos)
(Apache 2.0). It keeps the `novafos` domain, the `sensor.novafos_water_statistics`
statistic and the `novafos.update_token` action, so existing dashboards and the
[token Chrome extension](https://github.com/kpoppel/homeassistant-novafos-chrome-addon) keep working.

## What you get

| Entity | Meaning |
|---|---|
| `sensor.novafos_water_statistics` | Hourly consumption history (long-term statistics). State is always `unknown` on purpose; use it in statistics cards, apexcharts and the Energy dashboard. |
| `sensor.novafos_last_full_day` | Consumption on the latest complete day (attribute `date`) |
| `sensor.novafos_last_7_days` | Last 7 complete days |
| `sensor.novafos_this_month` / `sensor.novafos_this_year` | Month-to-date / year-to-date, matches the Novafos website |
| `sensor.novafos_data_until` | How far the meter data reaches (typically 1–2 days behind) |
| `sensor.novafos_token_expires` | When the current access token stops working (diagnostic) |

The consumption sensors keep their last values across restarts and while no token is valid.

### Energy dashboard

Settings → Dashboards → Energy → *Water consumption* → add `sensor.novafos_water_statistics`.

## The token (read this)

KMD's login server (`easy-energy-identity.kmd.dk`) only offers interactive MitID login
and issues **access tokens that live for one hour, with no refresh token**. The Novafos
website itself has token renewal switched off. So the integration cannot log in on
its own; it works on tokens you hand it:

- While a token is valid, the integration fetches right away and every 15 minutes.
- When it expires, nothing is fetched and the sensors keep their values.
- The next token continues where the last one stopped, so nothing is lost. The first
  token imports your full history (a few months takes well under a minute).

Ways to hand over a token:

1. **Chrome extension** (easiest): [homeassistant-novafos-chrome-addon](https://github.com/kpoppel/homeassistant-novafos-chrome-addon).
   Log in on the Novafos site and it calls `novafos.update_token` for you.
2. **Manually**: log in on the Novafos site with F12 → Network open, find the `token`
   request, copy `access_token` from the response, then go to Settings → Devices &
   services → Novafos → *Configure* and paste it. A `Bearer ` prefix or the whole JSON
   response is accepted too.
3. **Action**: `novafos.update_token` with `access_token` (for scripts/automations).

Logging in once a week is enough to keep the history complete.

## Installation

HACS → ⋮ → Custom repositories → `https://github.com/TheRealBatBro/Home-Assistant-Novafos`
(type *Integration*) → install *Novafos* → restart → Settings → Devices & services →
Add integration → *Novafos* → paste a current token.

Manual: copy `custom_components/novafos` into your `config/custom_components/` and restart.

Requires Home Assistant 2025.10 or newer.

## Upgrading from kpoppel/homeassistant-novafos

Existing entries are migrated automatically, and existing statistics are continued, not
duplicated. The optional *grouped* day/week/month/year sensors are gone: statistics
cards and apexcharts group the hourly statistic by any period themselves (stat type
*Change*).

## What changed compared with the original

- Hourly data is fetched one day per request. The API only returns real values for
  the first day of an hourly range, and later days come back as zeros.
- Only complete hours are imported. The original imported the not-yet-delivered hours
  as zeros and rechecked only one day back, so late-arriving consumption was lost.
- Times are interpreted as Danish time regardless of Home Assistant's time zone, and
  DST days (23/25 hours) map correctly.
- Token validity is read from the token itself instead of a 45-minute guess.
- Async client with timeouts, a real update interval, and statistics metadata for
  HA 2025.10+ (`unit_class`).
- Sensors for last day, last 7 days, month, year, data freshness and token expiry.

## Debugging

```yaml
logger:
  logs:
    custom_components.novafos: debug
```

## Development

```bash
pip install -r requirements_test.txt
pytest
```
