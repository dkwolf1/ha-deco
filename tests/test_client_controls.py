"""Tests for the experimental Deco device-page controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.exceptions import Unauthorized
import pytest

from custom_components.tplink_deco import client_controls
from custom_components.tplink_deco.const import CONF_EXPERIMENTAL_CLIENT_BLOCK_CONTROLS
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.const import SERVICE_SET_CLIENT_BLOCKED
from custom_components.tplink_deco.exceptions import UnsupportedFirmwareException

ONLINE = "02:00:00:00:00:01"
BLOCKED = "02:00:00:00:00:02"


@pytest.fixture
def setup():
    hass = MagicMock()
    entry = SimpleNamespace(entry_id="one", data={})
    api = SimpleNamespace(async_get_blocked_clients=AsyncMock(return_value={}))
    decos = SimpleNamespace(api=api)
    clients = SimpleNamespace(
        data={ONLINE.replace(":", "-"): SimpleNamespace(name="Tablet", online=True)}
    )
    return hass, entry, {COORDINATOR_DECOS_KEY: decos, COORDINATOR_CLIENTS_KEY: clients}


@pytest.mark.asyncio
async def test_controls_are_opt_in_and_skip_unsupported_firmware(setup):
    hass, entry, data = setup
    read = data[COORDINATOR_DECOS_KEY].api.async_get_blocked_clients
    assert await client_controls.async_setup_client_block_controls(*setup) is None
    read.assert_not_awaited()

    entry.data[CONF_EXPERIMENTAL_CLIENT_BLOCK_CONTROLS] = True
    read.side_effect = UnsupportedFirmwareException("unsupported")
    with patch.object(client_controls, "async_dispatcher_send"):
        assert await client_controls.async_setup_client_block_controls(*setup) is None
    read.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_client_stays_selectable_and_uses_admin_action(setup):
    hass, entry, data = setup
    entry.data[CONF_EXPERIMENTAL_CLIENT_BLOCK_CONTROLS] = True
    read = data[COORDINATOR_DECOS_KEY].api.async_get_blocked_clients
    read.return_value = {BLOCKED: "Offline laptop"}

    with patch.object(client_controls, "async_dispatcher_send"):
        controls = await client_controls.async_setup_client_block_controls(*setup)
        select = client_controls.DecoClientAccessSelect(controls)
        unblock = client_controls.DecoClientAccessButton(controls, False)
        unblock.hass = hass
        hass.services.async_call = AsyncMock()
        assert select.current_option is None
        assert not unblock.available
        with pytest.raises(ServiceValidationError, match="Select a Deco client"):
            await unblock.async_press()
        assert f"Tablet ({ONLINE}) - not blocked" in select.options
        label = f"Offline laptop ({BLOCKED}) - blocked"
        assert label in select.options
        await select.async_select_option(label)
        assert select.current_option == label
        context = Context(user_id="operator")
        unblock.async_set_context(context)
        await unblock.async_press()
        hass.services.async_call.assert_awaited_once_with(
            DOMAIN,
            SERVICE_SET_CLIENT_BLOCKED,
            {"config_entry_id": "one", "mac": BLOCKED, "blocked": False},
            blocking=True,
            context=context,
        )
        controls.apply_verified_state(BLOCKED, False, "Offline laptop")
        assert select.current_option.endswith(" - not blocked")


@pytest.mark.asyncio
async def test_non_admin_cannot_change_shared_selection(setup):
    hass, entry, data = setup
    controls = client_controls.ClientBlockControls(
        hass, entry, data[COORDINATOR_DECOS_KEY], data[COORDINATOR_CLIENTS_KEY]
    )
    select = client_controls.DecoClientAccessSelect(controls)
    select.hass = hass
    select.async_set_context(Context(user_id="guest"))
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=False))
    with pytest.raises(Unauthorized):
        await select.async_select_option(select.options[0])
    assert controls.selected_mac is None
