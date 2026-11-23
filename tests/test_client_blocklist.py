"""Tests for the experimental single-client block list action."""

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
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.const import SERVICE_SET_CLIENT_BLOCKED
from custom_components.tplink_deco.exceptions import TimeoutException
from custom_components.tplink_deco.exceptions import UnexpectedApiException
from custom_components.tplink_deco.services import SET_CLIENT_BLOCKED_SCHEMA
from custom_components.tplink_deco.services import async_setup_services

MAC = "02:00:00:00:00:01"
EMPTY_LIST = {"result": {"client_list": {}}}
BLOCKED_LIST = {"result": {"client_list": [{"mac": "02-00-00-00-00-01"}]}}


@pytest.fixture
def api():
    api = TplinkDecoApi(
        MagicMock(), "http://192.0.2.1", "admin", "test", verify_ssl=False
    )
    api.async_login_if_needed = AsyncMock()
    api._encode_payload = MagicMock(side_effect=lambda payload: payload)
    api._decrypt_data = MagicMock(side_effect=lambda _context, data: data)
    api._async_post = AsyncMock()
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_change_checks_blocklist_before_and_after_write(api, blocked):
    before, after = (
        (EMPTY_LIST, BLOCKED_LIST) if blocked else (BLOCKED_LIST, EMPTY_LIST)
    )
    api._async_post.side_effect = [{"data": before}, {"error_code": 0}, {"data": after}]

    assert await api.async_set_client_blocked(MAC, blocked, "Test client")
    requests = [call.kwargs for call in api._async_post.await_args_list]
    assert all(call["params"] == {"form": "black_list"} for call in requests)
    assert [call["data"]["operation"] for call in requests] == [
        "list",
        "block" if blocked else "unblock",
        "list",
    ]
    assert requests[1]["data"]["params"]["client_list"] == (
        [
            {
                "mac": "02-00-00-00-00-01",
                "name": base64.b64encode(b"Test client").decode(),
                "client_type": "other",
            }
        ]
        if blocked
        else ["02-00-00-00-00-01"]
    )


@pytest.mark.asyncio
async def test_unsupported_list_shape_does_not_trigger_a_write(api):
    api._async_post.return_value = {"error_code": 0}
    with pytest.raises(UnexpectedApiException, match="invalid blocked client list"):
        await api.async_set_client_blocked(MAC, True)
    api._async_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_blocked_is_not_written_again(api):
    api._async_post.return_value = {"data": BLOCKED_LIST}
    assert not await api.async_set_client_blocked(MAC, True)
    api._async_post.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_does_not_repeat_the_write(api):
    api._async_post.side_effect = [{"data": EMPTY_LIST}, TimeoutException()]
    with pytest.raises(TimeoutException):
        await api.async_set_client_blocked(MAC, True)
    assert api._async_post.await_count == 2


@pytest.mark.asyncio
async def test_success_response_without_list_change_is_not_success(api):
    api._async_post.side_effect = [
        {"data": EMPTY_LIST},
        {"error_code": 0},
        {"data": EMPTY_LIST},
    ]
    with pytest.raises(UnexpectedApiException, match="did not confirm"):
        await api.async_set_client_blocked(MAC, True)


@pytest.mark.parametrize("mac", ["", "ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"])
def test_invalid_target_is_rejected(mac):
    with pytest.raises(vol.Invalid):
        SET_CLIENT_BLOCKED_SCHEMA({"mac": mac, "blocked": True})


@pytest.mark.asyncio
async def test_only_admin_can_call_action_and_unknown_client_can_be_unblocked():
    set_blocked = AsyncMock(return_value=True)
    entry = SimpleNamespace(entry_id="test-entry", state=ConfigEntryState.LOADED)
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]
    hass.auth.async_get_user = AsyncMock(return_value=SimpleNamespace(is_admin=False))
    hass.async_run_hass_job.side_effect = lambda job, call: job.target(call)
    hass.data = {
        DOMAIN: {
            entry.entry_id: {
                COORDINATOR_DECOS_KEY: SimpleNamespace(
                    api=SimpleNamespace(async_set_client_blocked=set_blocked)
                ),
                COORDINATOR_CLIENTS_KEY: SimpleNamespace(data={}),
            }
        }
    }
    async_setup_services(hass)
    registration = hass.services.async_register.call_args
    domain, service, handler = registration.args[:3]
    assert (domain, service) == (DOMAIN, SERVICE_SET_CLIENT_BLOCKED)
    call = SimpleNamespace(
        data={"mac": MAC, "blocked": True}, context=Context(user_id="id")
    )

    with pytest.raises(Unauthorized):
        await handler(call)
    hass.auth.async_get_user.return_value.is_admin = True
    with pytest.raises(ServiceValidationError, match="known client"):
        await handler(call)
    set_blocked.assert_not_awaited()

    call.data["blocked"] = False
    assert await handler(call) == {"blocked": False, "changed": True}
    set_blocked.assert_awaited_once_with(MAC, False, "ha-deco test client")
