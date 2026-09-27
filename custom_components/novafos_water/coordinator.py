"""Data coordinator and statistics import for Novafos."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TZ, Meter, NovafosAuthError, NovafosClient, NovafosError
from .const import (
    BACKFILL_DAYS,
    CONF_ACCESS_TOKEN,
    DOMAIN,
    IMPORT_CHUNK_DAYS,
    MAX_PARALLEL_REQUESTS,
    UNIT_CLASSES,
    UNITS,
    UPDATE_INTERVAL,
    statistic_id,
)

_LOGGER = logging.getLogger(__name__)

type NovafosConfigEntry = ConfigEntry[NovafosCoordinator]


def meter_from_dict(d: dict[str, Any]) -> Meter:
    return Meter(**d)


class NovafosCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches daily totals for the sensors and keeps hourly statistics in sync.

    The KMD API only accepts short-lived (1 hour) tokens that the user pushes in.
    While no valid token is present the coordinator keeps its last data and makes
    no requests.
    """

    config_entry: NovafosConfigEntry

    def __init__(self, hass: HomeAssistant, entry: NovafosConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = NovafosClient(
            async_get_clientsession(hass), entry.options.get(CONF_ACCESS_TOKEN, "")
        )
        self.meters = [meter_from_dict(m) for m in entry.data.get("meters", [])]
        self.last_imported: dict[str, datetime] = {}
        self.last_error: str | None = None
        self._sync_task: asyncio.Task | None = None
        self._warned_expired = False

    def set_token(self, token: str) -> bool:
        """Use a new token. Returns False if it is the one already in use."""
        if token == self.client.token:
            return False
        self.client.token = token
        self.client.customer_id = None
        self._warned_expired = False
        return True

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        data = dict(self.data or {})
        if not self.client.token_valid():
            if not self._warned_expired:
                _LOGGER.info(
                    "Novafos access token is missing or expired; waiting for a new one "
                    "(options dialog or the novafos.update_token action)"
                )
                self._warned_expired = True
            return data

        try:
            if self.client.customer_id is None:
                await self.client.async_login()
                await self._async_refresh_meters()
            today = datetime.now(TZ).date()
            first = min(today.replace(month=1, day=1), today - timedelta(days=40))
            for meter in self.meters:
                days = await self.client.async_get_days(meter, first, today)
                data[meter.type] = summarize_days(days, today)
        except NovafosAuthError as err:
            self.last_error = str(err)
            raise UpdateFailed(f"Novafos rejected the access token: {err}") from err
        except NovafosError as err:
            self.last_error = str(err)
            raise UpdateFailed(str(err)) from err

        self.last_error = None
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = self.config_entry.async_create_background_task(
                self.hass, self.async_sync_statistics(), f"{DOMAIN} statistics sync"
            )
        return data

    async def _async_refresh_meters(self) -> None:
        meters = await self.client.async_get_meters()
        if not meters:
            raise NovafosError("No active water or heating meters on this customer")
        stored = [asdict(m) for m in meters]
        if stored != self.config_entry.data.get("meters"):
            first_time = not self.meters
            self.meters = meters
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, "meters": stored}
            )
            if first_time:
                # Sensors are created from the stored meters, so reload to add them.
                self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)

    async def async_sync_statistics(self) -> None:
        """Import all complete hourly values that are not in the recorder yet."""
        for meter in self.meters:
            try:
                await self._sync_meter(meter)
            except NovafosAuthError:
                _LOGGER.info(
                    "Token expired during statistics import for %s; it resumes with the next token",
                    meter.type,
                )
                break
            except NovafosError as err:
                _LOGGER.warning("Statistics import for %s failed: %s", meter.type, err)
        self.async_update_listeners()

    async def _sync_meter(self, meter: Meter) -> None:
        stat_id = statistic_id(meter.type)
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        today = datetime.now(TZ).date()
        if last.get(stat_id):
            row = last[stat_id][0]
            after = datetime.fromtimestamp(row["start"], timezone.utc)
            total = float(row["sum"] or 0.0)
            day = after.astimezone(TZ).date()
            self.last_imported[meter.type] = after + timedelta(hours=1)
        else:
            after = None
            total = 0.0
            first = await self.client.async_get_first_day(meter)
            day = max(first or today, today - timedelta(days=BACKFILL_DAYS))
            _LOGGER.info(
                "Importing %s history from %s; this takes a few minutes", meter.type, day
            )

        sem = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

        async def fetch(d: date) -> list[dict[str, Any]]:
            async with sem:
                return await self.client.async_get_hours(meter, d)

        while day <= today:
            chunk = [day + timedelta(days=i) for i in range(IMPORT_CHUNK_DAYS)]
            chunk = [d for d in chunk if d <= today]
            results = await asyncio.gather(*(fetch(d) for d in chunk))
            hours = [h for r in results for h in r if after is None or h["start"] > after]
            # Stop at the last complete hour: newer values are still zero placeholders.
            # Old chunks are taken as-is, so a stretch of incomplete data can't stall the import.
            if chunk[-1] >= today - timedelta(days=4):
                done = [i for i, h in enumerate(hours) if h["complete"]]
                hours = hours[: done[-1] + 1] if done else []
            if hours:
                stats = []
                for h in hours:
                    total += h["value"]
                    stats.append(
                        StatisticData(start=h["start"], state=h["value"], sum=round(total, 6))
                    )
                async_import_statistics(self.hass, self._metadata(meter), stats)
                after = hours[-1]["start"]
                self.last_imported[meter.type] = after + timedelta(hours=1)
                _LOGGER.debug("Imported %s %s hours up to %s", len(stats), meter.type, after)
                self.async_update_listeners()
            day = chunk[-1] + timedelta(days=1)

    @staticmethod
    def _metadata(meter: Meter) -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=statistic_id(meter.type),
            unit_class=UNIT_CLASSES[meter.type],
            unit_of_measurement=UNITS[meter.type],
        )


def summarize_days(days: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Derive the sensor values from a list of daily rows."""
    complete = [d for d in days if d["complete"]]
    last = complete[-1] if complete else None
    week = [d for d in complete if last and d["date"] > last["date"] - timedelta(days=7)]
    return {
        "last_day": last["value"] if last else None,
        "last_day_date": last["date"].isoformat() if last else None,
        "last_7_days": round(sum(d["value"] for d in week), 3) if last else None,
        "month_to_date": round(
            sum(d["value"] for d in days if d["date"] >= today.replace(day=1)), 3
        ),
        "year_to_date": round(
            sum(d["value"] for d in days if d["date"] >= today.replace(month=1, day=1)), 3
        ),
        "data_until": (
            datetime(last["date"].year, last["date"].month, last["date"].day, tzinfo=TZ)
            + timedelta(days=1)
            if last
            else None
        ),
    }
