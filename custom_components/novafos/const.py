"""Constants for the Novafos integration."""

from datetime import timedelta

DOMAIN = "novafos"
DEFAULT_NAME = "Novafos"

CONF_ACCESS_TOKEN = "access_token"

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
    """Same id as the upstream integration so existing dashboards keep working."""
    return f"sensor.{DOMAIN}_{meter_type}_statistics"
