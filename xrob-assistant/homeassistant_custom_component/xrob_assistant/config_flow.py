import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, CONF_URL, DEFAULT_URL

class XrobLifeManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            url = user_input[CONF_URL].rstrip('/')
            await self.async_set_unique_id(url)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Xrob Music", data={CONF_URL:url})
        return self.async_show_form(step_id="user", data_schema=vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str}), errors=errors)
