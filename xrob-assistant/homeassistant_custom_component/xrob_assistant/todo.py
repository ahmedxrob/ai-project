from datetime import timedelta, date
import logging
import aiohttp
from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity
from .const import CONF_URL

_LOGGER=logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    url=entry.data[CONF_URL].rstrip('/')
    async def fetch():
        async with aiohttp.ClientSession() as session:
            async with session.get(url+'/api/tasks',timeout=10) as response:
                response.raise_for_status(); return await response.json()
    coordinator=DataUpdateCoordinator(hass,_LOGGER,name='XROB tasks',update_method=fetch,update_interval=timedelta(minutes=1))
    await coordinator.async_config_entry_first_refresh()
    async_add_entities([XrobTodo(coordinator,entry,url)])

class XrobTodo(CoordinatorEntity,TodoListEntity):
    _attr_name='XROB Tasks'
    _attr_icon='mdi:checkbox-marked-circle-outline'
    _attr_supported_features=TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.UPDATE_TODO_ITEM | TodoListEntityFeature.DELETE_TODO_ITEM | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    def __init__(self,coordinator,entry,url):
        super().__init__(coordinator); self.url=url; self._attr_unique_id=f'{entry.entry_id}_tasks'
    @property
    def todo_items(self):
        return [TodoItem(uid=str(x['id']),summary=x['title'],status=TodoItemStatus.COMPLETED if x['status']=='done' else TodoItemStatus.NEEDS_ACTION,due=date.fromisoformat(x['due_date']) if x.get('due_date') else None) for x in self.coordinator.data]
    async def async_create_todo_item(self,item):
        async with aiohttp.ClientSession() as s:
            async with s.post(self.url+'/api/tasks',json={'data':{'title':item.summary,'due_date':str(item.due)[:10] if item.due else None}}) as r: r.raise_for_status()
        await self.coordinator.async_request_refresh()
    async def async_update_todo_item(self,item):
        old = next((x for x in self.coordinator.data if str(x['id']) == str(item.uid)), None)
        due_date = item.due.isoformat()[:10] if item.due else None
        data = {'title': item.summary, 'status': 'done' if item.status == TodoItemStatus.COMPLETED else 'open'}
        # A HA todo update may be only a title/status change. Preserve the app's
        # reminder time unless the due date itself was changed in HA.
        if old and old.get('due_date') != due_date:
            data['due_date'] = due_date
            data['due_time'] = None
        elif not old:
            data['due_date'] = due_date
        async with aiohttp.ClientSession() as s:
            async with s.patch(self.url+f'/api/tasks/{item.uid}',json={'data':data},timeout=10) as r: r.raise_for_status()
        await self.coordinator.async_request_refresh()
    async def async_delete_todo_items(self,uids):
        async with aiohttp.ClientSession() as s:
            for uid in uids:
                async with s.delete(self.url+f'/api/tasks/{uid}') as r: r.raise_for_status()
        await self.coordinator.async_request_refresh()
