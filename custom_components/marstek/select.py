"""Select platform for Marstek integration."""

from __future__ import annotations

from typing import Any

from pymarstek import build_command

from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MarstekConfigEntry
from .const import DEFAULT_UDP_PORT, DOMAIN
from .coordinator import MarstekDataUpdateCoordinator

CMD_ES_SET_MODE = "ES.SetMode"
MODE_OPTIONS = ["Auto", "AI", "Manual", "Passive", "Ups"]


def _build_mode_command(mode: str) -> str:
    """Build ES.SetMode command for target mode."""
    if mode == "Auto":
        config: dict[str, Any] = {"mode": "Auto", "auto_cfg": {"enable": 1}}
    elif mode == "AI":
        config = {"mode": "AI", "ai_cfg": {"enable": 1}}
    elif mode == "Manual":
        config = {
            "mode": "Manual",
            "manual_cfg": {
                "time_num": 0,
                "start_time": "00:00",
                "end_time": "23:59",
                "week_set": 127,
                "power": 0,
                "enable": 1,
            },
        }
    elif mode == "Passive":
        config = {"mode": "Passive", "passive_cfg": {"power": 100, "cd_time": 300}}
    else:
        config = {"mode": "Ups", "ups_cfg": {"enable": 1}}

    return build_command(CMD_ES_SET_MODE, {"id": 0, "config": config})


class MarstekModeSelect(CoordinatorEntity[MarstekDataUpdateCoordinator], SelectEntity):
    """Select entity for Marstek mode."""

    _attr_has_entity_name = True
    _attr_name = "Operation Mode"
    _attr_options = MODE_OPTIONS
    _attr_icon = "mdi:cog"

    def __init__(
        self,
        coordinator: MarstekDataUpdateCoordinator,
        device_info: dict[str, Any],
        config_entry: MarstekConfigEntry,
    ) -> None:
        """Initialize mode select."""
        super().__init__(coordinator)
        self._device_info = device_info
        self._config_entry = config_entry

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
        return f"{device_id}_operation_mode"

    @property
    def current_option(self) -> str | None:
        """Return selected option."""
        mode = self.coordinator.data.get("device_mode") if self.coordinator.data else None
        if not isinstance(mode, str):
            return None
        if mode == "UPS":
            return "Ups"
        return mode if mode in MODE_OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        """Select mode option and send ES.SetMode."""
        host = self._config_entry.data.get(CONF_HOST, self._device_info.get("ip", ""))
        if not isinstance(host, str) or not host:
            return

        await self.coordinator.udp_client.pause_polling(host)
        try:
            await self.coordinator.udp_client.send_request(
                _build_mode_command(option),
                host,
                DEFAULT_UDP_PORT,
                timeout=5.0,
                quiet_on_timeout=True,
            )
        finally:
            await self.coordinator.udp_client.resume_polling(host)

        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MarstekConfigEntry,
    async_add_entities,
) -> None:
    """Set up select entities for Marstek config entry."""
    coordinator = config_entry.runtime_data.coordinator
    device_info = config_entry.runtime_data.device_info
    async_add_entities([MarstekModeSelect(coordinator, device_info, config_entry)])
