"""Buttons for TP-Link Deco."""

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client_controls import DecoClientAccessButton
from .client_controls import DecoClientAccessRefreshButton
from .client_controls import DecoClientRenameButton
from .const import CLIENT_BLOCK_CONTROLS_KEY
from .const import CLIENT_RENAME_CONTROLS_KEY
from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .const import WIFI_OPTIMIZATION_KEY
from .device import create_device_info
from .wifi_optimization import DecoWifiOptimizationButton


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TP-Link Deco buttons."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    entities = [DecoRefreshStatusButton(data[COORDINATOR_DECOS_KEY])]
    if (controls := data.get(CLIENT_BLOCK_CONTROLS_KEY)) is not None:
        entities.extend(
            [
                DecoClientAccessButton(controls, True),
                DecoClientAccessButton(controls, False),
                DecoClientAccessRefreshButton(controls),
            ]
        )
    if (rename_controls := data.get(CLIENT_RENAME_CONTROLS_KEY)) is not None:
        entities.append(DecoClientRenameButton(rename_controls))
    if (wifi := data.get(WIFI_OPTIMIZATION_KEY)) is not None:
        entities.extend(
            [
                DecoWifiOptimizationButton(wifi, config_entry.entry_id, optimize=False),
                DecoWifiOptimizationButton(wifi, config_entry.entry_id, optimize=True),
            ]
        )
    async_add_entities(entities)


class DecoRefreshStatusButton(ButtonEntity):
    """Request an immediate device_list refresh."""

    _attr_has_entity_name = True
    _attr_name = "Refresh Deco status"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator) -> None:
        """Initialize the button."""
        self.coordinator = coordinator
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_refresh_deco_status"
        )

    @property
    def device_info(self) -> DeviceInfo | None:
        """Attach the button to the master Deco device."""
        master_deco = self.coordinator.data.master_deco
        if master_deco is None:
            return None
        return create_device_info(master_deco, master_deco, self.coordinator)

    async def async_press(self) -> None:
        """Refresh Deco status immediately."""
        await self.coordinator.async_request_device_refresh()
