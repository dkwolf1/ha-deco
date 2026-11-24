"""Tests for Deco node and client trackers."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from custom_components.tplink_deco import device_tracker
from custom_components.tplink_deco.const import ATTR_DECO_DEVICE
from custom_components.tplink_deco.const import ATTR_DECO_MAC
from custom_components.tplink_deco.const import CONF_CLIENT_POSTFIX
from custom_components.tplink_deco.const import CONF_CLIENT_PREFIX
from custom_components.tplink_deco.const import CONF_DECO_POSTFIX
from custom_components.tplink_deco.const import CONF_DECO_PREFIX
from custom_components.tplink_deco.const import CONF_ENABLE_NEW_CLIENT_TRACKERS
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.coordinator import TpLinkDeco
from custom_components.tplink_deco.coordinator import TpLinkDecoClient
from custom_components.tplink_deco.coordinator import TpLinkDecoData


def _node(number, name, **fields):
    deco = TpLinkDeco(f"02:00:00:00:00:{number:02x}")
    deco.update({"group_status": "connected", "custom_nickname": name, **fields})
    return deco


def _topology(*nodes):
    return TpLinkDecoData(
        next((node for node in nodes if node.master), None),
        {node.mac: node for node in nodes},
    )


def test_satellite_tracker_reports_actual_uplink_in_wireless_chain():
    main = _node(1, "Cellar", role="master", bssid_5g="00:00:00:26:B2:75")
    lounge = _node(
        2, "Lounge", bssid_5g="00:00:00:05:78:41", bssid_sta_5g="02-00-00-26-b2-75"
    )
    study = _node(3, "Study", bssid_sta_5g="02-00-00-05-78-41")
    coordinator = MagicMock(data=_topology(study, main, lounge))
    tracker = device_tracker.TplinkDecoDeviceTracker(coordinator, study, "", "")
    assert coordinator.data.uplinks == {lounge.mac: main.mac, study.mac: lounge.mac}
    assert tracker.extra_state_attributes[ATTR_DECO_MAC] == lounge.mac
    assert tracker.extra_state_attributes[ATTR_DECO_DEVICE] == "Lounge"

    lounge.online = False
    coordinator.data = _topology(study, main, lounge)
    assert ATTR_DECO_MAC not in tracker.extra_state_attributes
    assert ATTR_DECO_DEVICE not in tracker.extra_state_attributes


def test_explicit_uplink_takes_priority_and_works_on_ethernet():
    main = _node(1, "Main", role="master", bssid_5g="02:00:00:00:01:01")
    parent = _node(2, "Switch side", device_id="parent")
    child = _node(
        3,
        "Child",
        parent_device_id="parent",
        connection_type=["ethernet"],
        bssid_sta_5g=main.bssid_band5,
    )
    assert _topology(main, parent, child).uplinks == {child.mac: parent.mac}
    child.parent_device_id = None
    assert _topology(main, parent, child).uplinks == {}


def test_exact_bssid_match_wins_over_unique_suffix_and_ambiguity_is_not_guessed():
    main = _node(1, "Main", role="master", bssid_5g="00:00:00:00:01:01")
    parent = _node(2, "Parent", bssid_5g="02:00:00:00:01:01")
    child = _node(3, "Child", bssid_sta_5g="02:00:00:00:01:01")
    assert _topology(main, parent, child).uplinks == {child.mac: parent.mac}
    child.backhaul_bssid_band5 = "06:00:00:00:01:01"
    assert _topology(main, parent, child).uplinks == {}


def test_conflicting_band_matches_are_not_guessed():
    main = _node(1, "Main", role="master", bssid_2g="02:00:00:00:01:01")
    parent = _node(2, "Parent", bssid_5g="02:00:00:00:01:02")
    child = _node(
        3, "Child", bssid_sta_2g=main.bssid_band2_4, bssid_sta_5g=parent.bssid_band5
    )
    assert _topology(main, parent, child).uplinks == {}


@pytest.mark.parametrize(
    "bssid", [None, "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF", "bad"]
)
def test_placeholder_or_invalid_bssid_does_not_match(bssid):
    main = _node(1, "Main", role="master", bssid_5g=bssid)
    child = _node(2, "Child", bssid_sta_5g=bssid)
    assert _topology(main, child).uplinks == {}


def test_explicit_cycles_and_self_links_are_ignored():
    main = _node(1, "Main", role="master")
    first = _node(2, "First", device_id="first", parent_device_id="second")
    second = _node(3, "Second", device_id="second", parent_device_id="first")
    third = _node(4, "Third", device_id="third", parent_device_id="third")
    assert _topology(main, first, second, third).uplinks == {}


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
