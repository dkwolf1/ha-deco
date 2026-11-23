"""Tests for renaming one connected Deco client."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.exceptions import Unauthorized
import pytest
import voluptuous as vol

from custom_components.tplink_deco.api import TplinkDecoApi
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.const import SERVICE_RENAME_CLIENT
from custom_components.tplink_deco.exceptions import TimeoutException
from custom_components.tplink_deco.exceptions import UnexpectedApiException
from custom_components.tplink_deco.exceptions import UnsupportedFirmwareException
from custom_components.tplink_deco.services import RENAME_CLIENT_SCHEMA
from custom_components.tplink_deco.services import async_setup_services

MAC = "02:00:00:00:00:01"
NEW_NAME = "Caf\u00e9 display"


def _client_list(name, client_type="iot_device", mac=MAC):
    return {
        "data": {
            "result": {
                "client_list": [
                    {
                        "mac": mac,
                        "name": base64.b64encode(name.encode()).decode(),
                        "client_type": client_type,
                    }
                ]
            }
        }
    }


@pytest.fixture
def api():
    api = TplinkDecoApi(
        MagicMock(), "http://192.0.2.1", "admin", "test", verify_ssl=False
    )
    api.async_login_if_needed = AsyncMock()
    api._encode_payload = MagicMock(side_effect=lambda payload: payload)
    api._async_post = AsyncMock()
    return api


@pytest.mark.asyncio
async def test_writes_one_client_with_existing_type_and_reads_it_back(api):
    api._async_post.side_effect = [
        _client_list("Display", mac=MAC.replace(":", "-")),
        {"error_code": 0},
        _client_list(NEW_NAME),
    ]

    assert await api.async_rename_client(MAC, NEW_NAME)
    reads_and_write = [request.kwargs for request in api._async_post.await_args_list]
    assert all(call["params"] == {"form": "client_list"} for call in reads_and_write)
    before, write, after = [call["data"] for call in reads_and_write]
    assert before == after == {"operation": "read", "params": {"device_mac": "default"}}
    assert write == {
        "operation": "write",
        "params": {
            "client_list": [
                {
                    "mac": MAC.replace(":", "-"),
                    "name": base64.b64encode(NEW_NAME.encode()).decode(),
                    "user_set_name_type": True,
                    "client_type": "iot_device",
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_matching_name_is_a_no_op(api):
    api._async_post.return_value = _client_list(NEW_NAME)
    assert not await api.async_rename_client(MAC, NEW_NAME)
    api._async_post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_client_list("Display", client_type=None), UnsupportedFirmwareException),
        (_client_list("Display", mac="02:00:00:00:00:02"), UnexpectedApiException),
        ({"result": {"client_list": {}}}, UnexpectedApiException),
    ],
)
async def test_unverified_client_is_not_written(api, response, error):
    api._async_post.return_value = response
    with pytest.raises(error):
        await api.async_rename_client(MAC, NEW_NAME)
    api._async_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_firmware_is_not_written(api):
    api._outer_id = "legacy"
    with pytest.raises(UnsupportedFirmwareException):
        await api.async_rename_client(MAC, NEW_NAME)
    api._async_post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after", [_client_list("Display"), _client_list(NEW_NAME, "other")]
)
async def test_ack_without_expected_name_and_type_does_not_count_as_success(api, after):
    api._async_post.side_effect = [_client_list("Display"), {"error_code": 0}, after]
    with pytest.raises(UnexpectedApiException, match="did not verify"):
        await api.async_rename_client(MAC, NEW_NAME)
    assert api._async_post.await_count == 3


@pytest.mark.asyncio
async def test_timed_out_write_is_not_retried(api):
    api._async_post.side_effect = [_client_list("Display"), TimeoutException()]
    with pytest.raises(TimeoutException):
        await api.async_rename_client(MAC, NEW_NAME)
    assert api._async_post.await_count == 2


@pytest.mark.parametrize("name", ["", "  ", "bad\nname", "\u00e9" * 17])
def test_schema_refuses_invalid_names(name):
    with pytest.raises(vol.Invalid):
        RENAME_CLIENT_SCHEMA({"mac": MAC, "name": name})


@pytest.mark.asyncio
async def test_action_requires_admin_and_targets_one_mesh():
    hass = MagicMock()
    entries = {
        key: SimpleNamespace(entry_id=key, state=ConfigEntryState.LOADED, domain=DOMAIN)
        for key in ("first", "second")
    }
    rename = {key: AsyncMock(return_value=True) for key in entries}
    hass.config_entries.async_entries.return_value = list(entries.values())
    hass.config_entries.async_get_entry.side_effect = entries.get
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=False))
    hass.async_run_hass_job.side_effect = lambda job, call: job.target(call)
    hass.data = {
        DOMAIN: {
            key: {
                COORDINATOR_DECOS_KEY: SimpleNamespace(
                    api=SimpleNamespace(async_rename_client=method)
                )
            }
            for key, method in rename.items()
        }
    }
    async_setup_services(hass)
    handler = next(
        call.args[2]
        for call in hass.services.async_register.call_args_list
        if call.args[:2] == (DOMAIN, SERVICE_RENAME_CLIENT)
    )
    call = SimpleNamespace(
        data={"mac": MAC, "name": NEW_NAME}, context=Context(user_id="user")
    )
    with pytest.raises(Unauthorized):
        await handler(call)
    hass.auth.async_get_user.return_value.is_admin = True
    with pytest.raises(ServiceValidationError, match="config_entry_id"):
        await handler(call)
    for method in rename.values():
        method.assert_not_awaited()

    call.data["config_entry_id"] = "second"
    assert await handler(call) == {"name": NEW_NAME, "changed": True}
    rename["second"].assert_awaited_once_with(MAC, NEW_NAME)
    rename["first"].assert_not_awaited()
