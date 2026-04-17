"""Data update coordinator for Marstek devices."""

from __future__ import annotations

from datetime import timedelta
import asyncio
import json
import logging
from typing import Any

from pymarstek import MarstekUDPClient

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_UDP_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)


class MarstekDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Per-device data update coordinator."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        udp_client: MarstekUDPClient,
        device_ip: str,
    ) -> None:
        """Initialize the coordinator."""
        self.udp_client = udp_client
        self.config_entry = config_entry
        # Use initial IP, but will read from config_entry.data dynamically
        self._initial_device_ip = device_ip
        super().__init__(
            hass,
            _LOGGER,
            name=f"Marstek {device_ip}",
            update_interval=SCAN_INTERVAL,
            config_entry=config_entry,
        )
        _LOGGER.debug(
            "Device %s polling coordinator started, interval: %ss",
            device_ip,
            SCAN_INTERVAL.total_seconds(),
        )

        # Register listener to update entity names when config entry changes
        config_entry.async_on_unload(
            config_entry.add_update_listener(self._async_config_entry_updated)
        )

    @property
    def device_ip(self) -> str:
        """Get current device IP from config entry (supports dynamic IP updates)."""
        if self.config_entry:
            return self.config_entry.data.get(CONF_HOST, self._initial_device_ip)
        return self._initial_device_ip

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch all data using library's get_device_status method."""
        current_ip = self.device_ip
        _LOGGER.debug("Start polling device: %s", current_ip)

        if self.udp_client.is_polling_paused(current_ip):
            _LOGGER.debug("Polling paused for device: %s, skipping update", current_ip)
            return self.data or {}

        try:
            # Use library method to get complete device status
            # Note: get_device_status catches exceptions internally and returns default values
            # So we need to check the returned data to detect failures
            device_status = await self.udp_client.get_device_status(
                current_ip,
                port=DEFAULT_UDP_PORT,
                timeout=5.0,  # Increased timeout for device status requests
                include_pv=True,
                delay_between_requests=5.0,
            )

            # Check if we actually got valid data
            # get_device_status doesn't throw exceptions, it returns default values on failure
            # Default values when both requests fail: device_mode="Unknown", battery_soc=0, battery_power=0
            # We check device_mode because it's the most reliable indicator:
            # - If ES.GetMode succeeds, device_mode will be a real value (not "Unknown")
            # - If ES.GetMode fails, device_mode will be "Unknown" (default)
            device_mode = device_status.get("device_mode", "Unknown")
            battery_soc = device_status.get("battery_soc", 0)
            battery_power = device_status.get("battery_power", 0)

            # If device_mode is "Unknown", ES.GetMode definitely failed
            # This means we got no valid data from the device
            has_valid_data = device_mode != "Unknown"

            if not has_valid_data:
                # ES.GetMode failed (device_mode is default "Unknown")
                # This indicates connection failure - treat as exception
                _LOGGER.warning(
                    "No valid data received from device at %s (device_mode=Unknown, soc=%s, power=%s) - connection failed",
                    current_ip,
                    battery_soc,
                    battery_power,
                )
                error_msg = f"No valid data received from device at {current_ip}"
                # Raise will be caught by outer except block
                raise TimeoutError(error_msg) from None  # noqa: TRY301
            _LOGGER.debug(
                "Device %s poll done: SOC %s%%, Power %sW, Mode %s, Status %s",
                current_ip,
                device_status.get("battery_soc"),
                device_status.get("battery_power"),
                device_status.get("device_mode"),
                device_status.get("battery_status"),
            )
            await self._augment_energy_status(device_status, current_ip)
            self._normalize_pv_power_scaling(device_status)
            return device_status  # noqa: TRY300
        except (TimeoutError, OSError, ValueError) as err:
            # Connection failed - Scanner will detect IP changes and update config entry
            # Coordinator only returns previous data, no discovery here (mik-laj feedback)
            _LOGGER.warning(
                "Device %s status request failed: %s. "
                "Scanner will detect IP changes automatically",
                current_ip,
                err,
            )
            # Return previous data on error
            return self.data or {}

    async def _augment_energy_status(
        self, device_status: dict[str, Any], current_ip: str
    ) -> None:
        """Add energy counters from ES.GetStatus and raw ES.GetMode."""
        request_timeout = 5.0
        retry_attempts = 2

        async def _send_with_retry(
            method: str, request_id: int
        ) -> dict[str, Any] | None:
            for attempt in range(1, retry_attempts + 1):
                try:
                    return await self.udp_client.send_request(
                        json.dumps(
                            {"id": request_id, "method": method, "params": {"id": 0}},
                            separators=(",", ":"),
                        ),
                        current_ip,
                        DEFAULT_UDP_PORT,
                        timeout=request_timeout,
                    )
                except (TimeoutError, OSError, ValueError) as err:
                    _LOGGER.debug(
                        "%s failed for %s on attempt %d/%d: %s",
                        method,
                        current_ip,
                        attempt,
                        retry_attempts,
                        err,
                    )
                    if attempt < retry_attempts:
                        await asyncio.sleep(1.0)
            return None

        # ES.GetStatus exposes total_*_energy values (Wh)
        es_status_response = await _send_with_retry("ES.GetStatus", request_id=1001)
        if isinstance(es_status_response, dict):
            es_status_result = es_status_response.get("result", {})
            if isinstance(es_status_result, dict):
                for key in (
                    "bat_cap",
                    "pv_power",
                    "total_pv_energy",
                    "total_grid_output_energy",
                    "total_grid_input_energy",
                    "total_load_energy",
                ):
                    value = es_status_result.get(key)
                    if isinstance(value, (int, float)):
                        device_status[key] = value

        # Give device a brief pause before next query.
        await asyncio.sleep(1.0)

        # Raw ES.GetMode contains input/output energy in 0.1 Wh scale.
        es_mode_response = await _send_with_retry("ES.GetMode", request_id=1002)
        if isinstance(es_mode_response, dict):
            es_mode_result = es_mode_response.get("result", {})
            if isinstance(es_mode_result, dict):
                for key in (
                    "input_energy",
                    "output_energy",
                    "ct_state",
                    "a_power",
                    "b_power",
                    "c_power",
                    "total_power",
                ):
                    value = es_mode_result.get(key)
                    if isinstance(value, (int, float)):
                        device_status[key] = value

    def _normalize_pv_power_scaling(self, device_status: dict[str, Any]) -> None:
        """Normalize PV power units if payload appears to be deciwatts.

        Some devices/firmwares appear to report PV power in deciwatts for
        individual channels while voltage/current are still in V/A.
        We detect this by comparing reported power against V * I.
        """
        for channel in range(1, 5):
            power_key = f"pv{channel}_power"
            voltage_key = f"pv{channel}_voltage"
            current_key = f"pv{channel}_current"

            power = device_status.get(power_key)
            voltage = device_status.get(voltage_key)
            current = device_status.get(current_key)

            if not all(isinstance(v, (int, float)) for v in (power, voltage, current)):
                continue

            power_w = float(power)
            expected_w = float(voltage) * float(current)
            if expected_w <= 0:
                continue

            # If raw power is ~10x higher than V*I and dividing by 10 aligns,
            # treat it as deciwatts and normalize to watts.
            ratio = power_w / expected_w
            if ratio > 5 and abs((power_w / 10) - expected_w) / expected_w < 0.35:
                normalized = round(power_w / 10, 1)
                _LOGGER.debug(
                    "Normalized %s from %s to %s W (V=%s, I=%s)",
                    power_key,
                    power_w,
                    normalized,
                    voltage,
                    current,
                )
                device_status[power_key] = normalized

        # Fallback heuristic when current/voltage plausibility is unavailable:
        # compare PV channel sum against ES.GetStatus aggregate pv_power.
        aggregate_pv = device_status.get("pv_power")
        if not isinstance(aggregate_pv, (int, float)):
            return
        aggregate_w = float(aggregate_pv)
        if aggregate_w <= 0:
            return

        channel_powers: dict[int, float] = {}
        for channel in range(1, 5):
            value = device_status.get(f"pv{channel}_power")
            if isinstance(value, (int, float)):
                channel_powers[channel] = float(value)

        if not channel_powers:
            return

        raw_sum = sum(channel_powers.values())
        raw_error = abs(raw_sum - aggregate_w)

        # Only attempt correction if channel sum is significantly larger than
        # aggregate PV power, which indicates a likely factor-of-10 outlier.
        if raw_sum <= aggregate_w * 2.0:
            return

        best_channel: int | None = None
        best_scaled_value = 0.0
        best_error = raw_error

        for channel, value in channel_powers.items():
            candidate_sum = raw_sum - value + (value / 10.0)
            candidate_error = abs(candidate_sum - aggregate_w)
            if candidate_error < best_error:
                best_error = candidate_error
                best_channel = channel
                best_scaled_value = round(value / 10.0, 1)

        # Require meaningful improvement to avoid accidental corrections.
        if best_channel is not None and best_error < raw_error * 0.5:
            key = f"pv{best_channel}_power"
            _LOGGER.debug(
                "Normalized %s from %s to %s W using aggregate pv_power=%s",
                key,
                device_status.get(key),
                best_scaled_value,
                aggregate_w,
            )
            device_status[key] = best_scaled_value

    async def _async_config_entry_updated(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Handle config entry update - update entity names if IP changed."""
        if not self.config_entry:
            return
        # Get old IP from coordinator's initial IP
        old_ip = self._initial_device_ip
        new_ip = entry.data.get(CONF_HOST, old_ip)

        if new_ip != old_ip:
            _LOGGER.info(
                "Config entry updated, IP changed from %s to %s, updating entity names",
                old_ip,
                new_ip,
            )
            await self._update_entity_names(new_ip, old_ip)
            # Update initial IP for future comparisons
            self._initial_device_ip = new_ip

    async def _update_entity_names(self, new_ip: str, old_ip: str) -> None:
        """Update device and entity names in registry when IP changes."""
        if not self.config_entry:
            return
        # Update device name in device registry
        device_registry = dr.async_get(self.hass)
        device_identifier = (
            self.config_entry.data.get("ble_mac")
            or self.config_entry.data.get("mac")
            or self.config_entry.data.get("wifi_mac")
        )
        if device_identifier:
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, device_identifier)}
            )
            if device and device.name and old_ip in device.name:
                new_device_name = device.name.replace(old_ip, new_ip)
                _LOGGER.info(
                    "Updating device name from %s to %s",
                    device.name,
                    new_device_name,
                )
                device_registry.async_update_device(device.id, name=new_device_name)

        # Update entity names in entity registry (if any entities have IP in name)
        entity_registry = er.async_get(self.hass)
        entities = er.async_entries_for_config_entry(
            entity_registry, self.config_entry.entry_id
        )

        updated_count = 0
        for entity_entry in entities:
            if entity_entry.name and old_ip in entity_entry.name:
                new_name = entity_entry.name.replace(old_ip, new_ip)
                _LOGGER.debug(
                    "Updating entity %s name from %s to %s",
                    entity_entry.entity_id,
                    entity_entry.name,
                    new_name,
                )
                entity_registry.async_update_entity(
                    entity_entry.entity_id, name=new_name
                )
                updated_count += 1

        if updated_count > 0:
            _LOGGER.info(
                "Updated %d entity name(s) to reflect new IP: %s -> %s",
                updated_count,
                old_ip,
                new_ip,
            )
