"""End-to-end tests for Novafos inside Home Assistant."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.novafos_water.api import NovafosAuthError
from custom_components.novafos_water.const import DOMAIN

from .conftest import FIRST_DAY_OFFSET, LAG_HOURS, METER, make_token

STAT_ID = "novafos_water:water_consumption"
COST_ID = "novafos_water:water_cost"
CONSUMPTION = "sensor.novafos_water_consumption"
LEGACY_ID = "sensor.novafos_water_consumption"
METERS = [{"type": "water", "installation_id": METER.installation_id,
           "measurement_point_id": METER.measurement_point_id, "meter_number": METER.meter_number,
           "location": METER.location, "unit": METER.unit}]


def _entry(token, **kw):
    return MockConfigEntry(domain=DOMAIN, version=1, unique_id="11112222",
                           data={"name": "Novafos Water", "meters": METERS},
                           options={"access_token": token}, **kw)


async def _hourly(hass, stat_id=STAT_ID):
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, dt_util.utcnow() - timedelta(days=60), None,
        {stat_id}, "hour", None, {"sum", "state"},
    )
    return stats.get(stat_id, [])


async def test_config_flow(hass, mock_api):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.novafos_water.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Novafos", "access_token": "Bearer " + make_token()})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "11112222"
    assert result["result"].domain == "novafos_water"
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
    with patch("custom_components.novafos_water.api.NovafosClient.async_login",
               AsyncMock(side_effect=NovafosAuthError("401"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Novafos", "access_token": make_token()})
    assert result["errors"] == {"base": "invalid_auth"}


async def test_sensors_and_statistics(hass, mock_api):
    entry = _entry(make_token())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    last_day = hass.states.get("sensor.novafos_water_last_full_day")
    assert float(last_day.state) == 0.24  # 24 complete hours of 0.01
    assert last_day.attributes["date"] < dt_util.now().date().isoformat()
    assert hass.states.get("sensor.novafos_water_token_expires").state not in ("unknown", "unavailable")

    rows = await _hourly(hass)
    # Everything from the first day up to the last complete hour, nothing after it.
    newest = dt_util.utcnow() - timedelta(hours=LAG_HOURS)
    assert len(rows) >= FIRST_DAY_OFFSET * 24 - LAG_HOURS - 2
    assert all(r["state"] == 0.01 for r in rows)
    assert dt_util.utc_from_timestamp(rows[-1]["start"]) <= newest
    assert abs(rows[-1]["sum"] - 0.01 * len(rows)) < 1e-6
    consumption = hass.states.get(CONSUMPTION)
    assert abs(float(consumption.state) - rows[-1]["sum"]) < 1e-6
    assert consumption.attributes["statistic_id"] == STAT_ID
    assert consumption.attributes["imported_until"] is not None
    assert "state_class" not in consumption.attributes

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
    assert hass.states.get("sensor.novafos_water_this_year") is not None

    # Pushing a fresh token through the action (as the Chrome extension does) starts fetching.
    await hass.services.async_call(
        DOMAIN, "update_token",
        {"access_token": make_token(), "access_token_date_updated": ""}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api["hours"].called
    assert entry.options["access_token"] == entry.runtime_data.client.token



async def test_v70_history_is_moved_without_token(hass, mock_api):
    """v7.0 kept the history on the sensor entity; it must move to the external statistic."""
    start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) - timedelta(days=3)
    stats = [StatisticData(start=start + timedelta(hours=i), state=0.5, sum=0.5 * (i + 1)) for i in range(48)]
    meta = StatisticMetaData(mean_type=StatisticMeanType.NONE, has_sum=True, name=None, source="recorder",
                             statistic_id=LEGACY_ID, unit_class="volume", unit_of_measurement="m³")
    async_import_statistics(hass, meta, stats)
    await async_wait_recording_done(hass)

    entry = _entry(make_token(-60))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    moved = await _hourly(hass)
    assert len(moved) == 48 and moved[-1]["sum"] == 24.0
    assert await _hourly(hass, LEGACY_ID) == []
    assert float(hass.states.get(CONSUMPTION).state) == 24.0
    mock_api["hours"].assert_not_called()


async def test_energy_dashboard_accepts_statistic(hass, mock_api):
    """The Energy dashboard's own validation must report no issues for the water source."""
    from homeassistant.components.energy import data as energy_data, validate
    from homeassistant.setup import async_setup_component

    entry = _entry(make_token())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _set_price(hass, entry, 93.55)

    assert await async_setup_component(hass, "energy", {})
    manager = await energy_data.async_get_manager(hass)
    await manager.async_update({"energy_sources": [{
        "type": "water", "stat_energy_from": STAT_ID, "stat_cost": COST_ID,
        "entity_energy_price": None, "number_energy_price": None,
    }]})
    result = await validate.async_validate(hass)
    issues = result.as_dict()["energy_sources"]
    assert issues == [[]], issues


async def _set_price(hass, entry, price):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"price": price})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def test_price_option_builds_cost_statistic(hass, mock_api):
    entry = _entry(make_token())
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    token = entry.options["access_token"]
    usage = await _hourly(hass)

    # Price only, token field left empty: the token is kept.
    await _set_price(hass, entry, 93.55)
    year = str(dt_util.now().year)
    assert entry.options["prices"] == {year: 93.55}
    assert entry.options["access_token"] == token
    cost = await _hourly(hass, COST_ID)
    assert len(cost) == len(usage)
    assert abs(cost[-1]["sum"] - usage[-1]["sum"] * 93.55) < 0.01

    # Changing the price recalculates the history, without a token.
    entry.runtime_data.client.token = make_token(-60)
    await _set_price(hass, entry, 100)
    cost = await _hourly(hass, COST_ID)
    assert abs(cost[-1]["sum"] - usage[-1]["sum"] * 100) < 0.01
