"""Open Charge Point Protocol integration."""
import asyncio
from datetime import timedelta
import logging
from typing import Dict, Optional

from ocpp.exceptions import NotImplementedError
from ocpp.messages import CallError
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp, call, call_result
from ocpp.v16.enums import Action
import voluptuous as vol
import websockets

from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_PORT
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity

_LOGGER = logging.getLogger(__name__)
logging.getLogger("ocpp").setLevel(logging.DEBUG)

ICON = "mdi:ev-station"

SCAN_INTERVAL = timedelta(seconds=10)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Optional(CONF_PORT, default=9000): cv.positive_int}
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the EV sensors."""
    port = config[CONF_PORT]
    _LOGGER.info(config)

    data = await CentralSystem.create(port=port)

    dev = []
    dev.append(ChargeMetric(data))
    async_add_entities(dev, True)


class CentralSystem:
    """Server for handling OCPP connections."""

    def __init__(self):
        """Instantiate instance of a CentralSystem."""
        self._server = None
        self._connected_charger: Optional[ChargePoint] = None

    @staticmethod
    async def create(host: str = "0.0.0.0", port: int = 9000):
        """Create instance and start listening for OCPP connections on given port."""
        self = CentralSystem()
        server = await websockets.serve(
            self.on_connect, "0.0.0.0", port, subprotocols=["ocpp1.6"]
        )

        self._server = server
        return self

    async def on_connect(self, websocket, path: str):
        """Request handler executed for every new OCPP connection."""
        # For now only 1 charger can connect.
        if self._connected_charger is not None:
            return

        try:
            cp = ChargePoint(path, websocket)
            self._connected_charger = cp
            _LOGGER.info(f"Chargers connected: {self.chargers_connected()}")
            await cp.start()
        finally:
            self._connected_charger = None

    def chargers_connected(self):
        """Return number of chargers connected."""
        if self._connected_charger is not None:
            return 1
        return 0


class ChargePoint(cp):
    """Server side representation of a charger."""

    def __init__(self, id, connection, interval_meter_metrics: int = 60):
        """Instantiate instance of a ChargePoint."""
        super().__init__(id, connection)
        self.interval_meter_metrics = interval_meter_metrics

        # Indicates if the charger requires a reboot to apply new
        # configuration.
        self._requires_reboot = False

    async def post_connect(self):
        """Logic to be executed right after a charger connects."""
        try:
            await self.configure(
                "MeterValueSampleInterval", str(self.interval_meter_metrics)
            )
        except (ConfigurationError, NotImplementedError) as e:
            _LOGGER.debug("Configuration of the charger failed: %r", e)

    async def configure(self, key: str, value: str):
        """Configure charger by setting the key to target value.

        First the configuration key is read using GetConfiguration. The key's
        value is compared with the target value. If the key is already set to
        the correct value nothing is done.

        If the key has a different value a ChangeConfiguration request is issued.

        """
        req = call.GetConfigurationPayload(key=[key])

        resp = await self.call(req)

        for key_value in resp.configuration_key:
            # If the key already has the targeted value we don't need to set
            # it.
            if key_value.key == key and key_value.value == value:
                return

            if key_value.read_only:
                raise ConfigurationError(f"'{key}' is a read only setting")

        req = await call.ChangeConfigurationPayload(key=key, value=value)

        resp = await self.call(req)

        if resp.status in ["Rejected", "NotSupported"]:
            raise ConfigurationError(
                f"charger returned '{resp.status}' while setting '{key}' to '{value}'"
            )

        if resp.status == "RebootRequired":
            self._requires_reboot = True

    async def _get_specific_response(self, unique_id, timeout):
        # The ocpp library silences CallErrors by default. See
        # https://github.com/mobilityhouse/ocpp/issues/104.
        # This code 'unsilences' CallErrors by raising them as exception
        # upon receiving.
        resp = await super()._get_specific_response(unique_id, timeout)

        if isinstance(resp, CallError):
            raise resp.to_exception()

        return resp

    async def _handle_call(self, msg):
        try:
            await super()._handle_call(msg)
        except NotImplementedError as e:
            response = msg.create_call_error(e).to_json()
            await self._send(response)

    async def start(self):
        """Start charge point."""
        await asyncio.gather(super().start(), self.post_connect())

    @on(Action.MeterValues)
    def on_meter_values(self, connector_id: int, meter_value: Dict, **kwargs):
        """Request handler for MeterValues Calls."""
        return call_result.MeterValuesPayload()


class ChargeMetric(Entity):
    """Sensor for charge metrics."""

    def __init__(self, data):
        """Instantiate instance of a ChargeMetrics."""
        self._name = "Chargers connected"
        self.data = data
        self._state = None

        self._unit_of_measurement = "#"
        self.type = "connected_chargers"

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        _LOGGER.info("get state")
        return self.data.chargers_connected()

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return "Chargers connected"

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    def update(self):
        """Get the latest data and updates the states."""
        _LOGGER.info("get update")


class ConfigurationError(Exception):
    """Error used to signal a error while configuring the charger."""

    pass
