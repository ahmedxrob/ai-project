from datetime import timedelta
import logging
import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfMass
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from .const import DOMAIN, CONF_URL

SCAN_INTERVAL = timedelta(minutes=1)

async def async_setup_entry(hass, entry, async_add_entities):
    url = entry.data[CONF_URL].rstrip('/')
    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get(url + '/api/ha/state', timeout=10) as response:
                response.raise_for_status()
                return await response.json()
    coordinator = DataUpdateCoordinator(hass, logging.getLogger(__name__), name="XROB Assistant", update_method=fetch, update_interval=SCAN_INTERVAL)
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([XrobSensor(coordinator, entry, key, name, unit) for key,name,unit in [
        ('weight_kg','Weight',UnitOfMass.KILOGRAM),('unpaid_bills','Unpaid bills',None),('unpaid_total_mad','Unpaid bills total','MAD'),('expiring_documents','Expiring documents',None),('open_tasks','Open tasks',None),('events_today','Events today',None),('routines_due_today','Routines due today',None),('today_agenda_count','Agenda items today',None),('today_open_items','Items to handle today',None),('next_routine','Next routine',None)
    ]])

class XrobSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, entry, key, name, unit):
        super().__init__(coordinator)
        self.key=key; self._attr_name=f"XROB {name}"; self._attr_unique_id=f"{entry.entry_id}_{key}"; self._attr_native_unit_of_measurement=unit
        self._attr_icon='mdi:calendar-today' if key in ('today_agenda_count','today_open_items') else 'mdi:calendar-check' if 'event' in key else 'mdi:heart-pulse' if key=='weight_kg' else 'mdi:clipboard-list'
    @property
    def native_value(self): return self.coordinator.data.get(self.key)

    @property
    def extra_state_attributes(self):
        if self.key == 'today_agenda_count':
            return {
                'agenda_date': self.coordinator.data.get('today_agenda_date'),
                'items': self.coordinator.data.get('today_agenda', []),
            }
        return None
