# Calls to a Container/Device

Kenzy's architecture allows for a few ways to send commands between containers to devices.  Most commands are sent via JSON, although it is not a requirement per se.

# Referencing Devices &amp; Containers

Kenzy uses a standard URL template for calls to a device or container.  The pattern is as follows:

```
/brain/{command}
/container/{uuid}/{command}
/device/{uuid}/{command}
/{type}/{command}
```

The {command} must be listed in the target object's "accepts" method/property or else an error will be generated.  The {id} is the UUID for the specific device or container that should receive the message.  You can send normal HTTP type requests to any of these URL templates and the resulting httpRequest object will be sent into the target device/container's related function.

__NOTE:__
Be advised that you can make most calls to the brain instance and the request will be forwarded along to the appropriate container as required.  Alternatively, if you are connecting to a standard container device then you can only send commands to devices that live on that one container.  The web control panel is a great example of a tool that leverages the benefit of the Brain being the entry point/forwarder for most calls.

# Brain.collect(httpRequest)
As an example of Device/Container/Brain communication let's run through how a "Listener" receives a phrase that it needs to send to a Brain to generate a response.

The Listener is designed to capture audio and convert that speech to text.  That's part of the device itself so no issues there.  Once the audio is converted to text then we need to communicate to the Brain.  The included DeviceContainer (```kenzy.containers.DeviceContainer```) has the function already set up to send data to the brain for further processing.  This is handled through the container's callbackHandler which is made available to the device as a callable function.

```
from kenzy.containers import DeviceContainer
from kenzy.devices import Listener

container = DeviceContainer()
container.initialize()
container.start()

listener = Listener(callback=container.callbackHandler)
container.addDevice("LISTENER", listener, autoStart=True)

container.wait()
```

In the above the "container.callbackHandler" has the following definition:

```
def callbackHandler(inType, data, context=None)
```

A sample of the callbackHandler call would look something like this.

```
container.callbackHandler("AUDIO_INPUT", "what time is it")
```

The DeviceContainer will then take the input and send it to the brain as the following HTTP request:

```
POST /brain/collect
Content-Type: application/json

{ 
    "type": "AUDIO_INPUT", 
    "data": "what time is it" 
}
```
And the corresponding response would be:
```
HTTP/1.1 200 OK
Date: Mon, 07 Feb 2022 21:10:00 GMT
Content-Type: application/json

{
  "error": false,
  "message": "Complete"
}
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)