"""Wireless network status support for TP-Link Deco."""

from dataclasses import dataclass
from datetime import timedelta
import logging
import re
from time import monotonic
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import TplinkDecoApi
from .api import decode_name_with_fallback
from .const import ATTR_SSID
from .const import DOMAIN
from .coordinator import TplinkDecoUpdateCoordinator
from .coordinator import async_call_and_propagate_config_error

_LOGGER: logging.Logger = logging.getLogger(__name__)
WIRELESS_RETRY_INTERVAL_SECONDS = 300
# Failures tolerated before retries are deferred, so a single slow response
# does not make the sensors unavailable for the whole retry interval.
WIRELESS_FAILURES_BEFORE_RETRY_DELAY = 2
WIRELESS_CHANNEL_BANDS = {
    "band2_4": "2.4 GHz",
    "band5": "5 GHz",
    "band5_1": "5 GHz (1)",
    "band5_2": "5 GHz (2)",
    "band6": "6 GHz",
    "band6_2": "6 GHz (2)",
}


@dataclass(frozen=True, kw_only=True)
class WirelessNetworkDescription(BinarySensorEntityDescription):
    """Describe a wireless network exposed by Deco firmware."""

    paths: tuple[tuple[str, ...], ...]


WIRELESS_NETWORKS = (
    WirelessNetworkDescription(
        key="main",
        name="Main WiFi",
        icon="mdi:wifi",
        paths=(("band*", "host"),),
        entity_registry_enabled_default=False,
    ),
    WirelessNetworkDescription(
        key="guest",
        name="Guest WiFi",
        icon="mdi:wifi-lock-open",
        paths=(("band*", "guest"),),
    ),
    WirelessNetworkDescription(
        key="iot",
        name="IoT WiFi",
        icon="mdi:home-automation",
        paths=(("iot", "host"),),
    ),
)


def _key_matches(pattern: str, key: object) -> bool:
    """Return whether a firmware key matches a path segment."""
    key_string = str(key)
    if pattern.endswith("*"):
        return key_string.startswith(pattern[:-1])
    return key_string == pattern


def _resolve_sections(
    config: object, paths: tuple[tuple[str, ...], ...]
) -> list[dict[str, Any]]:
    """Resolve dictionaries addressed by firmware-independent paths."""
    sections = []
    for path in paths:
        nodes = [config]
        for pattern in path:
            nodes = [
                value
                for node in nodes
                if isinstance(node, dict)
                for key, value in node.items()
                if _key_matches(pattern, key)
            ]
        sections.extend(node for node in nodes if isinstance(node, dict))
    return sections


def _is_enabled(value: object) -> bool:
    """Normalize enable flags used by different Deco firmwares."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {
        "1",
        "enable",
        "enabled",
        "on",
        "true",
    }


def parse_wireless_states(config: object) -> dict[str, bool]:
    """Extract only supported network states from a wireless response."""
    states = {}
    for network in WIRELESS_NETWORKS:
        values = [
            _is_enabled(section["enable"])
            for section in _resolve_sections(config, network.paths)
            if "enable" in section
        ]
        if values:
            states[network.key] = any(values)
    return states


def _resolve_band_sections(
    config: object, paths: tuple[tuple[str, ...], ...]
) -> list[tuple[str, dict[str, Any]]]:
    """Resolve network sections together with the top-level band key."""
    sections = []
    for path in paths:
        if not isinstance(config, dict):
            continue
        for key, value in config.items():
            if _key_matches(path[0], key):
                sections.extend(
                    (str(key), section)
                    for section in _resolve_sections(value, (path[1:],))
                )
    return sections


def parse_wireless_ssids(config: object) -> dict[str, dict[str, str]]:
    """Extract the network names per band, ignoring every other setting."""
    ssids = {}
    for network in WIRELESS_NETWORKS:
        names = {}
        for band, section in _resolve_band_sections(config, network.paths):
            ssid = section.get("ssid")
            if isinstance(ssid, str) and ssid:
                name = decode_name_with_fallback(ssid)
                if name:
                    names[band] = name
        if names:
            ssids[network.key] = names
    return ssids


def wireless_ssid_attributes(names: dict[str, str]) -> dict[str, str]:
    """Return one ssid attribute, or one per band when the names differ."""
    if len(set(names.values())) == 1:
        return {ATTR_SSID: next(iter(names.values()))}
    return {f"{ATTR_SSID}_{band}": name for band, name in sorted(names.items())}


def _channel_number(value: object) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value.strip()):
        value = int(value)
    return value if type(value) is int and 1 <= value <= 233 else None


def parse_wireless_channels(config: object) -> dict[str, dict[str, int]]:
    """Extract numeric channels and widths without retaining wireless settings."""
    if not isinstance(config, dict):
        return {}

    channels = {}
    for band in WIRELESS_CHANNEL_BANDS:
        radio = config.get(band)
        if not isinstance(radio, dict):
            continue

        values = {}
        host = radio.get("host")
        if isinstance(host, dict) and (
            "enable" not in host or _is_enabled(host["enable"])
        ):
            if (channel := _channel_number(host.get("channel"))) is not None:
                values["channel"] = channel
            width = host.get("channel_width")
            if isinstance(width, str) and (
                match := re.fullmatch(
                    r"(?:HT|VHT|HE|EHT)?(20|40|80|160|320)(?:\s*MHZ)?",
                    width.strip().upper(),
                )
            ):
                width = int(match[1])
            if type(width) is int and width in (20, 40, 80, 160, 320):
                values["channel_width_mhz"] = width

        backhaul = radio.get("backhaul")
        if isinstance(backhaul, dict) and (
            "enable" not in backhaul or _is_enabled(backhaul["enable"])
        ):
            if (channel := _channel_number(backhaul.get("channel"))) is not None:
                values["backhaul_channel"] = channel
        if values:
            channels[band] = values
    return channels


def summarize_wifi_scan_result(result: dict) -> dict:
    """Expose only documented counters, flags, and channel recommendations."""
    summary = {}
    need_optimize = result.get("need_optimize")
    if isinstance(need_optimize, bool):
        summary["need_optimize"] = need_optimize
    elif type(need_optimize) is int and need_optimize in (0, 1):
        summary["need_optimize"] = bool(need_optimize)
    elif isinstance(need_optimize, str):
        flag = need_optimize.strip().lower()
        if flag in {"0", "1", "true", "false"}:
            summary["need_optimize"] = flag in {"1", "true"}

    for key in ("scan_count", "optimize_count"):
        value = result.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
            value = int(value)
        if type(value) is int and value >= 0:
            summary[key] = value

    items = result.get("optimize_item")
    channels = {}
    if isinstance(items, dict):
        for band, key in (
            ("band2_4", "channel_2g"),
            ("band5_1", "channel_5g"),
            ("band5_2", "channel_5g_2"),
            ("band6", "channel_6g"),
        ):
            candidate = items.get(band, items.get(key))
            if isinstance(candidate, dict):
                candidate = candidate.get(key)
            if (channel := _channel_number(candidate)) is not None:
                channels[band] = channel
    if channels:
        summary["recommended_channels"] = channels

    if summary:
        return {"status": "available", **summary}
    return {"status": "unrecognized_result" if result else "empty_result"}


class TplinkDecoWirelessUpdateCoordinator(DataUpdateCoordinator):
    """Coordinate optional wireless status requests."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: TplinkDecoApi,
        config_entry: ConfigEntry | None,
        deco_coordinator: TplinkDecoUpdateCoordinator,
        update_interval: timedelta | None,
        expose_ssids: bool = False,
    ) -> None:
        self.api = api
        self.expose_ssids = expose_ssids
        self._deco_coordinator = deco_coordinator
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}-wireless",
            update_interval=update_interval,
        )
        self.data = {}
        self.ssids: dict[str, dict[str, str]] = {}
        self.channels: dict[str, dict[str, int]] = {}
        self.channel_master_mac: str | None = None
        self._next_retry = 0.0
        self._consecutive_failures = 0

    @property
    def retry_remaining_seconds(self) -> int:
        """Return seconds remaining before the optional endpoint is retried."""
        return max(0, round(self._next_retry - monotonic()))

    def reset_retry(self) -> None:
        """Allow the next refresh to query the Deco immediately."""
        self._next_retry = 0.0
        self._consecutive_failures = 0

    async def _async_update_data(self) -> dict[str, bool]:
        """Fetch wireless states without affecting integration setup."""
        if self._deco_coordinator.paused:
            _LOGGER.debug("Wireless polling is paused")
            return self.data

        now = monotonic()
        if now < self._next_retry:
            raise UpdateFailed("Wireless status retry is deferred after a failure")

        master = self._deco_coordinator.data.master_deco
        try:
            config = await async_call_and_propagate_config_error(
                self.api.async_get_wireless, "wlan"
            )
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            self._consecutive_failures += 1
            if self._consecutive_failures >= WIRELESS_FAILURES_BEFORE_RETRY_DELAY:
                self._next_retry = now + WIRELESS_RETRY_INTERVAL_SECONDS
            reason = f"{type(err).__name__}: {err}" if str(err) else type(err).__name__
            raise UpdateFailed(f"Unable to read wireless status: {reason}") from err

        self.reset_retry()
        states = parse_wireless_states(config)
        self.ssids = parse_wireless_ssids(config) if self.expose_ssids else {}
        self.channels = parse_wireless_channels(config)
        self.channel_master_mac = master.mac if master is not None else None
        _LOGGER.debug("Wireless status networks=%s", sorted(states))
        return states
