"""Tests for experimental Deco Wi-Fi controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

from homeassistant.const import CONF_HOST
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized
import pytest

from custom_components.tplink_deco import config_flow
from custom_components.tplink_deco import wifi_optimization as wifi
from custom_components.tplink_deco.const import CONF_DEVICE_LIST_INTERVAL
from custom_components.tplink_deco.const import CONF_EXPERIMENTAL_WIFI_OPTIMIZATION
from custom_components.tplink_deco.const import CONF_WIFI_OPTIMIZATION_EMAIL
from custom_components.tplink_deco.const import CONF_WIFI_OPTIMIZATION_FINGERPRINT


def _quality(state, verdict):
    return {
        "network_quality": {
            "state": state,
            "detail": [{"type": "channel_quality", "state": verdict}],
        }
    }


def test_feature_options_require_owner_email_only_when_enabled():
    assert not config_flow._validate_wifi_optimization({})
    data = {CONF_EXPERIMENTAL_WIFI_OPTIMIZATION: True}
    assert set(config_flow._validate_wifi_optimization(data)) == {
        CONF_WIFI_OPTIMIZATION_EMAIL,
    }
    data[CONF_WIFI_OPTIMIZATION_EMAIL] = "owner@example.test"
    assert not config_flow._validate_wifi_optimization(data)


def _options_flow(monkeypatch, *, stored=None, discovery_error=None):
    entry = SimpleNamespace(
        entry_id="test-entry",
        data={CONF_HOST: "http://192.0.2.1", CONF_DEVICE_LIST_INTERVAL: 30},
    )
    if stored:
        entry.data[CONF_WIFI_OPTIMIZATION_FINGERPRINT] = stored
    flow = config_flow.TplinkDecoOptionsFlowHandler(entry)
    flow.handler = entry.entry_id
    flow.flow_id = "test-flow"
    flow.hass = Mock()
    flow.hass.config_entries.async_get_known_entry.return_value = entry
    flow.hass.async_add_executor_job = AsyncMock(
        return_value="a" * 64, side_effect=discovery_error
    )
    flow.async_create_entry = Mock(return_value={"type": "create_entry"})
    monkeypatch.setattr(config_flow, "async_process_requirements", AsyncMock())
    monkeypatch.setattr(
        config_flow, "_async_test_credentials", AsyncMock(return_value={})
    )
    return flow


@pytest.mark.asyncio
async def test_discovered_fingerprint_is_saved_only_after_confirmation(monkeypatch):
    flow = _options_flow(monkeypatch)
    user_input = {
        CONF_EXPERIMENTAL_WIFI_OPTIMIZATION: True,
        CONF_WIFI_OPTIMIZATION_EMAIL: "owner@example.test",
        CONF_WIFI_OPTIMIZATION_FINGERPRINT: "b" * 64,
    }
    form = await flow.async_step_init(user_input)
    assert form["step_id"] == "wifi_fingerprint"
    assert form["description_placeholders"] == {
        "host": "192.0.2.1",
        "fingerprint": "a" * 64,
    }
    flow.hass.async_add_executor_job.assert_awaited_once_with(
        wifi.read_ssh_fingerprint, "192.0.2.1"
    )
    config_flow._async_test_credentials.assert_not_awaited()
    rejected = await flow.async_step_wifi_fingerprint({"trust_fingerprint": False})
    assert rejected["errors"] == {"base": "wifi_fingerprint_not_confirmed"}
    flow.hass.config_entries.async_update_entry.assert_not_called()

    await flow.async_step_wifi_fingerprint({"trust_fingerprint": True})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert saved[CONF_WIFI_OPTIMIZATION_FINGERPRINT] == "a" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_keep_existing_fingerprint_only_for_the_same_host(monkeypatch, changed):
    flow = _options_flow(monkeypatch, stored="1" * 64)
    form = await flow.async_step_init(
        {
            CONF_HOST: "http://192.0.2.2" if changed else "https://192.0.2.1",
            CONF_EXPERIMENTAL_WIFI_OPTIMIZATION: True,
            CONF_WIFI_OPTIMIZATION_EMAIL: "owner@example.test",
        }
    )
    if changed:
        assert form["step_id"] == "wifi_fingerprint"
        flow.hass.config_entries.async_update_entry.assert_not_called()
    else:
        config_flow.async_process_requirements.assert_not_awaited()
        saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert saved[CONF_WIFI_OPTIMIZATION_FINGERPRINT] == "1" * 64


@pytest.mark.asyncio
async def test_failed_discovery_leaves_options_unchanged(monkeypatch):
    flow = _options_flow(monkeypatch, discovery_error=TimeoutError)
    form = await flow.async_step_init(
        {
            CONF_EXPERIMENTAL_WIFI_OPTIMIZATION: True,
            CONF_WIFI_OPTIMIZATION_EMAIL: "owner@example.test",
        }
    )
    assert form["step_id"] == "init"
    assert form["errors"] == {"base": "wifi_fingerprint_unavailable"}
    flow.hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_manual_renewal_requires_confirmation_and_is_not_saved(monkeypatch):
    flow = _options_flow(monkeypatch, stored="1" * 64)
    form = await flow.async_step_init(
        {
            CONF_EXPERIMENTAL_WIFI_OPTIMIZATION: True,
            CONF_WIFI_OPTIMIZATION_EMAIL: "owner@example.test",
            config_flow.RENEW_WIFI_FINGERPRINT: True,
        }
    )
    assert form["step_id"] == "wifi_fingerprint"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    await flow.async_step_wifi_fingerprint({"trust_fingerprint": True})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert saved[CONF_WIFI_OPTIMIZATION_FINGERPRINT] == "a" * 64
    assert config_flow.RENEW_WIFI_FINGERPRINT not in saved


def test_scan_acknowledgement_does_not_claim_finished(monkeypatch):
    client = Mock()
    client.get.return_value = _quality("idle", "optimal")
    client.set.return_value = {"error_code": 0}
    monkeypatch.setattr(wifi, "sleep", Mock())
    result = wifi._run_wifi_operation(client, False)
    assert result == {
        "advice": "optimal",
        "outcome": "scan_not_observed",
        "acknowledged": True,
    }
    client.set.assert_called_once_with(
        "avira_scan_start", {"scan_type": ["network_quality"]}, confirm=True
    )
    client.raw.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.parametrize(
    ("verdict", "attempted"), [("optimal", False), ("optimizable", True)]
)
def test_optimize_rechecks_advice_and_never_retries(monkeypatch, verdict, attempted):
    client = Mock()
    client.get.side_effect = [_quality("idle", verdict), _quality("idle", "optimal")]
    client.raw.return_value = True
    monkeypatch.setattr(wifi, "sleep", Mock())
    result = wifi._run_wifi_operation(client, True)
    assert result["outcome"] == ("advice_cleared" if attempted else "not_started")
    if attempted:
        client.raw.assert_called_once_with(wifi.OPTIMIZE_OPCODE)
    else:
        client.raw.assert_not_called()
    client.set.assert_not_called()
    client.close.assert_called_once()


def test_timeout_does_not_repeat_optimize():
    client = Mock()
    client.get.return_value = _quality("idle", "optimizable")
    client.raw.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        wifi._run_wifi_operation(client, True)
    client.raw.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_optimization_requires_same_admin_to_press_twice(monkeypatch):
    install = AsyncMock()
    monkeypatch.setattr(wifi, "async_process_requirements", install)
    monkeypatch.setattr(wifi, "async_call_later", Mock(side_effect=lambda *_: Mock()))
    monkeypatch.setattr(wifi, "async_dispatcher_send", Mock())
    hass = Mock()
    hass.async_add_executor_job = AsyncMock(
        return_value={"advice": "optimal", "outcome": "advice_cleared"}
    )
    entry = Mock(entry_id="entry")
    wireless = Mock(async_refresh=AsyncMock())
    controls = wifi.DecoWifiOptimization(
        hass,
        entry,
        Mock(),
        wireless,
        "https://192.0.2.1",
        "owner@example.test",
        "password",
        "1" * 64,
    )
    controls.advice = "optimizable"
    await controls.async_press(True, "admin1")
    assert controls.state == "confirm_optimization"
    await controls.async_press(True, "admin2")
    hass.async_add_executor_job.assert_not_awaited()
    install.assert_not_awaited()

    await controls.async_press(True, "admin2")
    hass.async_add_executor_job.assert_awaited_once_with(controls._execute, True)
    install.assert_awaited_once()
    wireless.async_refresh.assert_awaited_once()
    assert controls.state == "optimal"


@pytest.mark.asyncio
async def test_wifi_buttons_reject_non_admin():
    controls = Mock(async_press=AsyncMock())
    button = wifi.DecoWifiOptimizationButton(controls, "entry", optimize=False)
    button.hass = Mock()
    button.hass.auth.async_get_user = AsyncMock(
        return_value=SimpleNamespace(is_admin=False)
    )
    button._context = Context(user_id="member")
    with pytest.raises(Unauthorized):
        await button.async_press()
    controls.async_press.assert_not_awaited()
