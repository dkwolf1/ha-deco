from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import COORDINATOR_DECOS_KEY
from .const import COORDINATOR_WIRELESS_KEY
from .const import DOMAIN
from .const import SIGNAL_DECO_ADDED
from .const import WIFI_OPTIMIZATION_KEY
from .device import create_device_info

LEGACY_POLLING_UNIQUE_ID = "tplink_deco_polling"


def polling_unique_id(config_entry_id: str) -> str:
    """Return the config-entry-scoped polling switch unique ID."""
    return f"{LEGACY_POLLING_UNIQUE_ID}_{config_entry_id}"


def _migrate_polling_switch_unique_id(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Migrate the legacy polling switch without changing its entity ID."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        SWITCH_DOMAIN, DOMAIN, LEGACY_POLLING_UNIQUE_ID
    )
    if entity_id is None:
        return

    entry = registry.async_get(entity_id)
    if entry is None or entry.config_entry_id != config_entry.entry_id:
        return

    registry.async_update_entity(
        entity_id,
        new_unique_id=polling_unique_id(config_entry.entry_id),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Set up switch."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = data[COORDINATOR_DECOS_KEY]

    _migrate_polling_switch_unique_id(hass, config_entry)
    async_add_entities([DecoPollingSwitch(coordinator)])
    if (wireless := data.get(COORDINATOR_WIRELESS_KEY)) is None:
        return
    if (tmp := data.get(WIFI_OPTIMIZATION_KEY)) is None:
        return
    tracked = set()

    @callback
    def add_wireless_switches() -> None:
        master = coordinator.data.master_deco
        if (
            master is None
            or not wireless.last_update_success
            or wireless.channel_master_mac != master.mac
        ):
            return
        entities = []
        for network in sorted(wireless.toggle_networks):
            key = (master.mac, network)
            if key not in tracked:
                entities.append(
                    DecoOptionalWifiSwitch(wireless, coordinator, master, network, tmp)
                )
                tracked.add(key)
        if entities:
            async_add_entities(entities)

    config_entry.async_on_unload(wireless.async_add_listener(add_wireless_switches))
    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DECO_ADDED, add_wireless_switches)
    )
    add_wireless_switches()


class DecoOptionalWifiSwitch(CoordinatorEntity, SwitchEntity):
    """Control an optional SSID on firmware that reports its enable flag."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, wireless, decos, master, network, tmp):
        super().__init__(wireless)
        self._decos = decos
        self._master_mac = master.mac
        self._network = network
        self._tmp = tmp
        self._attr_translation_key = f"wifi_{network}_switch"
        self._attr_unique_id = f"{master.mac}_wifi_{network}_switch"
        self._attr_device_info = create_device_info(master, master, decos)

    @property
    def available(self) -> bool:
        master = self._decos.data.master_deco
        return (
            super().available
            and not self._decos.paused
            and master is not None
            and master.mac == self._master_mac == self.coordinator.channel_master_mac
            and self._network in self.coordinator.toggle_networks
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.toggle_networks.get(self._network)

    async def _async_set_enabled(self, enabled: bool) -> None:
        context = self._context
        user_id = context.user_id if context is not None else None
        user = await self.hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized()
        if not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="optional_wifi_unavailable"
            )
        try:
            await self._tmp.async_set_network_enabled(
                self._network, self._master_mac, enabled
            )
        except HomeAssistantError:
            await self.coordinator.async_request_refresh()
            raise
        except Exception:
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="optional_wifi_unverified"
            ) from None
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set_enabled(False)


class DecoPollingSwitch(SwitchEntity):
    """Switch to control Deco polling."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_name = "Polling"
        self._attr_unique_id = polling_unique_id(coordinator.config_entry.entry_id)
        self._attr_icon = "mdi:lan-connect"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach switch to the master Deco device."""
        master_deco = self.coordinator.data.master_deco
        return create_device_info(master_deco, master_deco, self.coordinator)

    @property
    def is_on(self) -> bool:
        """Return True if polling is active."""
        return not self.coordinator.paused

    async def async_turn_off(self, **kwargs):
        """Pause polling."""
        self.coordinator.paused = True
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """Resume polling."""
        self.coordinator.paused = False
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()
