"""Tests for the API client that don't need Home Assistant."""

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest

from custom_components.novafos_water.api import NovafosAuthError, NovafosClient, clean_token, token_expiry

from .conftest import METER, make_token


def test_clean_token_formats():
    tok = make_token()
    assert clean_token(tok) == tok
    assert clean_token(f"  Bearer {tok}\n") == tok
    assert clean_token(json.dumps({"access_token": tok, "token_type": "Bearer"})) == tok


def test_token_expiry():
    assert token_expiry(make_token(3600)) > datetime.now(timezone.utc)
    with pytest.raises(NovafosAuthError):
        token_expiry("not-a-jwt")
    assert NovafosClient(None, make_token(-10)).token_valid() is False


async def test_expired_token_makes_no_request():
    session = AsyncMock()
    with pytest.raises(NovafosAuthError):
        await NovafosClient(session, make_token(-10)).async_login()
    session.request.assert_not_called()


def _rows(n):
    return [{"DateFrom": "x", "Value": 0.001, "IsComplete": True} for _ in range(n)]


@pytest.mark.parametrize(("day", "hours", "first_utc"), [
    (date(2026, 9, 25), 24, "2026-09-24T22:00:00+00:00"),
    (date(2026, 10, 25), 25, "2026-10-24T22:00:00+00:00"),  # DST ends: 25 hours
    (date(2027, 3, 28), 23, "2027-03-27T23:00:00+00:00"),  # DST starts: 23 hours
])
async def test_hours_map_to_utc_across_dst(day, hours, first_utc):
    client = NovafosClient(None, make_token())
    client._timeseries = AsyncMock(return_value=_rows(hours))
    out = await client.async_get_hours(METER, day)
    assert len(out) == hours
    assert out[0]["start"].isoformat() == first_utc
    assert len({h["start"] for h in out}) == hours  # no duplicate hours at fall-back
