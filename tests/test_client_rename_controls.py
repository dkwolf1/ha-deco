"""Tests for optional device-page client rename controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.exceptions import Unauthorized
import pytest

from custom_components.tplink_deco import client_controls
from custom_components.tplink_deco import text
from custom_components.tplink_deco.const import CLIENT_RENAME_CONTROLS_KEY
from custom_components.tplink_deco.const import CONF_EXPERIMENTAL_CLIENT_RENAME_CONTROLS
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.const import SERVICE_RENAME_CLIENT

MAC = "02:00:00:00:00:01"
OTHER_MAC = "02:00:00:00:00:02"
LABEL = f"Display ({MAC})"
OTHER_LABEL = f"Tablet ({OTHER_MAC})"


@pytest.fixture
def setup(monkeypatch):
    hass = MagicMock()
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=True))
    hass.services.async_call = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-a", data={CONF_EXPERIMENTAL_CLIENT_RENAME_CONTROLS: True}
    )
    clients = SimpleNamespace(
        data={
            MAC.replace(":", "-"): SimpleNamespace(name="Display", online=True),
            OTHER_MAC: SimpleNamespace(name="Tablet", online=True),
            "02:00:00:00:00:03": SimpleNamespace(name="Offline", online=False),
        }
    )
    data = {COORDINATOR_DECOS_KEY: SimpleNamespace(), COORDINATOR_CLIENTS_KEY: clients}
    hass.data = {DOMAIN: {entry.entry_id: data}}
    monkeypatch.setattr(client_controls, "async_dispatcher_send", MagicMock())
    return hass, entry, data


def _entities(setup):
    controls = client_controls.async_setup_client_rename_controls(*setup)
    entities = [
        client_controls.DecoClientRenameSelect(controls),
        client_controls.DecoClientRenameText(controls),
        client_controls.DecoClientRenameButton(controls),
    ]
    for entity in entities:
        entity.hass = setup[0]
        entity.async_set_context(Context(user_id="admin"))
    return entities


@pytest.mark.asyncio
async def test_controls_require_opt_in(setup):
    hass, entry, data = setup
    entry.data.clear()
    add = MagicMock()
    assert client_controls.async_setup_client_rename_controls(*setup) is None
    await text.async_setup_entry(hass, entry, add)
    add.assert_not_called()

    entry.data[CONF_EXPERIMENTAL_CLIENT_RENAME_CONTROLS] = True
    data[CLIENT_RENAME_CONTROLS_KEY] = (
        client_controls.async_setup_client_rename_controls(*setup)
    )
    await text.async_setup_entry(hass, entry, add)
    assert isinstance(add.call_args.args[0][0], client_controls.DecoClientRenameText)


@pytest.mark.asyncio
async def test_draft_only_writes_when_button_is_pressed(setup):
    hass, _, _ = setup
    select, field, button = _entities(setup)
    assert select.options == [client_controls.NO_CLIENT_SELECTED, LABEL, OTHER_LABEL]
    assert select.current_option == client_controls.NO_CLIENT_SELECTED
    assert not field.available
    assert not button.available
    await select.async_select_option(LABEL)
    await field.async_set_value("Display test")
    assert field.native_value == "Display test"
    hass.services.async_call.assert_not_awaited()

    await button.async_press()
    hass.services.async_call.assert_awaited_once_with(
        DOMAIN,
        SERVICE_RENAME_CLIENT,
        {"config_entry_id": "entry-a", "mac": MAC, "name": "Display test"},
        blocking=True,
        context=button._context,
    )
    assert field.native_value == ""
    assert not button.available


@pytest.mark.asyncio
async def test_changing_client_clears_draft_and_offline_client_cannot_be_renamed(setup):
    hass, _, data = setup
    select, field, button = _entities(setup)
    await select.async_select_option(LABEL)
    await field.async_set_value("Not for tablet")
    await select.async_select_option(OTHER_LABEL)
    assert field.native_value == ""
    await field.async_set_value("Tablet test")
    await select.async_select_option(client_controls.NO_CLIENT_SELECTED)
    assert select.current_option == client_controls.NO_CLIENT_SELECTED
    assert field.native_value == ""
    assert not button.available
    await select.async_select_option(OTHER_LABEL)
    await field.async_set_value("Tablet test")
    data[COORDINATOR_CLIENTS_KEY].data[OTHER_MAC].online = False
    assert select.current_option == client_controls.NO_CLIENT_SELECTED
    with pytest.raises(ServiceValidationError):
        await button.async_press()
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_cannot_select_edit_or_submit(setup):
    hass, _, _ = setup
    select, field, button = _entities(setup)
    hass.auth.async_get_user.return_value.is_admin = False
    with pytest.raises(Unauthorized):
        await select.async_select_option(LABEL)
    with pytest.raises(Unauthorized):
        await field.async_set_value("No")
    with pytest.raises(Unauthorized):
        await button.async_press()
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_action_keeps_draft(setup):
    hass, _, _ = setup
    select, field, button = _entities(setup)
    await select.async_select_option(LABEL)
    await field.async_set_value("Display test")
    hass.services.async_call.side_effect = ServiceValidationError("Not verified")
    with pytest.raises(ServiceValidationError, match="Not verified"):
        await button.async_press()
    assert button.available
    assert field.native_value == "Display test"
