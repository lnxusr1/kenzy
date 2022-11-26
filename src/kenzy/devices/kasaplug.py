import logging
import sys
import traceback
from kenzy.shared import threaded
from kenzy import GenericDevice
import asyncio
from kasa import SmartPlug, Discover, SmartDeviceException


class KasaPlug(GenericDevice):
    """
    Kasa SmartPlug to control a python-kasa plug device.
    """
    
    def __init__(self, **kwargs):
        """
        KasaPlug Initialization

        Args:
            parent (object): Containing object's reference.  Normally this would be the device container. (optional)
            alias (str): Alias of device
            address (str): IP address of device
            callback (function): Callback function for which to send any captured data.
        """

        # Local variable instantiation and initialization
        self.type = "KASADEV"
        self.logger = logging.getLogger(kwargs.get("nickname", self.type))
        
        self.is_on = False
        self.thread = None

        from kenzy import __version__
        self.version = __version__
        self._packageName = "kenzy"

        super(KasaPlug, self).__init__(**kwargs)

    def updateSettings(self):

        self.parent = self.args.get("parent")
        self._callbackHandler = self.args.get("callback")                       # Callback function accepts two positional args (Type, Text)

        self.alias = self.args.get("alias")                                     # Alias of the smart plug device
        self.address = self.args.get("address")                                 # IP Address of the smart plug device
        self.checkInterval = self.args.get("checkInterval", 10)                 # Number of seconds between on/off checks

        if self.alias is None and self.address is None:
            self.logger.error("KasaDevice requires an alias or address to be specified.  Unable to start kasa device.")

        if self.alias is not None and self.nickname is None:
            self.nickname = self.alias

        return True

    @threaded
    def _doCallback(self, inData):
        """
        Calls the specified callback as a thread to keep from blocking additional processing.

        Args:
            text (str):  Text to send to callback function
        
        Returns:
            (thread):  The thread on which the callback is created to be sent to avoid blocking calls.
        """

        try:
            if self._callbackHandler is not None:
                self._callbackHandler("KASADEV_INPUT", inData, deviceId=self.deviceId)
        except Exception:
            pass
        
        return

    @threaded
    def checkStatus(self):
        if self.address is None and self.alias is not None:
            self.logger.info("Checking for devices for " + str(self.alias))
            devices = asyncio.run(Discover.discover())
            for addr, obj in devices.items():
                if str(obj.alias).lower().strip() == str(self.alias).lower().strip():
                    self.address = addr
                    self.logger.info(str(self.alias) + " - Found kasa device at " + str(self.address))

        if self.address is not None:
            asyncio.run(self._checkStatus())

    async def _checkStatus(self):
        while self._isRunning:
            try:
                d = SmartPlug(self.address)
                await d.update()

                self.alias = d.alias
                if self.nickname is None:
                    self.nickname = self.alias

                if self.is_on != d.is_on:
                    self.logger.info(str(self.nickname) + " (" + str(self.address) + ") status: " + str(self.is_on))

                self.is_on = d.is_on
            except SmartDeviceException:
                self.logger.debug(str(sys.exc_info()[0]))
                self.logger.debug(str(traceback.format_exc()))
                devName = self.nickname if self.nickname is not None else self.alias if self.alias is not None else self.address
                self.logger.error(str(devName) + " - Unable to get status for KasaPlug")
                pass

            await asyncio.sleep(10)

    async def _on(self):
        try:
            d = SmartPlug(self.address)

            await d.turn_on()
            self.is_on = True
        except SmartDeviceException:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            devName = self.nickname if self.nickname is not None else self.alias if self.alias is not None else self.address
            self.logger.error(str(devName) + " - Unable to turn on KasaPlug")
            pass

    async def _off(self):
        try:
            d = SmartPlug(self.address)

            await d.turn_off()
            self.is_on = False
        except SmartDeviceException:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            devName = self.nickname if self.nickname is not None else self.alias if self.alias is not None else self.address
            self.logger.error(str(devName) + " - Unable to turn off KasaPlug")
            pass

    def on(self, httpRequest=None):
        asyncio.run(self._on())
        return True

    def off(self, httpRequest=None):
        asyncio.run(self._off())
        return True

    def accepts(self):
        return ["start", "stop", "on", "off"]
        
    def isRunning(self):
        """
        Identifies if the device is actively running.

        Returns:
            (bool):  True if running; False if not running.
        """

        return self._isRunning
        
    def stop(self, httpRequest=None):
        """
        Stops the KasaDevice.  Function provided for compatibility as kasadevice does not require a daemon.
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.

        Returns:
            (bool):  True on success else will raise an exception.
        """
        self._isRunning = False
        return True
        
    def start(self, httpRequest=None, useThreads=True):
        """
        Starts the KasaDevice.  Function provided for compatibility as kasadevice does not require a daemon.

        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.
            useThreads (bool):  Indicates if the brain should be started on a new thread.
        
        Returns:
            (bool):  True on success else will raise an exception.
        """
        self._isRunning = True
        self.thread = self.checkStatus()
        
        return True
    
    def wait(self, seconds=0):
        """
        Waits for any active kasadevices to complete before closing.  Provided for compatibility as kasadevice does not requrie a daemon.
        
        Args:
            seconds (int):  Number of seconds to wait before calling the "stop()" function
            
        Returns:
            (bool):  True on success else will raise an exception.
        """
        return True