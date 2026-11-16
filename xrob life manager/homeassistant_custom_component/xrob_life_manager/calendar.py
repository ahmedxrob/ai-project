from datetime import datetime, timedelta
import logging
import aiohttp
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from homeassistant.util import dt as dt_util
from .const import CONF_URL

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    url = entry.data[CONF_URL].rstrip('/')
    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get(url + '/api/events', timeout=10) as response:
                response.raise_for_status()
                return await response.json()
    coordinator = DataUpdateCoordinator(hass, _LOGGER, name='XROB calendar', update_method=fetch, update_interval=timedelta(minutes=1))
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([XrobCalendar(coordinator, entry)])

class XrobCalendar(CoordinatorEntity, CalendarEntity):
    _attr_name = 'XROB Calendar'
    _attr_icon = 'mdi:calendar'
    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f'{entry.entry_id}_calendar'
    @property
    def event(self):
        upcoming = sorted(self.coordinator.data, key=lambda x: x.get('start_at',''))
        now = datetime.now().isoformat(timespec='minutes')
        for item in upcoming:
            if item.get('start_at','') >= now:
                return self._to_event(item)
        return None
    def _parse_local(self, value):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.get_time_zone(self.hass.config.time_zone))
        return parsed
    def _to_event(self, item):
        start = self._parse_local(item['start_at'])
        end = self._parse_local(item['end_at']) if item.get('end_at') else start + timedelta(hours=1)
        return CalendarEvent(start=start, end=end, summary=item['title'], description=item.get('notes',''), uid=str(item['id']))
    async def async_get_events(self, hass, start_date, end_date):
        result=[]
        for item in self.coordinator.data:
            start=self._parse_local(item['start_at'])
            if start_date <= start <= end_date:
                result.append(self._to_event(item))
        return result
