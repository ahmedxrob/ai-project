import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from .const import DOMAIN, PLATFORMS, CONF_URL

SERVICE_CREATE_EVENT = "create_event"
SERVICE_RECORD_WEIGHT = "record_weight"
SERVICE_COMPLETE_ROUTINE = "complete_routine"

async def _post(hass: HomeAssistant, path: str, data: dict):
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("XROB Life Manager is not configured")
    url = entries[0].data[CONF_URL].rstrip("/")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{url}{path}", json={"data": data}, timeout=10) as response:
                response.raise_for_status()
                return await response.json()
    except Exception as err:
        raise HomeAssistantError(f"XROB Life Manager API request failed: {err}") from err

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry.data
    if not hass.services.has_service(DOMAIN, SERVICE_CREATE_EVENT):
        async def create_event(call: ServiceCall):
            await _post(hass, "/api/events", {"title": call.data["title"], "start_at": call.data["start_at"], "end_at": call.data.get("end_at"), "category": call.data.get("category", "personal"), "notes": call.data.get("notes", "")})
        async def record_weight(call: ServiceCall):
            await _post(hass, "/api/weights", {"date": call.data.get("date", dt_util.now().date().isoformat()), "kg": call.data["kg"], "notes": call.data.get("notes", "")})
        async def complete_routine(call: ServiceCall):
            entries = hass.config_entries.async_entries(DOMAIN)
            if not entries: raise HomeAssistantError("XROB Life Manager is not configured")
            url = entries[0].data[CONF_URL].rstrip("/")
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{url}/api/routines/{call.data['routine_id']}/complete", timeout=10) as response:
                    response.raise_for_status()
        hass.services.async_register(DOMAIN, SERVICE_CREATE_EVENT, create_event, schema=vol.Schema({vol.Required("title"): cv.string, vol.Required("start_at"): cv.string, vol.Optional("end_at"): cv.string, vol.Optional("category", default="personal"): cv.string, vol.Optional("notes", default=""): cv.string}))
        hass.services.async_register(DOMAIN, SERVICE_RECORD_WEIGHT, record_weight, schema=vol.Schema({vol.Required("kg"): vol.Coerce(float), vol.Optional("date"): cv.string, vol.Optional("notes", default=""): cv.string}))
        hass.services.async_register(DOMAIN, SERVICE_COMPLETE_ROUTINE, complete_routine, schema=vol.Schema({vol.Required("routine_id"): vol.Coerce(int)}))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.config_entries.async_entries(DOMAIN):
            for service in (SERVICE_CREATE_EVENT, SERVICE_RECORD_WEIGHT, SERVICE_COMPLETE_ROUTINE):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
