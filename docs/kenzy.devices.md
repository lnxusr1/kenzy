# Custom Devices

Kenzy is designed to be extensible so that you can create your own input/output/control devices.  The setup is straight forward.  Kenzy has devies built-in for Cameras, Microphones (Speech-to-Text), and Audio Output (Text-to-Speech), but you can all all sorts of devices such as temperature sensors, motion sensors, servo controllers, motor controllers, and more.  Your imagination is the only limit!

## Defining a Device Class

You can build your own devices by inheriting ```kenzy.GenericDevice``` which has the following basic structure:

```
import kenzy.GenericDevice

class MyCustomDevice(kenzy.GenericDevice):
    def __init__(self, **kwargs):
        
        self.args = kwargs
        self._isRunning = False 

        self.version = "1.0.0"

        super(MyCustomDevice, self).__init__(**kwargs)

	@property
    def accepts(self):
        return ["start","stop"] # Add "upgrade" to allow 
                                # remote "pip install --upgrade" option.
    
    @property
    def isRunning(self):
        return self._isRunning
    
    def start(self, httpRequest=None):
        self._isRunning = True
        return True
    
    def stop(self, httpRequest=None):
        self._isRunning = False
        return True
    
    def upgrade(self, httpRequest=None):
        return upgradePackage(self._packageName)
```

The ```accepts``` property defines the functions that are callable via the container's HTTP interface.  For example, a call to "/device/{uuid}/stop" on the contaienr (or Brain) would in turn call the ```MyCustomDevice.stop()``` method on the device instance and include the inbound httpRequest as its argument.  You can add new functions so long as they accept an "httpRequest" optional argument and are included in the ```accepts``` list.

Make sure to check out [Device/Brain Communication](kenzy.communication.md) for information on using the callback handler to send information to the Brain.

## Adding Device to Container

```
import kenzy.containers

container = kenzy.containers.DeviceContainer()
container.initialize()
container.start()

myCustomDevice = MyDevice(callback=container.callbackHandler)
container.addDevice("MY_DEVICE", myCustomDevice, autoStart=True)

container.wait()
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)