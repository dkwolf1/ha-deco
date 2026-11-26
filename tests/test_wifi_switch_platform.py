"""Verify that guest and IoT switches only use the configured TMP transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from homeassistant.core import Context
import pytest

from custom_components.tplink_deco.switch import DecoOptionalWifiSwitch


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["guest", "iot"])
async def test_preview_switch_uses_only_pinned_tmp(monkeypatch, network):
    master = SimpleNamespace(mac="test-master")
    decos = SimpleNamespace(data=SimpleNamespace(master_deco=master), paused=False)
    wireless = MagicMock(
        last_update_success=True,
        channel_master_mac=master.mac,
        toggle_networks={network: False},
        async_request_refresh=AsyncMock(),
    )
    wireless.api.async_set_optional_wireless = AsyncMock()
    tmp = SimpleNamespace(async_set_network_enabled=AsyncMock())
    monkeypatch.setattr(
        "custom_components.tplink_deco.switch.create_device_info", MagicMock()
    )
    switch = DecoOptionalWifiSwitch(wireless, decos, master, network, tmp)
    switch.hass = SimpleNamespace(
        auth=SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True))
        )
    )
    switch.async_set_context(Context(user_id="admin"))
    await switch.async_turn_on()
    tmp.async_set_network_enabled.assert_awaited_once_with(network, master.mac, True)
    wireless.api.async_set_optional_wireless.assert_not_awaited()
    wireless.async_request_refresh.assert_awaited_once()
