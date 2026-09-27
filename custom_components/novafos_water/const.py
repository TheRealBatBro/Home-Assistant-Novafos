"""Constants for the Novafos integration."""

from datetime import timedelta

DOMAIN = "novafos_water"
DEFAULT_NAME = "Novafos Water"

CONF_ACCESS_TOKEN = "access_token"
# {"2026": 93.55, ...}: price per m³ incl. VAT for each calendar year (Danish tariffs change on 1 January).
CONF_PRICES = "prices"
CONF_PRICE = "price"

# Polling only does anything while the pushed token is still valid (1 hour).
UPDATE_INTERVAL = timedelta(minutes=15)

# How far back the first statistics import goes (bounded by the meter's first day).
BACKFILL_DAYS = 400
# Import statistics in chunks so a backfill interrupted by token expiry keeps its progress.
IMPORT_CHUNK_DAYS = 14
# Hourly data must be fetched one day per request; limit how many run at once.
MAX_PARALLEL_REQUESTS = 4

UNITS = {"water": "m³", "heating": "kWh"}
UNIT_CLASSES = {"water": "volume", "heating": "energy"}


def statistic_id(meter_type: str) -> str:
    """External statistic holding the hourly history (what the Energy dashboard uses)."""
    return f"{DOMAIN}:{meter_type}_consumption"


def cost_statistic_id(meter_type: str) -> str:
    """External statistic with the cost of each hour (for the Energy dashboard's cost option)."""
    return f"{DOMAIN}:{meter_type}_cost"


def price_for_year(prices: dict[str, float], year: int) -> float | None:
    """The year's price, else the closest earlier year's, else the earliest one given."""
    if not prices:
        return None
    years = sorted(int(y) for y in prices)
    earlier = [y for y in years if y <= year]
    return float(prices[str(earlier[-1] if earlier else years[0])])


def legacy_statistic_id(meter_type: str) -> str:
    """Where v7.0 stored the history (on the sensor entity); copied over on upgrade."""
    return f"sensor.novafos_{meter_type}_consumption"
