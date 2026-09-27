"""Fixtures for Novafos tests. The fake API mirrors responses captured from the live KMD API."""

import base64
import json
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.novafos_water.api import TZ, Meter

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


def make_token(exp_offset: int = 3600) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'RS256'})}.{b64({'exp': int(time.time()) + exp_offset})}.sig"


METER = Meter(
    type="water",
    installation_id=12345678,
    measurement_point_id=23456789,
    meter_number="99990000",
    location="Bryggers",
    unit={"Id": 1234, "Name": "M3", "Description": "Vand", "Decimals": 0, "Order": 1},
)
FIRST_DAY_OFFSET = 20  # meter has data from 20 days ago
LAG_HOURS = 36  # newest 36 hours are incomplete zero placeholders


def fake_hours(meter, day: date):
    start = datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(timezone.utc)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ).astimezone(timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LAG_HOURS)
    out, t = [], start
    while t < end:
        complete = t < cutoff
        out.append({"start": t, "value": 0.01 if complete else 0.0, "complete": complete})
        t += timedelta(hours=1)
    return out


def fake_days(meter, first: date, last: date):
    out, d = [], first
    while d <= last:
        hours = fake_hours(meter, d)
        out.append({"date": d, "value": round(sum(h["value"] for h in hours), 3),
                    "complete": all(h["complete"] for h in hours)})
        d += timedelta(days=1)
    return out


@pytest.fixture
def mock_api():
    today = datetime.now(TZ).date()

    async def login(self):
        self.customer_id, self.customer_number, self.address = "11112222", "3334444", "Testvej 1"

    base = "custom_components.novafos_water.api.NovafosClient"
    with (
        patch(f"{base}.async_login", login),
        patch(f"{base}.async_get_meters", AsyncMock(return_value=[METER])) as meters,
        patch(f"{base}.async_get_first_day",
              AsyncMock(return_value=today - timedelta(days=FIRST_DAY_OFFSET))),
        patch(f"{base}.async_get_hours", AsyncMock(side_effect=fake_hours)) as hours,
        patch(f"{base}.async_get_days", AsyncMock(side_effect=fake_days)),
    ):
        yield {"meters": meters, "hours": hours}
