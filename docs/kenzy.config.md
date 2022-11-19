# Setting a Startup Configuration

By default Kenzy will build and save a configuration file to ```~/.kenzy/config.json```.  This file can be edited to meet your needs and is fairly flexible in options.  It can reference any supported device or container and start up multiple devices and/or containers as desired.

## Generic Example

There are two root-level options that allow for a bit of flexibility when starting Kenzy.  First is the appVersion which defines the version of Kenzy that this configuration is targeted (this helps Kenzy know if adjustments need to be made for future revisions to the configuration file format).  The second is the root-level settings which presently allows you to specify a "modulesFolder".  This is basically a path you can specify that will get added to the environment variable for PYTHONPATH to make importing objects easier if you're using your own custom written classes.

The generic format of the container is as follows:

```
{
    "appVersion": "0.9.2",
    "settings": {
        "modulesFolder": "/path/to/modules/folder"
    },
    "containers": [
        {
            "module": "path.and.module.Name",
            "autoStart": true,
            "settings": {
                'tcpPort': 8081,
                'hostName': "localhost",
                'sslCertFile': "/etc/ssl/certs/snakeoil.crt", 
                'sslKeyFile': "/etc/ssl/private/snakeoil.key",
                'brainUrl': "https://localhost:8080", 
                'groupName': "Living Room", 
                'authentication': {
                    'key': "<API KEY HERE>",
                    'username': "<USERNAME HERE>",
                    'password': "<PASSWORD HERE>"
                }
            },
            "initialize": {
                "paramName1": "paramValue1",
                "paramName2": "paramValue2"
            },
            "devices": [
                {
                    "module": "path.and.module.Name",
                    "autoStart": true,
                    "uuid": "my-uuid-val-here",
                    "parameters": {
                        "paramName1": "paramValue1",
                        "paramName2": "paramValue2"
                    }
                }
            ]
        }
    ]
}
```

## Starting a DeviceContainer with a Listener and Speaker

Since the container's definition for "settings" and "initialize" allow for name-based arguments to be passed to the "\__init\__()" and "initialize()" functions respectively we can expand to include all available options that the classes support and change any of the defaults we desire.  The same is true for the parameters section of each device's configuration.

It's worth mentioned that by default a DeviceContainer will search via UPNP for a Brain that is offering the necessary endpoint.  That means you don't have to directly specify the brainUrl.  If you disable UPNP then you'd need to specify the link to the brain in the format "http://brain-ip-or-host:8080".

```
{
	"appVersion": "0.9.2",
	"settings": {
		"modulesFolder": null
	},
	"containers": [
		{
			"module": "kenzy.containers.DeviceContainer",
			"autoStart": true,
			"settings": {
				"groupName": "living room",
				"tcpPort": 8081,
				"hostName": null,
                "brainUrl": null,
				"sslCertFile": null,
				"sslKeyFile": null,
                "authentication": {
					"key": null,
					"username": "admin",
					"password": "admin"
				}
			},
			"devices": [
				{
					"module": "kenzy.devices.Listener",
					"autoStart": true,
					"uuid": null,
					"parameters": {
						"speechModel": null,
						"speechScorer": null,
						"audioChannels": 1,
						"audioSampleRate": 16000,
						"vadAggressiveness": 1,
						"speechRatio": 0.75,
						"speechBufferSize": 50,
						"speechBufferPadding": 350,
						"audioDeviceIndex": null,
                        "nickname": "Table Mic"
					}
				},
				{
					"module": "kenzy.devices.Speaker",
					"autoStart": true,
					"uuid": null,
                    "parameters": {
                        "nickname": "Table Speaker"
                    }
				}
			]
		}
	]
}

```

## Creating a Brain

You can create a brain instance by itself or include this section along with a DeviceContainer definition as the root level "containers" is a list and can have multiple containers defined within it.

```
{
	"appVersion": "0.9.2",
	"settings": {
		"modulesFolder": null
	},
	"containers": [
		{
			"module": "kenzy.containers.Brain",
			"autoStart": true,
			"settings": {
				"groupName": "core",
				"tcpPort": 8080,
				"hostName": null,
				"sslCertFile": null,
				"sslKeyFile": null,
				"authentication": {
					"key": null,
					"username": "admin",
					"password": "admin"
				}
			},
			"initialize": {
				"startUPNP": true,
				"skillFolder": null
			}
		}
    ]
}
```

## The Default Configuration

If you don't specify any parameters and just issue a ```python3 -m kenzy``` then the following configuration is attempted to be loaded.

```
{
    "appVersion": "0.9.2",
    "settings": {
        "modulesFolder": null
    },
    "containers": [
        {
            "module": "kenzy.containers.Brain",
            "autoStart": true,
            "settings": {
                "tcpPort": 8080
            }
        },
        {
            "module": "kenzy.containers.DeviceContainer",
            "autoStart": true,
            "settings": {
                "tcpPort": 8081
            },
            "devices": [
                {
                    "autoStart": true,
                    "module": "kenzy.devices.Listener"
                },
                {
                    "autoStart": true,
                    "module": "kenzy.devices.Speaker"
                },
                {
                    "autoStart": true,
                    "module": "kenzy.devices.Watcher"
                },
                {
                    "autoStart": true,
                    "isPanel": true,
                    "module": "kenzy.panels.RaspiPanel"
                }
            ]
        }
    ]
}
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)