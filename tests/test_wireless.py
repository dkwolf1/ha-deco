"""Tests for wireless network status support."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.tplink_deco.api import TplinkDecoApi
from custom_components.tplink_deco.binary_sensor import TplinkDecoWirelessBinarySensor
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import COORDINATOR_MOBILE_KEY
from custom_components.tplink_deco.const import COORDINATOR_WIRELESS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.exceptions import LoginInvalidException
from custom_components.tplink_deco.exceptions import TimeoutException
from custom_components.tplink_deco.sensor import TplinkDecoWirelessChannelSensor
from custom_components.tplink_deco.sensor import async_setup_entry
from custom_components.tplink_deco.wireless import TplinkDecoWirelessUpdateCoordinator
from custom_components.tplink_deco.wireless import WIRELESS_NETWORKS
from custom_components.tplink_deco.wireless import parse_wireless_channels
from custom_components.tplink_deco.wireless import parse_wireless_ssids
from custom_components.tplink_deco.wireless import parse_wireless_states
from custom_components.tplink_deco.wireless import wireless_ssid_attributes


def _coordinator(
    *responses, expose_ssids: bool = False
) -> TplinkDecoWirelessUpdateCoordinator:
    coordinator = object.__new__(TplinkDecoWirelessUpdateCoordinator)
    coordinator.expose_ssids = expose_ssids
    coordinator.api = MagicMock()
    coordinator.api.async_get_wireless = AsyncMock(side_effect=responses)
    coordinator._deco_coordinator = MagicMock(paused=False)
    coordinator.data = {}
    coordinator.ssids = {}
    coordinator.channels = {}
    coordinator.channel_master_mac = None
    coordinator._next_retry = 0.0
    coordinator._consecutive_failures = 0
    return coordinator


def test_parse_wireless_channels_only_exposes_reported_numeric_values():
    config = {
        "band2_4": {
            "host": {"channel": "6", "channel_width": "HT20", "password": "secret"},
            "backhaul": {"channel": 11},
        },
        "band5_1": {
            "host": {"channel": 36, "channel_width": "HE80", "ssid": "private"},
            "guest": {"channel": 44},
        },
        "band5_2": {"host": {"channel": "auto", "channel_width": "auto"}},
        "band6": {"host": {"channel": 37, "channel_width": "EHT320"}},
        "band6_2": {"host": {"enable": False, "channel": 53, "channel_width": 160}},
        "band_fake": {"host": {"channel": 1}},
    }
    assert parse_wireless_channels(config) == {
        "band2_4": {"channel": 6, "channel_width_mhz": 20, "backhaul_channel": 11},
        "band5_1": {"channel": 36, "channel_width_mhz": 80},
        "band6": {"channel": 37, "channel_width_mhz": 320},
    }


@pytest.mark.parametrize("value", [True, False, 0, 234, -1, 6.5, "auto", {}, None])
def test_parse_wireless_channels_ignores_invalid_channel(value):
    assert parse_wireless_channels({"band2_4": {"host": {"channel": value}}}) == {}


@pytest.mark.asyncio
async def test_channel_monitor_uses_existing_poll_and_follows_reported_changes():
    coordinator = _coordinator(
        {"band2_4": {"host": {"enable": True, "channel": 1, "channel_width": 20}}},
        {"band2_4": {"host": {"enable": True, "channel": "6"}}},
        {"band2_4": {"host": {"enable": True, "channel": "auto"}}},
    )
    decos = coordinator._deco_coordinator
    master = decos.data.master_deco
    master.mac = "02:00:00:00:00:01"
    coordinator.last_update_success = True
    with patch("custom_components.tplink_deco.sensor.create_device_info"):
        sensor = TplinkDecoWirelessChannelSensor(coordinator, decos, master, "band2_4")

    assert await coordinator._async_update_data() == {"main": True}
    assert sensor.available
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes == {"channel_width_mhz": 20}
    coordinator.api.async_get_wireless.assert_awaited_once_with("wlan")
    await coordinator._async_update_data()
    assert sensor.native_value == 6
    assert sensor.extra_state_attributes is None
    coordinator.last_update_success = False
    assert not sensor.available
    coordinator.last_update_success = True
    await coordinator._async_update_data()
    assert not sensor.available
    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_channel_sensor_discovery_only_adds_a_reported_band_once():
    entry = MagicMock(entry_id="mesh")
    decos = MagicMock(data=SimpleNamespace(master_deco=None, decos={}))
    wireless = MagicMock(
        last_update_success=True,
        channels={"band2_4": {"channel": 6}, "band5_1": {"backhaul_channel": 44}},
        channel_master_mac="02:00:00:00:00:01",
    )
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                entry.entry_id: {
                    COORDINATOR_DECOS_KEY: decos,
                    COORDINATOR_CLIENTS_KEY: MagicMock(has_successful_refresh=False),
                    COORDINATOR_MOBILE_KEY: MagicMock(data=None),
                    COORDINATOR_WIRELESS_KEY: wireless,
                }
            }
        }
    )
    add_entities = MagicMock()
    with (
        patch("custom_components.tplink_deco.sensor.async_dispatcher_connect"),
        patch("custom_components.tplink_deco.sensor.create_device_info"),
        patch("homeassistant.helpers.entity_registry.async_get"),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
            return_value=[],
        ),
    ):
        await async_setup_entry(hass, entry, add_entities)
        on_wireless_update = wireless.async_add_listener.call_args.args[0]
        decos.data.master_deco = SimpleNamespace(mac="02:00:00:00:00:02")
        on_wireless_update()
        add_entities.assert_not_called()
        decos.data.master_deco = SimpleNamespace(mac=wireless.channel_master_mac)
        on_wireless_update()
        on_wireless_update()

        add_entities.assert_called_once()
        (channel,) = add_entities.call_args.args[0]
        assert channel.unique_id == "02:00:00:00:00:01_wifi_channel_band2_4"
        wireless.channels["band2_4"]["channel_width_mhz"] = 40
        on_wireless_update()
        on_wireless_update()

    assert add_entities.call_count == 2
    (width,) = add_entities.call_args.args[0]
    assert width.unique_id == "02:00:00:00:00:01_wifi_channel_width_band2_4"
    assert width.native_value == 40
    assert width.native_unit_of_measurement == "MHz"
    assert width.available
    assert channel.extra_state_attributes == {"channel_width_mhz": 40}
    del wireless.channels["band2_4"]["channel_width_mhz"]
    assert not width.available


def test_parse_wireless_states_supports_multiple_firmware_values():
    config = {
        "band2_4": {
            "host": {"enable": "on", "ssid": "private"},
            "guest": {"enable": 0, "password": "private"},
        },
        "band5_1": {
            "host": {"enable": True},
            "guest": {"enable": "enabled"},
        },
        "iot": {"host": {"enable": "off", "psk": "private"}},
    }

    assert parse_wireless_states(config) == {
        "main": True,
        "guest": True,
        "iot": False,
    }


def test_parse_wireless_states_omits_networks_not_exposed_by_firmware():
    config = {"band2_4": {"host": {"enable": "1"}}}

    assert parse_wireless_states(config) == {"main": True}


def test_parse_wireless_states_returns_no_sensitive_values():
    config = {
        "band2_4": {
            "guest": {
                "enable": True,
                "ssid": ["private-network"],
                "password": {"value": "private-password"},
            }
        }
    }

    states = parse_wireless_states(config)

    assert states == {"guest": True}
    assert "private" not in repr(states)


def test_parse_wireless_ssids_decodes_names_per_band():
    config = {
        "band2_4": {
            "host": {"enable": True, "ssid": "SG9tZQ==", "password": "secret"},
            "guest": {"enable": True, "ssid": "Ospiti"},
        },
        "band5_1": {"host": {"enable": True, "ssid": "SG9tZS01Rw=="}},
        "iot": {"host": {"enable": True, "ssid": "Q2FzYS1Jb1Q=", "psk": "secret"}},
    }

    ssids = parse_wireless_ssids(config)

    assert ssids == {
        "main": {"band2_4": "Home", "band5_1": "Home-5G"},
        "guest": {"band2_4": "Ospiti"},
        "iot": {"iot": "Casa-IoT"},
    }
    assert "secret" not in repr(ssids)


def test_parse_wireless_ssids_ignores_missing_and_non_string_values():
    config = {
        "band2_4": {
            "host": {"enable": True, "ssid": ""},
            "guest": {"enable": True, "ssid": ["private-network"]},
        },
        "iot": {"host": {"enable": False}},
    }

    assert parse_wireless_ssids(config) == {}


def test_wireless_ssid_attributes_uses_single_ssid_for_shared_name():
    names = {"band2_4": "Home", "band5_1": "Home", "band6": "Home"}

    assert wireless_ssid_attributes(names) == {"ssid": "Home"}


def test_wireless_ssid_attributes_uses_per_band_ssids_for_different_names():
    names = {"band5_1": "Home-5G", "band2_4": "Home"}

    assert wireless_ssid_attributes(names) == {
        "ssid_band2_4": "Home",
        "ssid_band5_1": "Home-5G",
    }


def _wireless_sensor(key: str, ssids: dict) -> TplinkDecoWirelessBinarySensor:
    sensor = object.__new__(TplinkDecoWirelessBinarySensor)
    sensor.coordinator = MagicMock(ssids=ssids)
    sensor.entity_description = next(
        network for network in WIRELESS_NETWORKS if network.key == key
    )
    return sensor


def test_wireless_binary_sensor_omits_ssid_attribute_without_name():
    assert _wireless_sensor("guest", {}).extra_state_attributes is None


def test_wireless_binary_sensor_exposes_ssid_attribute():
    sensor = _wireless_sensor("guest", {"guest": {"band2_4": "Ospiti"}})

    assert sensor.extra_state_attributes == {"ssid": "Ospiti"}


def test_main_wifi_sensor_is_disabled_by_default():
    main_network = next(
        network for network in WIRELESS_NETWORKS if network.key == "main"
    )

    assert main_network.entity_registry_enabled_default is False


@pytest.mark.asyncio
async def test_wireless_coordinator_retries_next_poll_after_single_failure():
    coordinator = _coordinator(
        OSError("temporary failure"),
        {"band2_4": {"guest": {"enable": "on"}}},
    )

    with patch(
        "custom_components.tplink_deco.wireless.monotonic",
        side_effect=[100.0, 110.0],
    ):
        with pytest.raises(UpdateFailed, match="Unable to read wireless status"):
            await coordinator._async_update_data()

        assert coordinator._next_retry == 0.0
        assert await coordinator._async_update_data() == {"guest": True}
        assert coordinator._consecutive_failures == 0


@pytest.mark.asyncio
async def test_wireless_coordinator_does_not_store_ssids_by_default():
    coordinator = _coordinator(
        {"band2_4": {"guest": {"enable": "on", "ssid": "Ospiti"}}}
    )

    assert await coordinator._async_update_data() == {"guest": True}
    assert coordinator.ssids == {}


@pytest.mark.asyncio
async def test_wireless_coordinator_stores_ssids_when_enabled():
    coordinator = _coordinator(
        {"band2_4": {"guest": {"enable": "on", "ssid": "Ospiti"}}},
        expose_ssids=True,
    )

    assert await coordinator._async_update_data() == {"guest": True}
    assert coordinator.ssids == {"guest": {"band2_4": "Ospiti"}}


@pytest.mark.asyncio
async def test_wireless_coordinator_defers_after_repeated_failures():
    coordinator = _coordinator(
        OSError("temporary failure"),
        OSError("temporary failure"),
        {"band2_4": {"guest": {"enable": "on"}}},
    )

    with patch(
        "custom_components.tplink_deco.wireless.monotonic",
        side_effect=[100.0, 110.0, 200.0, 411.0],
    ):
        with pytest.raises(UpdateFailed, match="Unable to read wireless status"):
            await coordinator._async_update_data()

        with pytest.raises(UpdateFailed, match="Unable to read wireless status"):
            await coordinator._async_update_data()

        with pytest.raises(UpdateFailed, match="retry is deferred"):
            await coordinator._async_update_data()

        assert await coordinator._async_update_data() == {"guest": True}
        assert coordinator._next_retry == 0.0


def test_wireless_coordinator_reset_retry_clears_deferral():
    coordinator = _coordinator()
    coordinator._next_retry = 500.0
    coordinator._consecutive_failures = 3

    coordinator.reset_retry()

    assert coordinator._next_retry == 0.0
    assert coordinator._consecutive_failures == 0


@pytest.mark.asyncio
async def test_wireless_coordinator_reports_exception_type_for_empty_errors():
    coordinator = _coordinator(TimeoutError())

    with pytest.raises(
        UpdateFailed, match="Unable to read wireless status: TimeoutError$"
    ):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_wireless_coordinator_propagates_auth_failure():
    coordinator = _coordinator(LoginInvalidException(1))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator._next_retry == 0.0


@pytest.mark.asyncio
async def test_wireless_coordinator_respects_polling_pause():
    coordinator = _coordinator({"band2_4": {"host": {"enable": "on"}}})
    coordinator._deco_coordinator.paused = True
    coordinator.data = {"main": False}

    assert await coordinator._async_update_data() == {"main": False}
    coordinator.api.async_get_wireless.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_get_wireless_uses_read_operation():
    api = TplinkDecoApi(
        session=MagicMock(),
        host="http://192.168.1.1",
        username="admin",
        password="password",
        verify_ssl=False,
    )
    api.async_login_if_needed = AsyncMock()
    api._encode_payload = MagicMock(return_value="payload")
    api._async_post = AsyncMock(return_value={"data": "encrypted"})
    api._decrypt_data = MagicMock(
        return_value={"error_code": 0, "result": {"band2_4": {}}}
    )

    result = await api.async_get_wireless("wlan")

    assert result == {"band2_4": {}}
    api._encode_payload.assert_called_once_with({"operation": "read"})
    api._async_post.assert_awaited_once_with(
        "Get Wireless wlan",
        "http://192.168.1.1/cgi-bin/luci/;stok=None/admin/wireless",
        params={"form": "wlan"},
        data="payload",
    )


@pytest.mark.asyncio
async def test_async_get_wireless_uses_configured_timeout_retries():
    api = TplinkDecoApi(
        session=MagicMock(),
        host="http://192.168.1.1",
        username="admin",
        password="password",
        verify_ssl=False,
        timeout_error_retries=1,
    )
    api._async_get_wireless = AsyncMock(
        side_effect=[TimeoutException("Get Wireless wlan"), {"band2_4": {}}]
    )

    assert await api.async_get_wireless("wlan") == {"band2_4": {}}
    assert api._async_get_wireless.await_count == 2
