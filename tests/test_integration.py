"""End-to-end tests for Novafos inside Home Assistant."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.novafos.api import NovafosAuthError
from custom_components.novafos.const import DOMAIN

from .conftest import FIRST_DAY_OFFSET, LAG_HOURS, METER, make_token

STAT_ID = "sensor.novafos_water_statistics"
METERS = [{"type": "water", "installation_id": METER.installation_id,
           "measurement_point_id": METER.measurement_point_id, "meter_number": METER.meter_number,
           "location": METER.location, "unit": METER.unit}]


def _entry(token, **kw):
    return MockConfigEntry(domain=DOMAIN, version=5, unique_id="11112222",
                           data={"name": "Novafos", "meters": METERS},
                           options={"access_token": token}, **kw)


async def _hourly(hass):
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utcnow() - timedelta(days=60), None,
        {STAT_ID}, "hour", None, {"sum", "state"},
    )
    return stats.get(STAT_ID, [])


async def test_config_flow(hass, mock_api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.novafos.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Novafos", "access_token": "Bearer " + make_token()})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "11112222"
    assert result["data"]["meters"][0]["installation_id"] == METER.installation_id
    assert not result["options"]["access_token"].startswith("Bearer")


async def test_config_flow_errors(hass, mock_api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Novafos", "access_token": make_token(-60)})
    assert result["errors"] == {"base": "token_expired"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Novafos", "access_token": "garbage"})
    assert result["errors"] == {"base": "invalid_token"}
    with patch("custom_components.novafos.api.NovafosClient.async_login",
               AsyncMock(side_effect=NovafosAuthError("401"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Novafos", "access_token": make_token()})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_sensors_and_statistics(hass, mock_api):
    entry = _entry(make_token())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    last_day = hass.states.get("sensor.novafos_last_full_day")
    assert float(last_day.state) == 0.24  # 24 complete hours of 0.01
    assert last_day.attributes["date"] < dt_util.now().date().isoformat()
    assert hass.states.get("sensor.novafos_token_expires").state not in ("unknown", "unavailable")
    assert hass.states.get(STAT_ID).state == "unknown"

    rows = await _hourly(hass)
    # Everything from the first day up to the last complete hour, nothing after it.
    newest = dt_util.utcnow() - timedelta(hours=LAG_HOURS)
    assert len(rows) >= FIRST_DAY_OFFSET * 24 - LAG_HOURS - 2
    assert all(r["state"] == 0.01 for r in rows)
    assert dt_util.utc_from_timestamp(rows[-1]["start"]) <= newest
    assert abs(rows[-1]["sum"] - 0.01 * len(rows)) < 1e-6
    assert hass.states.get(STAT_ID).attributes["imported_until"] is not None

    # A later sync only fetches from the last imported day and does not double count.
    mock_api["hours"].reset_mock()
    await entry.runtime_data.async_sync_statistics()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api["hours"].call_count <= 3
    again = await _hourly(hass)
    assert len(again) == len(rows)
    assert again[-1]["sum"] == rows[-1]["sum"]


async def test_expired_token_keeps_entry_loaded(hass, mock_api):
    entry = _entry(make_token(-60))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED
    mock_api["hours"].assert_not_called()
    assert hass.states.get("sensor.novafos_this_year") is not None

    # Pushing a fresh token through the action (as the Chrome extension does) starts fetching.
    await hass.services.async_call(
        DOMAIN, "update_token",
        {"access_token": make_token(), "access_token_date_updated": ""}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api["hours"].called
    assert entry.options["access_token"] == entry.runtime_data.client.token


async def test_migrate_upstream_entry(hass, mock_api):
    token = make_token()
    entry = MockConfigEntry(
        domain=DOMAIN, version=4, unique_id="Novafos",
        data={"name": "Novafos", "use_grouped_sensors": True},
        options={"access_token": token, "access_token_date_updated": "2026-01-01T00:00:00"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 5
    assert entry.data["meters"][0]["installation_id"] == METER.installation_id
    assert entry.options == {"access_token": token}
    assert hass.states.get("sensor.novafos_this_year") is not None
