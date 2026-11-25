"""Opt-in, on-demand Wi-Fi optimization on the main Deco."""

import asyncio
import hashlib
import socket
from time import monotonic
from time import sleep
from urllib.parse import urlsplit

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.requirements import async_process_requirements

from .const import DOMAIN
from .device import create_device_info

TMP_REQUIREMENT = (
    "pydeco-tmp @ https://github.com/mthsg/pydeco-tmp/archive/"
    "ee0fc9bb3184c8087f2341db88bb1e267a76e747.zip"
)
OPTIMIZE_OPCODE = 0x4266
TMP_SSH_PORT = 20001
VERDICTS = ("optimal", "optimizable")
ADVICE_STATES = (*VERDICTS, "scanning", "confirm_optimization", "optimizing")


def read_ssh_fingerprint(host: str) -> str:
    """Read the server key without sending authentication credentials."""
    from paramiko import SSHException
    from paramiko import Transport

    try:
        with socket.create_connection((host, TMP_SSH_PORT), timeout=5) as sock:
            with Transport(sock) as transport:
                transport.start_client(timeout=5)
                key = transport.get_remote_server_key()
                return hashlib.sha256(key.asbytes()).hexdigest()
    except SSHException as err:
        raise OSError("Unable to read the Deco SSH host key") from err


def _wifi_error(key):
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=key)


def _read_quality(client):
    result = client.get("network_quality", {"scan_type": ["network_quality"]})
    quality = result.get("network_quality") if isinstance(result, dict) else None
    if not isinstance(quality, dict):
        return None, None
    details = quality.get("detail")
    verdict = None
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("type") == "channel_quality":
                verdict = detail.get("state")
                break
    state = quality.get("state")
    return (
        state if state in ("idle", "scanning", "waiting", "optimizing") else None,
        verdict if verdict in (*VERDICTS, "checking") else None,
    )


def _acknowledged(reply):
    return reply is True or (
        isinstance(reply, dict)
        and type(reply.get("error_code")) is int
        and reply["error_code"] == 0
    )


def _run_wifi_operation(client, optimize):
    """Use one TMP session and never retry a write."""
    try:
        client.connect()
        state, before = _read_quality(client)
        if state != "idle" or (optimize and before != "optimizable"):
            return {
                "advice": before if state == "idle" else None,
                "outcome": "not_started",
            }
        if optimize:
            reply = client.raw(OPTIMIZE_OPCODE)
        else:
            reply = client.set(
                "avira_scan_start", {"scan_type": ["network_quality"]}, confirm=True
            )
        accepted = _acknowledged(reply)
        if reply is False:
            return {"advice": before, "outcome": "rejected", "acknowledged": False}

        deadline = monotonic() + 60
        observed = False
        advice = None
        for attempt in range(30):
            if attempt:
                sleep(min(2, max(0, deadline - monotonic())))
            if monotonic() >= deadline:
                break
            state, verdict = _read_quality(client)
            if not optimize and state == "optimizing":
                return {
                    "advice": None,
                    "outcome": "unconfirmed",
                    "acknowledged": accepted,
                }
            if state in ("scanning", "waiting", "optimizing"):
                observed = True
                advice = None
            elif state == "idle" and verdict in VERDICTS:
                advice = verdict
                if optimize and advice == "optimal":
                    return {
                        "advice": advice,
                        "outcome": "advice_cleared",
                        "acknowledged": accepted,
                    }
                if not optimize and (observed or (accepted and advice != before)):
                    return {
                        "advice": advice,
                        "outcome": "scan_observed" if observed else "advice_changed",
                        "acknowledged": accepted,
                    }

        if not optimize and accepted and advice in VERDICTS:
            return {
                "advice": advice,
                "outcome": "scan_not_observed",
                "acknowledged": True,
            }
        return {"advice": None, "outcome": "unconfirmed", "acknowledged": accepted}
    finally:
        client.close()


class DecoWifiOptimization:
    """Keep preview state separate from regular Deco polling."""

    def __init__(
        self, hass, entry, decos, wireless, host, email, password, fingerprint
    ):
        self.hass = hass
        self.decos = decos
        self.wireless = wireless
        self._host = urlsplit(host).hostname
        self._email = email
        self._password = password
        self._fingerprint = fingerprint.lower()
        self._signal = f"{DOMAIN}_wifi_optimization_{entry.entry_id}"
        self._lock = asyncio.Lock()
        self._cancel_confirmation = None
        self._confirming_user = None
        self._library_ready = False
        self.advice = None
        self.activity = None
        self.result = {}
        entry.async_on_unload(self._clear_confirmation)

    @property
    def available(self):
        return self.decos.data.master_deco is not None

    @property
    def busy(self):
        return self._lock.locked()

    @property
    def state(self):
        if self.activity:
            return self.activity
        if self._cancel_confirmation is not None:
            return "confirm_optimization"
        return self.advice

    @callback
    def async_listen(self, listener):
        return async_dispatcher_connect(self.hass, self._signal, listener)

    @callback
    def _publish(self):
        async_dispatcher_send(self.hass, self._signal)

    @callback
    def _clear_confirmation(self):
        if self._cancel_confirmation is not None:
            self._cancel_confirmation()
            self._cancel_confirmation = None
        self._confirming_user = None

    @callback
    def _expire_confirmation(self, _now):
        self._clear_confirmation()
        self._publish()

    def _execute(self, optimize):
        from pydeco import DecoClient

        return _run_wifi_operation(
            DecoClient(
                self._host,
                self._email,
                self._password,
                host_key_sha256=self._fingerprint,
                timeout=30,
            ),
            optimize,
        )

    async def async_press(self, optimize, user_id):
        if self.busy or not self.available:
            raise _wifi_error("wifi_optimization_busy")
        if optimize:
            if self.advice != "optimizable":
                raise _wifi_error("wifi_optimization_not_recommended")
            if self._confirming_user != user_id or self._cancel_confirmation is None:
                self._clear_confirmation()
                self._confirming_user = user_id
                self._cancel_confirmation = async_call_later(
                    self.hass, 30, self._expire_confirmation
                )
                self._publish()
                return

        self._clear_confirmation()
        try:
            async with self._lock:
                self.activity = "optimizing" if optimize else "scanning"
                self._publish()
                if not self._library_ready:
                    await async_process_requirements(
                        self.hass, DOMAIN, [TMP_REQUIREMENT]
                    )
                    self._library_ready = True
                self.result = await self.hass.async_add_executor_job(
                    self._execute, optimize
                )
                self.advice = self.result["advice"]
                if self.result["outcome"] in ("unconfirmed", "rejected"):
                    raise _wifi_error("wifi_optimization_unconfirmed")
                if self.result["outcome"] == "not_started":
                    raise _wifi_error(
                        "wifi_optimization_not_recommended"
                        if optimize
                        else "wifi_optimization_busy"
                    )
                if optimize:
                    self.wireless.reset_retry()
                    await self.wireless.async_refresh()
        except HomeAssistantError:
            raise
        except Exception:
            self.advice = None
            self.result = {"outcome": "request_failed"}
            raise _wifi_error("wifi_optimization_unconfirmed") from None
        finally:
            self.activity = None
            self._publish()


class _DecoWifiEntity:
    _attr_has_entity_name = True

    def __init__(self, controls, entry_id, key):
        self.controls = controls
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{key}"

    @property
    def device_info(self):
        master = self.controls.decos.data.master_deco
        if master is None:
            return None
        return create_device_info(master, master, self.controls.decos)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.controls.async_listen(self.async_write_ha_state))
        self.async_on_remove(
            self.controls.decos.async_add_listener(self.async_write_ha_state)
        )


class DecoWifiAdviceSensor(_DecoWifiEntity, SensorEntity):
    """Expose the last reported advice and distinguish it from a scan result."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ADVICE_STATES)
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi-check"

    def __init__(self, controls, entry_id):
        super().__init__(controls, entry_id, "wifi_optimization_advice")

    @property
    def available(self):
        return self.controls.available

    @property
    def native_value(self):
        return self.controls.state

    @property
    def extra_state_attributes(self):
        return {
            key: self.controls.result[key]
            for key in ("outcome", "acknowledged")
            if key in self.controls.result
        }


class DecoWifiOptimizationButton(_DecoWifiEntity, ButtonEntity):
    """Start an on-demand scan or confirm and apply the Deco's advice."""

    def __init__(self, controls, entry_id, *, optimize):
        key = "wifi_optimize" if optimize else "wifi_scan"
        super().__init__(controls, entry_id, key)
        self._optimize = optimize
        self._attr_entity_category = EntityCategory.CONFIG if optimize else None
        self._attr_icon = "mdi:wifi-cog" if optimize else "mdi:wifi-sync"

    @property
    def available(self):
        return (
            self.controls.available
            and not self.controls.busy
            and (not self._optimize or self.controls.advice == "optimizable")
        )

    async def async_press(self):
        context = self._context
        user_id = context.user_id if context is not None else None
        user = await self.hass.auth.async_get_user(user_id) if user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized()
        await self.controls.async_press(self._optimize, user_id)
