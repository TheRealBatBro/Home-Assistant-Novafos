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
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TZ, Meter, NovafosAuthError, NovafosClient, NovafosError
from .const import (
    BACKFILL_DAYS,
    CONF_ACCESS_TOKEN,
    CONF_PRICES,
    DOMAIN,
    IMPORT_CHUNK_DAYS,
    MAX_PARALLEL_REQUESTS,
    UNIT_CLASSES,
    UNITS,
    UPDATE_INTERVAL,
    cost_statistic_id,
    legacy_statistic_id,
    price_for_year,
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
        # Running total (the statistic's sum) through the last imported hour, per meter type.
        self.total: dict[str, float] = {}
        self.last_error: str | None = None
        self._sync_task: asyncio.Task | None = None
        self._cost_lock = asyncio.Lock()
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

    async def async_load_statistics(self) -> None:
        """Read where each meter's history stands; copy v7.0 history over if needed.

        Needs no token, so the consumption sensor has a value right after a restart.
        """
        for meter in self.meters:
            if (last := await self._last_stat(statistic_id(meter.type))) is None:
                last = await self._copy_legacy(meter)
            if last is not None:
                self.last_imported[meter.type] = last[0] + timedelta(hours=1)
                self.total[meter.type] = last[1]

    async def _last_stat(self, stat_id: str) -> tuple[datetime, float] | None:
        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, stat_id, True, {"sum"}
        )
        if not last.get(stat_id):
            return None
        row = last[stat_id][0]
        return datetime.fromtimestamp(row["start"], timezone.utc), float(row["sum"] or 0.0)

    async def _copy_legacy(self, meter: Meter) -> tuple[datetime, float] | None:
        legacy = legacy_statistic_id(meter.type)
        rows = (
            await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                datetime(2000, 1, 1, tzinfo=timezone.utc),
                None,
                {legacy},
                "hour",
                None,
                {"sum", "state"},
            )
        ).get(legacy)
        if not rows:
            return None
        stats = [
            StatisticData(
                start=datetime.fromtimestamp(r["start"], timezone.utc),
                state=r["state"],
                sum=r["sum"],
            )
            for r in rows
        ]
        async_add_external_statistics(self.hass, self._metadata(meter), stats)
        # Queued after the import in the recorder, so the copy is written first.
        get_instance(self.hass).async_clear_statistics([legacy])
        _LOGGER.info(
            "Moved %s hours of %s history to %s", len(stats), meter.type, statistic_id(meter.type)
        )
        return stats[-1]["start"], float(stats[-1]["sum"] or 0.0)

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
        await self.async_rebuild_costs()

    async def async_rebuild_costs(self) -> None:
        """Recalculate the cost statistic from the consumption history and the configured prices.

        Rebuilt in full (a few thousand rows) so a price change also applies to past hours of
        that year. Needs no token.
        """
        prices = self.config_entry.options.get(CONF_PRICES) or {}
        if not prices:
            return
        async with self._cost_lock:
            recorder = get_instance(self.hass)
            await recorder.async_block_till_done()
            for meter in self.meters:
                if meter.type != "water":
                    continue
                stat_id = statistic_id(meter.type)
                rows = (
                    await recorder.async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        datetime(2000, 1, 1, tzinfo=timezone.utc),
                        None,
                        {stat_id},
                        "hour",
                        None,
                        {"state"},
                    )
                ).get(stat_id)
                if not rows:
                    continue
                total, stats = 0.0, []
                for r in rows:
                    start = datetime.fromtimestamp(r["start"], timezone.utc)
                    cost = (r["state"] or 0.0) * price_for_year(prices, start.astimezone(TZ).year)
                    total += cost
                    stats.append(StatisticData(start=start, state=round(cost, 4), sum=round(total, 4)))
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        mean_type=StatisticMeanType.NONE,
                        has_sum=True,
                        name=f"{self.config_entry.data.get('name') or 'Novafos Water'} cost",
                        source=DOMAIN,
                        statistic_id=cost_statistic_id(meter.type),
                        unit_class=None,
                        unit_of_measurement=self.hass.config.currency,
                    ),
                    stats,
                )
                _LOGGER.debug("Cost statistic rebuilt: %s hours, %.2f %s", len(stats), total,
                              self.hass.config.currency)

    async def _sync_meter(self, meter: Meter) -> None:
        today = datetime.now(TZ).date()
        if meter.type in self.last_imported:
            # Tracked here, not re-read: the recorder may not have committed the last import yet.
            after = self.last_imported[meter.type] - timedelta(hours=1)
            total = self.total[meter.type]
            day = after.astimezone(TZ).date()
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
                async_add_external_statistics(self.hass, self._metadata(meter), stats)
                after = hours[-1]["start"]
                self.last_imported[meter.type] = after + timedelta(hours=1)
                self.total[meter.type] = round(total, 6)
                _LOGGER.debug("Imported %s %s hours up to %s", len(stats), meter.type, after)
                self.async_update_listeners()
            day = chunk[-1] + timedelta(days=1)

    def _metadata(self, meter: Meter) -> StatisticMetaData:
        name = self.config_entry.data.get("name") or "Novafos Water"
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name} consumption" if meter.type == "water" else f"{name} {meter.type}",
            source=DOMAIN,
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
