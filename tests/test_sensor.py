"""Tests for TP-Link Deco sensors."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from custom_components.tplink_deco import sensor
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import COORDINATOR_MOBILE_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.coordinator import TpLinkDeco
from custom_components.tplink_deco.coordinator import TpLinkDecoClient
from custom_components.tplink_deco.coordinator import TpLinkDecoData
from custom_components.tplink_deco.sensor import TplinkTotalClientDataRateSensor


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["band2_4", "band5"], "band2_4, band5"),
        (["band5"], "band5"),
        ("wired", "wired"),
        ([None, "", {}, "band5", "band2_4"], "band5, band2_4"),
        ([], None),
        (None, None),
        ({}, None),
    ],
)
def test_backhaul_sensor_preserves_all_reported_bands(value, expected):
    description = next(
        item
        for item in sensor.DIAGNOSTIC_SENSOR_DESCRIPTIONS
        if item.key == "connection_type"
    )
    coordinator = MagicMock()
    deco = TpLinkDeco("AA:BB:CC:DD:FF:01")
    coordinator.data = TpLinkDecoData(deco, {deco.mac: deco})
    entity = sensor.TplinkDecoDiagnosticSensor(coordinator, deco.mac, description)
    deco.connection_type = value

    assert entity.native_value == expected
    assert deco.connection_type is value
    assert entity.unique_id == f"{deco.mac}_connection_type"
    assert not entity.entity_registry_enabled_default


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_satellite", "initial_master", "registered", "initial_totals"),
    [
        (False, True, {}, set()),
        (True, True, {}, {"down", "up"}),
        (False, True, {"down": "deco-entry", "up": "deco-entry"}, {"down", "up"}),
        (False, True, {"down": "deco-entry", "up": "another-entry"}, {"down"}),
        (True, False, {}, set()),
    ],
)
async def test_network_totals_follow_inventory_and_preserve_registered_entities(
    initial_satellite, initial_master, registered, initial_totals
):
    master = TpLinkDeco("AA:BB:CC:DD:FF:01")
    master.master = True
    master.name = "Master"
    satellite = TpLinkDeco("AA:BB:CC:DD:FF:02")
    satellite.master = False
    satellite.name = "Satellite"
    satellite.online = False
    decos = MagicMock()
    decos.data = TpLinkDecoData(
        master if initial_master else None, {master.mac: master}
    )
    if initial_satellite:
        decos.data.decos[satellite.mac] = satellite
    clients = MagicMock()
    clients.data = {}
    clients.has_successful_refresh = False
    mobile = MagicMock()
    mobile.data = None
    entry = MagicMock(entry_id="deco-entry")
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                entry.entry_id: {
                    COORDINATOR_DECOS_KEY: decos,
                    COORDINATOR_CLIENTS_KEY: clients,
                    COORDINATOR_MOBILE_KEY: mobile,
                }
            }
        }
    )
    registry = MagicMock()
    registered_ids = {
        f"{master.mac}_total_{direction}": direction for direction in registered
    }
    registry.async_get_entity_id.side_effect = (
        lambda _platform, _domain, uid: registered_ids.get(uid)
    )
    registry.async_get.side_effect = lambda key: SimpleNamespace(
        config_entry_id=registered[key]
    )
    entities = []

    with (
        patch.object(sensor.er, "async_get", return_value=registry),
        patch.object(sensor, "async_dispatcher_connect") as connect,
    ):
        await sensor.async_setup_entry(hass, entry, entities.extend)

    def total_ids():
        return [
            entity.unique_id
            for entity in entities
            if entity.unique_id.startswith(f"{master.mac}_total_")
        ]

    def health_count():
        return sum(
            isinstance(entity, sensor.TplinkCoordinatorHealthSensor)
            for entity in entities
        )

    assert set(total_ids()) == {f"{master.mac}_total_{d}" for d in initial_totals}
    assert f"{master.mac}_down" in {e.unique_id for e in entities}
    health_size = len(sensor.COORDINATOR_HEALTH_SENSOR_DESCRIPTIONS)
    assert health_count() == (health_size if initial_master else 0)

    decos.data.master_deco = master
    decos.data.decos[satellite.mac] = satellite
    on_update = decos.async_add_listener.call_args.args[0]
    on_update()
    connect.call_args.args[2]()
    on_update()

    assert sorted(total_ids()) == [f"{master.mac}_total_down", f"{master.mac}_total_up"]
    assert health_count() == health_size


def _client(
    mac: str,
    deco_mac: str,
    *,
    online: bool,
    down: float,
    up: float,
) -> TpLinkDecoClient:
    client = TpLinkDecoClient(mac)
    client.deco_mac = deco_mac
    client.online = online
    client.down_kilobytes_per_s = down
    client.up_kilobytes_per_s = up
    return client


@pytest.mark.parametrize(
    "client_attribute,expected",
    [
        ("down_kilobytes_per_s", 12.0),
        ("up_kilobytes_per_s", 3.0),
    ],
)
def test_total_data_rate_excludes_offline_clients(client_attribute, expected):
    coordinator_clients = MagicMock()
    coordinator_clients.data = {
        "online": _client(
            "AA:BB:CC:DD:EE:01",
            "AA:BB:CC:DD:FF:01",
            online=True,
            down=12.0,
            up=3.0,
        ),
        "offline": _client(
            "AA:BB:CC:DD:EE:02",
            "AA:BB:CC:DD:FF:01",
            online=False,
            down=5186.0,
            up=24.0,
        ),
    }

    sensor = TplinkTotalClientDataRateSensor(
        MagicMock(),
        coordinator_clients,
        "Total",
        "total",
        client_attribute,
        None,
    )

    assert sensor.native_value == expected


def test_deco_data_rate_only_includes_online_clients_for_that_deco():
    selected_deco = TpLinkDeco("AA:BB:CC:DD:FF:01")
    coordinator_clients = MagicMock()
    coordinator_clients.data = {
        "selected": _client(
            "AA:BB:CC:DD:EE:01",
            selected_deco.mac,
            online=True,
            down=12.0,
            up=3.0,
        ),
        "other_deco": _client(
            "AA:BB:CC:DD:EE:02",
            "AA:BB:CC:DD:FF:02",
            online=True,
            down=50.0,
            up=8.0,
        ),
        "offline": _client(
            "AA:BB:CC:DD:EE:03",
            selected_deco.mac,
            online=False,
            down=5186.0,
            up=24.0,
        ),
    }

    sensor = TplinkTotalClientDataRateSensor(
        MagicMock(),
        coordinator_clients,
        "Selected Deco Down",
        "selected_deco_down",
        "down_kilobytes_per_s",
        selected_deco,
    )

    assert sensor.native_value == 12.0
