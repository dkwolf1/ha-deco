"""Optional device-page controls for a single selected Deco client."""

import asyncio
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.text import TextEntity
from homeassistant.const import ATTR_CONFIG_ENTRY_ID
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity import Entity
import voluptuous as vol

from .const import CONF_EXPERIMENTAL_CLIENT_BLOCK_CONTROLS
from .const import CONF_EXPERIMENTAL_CLIENT_RENAME_CONTROLS
from .const import COORDINATOR_CLIENTS_KEY
from .const import COORDINATOR_DECOS_KEY
from .const import DOMAIN
from .const import SERVICE_RENAME_CLIENT
from .const import SERVICE_SET_CLIENT_BLOCKED
from .device import create_device_info
from .services import CLIENT_ACTION_ERRORS
from .services import SET_CLIENT_BLOCKED_SCHEMA
from .services import normalize_client_mac
from .services import validate_client_name

_LOGGER = logging.getLogger(__name__)
NO_CLIENT_SELECTED = "select_client"


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
        except (*CLIENT_ACTION_ERRORS, TimeoutError):
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
    _attr_entity_category = EntityCategory.CONFIG

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
        return [NO_CLIENT_SELECTED, *self.controls.options]

    @property
    def current_option(self) -> str:
        return next(
            (
                label
                for label, mac in self.controls.options.items()
                if mac == self.controls.selected_mac
            ),
            NO_CLIENT_SELECTED,
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
        if option == NO_CLIENT_SELECTED:
            self.controls.selected_mac = None
            async_dispatcher_send(self.hass, self.controls.signal)
            return
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


@callback
def async_setup_client_rename_controls(hass, entry, data):
    """Create controls only when enabled; use the regular client updates."""
    if not entry.data.get(CONF_EXPERIMENTAL_CLIENT_RENAME_CONTROLS, False):
        return None
    return ClientRenameControls(
        hass, entry, data[COORDINATOR_DECOS_KEY], data[COORDINATOR_CLIENTS_KEY]
    )


class ClientRenameControls:
    """Keep a temporary rename draft for one client in one Deco mesh."""

    def __init__(self, hass, entry, decos, clients):
        self.hass = hass
        self.entry = entry
        self.decos = decos
        self.clients = clients
        self.signal = f"{DOMAIN}_client_rename_controls_{entry.entry_id}"
        self.selected_mac: str | None = None
        self.draft = ""
        self.busy = False

    @property
    def options(self) -> dict[str, str]:
        options = {}
        for mac, client in (self.clients.data or {}).items():
            if not client.online:
                continue
            try:
                mac = normalize_client_mac(mac)
            except vol.Invalid:
                continue
            options[f"{client.name or 'Client'} ({mac})"] = mac
        return dict(sorted(options.items(), key=lambda item: item[0].casefold()))

    @property
    def can_edit(self) -> bool:
        return not self.busy and self.selected_mac in self.options.values()

    @callback
    def publish(self) -> None:
        async_dispatcher_send(self.hass, self.signal)


class _DecoRenameEntity(_DecoAccessEntity):
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.controls.clients.async_add_listener(self.async_write_ha_state)
        )

    async def _async_require_admin(self) -> None:
        context = self._context
        if context is not None and context.user_id:
            user = await self.hass.auth.async_get_user(context.user_id)
            if user is None or not user.is_admin:
                raise Unauthorized(context=context)


class DecoClientRenameSelect(_DecoRenameEntity, SelectEntity):
    """Choose which connected client to rename."""

    _attr_translation_key = "client_to_rename"
    _attr_icon = "mdi:devices"

    def __init__(self, controls):
        super().__init__(controls, "client_to_rename")

    @property
    def options(self) -> list[str]:
        return [NO_CLIENT_SELECTED, *self.controls.options]

    @property
    def current_option(self) -> str:
        return next(
            (
                label
                for label, mac in self.controls.options.items()
                if mac == self.controls.selected_mac
            ),
            NO_CLIENT_SELECTED,
        )

    async def async_select_option(self, option: str) -> None:
        await self._async_require_admin()
        if self.controls.busy:
            raise ServiceValidationError("A client rename is already in progress")
        mac = None
        if option != NO_CLIENT_SELECTED:
            if (mac := self.controls.options.get(option)) is None:
                raise ServiceValidationError("This client is no longer connected")
        if mac != self.controls.selected_mac:
            self.controls.selected_mac = mac
            self.controls.draft = ""
            self.controls.publish()


class DecoClientRenameText(_DecoRenameEntity, TextEntity):
    """Hold a draft without sending any request to the Deco."""

    _attr_translation_key = "new_client_name"
    _attr_icon = "mdi:form-textbox"
    _attr_native_min = 0
    _attr_native_max = 32

    def __init__(self, controls):
        super().__init__(controls, "new_client_name")

    @property
    def available(self) -> bool:
        return self.controls.can_edit

    @property
    def native_value(self) -> str:
        return self.controls.draft

    async def async_set_value(self, value: str) -> None:
        selected = self.controls.selected_mac
        await self._async_require_admin()
        if selected != self.controls.selected_mac:
            raise ServiceValidationError(
                "The selected client changed; enter the name again"
            )
        if not self.controls.can_edit:
            raise ServiceValidationError("Select a connected Deco client first")
        try:
            self.controls.draft = validate_client_name(value) if value else ""
        except vol.Invalid as err:
            raise ServiceValidationError(str(err)) from None
        self.controls.publish()


class DecoClientRenameButton(_DecoRenameEntity, ButtonEntity):
    """Submit the selected client and its draft to the verified rename action."""

    _attr_translation_key = "rename_client"
    _attr_icon = "mdi:pencil"

    def __init__(self, controls):
        super().__init__(controls, "rename_client")

    @property
    def available(self) -> bool:
        return self.controls.can_edit and bool(self.controls.draft)

    async def async_press(self) -> None:
        context = self._context
        mac, draft = self.controls.selected_mac, self.controls.draft
        await self._async_require_admin()
        if (mac, draft) != (self.controls.selected_mac, self.controls.draft):
            raise ServiceValidationError(
                "The client or name changed; review and try again"
            )
        if not self.available:
            raise ServiceValidationError("Select a connected client and enter a name")
        self.controls.busy = True
        try:
            self.controls.publish()
            await self.hass.services.async_call(
                DOMAIN,
                SERVICE_RENAME_CLIENT,
                {
                    ATTR_CONFIG_ENTRY_ID: self.controls.entry.entry_id,
                    "mac": mac,
                    "name": draft,
                },
                blocking=True,
                context=context,
            )
            self.controls.draft = ""
        finally:
            self.controls.busy = False
            self.controls.publish()
