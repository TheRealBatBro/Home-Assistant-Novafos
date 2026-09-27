"""Sensors for Novafos."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Meter
from .const import DEFAULT_NAME, DOMAIN, UNITS, statistic_id
from .coordinator import NovafosConfigEntry, NovafosCoordinator

DEVICE_CLASSES = {"water": SensorDeviceClass.WATER, "heating": SensorDeviceClass.ENERGY}
ICONS = {"water": "mdi:water", "heating": "mdi:radiator"}


@dataclass(frozen=True, kw_only=True)
class NovafosSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] = lambda d: {}


METER_SENSORS = (
    NovafosSensorDescription(
        key="last_day",
        translation_key="last_day",
        value_fn=lambda d: d["last_day"],
        attrs_fn=lambda d: {"date": d["last_day_date"]},
    ),
    NovafosSensorDescription(
        key="last_7_days",
        translation_key="last_7_days",
        value_fn=lambda d: d["last_7_days"],
        attrs_fn=lambda d: {"until": d["last_day_date"]},
    ),
    NovafosSensorDescription(
        key="month_to_date",
        translation_key="month_to_date",
        value_fn=lambda d: d["month_to_date"],
    ),
    NovafosSensorDescription(
        key="year_to_date",
        translation_key="year_to_date",
        value_fn=lambda d: d["year_to_date"],
    ),
    NovafosSensorDescription(
        key="data_until",
        translation_key="data_until",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: d["data_until"],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NovafosConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [TokenExpirySensor(coordinator)]
    for meter in coordinator.meters:
        entities.append(StatisticsSensor(coordinator, meter))
        entities.extend(MeterSensor(coordinator, meter, d) for d in METER_SENSORS)
    async_add_entities(entities)


def _meter_device(entry: NovafosConfigEntry, meter: Meter) -> DeviceInfo:
    name = entry.data.get(CONF_NAME, DEFAULT_NAME)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{meter.installation_id}")},
        name=f"{name} {meter.type}" if meter.type != "water" else name,
        manufacturer="Novafos / KMD Easy-Energy",
        model=f"{meter.type.capitalize()} meter" + (f" ({meter.location})" if meter.location else ""),
        serial_number=meter.meter_number,
    )


class NovafosEntity(CoordinatorEntity[NovafosCoordinator]):
    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        # Data is historical; keep showing it while waiting for a new token.
        return True


class MeterSensor(NovafosEntity, RestoreSensor):
    """A consumption figure derived from daily data."""

    entity_description: NovafosSensorDescription

    def __init__(self, coordinator: NovafosCoordinator, meter: Meter, description) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._meter = meter
        self._attr_unique_id = f"{meter.installation_id}_{description.key}"
        self._attr_device_info = _meter_device(coordinator.config_entry, meter)
        if description.device_class is None:
            self._attr_device_class = DEVICE_CLASSES[meter.type]
            self._attr_native_unit_of_measurement = UNITS[meter.type]
            self._attr_suggested_display_precision = 3
            self._attr_icon = ICONS[meter.type]
        self._attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._meter.type in (self.coordinator.data or {}):
            self._update_from(self.coordinator.data[self._meter.type])
        elif (last := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = last.native_value
            if (state := await self.async_get_last_state()) is not None:
                self._attrs = {
                    k: v for k, v in state.attributes.items() if k in ("date", "until")
                }

    def _update_from(self, data: dict[str, Any]) -> None:
        self._attr_native_value = self.entity_description.value_fn(data)
        self._attrs = self.entity_description.attrs_fn(data)

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._meter.type in (self.coordinator.data or {}):
            self._update_from(self.coordinator.data[self._meter.type])
        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs


class StatisticsSensor(NovafosEntity, SensorEntity):
    """Carrier for the imported hourly statistics.

    Its state stays unknown on purpose: the data is hours to days old, and a state
    would be recorded as a current reading and corrupt the imported statistics.
    Use it in statistics cards, apexcharts and the Energy dashboard.
    """

    _attr_translation_key = "statistics"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 3
    _attr_native_value = None

    def __init__(self, coordinator: NovafosCoordinator, meter: Meter) -> None:
        super().__init__(coordinator)
        self._meter = meter
        self.entity_id = statistic_id(meter.type)
        self._attr_unique_id = f"{meter.installation_id}_statistics"
        self._attr_device_info = _meter_device(coordinator.config_entry, meter)
        self._attr_device_class = DEVICE_CLASSES[meter.type]
        self._attr_native_unit_of_measurement = UNITS[meter.type]
        self._attr_icon = ICONS[meter.type]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        until = self.coordinator.last_imported.get(self._meter.type)
        return {"imported_until": until.isoformat() if until else None}


class TokenExpirySensor(NovafosEntity, SensorEntity):
    """When the current access token stops working."""

    _attr_translation_key = "token_expires"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: NovafosCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_token_expires"
        if coordinator.meters:
            self._attr_device_info = _meter_device(entry, coordinator.meters[0])

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.client.token_expires

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "valid": self.coordinator.client.token_valid(),
            "last_error": self.coordinator.last_error,
        }
