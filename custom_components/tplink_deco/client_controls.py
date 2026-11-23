"""Optional device-page controls for a single selected Deco client."""

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity import Entity
import voluptuous as vol

from .const import CONF_EXPERIMENTAL_CLIENT_BLOCK_CONTROLS
from .const import COORDINATOR_CLIENTS_KEY
from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .const import SERVICE_SET_CLIENT_BLOCKED
from .device import create_device_info
from .services import CLIENT_BLOCKLIST_ERRORS
from .services import SET_CLIENT_BLOCKED_SCHEMA

_LOGGER = logging.getLogger(__name__)


async def async_setup_client_block_controls(hass, entry, data):
    """Probe availability once, only when the controls are explicitly enabled."""
    if not entry.data.get(CONF_EXPERIMENTAL_CLIENT_BLOCK_CONTROLS, False):
        return None
    controls = ClientBlockControls(
        hass, entry, data[COORDINATOR_DECOS_KEY], data[COORDINATOR_CLIENTS_KEY]
    )
    if not await controls.async_refresh():
        _LOGGER.info("Experimental client block controls are unavailable on this Deco")
        return None
    return controls


class ClientBlockControls:
    """Shared selection and last verified block list for one Deco entry."""

    def __init__(self, hass, entry, decos, clients):
        self.hass = hass
        self.entry = entry
        self.decos = decos
        self.clients = clients
        self.signal = f"{DOMAIN}_client_block_controls_{entry.entry_id}"
        self.selected_mac: str | None = None
        self.blocked: dict[str, str | None] = {}
        self.status_known = False

    @property
    def options(self) -> dict[str, str]:
        """Return labelled choices, including blocked clients not seen by polling."""
        names = dict(self.blocked)
        for mac, client in (self.clients.data or {}).items():
            try:
                mac = SET_CLIENT_BLOCKED_SCHEMA({"mac": mac, "blocked": False})["mac"]
            except vol.Invalid:
                continue
            if client.online or mac == self.selected_mac or mac in names:
                names[mac] = client.name or names.get(mac)
        if self.selected_mac is not None:
            names.setdefault(self.selected_mac, None)
        options = {}
        for mac, name in sorted(
            names.items(), key=lambda item: ((item[1] or "").casefold(), item[0])
        ):
            if not self.status_known:
                status = "status unknown"
            else:
                status = "blocked" if mac in self.blocked else "not blocked"
            options[f"{name or 'Client'} ({mac}) - {status}"] = mac
        return options

    async def async_refresh(self) -> bool:
        """Refresh status without modifying the router."""
        try:
            async with asyncio.timeout(5):
                self.blocked = await self.decos.api.async_get_blocked_clients()
        except (*CLIENT_BLOCKLIST_ERRORS, TimeoutError):
            self.mark_status_unknown()
            return False
        self.status_known = True
        async_dispatcher_send(self.hass, self.signal)
        return True

    @callback
    def mark_status_unknown(self) -> None:
        self.status_known = False
        async_dispatcher_send(self.hass, self.signal)

    @callback
    def apply_verified_state(self, mac, blocked, name) -> None:
        """Update the selected status after a verified write or no-op."""
        if blocked:
            self.blocked[mac] = name
        else:
            self.blocked.pop(mac, None)
        async_dispatcher_send(self.hass, self.signal)


class _DecoAccessEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, controls, suffix):
        self.controls = controls
        self._attr_unique_id = f"{controls.entry.entry_id}_{suffix}"

    @property
    def device_info(self) -> DeviceInfo | None:
        coordinator = self.controls.decos
        master = coordinator.data.master_deco
        if master is None:
            return None
        return create_device_info(master, master, coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.controls.signal, self.async_write_ha_state
            )
        )


class DecoClientAccessSelect(_DecoAccessEntity, SelectEntity):
    """Select a client by name and show its block list status."""

    _attr_translation_key = "client_access"
    _attr_icon = "mdi:devices"

    def __init__(self, controls):
        super().__init__(controls, "client_access_select")

    @property
    def options(self) -> list[str]:
        return list(self.controls.options)

    @property
    def current_option(self) -> str | None:
        return next(
            (
                label
                for label, mac in self.controls.options.items()
                if mac == self.controls.selected_mac
            ),
            None,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.controls.clients.async_add_listener(self.async_write_ha_state)
        )

    async def async_select_option(self, option: str) -> None:
        context = self._context
        if context is not None and context.user_id:
            user = await self.hass.auth.async_get_user(context.user_id)
            if user is None or not user.is_admin:
                raise Unauthorized(context=context)
        if (mac := self.controls.options.get(option)) is None:
            raise ServiceValidationError("This client is no longer in the list")
        self.controls.selected_mac = mac
        if not await self.controls.async_refresh():
            raise ServiceValidationError("Could not read the Deco block list")


class DecoClientAccessButton(_DecoAccessEntity, ButtonEntity):
    """Invoke the existing administrator-only action for the selected client."""

    def __init__(self, controls, blocked: bool):
        super().__init__(controls, "block_client" if blocked else "unblock_client")
        self._blocked = blocked
        self._attr_translation_key = "block_client" if blocked else "unblock_client"
        self._attr_icon = "mdi:lock" if blocked else "mdi:lock-open"

    @property
    def available(self) -> bool:
        return self.controls.selected_mac is not None

    async def async_press(self) -> None:
        if (mac := self.controls.selected_mac) is None:
            raise ServiceValidationError("Select a Deco client first")
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CLIENT_BLOCKED,
            {
                ATTR_CONFIG_ENTRY_ID: self.controls.entry.entry_id,
                "mac": mac,
                "blocked": self._blocked,
            },
            blocking=True,
            context=self._context,
        )
        if not self.controls.status_known:
            await self.controls.async_refresh()


class DecoClientAccessRefreshButton(_DecoAccessEntity, ButtonEntity):
    """Refresh the displayed state after changes made in the Deco app."""

    _attr_translation_key = "refresh_client_block_list"
    _attr_icon = "mdi:refresh"

    def __init__(self, controls):
        super().__init__(controls, "refresh_client_block_list")

    async def async_press(self) -> None:
        if not await self.controls.async_refresh():
            raise ServiceValidationError("Could not read the Deco block list")
