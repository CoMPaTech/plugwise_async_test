#!/usr/bin/env python3
"""Plugwise Home Assistant component."""

import asyncio
import logging
import requests
import xml.etree.cElementTree as Etree
# Time related
import datetime as dt
import pytz
from dateutil.parser import parse
# For XML corrections
import re
import json

import aiohttp
import async_timeout

PLUGWISE_PING_ENDPOINT = "/ping"
PLUGWISE_DIRECT_OBJECTS_ENDPOINT = "/core/direct_objects"
PLUGWISE_DOMAIN_OBJECTS_ENDPOINT = "/core/domain_objects"
PLUGWISE_LOCATIONS_ENDPOINT = "/core/locations"
PLUGWISE_APPLIANCES = "/core/appliances"
PLUGWISE_RULES = "/core/rules"

DEFAULT_TIMEOUT = 10
MIN_TIME_BETWEEN_UPDATES = dt.timedelta(seconds=2)

_LOGGER = logging.getLogger(__name__)

class Plugwise:
    """Define the Plugwise object."""
    # pylint: disable=too-many-instance-attributes, too-many-public-methods

    def __init__(
        self, host, password, username='smile', port=80, timeout=DEFAULT_TIMEOUT, websession=None, legacy_anna=False,
    ):
        """Set the constructor for this class."""

        if websession is None:
            async def _create_session():
                return aiohttp.ClientSession()

            loop = asyncio.get_event_loop()
            self.websession = loop.run_until_complete(_create_session())
        else:
            self.websession = websession

        self._auth=aiohttp.BasicAuth(username, password=password)

        self._legacy_anna = legacy_anna
        self._timeout = timeout
        self._endpoint = "http://" + host + ":" + str(port)
        self._throttle_time = None
        self._throttle_all_time = None
        self._domain_objects = None

    async def connect(self, retry=2):
        """Connect to Plugwise device."""
        # pylint: disable=too-many-return-statements
        url = self._endpoint + PLUGWISE_PING_ENDPOINT
        try:
            with async_timeout.timeout(self._timeout):
                resp = await self.websession.get(url,auth=self._auth)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if retry < 1:
                _LOGGER.error("Error connecting to Plugwise", exc_info=True)
                return False
            return await self.connect(retry - 1)

        result = await resp.text()
        if not 'error' in result:
            _LOGGER.error('Connected but expected text not returned, we got %s',result)
            return False

        return True

    def sync_connect(self):
        """Close the Plugwise connection."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.connect())
        loop.run_until_complete(task)

    async def close_connection(self):
        """Close the Plugwise connection."""
        await self.websession.close()

    def sync_close_connection(self):
        """Close the Plugwise connection."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.close_connection())
        loop.run_until_complete(task)

    async def request(self, command, retry=3):
        """Request data."""
        # pylint: disable=too-many-return-statements

        url = self._endpoint + command
        _LOGGER.debug("Plugwise command: %s",command)

        try:
            with async_timeout.timeout(self._timeout):
                resp = await self.websession.get(url,auth=self._auth)
        except asyncio.TimeoutError:
            if retry < 1:
                _LOGGER.error("Timed out sending command to Plugwise: %s", command)
                return None
            return await self.request(command, retry - 1)
        except aiohttp.ClientError:
            _LOGGER.error("Error sending command to Plugwise: %s", command, exc_info=True)
            return None

        result = await resp.text()

        #_LOGGER.debug(result)

	# B*llsh*t for now, but we should parse it (xml, not json)
        if not result or result == '{"errorCode":0}':
            return None

        return Etree.fromstring(self.escape_illegal_xml_characters(result))

    def sync_request(self, command, retry=2):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.request(command, retry))
        return loop.run_until_complete(task)

    async def update_domain_objects(self):
        """Request data."""
        self._domain_objects = await self.request(PLUGWISE_DOMAIN_OBJECTS_ENDPOINT)

        #_LOGGER.debug("Plugwise data update_domain_objects: %s",self._domain_objects)

        return self._domain_objects

    def sync_update_domain_objects(self):
        """Request data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_domain_objects())
        loop.run_until_complete(task)

    async def throttle_update_domain_objects(self):
        """Throttle update device."""
        if (self._throttle_time is not None
                and dt.datetime.now() - self._throttle_time < MIN_TIME_BETWEEN_UPDATES):
            return
        self._throttle_time = dt.datetime.now()
        await self.update_domain_objects()

    async def update_device(self):
        """Update device."""
        await self.throttle_update_domain_objects()

    async def find_all_appliances(self):
        """Find all Plugwise devices."""
        #await self.update_rooms()
        #await self.update_heaters()
        await self.update_domain_objects()

    @staticmethod
    def escape_illegal_xml_characters(xmldata):
        """Replace illegal &-characters."""
        return re.sub(r"&([^a-zA-Z#])", r"&amp;\1", xmldata)

    @staticmethod
    def get_point_log_id(xmldata, log_type):
        """Get the point log ID based on log type."""
        locator = (
            "module/services/*[@log_type='" + log_type + "']/functionalities/point_log"
        )
        if xmldata.find(locator) is not None:
            return xmldata.find(locator).attrib["id"]
        return None

    @staticmethod
    def get_measurement_from_point_log(xmldata, point_log_id):
        """Get the measurement from a point log based on point log ID."""
        locator = "*/logs/point_log[@id='" + point_log_id + "']/period/measurement"
        if xmldata.find(locator) is not None:
            return xmldata.find(locator).text
        return None

    def get_current_preset(self):
        """Get the current active preset."""
        if self._legacy_anna:
            active_rule = self._domain_objects.find("rule[active='true']/directives/when/then")
            if active_rule is None or "icon" not in active_rule.keys():
                return "none"
            return active_rule.attrib["icon"]

        log_type = "preset_state"
        locator = (
            "appliance[type='thermostat']/logs/point_log[type='"
            + log_type
            + "']/period/measurement"
        )
        return self._domain_objects.find(locator).text

    def get_schedule_temperature(self):
        """Get the temperature setting from the selected schedule."""
        point_log_id = self.get_point_log_id(self._domain_objects, "schedule_temperature")
        if point_log_id:
            measurement = self.get_measurement_from_point_log(self._domain_objects, point_log_id)
            if measurement:
                value = float(measurement)
                return value
        return None

    def get_current_temperature(self):
        """Get the curent (room) temperature from the thermostat - match to HA name."""
        current_temp_point_log_id = self.get_point_log_id(self._domain_objects, "temperature")
        if current_temp_point_log_id:
            measurement = self.get_measurement_from_point_log(
                self._domain_objects, current_temp_point_log_id
            )
            value = float(measurement)
            return value
        return None


import logging 
import voluptuous as vol

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateDevice

from homeassistant.components.climate.const import (
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)

from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    TEMP_CELSIUS,
)

import homeassistant.helpers.config_validation as cv


SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_PRESET_MODE

DEFAULT_NAME = "Plugwise async Dev Thermostat"
DEFAULT_ICON = "mdi:thermometer"


# Read platform configuration
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_HOST): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):

    plugwise_data_connection = Plugwise(host=config[CONF_HOST],password=config[CONF_PASSWORD],websession=async_get_clientsession(hass))

    if not await plugwise_data_connection.connect():
        _LOGGER.error("Failed to connect to Plugwise")
        return

    await plugwise_data_connection.find_all_appliances()

    data = plugwise_data_connection.request(PLUGWISE_DOMAIN_OBJECTS_ENDPOINT)

    _LOGGER.debug("Plugwise; %s", data)

    data = plugwise_data_connection.get_current_preset()
    _LOGGER.debug("Plugwise current preset; %s", data)
    data = plugwise_data_connection.get_current_temperature()
    _LOGGER.debug("Plugwise current temperature; %s", data)
    data = plugwise_data_connection.get_schedule_temperature()
    _LOGGER.debug("Plugwise schedule temperature; %s", data)

    dev = []
    dev.append(PlugwiseAnna(plugwise_data_connection,config[CONF_NAME]))
    async_add_entities(dev)


class PlugwiseAnna(ClimateDevice):
    """Representation of the Smile/Anna thermostat."""

    def __init__(self, plugwise_data_connection, name):
        """Set up the Plugwise API."""
        self._conn = plugwise_data_connection
        self._name = name
        self._hvac_mode = None

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return SUPPORT_FLAGS

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return DEFAULT_ICON

    @property
    def name(self):
        """Return the name of the entity."""
        return self._name

    @property
    def temperature_unit(self):
        """Return the unit of measurement which this thermostat uses."""
        return TEMP_CELSIUS

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._conn.get_schedule_temperature()

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return self._conn.get_current_temperature()

    @property
    def preset_mode(self):
        return self._conn.get_current_preset()

    @property
    def hvac_modes(self):
        return None

    @property
    def hvac_mode(self):
        return None

    @property
    def preset_modes(self):
        return None

    async def async_update(self):
        """Retrieve latest state."""
        _LOGGER.debug("Plugwise updating")
        self = await self._conn.update_device()
