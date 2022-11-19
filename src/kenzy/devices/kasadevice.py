import logging
from kenzy.shared import threaded
from kenzy import GenericDevice
import asyncio
from kasa import SmartDevice, Discover
import time


class KasaDevice(GenericDevice):
    """
    Kasa SmartDevice to control a python-kasa plug device.
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
        self.logger = logging.getLogger(self.type)
        
        self.is_on = False
        self.thread = None

        from kenzy import __version__
        self.version = __version__
        self._packageName = "kenzy"

        super(KasaDevice, self).__init__(**kwargs)

    def updateSettings(self):

        self.parent = self.args.get("parent")
        self._callbackHandler = self.args.get("callback")                       # Callback function accepts two positional args (Type, Text)

        self.alias = self.args.get("alias")
        self.address = self.args.get("address")

        if self.alias is None and self.address is None:
            self.logger.error("KasaDevice requires an alias or address to be specified.  Unable to start kasa device.")

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
                    self.logger.info("Found kasa device at " + str(self.address))

        if self.address is not None:
            asyncio.run(self._checkStatus())

    async def _checkStatus(self):
        while self._isRunning:
            d = SmartDevice(self.address)
            await d.update()
            
            if self.is_on != d.is_on:
                self.logger.debug(str(self.address) + " status: " + str(self.is_on))

            self.is_on = d.is_on
            time.sleep(5)

    async def _on(self):
        d = SmartDevice(self.address)

        await d.turn_on()
        self.is_on = True

    async def _off(self):
        d = SmartDevice(self.address)

        await d.turn_off()
        self.is_on = False

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