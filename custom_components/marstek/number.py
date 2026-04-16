"""Number platform for Marstek integration."""

from __future__ import annotations

from typing import Any

from pymarstek import build_command

from homeassistant.components.number import NumberEntity
from homeassistant.const import CONF_HOST, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MarstekConfigEntry
from .const import DEFAULT_UDP_PORT, DOMAIN
from .coordinator import MarstekDataUpdateCoordinator

CMD_ES_SET_MODE = "ES.SetMode"


def _build_passive_command(power: int) -> str:
    """Build ES.SetMode command for Passive mode."""
    payload = {
        "id": 0,
        "config": {
            "mode": "Passive",
            "passive_cfg": {
                "power": power,
                "cd_time": 300,
            },
        },
    }
    return build_command(CMD_ES_SET_MODE, payload)


class MarstekPassivePowerNumber(
    CoordinatorEntity[MarstekDataUpdateCoordinator], NumberEntity
):
    """Number entity for passive mode power."""

    _attr_has_entity_name = True
    _attr_name = "Passive Power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:flash"
    _attr_native_min_value = 0
    _attr_native_max_value = 3000
    _attr_native_step = 10
    _attr_mode = "box"

    def __init__(
        self,
        coordinator: MarstekDataUpdateCoordinator,
        device_info: dict[str, Any],
        config_entry: MarstekConfigEntry,
    ) -> None:
        """Initialize passive power number."""
        super().__init__(coordinator)
        self._device_info = device_info
        self._config_entry = config_entry
        self._value = 100.0

        device_identifier = (
            device_info.get("ble_mac")
            or device_info.get("mac")
            or device_info.get("wifi_mac")
            or device_info["ip"]
        )
        device_ip = config_entry.data.get(CONF_HOST, device_info.get("ip", "Unknown"))
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_identifier)},
            name=f"Marstek {device_info['device_type']} v{device_info['version']} ({device_ip})",
            manufacturer="Marstek",
            model=device_info["device_type"],
            sw_version=str(device_info["version"]),
            hw_version=device_info.get("wifi_mac", ""),
        )

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        device_id = (
            self._device_info.get("ble_mac")
            or self._device_info.get("mac")
            or self._device_info.get("wifi_mac")
            or self._device_info.get("ip", "unknown")
        )
        return f"{device_id}_passive_power"

    @property
    def native_value(self) -> float:
        """Return current configured value."""
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Set passive power and apply via ES.SetMode."""
        new_value = int(value)
        host = self._config_entry.data.get(CONF_HOST, self._device_info.get("ip", ""))
        if not isinstance(host, str) or not host:
            return

        await self.coordinator.udp_client.pause_polling(host)
        try:
            await self.coordinator.udp_client.send_request(
                _build_passive_command(new_value),
                host,
                DEFAULT_UDP_PORT,
                timeout=5.0,
                quiet_on_timeout=True,
            )
        finally:
            await self.coordinator.udp_client.resume_polling(host)

        self._value = float(new_value)
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MarstekConfigEntry,
    async_add_entities,
) -> None:
    """Set up number entities for Marstek config entry."""
    coordinator = config_entry.runtime_data.coordinator
    device_info = config_entry.runtime_data.device_info
    async_add_entities([MarstekPassivePowerNumber(coordinator, device_info, config_entry)])
