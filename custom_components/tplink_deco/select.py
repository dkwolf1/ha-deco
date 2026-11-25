from homeassistant.components.device_tracker.const import CONF_SCAN_INTERVAL
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client_controls import DecoClientAccessSelect
from .client_controls import DecoClientRenameSelect
from .const import CLIENT_BLOCK_CONTROLS_KEY
from .const import CLIENT_RENAME_CONTROLS_KEY
from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .device import create_device_info

POLLING_INTERVAL_OPTIONS = ["10", "30", "60", "120"]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up select entities."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = data[COORDINATOR_DECOS_KEY]
    entities = [DecoPollingIntervalSelect(hass, config_entry, coordinator)]
    if (controls := data.get(CLIENT_BLOCK_CONTROLS_KEY)) is not None:
        entities.append(DecoClientAccessSelect(controls))
    if (rename_controls := data.get(CLIENT_RENAME_CONTROLS_KEY)) is not None:
        entities.append(DecoClientRenameSelect(rename_controls))
    async_add_entities(entities)


class DecoPollingIntervalSelect(SelectEntity):
    """Select entity to control Deco polling interval."""

    _attr_has_entity_name = True
    _attr_name = "Polling interval"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-cog"
    _attr_options = POLLING_INTERVAL_OPTIONS

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, coordinator):
        self.hass = hass
        self.config_entry = config_entry
        self.coordinator = coordinator
        self._attr_unique_id = f"{config_entry.entry_id}_polling_interval"

    @property
    def current_option(self) -> str:
        """Return current polling interval as string."""
        value = self.config_entry.data.get(CONF_SCAN_INTERVAL, 30)
        return str(value)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Attach select to the master Deco device."""
        master_deco = self.coordinator.data.master_deco
        if master_deco is None:
            return None
        return create_device_info(master_deco, master_deco, self.coordinator)

    async def async_select_option(self, option: str) -> None:
        """Change polling interval."""
        new_value = int(option)

        data = dict(self.config_entry.data)
        data[CONF_SCAN_INTERVAL] = new_value

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=data,
        )

        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
