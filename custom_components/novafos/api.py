"""Async client for the KMD Easy-Energy plugin API used by Novafos."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

API_URL = "https://easy-energy-plugin-api.kmd.dk"
# The API speaks Danish local time regardless of Home Assistant's time zone.
TZ = ZoneInfo("Europe/Copenhagen")

ZOOM_YEAR = 0
ZOOM_MONTH = 1
ZOOM_DAY = 2
ZOOM_HOUR = 3

CONSUMPTION_TYPES = {6: "water", 5: "heating"}


class NovafosError(Exception):
    """Base error."""


class NovafosAuthError(NovafosError):
    """Token missing, malformed, expired or rejected."""


class NovafosApiError(NovafosError):
    """Unexpected API response."""


def token_expiry(token: str) -> datetime:
    """Return the expiry of a KMD access token (JWT). Signature is not checked."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
    except (IndexError, KeyError, ValueError, TypeError) as err:
        raise NovafosAuthError("Not a valid access token") from err


def clean_token(token: str) -> str:
    """Accept a raw token, 'Bearer <token>', or the whole JSON token response."""
    token = token.strip()
    if token.startswith("{"):
        try:
            token = json.loads(token)["access_token"]
        except (ValueError, KeyError, TypeError) as err:
            raise NovafosAuthError("JSON does not contain access_token") from err
    if token.lower().startswith("bearer "):
        token = token[7:]
    return token.strip().strip('"')


@dataclass
class Meter:
    """An active meter on the customer."""

    type: str
    installation_id: int
    measurement_point_id: int
    meter_number: str
    location: str
    unit: dict[str, Any]


def _utc(local: datetime) -> str:
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=TZ)
    nxt = day + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ)


class NovafosClient:
    """Minimal client. One instance per config entry."""

    def __init__(self, session: aiohttp.ClientSession, token: str = "") -> None:
        self._session = session
        self.token = token
        self.customer_id: str | None = None
        self.customer_number: str | None = None
        self.customer_name: str | None = None
        self.address: str | None = None

    @property
    def token_expires(self) -> datetime | None:
        try:
            return token_expiry(self.token)
        except NovafosAuthError:
            return None

    def token_valid(self, margin: timedelta = timedelta(minutes=1)) -> bool:
        exp = self.token_expires
        return exp is not None and exp - margin > datetime.now(timezone.utc)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.token_valid(timedelta(0)):
            raise NovafosAuthError("Access token is missing or expired")
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.customer_id:
            headers["Customer-Id"] = self.customer_id
            headers["Customer-Number"] = self.customer_number or ""
        try:
            async with self._session.request(
                method,
                f"{API_URL}{path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
                **kwargs,
            ) as resp:
                if resp.status in (401, 403):
                    raise NovafosAuthError(f"Token rejected (HTTP {resp.status})")
                if resp.status >= 400:
                    text = await resp.text()
                    raise NovafosApiError(f"HTTP {resp.status} from {path}: {text[:200]}")
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise NovafosApiError(f"Request to {path} failed: {err}") from err
        except TimeoutError as err:
            raise NovafosApiError(f"Request to {path} timed out") from err

    async def async_login(self) -> None:
        """Resolve the customer the token belongs to."""
        self.customer_id = None
        profile = await self._request("GET", "/api/profile/get")
        try:
            customer = next(
                (c for c in profile["Customers"] if c.get("IsDefault")),
                profile["Customers"][0],
            )
        except (KeyError, IndexError, TypeError) as err:
            raise NovafosApiError(f"No customer in profile: {profile}") from err
        self.customer_id = str(customer["Id"])
        # Number arrives as a float (e.g. 1234567.0); send it as an integer.
        number = customer["Number"]
        self.customer_number = str(int(number)) if isinstance(number, float) else str(number)
        self.customer_name = customer.get("FullName")
        self.address = ", ".join(
            p for p in (customer.get("StreetAddress"), customer.get("City")) if p
        )

    async def async_get_meters(self) -> list[Meter]:
        data = await self._request(
            "POST", "/api/meter/customerActiveMeters", json={"IncludeUnits": True}
        )
        meters = []
        for m in data:
            kind = CONSUMPTION_TYPES.get(m.get("ConsumptionTypeId"))
            if kind and m.get("IsActive") and m.get("Units"):
                meters.append(
                    Meter(
                        type=kind,
                        installation_id=m["InstallationId"],
                        measurement_point_id=m["MeasurementPointId"],
                        meter_number=str(m.get("MeterNumber", "")),
                        location=m.get("Location") or "",
                        unit=m["Units"][0],
                    )
                )
        return meters

    async def async_get_first_day(self, meter: Meter) -> date | None:
        """Earliest day the meter has data."""
        data = await self._request("GET", "/api/consumption/availableTimeSeriesPeriods")
        for row in data:
            if row.get("RangeType") == 0 and row.get("MeasurementPointId") in (
                meter.measurement_point_id,
                None,
            ):
                return datetime.fromisoformat(row["MinDate"]).date()
        return None

    async def _timeseries(
        self, meter: Meter, start: datetime, end: datetime, zoom: int
    ) -> list[dict[str, Any]]:
        body = {
            "InstallationId": meter.installation_id,
            "MeasurementPointId": meter.measurement_point_id,
            "Unit": meter.unit,
            "ZoomLevel": zoom,
            "PriceData": "false",
            "Interval": "PT1H",
            "DateFrom": _utc(start),
            "DateTo": _utc(end - timedelta(seconds=1)),
        }
        data = await self._request(
            "POST", "/api/consumption/consumptionTimeSeries", json=body
        )
        series = (data or {}).get("Series") or []
        return series[0].get("Data") or [] if series else []

    async def async_get_hours(self, meter: Meter, day: date) -> list[dict[str, Any]]:
        """Hourly values for one local day as [{start (UTC), value, complete}].

        The API only fills in real values for the first day of an hourly range
        (the rest come back as 0.0), so this must be called one day at a time.
        """
        start, end = _day_bounds(day)
        rows = await self._timeseries(meter, start, end, ZOOM_HOUR)
        # Subtract in UTC: same-zone aware datetimes subtract as wall time (always 24h).
        utc_start = start.astimezone(timezone.utc)
        hours = int((end.astimezone(timezone.utc) - utc_start).total_seconds() // 3600)
        out = []
        for i, row in enumerate(rows):
            if len(rows) == hours:
                # Index-based so DST days (23/25 hours) map to the right UTC hour.
                ts = utc_start + timedelta(hours=i)
            else:
                ts = datetime.fromisoformat(row["DateFrom"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=TZ)
                ts = ts.astimezone(timezone.utc)
            out.append(
                {
                    "start": ts,
                    "value": float(row.get("Value") or 0.0),
                    "complete": bool(row.get("IsComplete")),
                }
            )
        return out

    async def async_get_days(
        self, meter: Meter, first: date, last: date
    ) -> list[dict[str, Any]]:
        """Daily values (inclusive range) as [{date, value, complete}]."""
        start, _ = _day_bounds(first)
        _, end = _day_bounds(last)
        rows = await self._timeseries(meter, start, end, ZOOM_DAY)
        return [
            {
                "date": datetime.fromisoformat(r["DateFrom"]).date(),
                "value": float(r.get("Value") or 0.0),
                "complete": bool(r.get("IsComplete")),
            }
            for r in rows
        ]
