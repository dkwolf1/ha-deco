"""Tests for the default registration of Deco client trackers."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from custom_components.tplink_deco import device_tracker
from custom_components.tplink_deco.const import CONF_CLIENT_POSTFIX
from custom_components.tplink_deco.const import CONF_CLIENT_PREFIX
from custom_components.tplink_deco.const import CONF_DECO_POSTFIX
from custom_components.tplink_deco.const import CONF_DECO_PREFIX
from custom_components.tplink_deco.const import CONF_ENABLE_NEW_CLIENT_TRACKERS
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.coordinator import TpLinkDecoClient


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, False, True])
@pytest.mark.parametrize("inherited_default", [False, True])
async def test_tracker_default_applies_to_initial_and_later_discovery(
    setting, inherited_default, monkeypatch
):
    monkeypatch.setattr(
        device_tracker.ScannerEntity,
        "entity_registry_enabled_default",
        property(lambda self: inherited_default),
    )
    expected = setting is True or inherited_default
    first = TpLinkDecoClient("AA:BB:CC:DD:EE:01")
    second = TpLinkDecoClient("AA:BB:CC:DD:EE:02")
    clients = MagicMock()
    clients.data = {first.mac: first}
    decos = MagicMock()
    entry_data = {
        CONF_CLIENT_PREFIX: "",
        CONF_CLIENT_POSTFIX: "",
        CONF_DECO_PREFIX: "",
        CONF_DECO_POSTFIX: "",
    }
    if setting is not None:
        entry_data[CONF_ENABLE_NEW_CLIENT_TRACKERS] = setting
    entry = SimpleNamespace(entry_id="deco-entry", data=entry_data)
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                entry.entry_id: {
                    COORDINATOR_CLIENTS_KEY: clients,
                    COORDINATOR_DECOS_KEY: decos,
                }
            }
        }
    )
    add_entities = MagicMock()

    with (
        patch.object(device_tracker, "_async_setup_decos") as setup_decos,
        patch.object(device_tracker, "async_dispatcher_connect"),
    ):
        await device_tracker.async_setup_entry(hass, entry, add_entities)

    setup_decos.assert_called_once_with(hass, add_entities, decos, "", "")
    (initial,) = add_entities.call_args.args[0]
    assert initial.entity_registry_enabled_default is expected

    clients.data[second.mac] = second
    (on_update,) = clients.async_add_listener.call_args.args
    on_update()

    assert add_entities.call_count == 2
    (discovered,) = add_entities.call_args.args[0]
    assert discovered.unique_id == second.mac
    assert discovered.entity_registry_enabled_default is expected
