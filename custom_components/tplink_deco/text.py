"""Text fields for TP-Link Deco."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client_controls import DecoClientRenameText
from .const import CLIENT_RENAME_CONTROLS_KEY
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up optional client rename text fields."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    if (controls := data.get(CLIENT_RENAME_CONTROLS_KEY)) is not None:
        async_add_entities([DecoClientRenameText(controls)])
