# KENZY: Core Service Settings

All devices run inside a "service" container.  This service is effectively a HTTP/S server that is designed to facilitate communication from one device to another.  Service settings are set *per device* and govern how Kenzy's device components talk to each other.  You can encrypt communications between nodes using SSL and you can authorize access by setting an api_key (that must be the same on all connected services/devices).

## Parameters
| Parameter     | Type | Default  | Description                                              |
| :------------ | :--- | :------- | :------------------------------------------------------- |
| id            | str  | *None*   | Internal UUID for device/service combination             |
| host          | str  | 0.0.0.0  | IPv4 address for service listener                        |
| port          | int  | 9700     | TCP port for service listener                            |
| service_url   | str  | *None*   | URL for posting events and should normally be left blank |
| api_key       | str  | *None*   | Secret key interacting with service_url                  |
| upnp.type     | str  | client   | Options are: client, server, or standalone               |
| upnp.timeout  | int  | 45       | Timeout for upnp client operations                       |
| ssl.enable    | bool | false    | Enable SSL for local service listener                    |
| ssl.cert_file | str  | *None*   | Certificate file (CRT) for SSL                           |
| ssl.key_file  | str  | *None*   | Private key file for SSL                                 |

## UPNP Configuration

An optional UPNP service is available that should be set to "server" only on the skillmanager (or core) node.  For all other device nodes if they are then set to "client" mode then the service_url will be auto-discovered from the network (assuming another device service with upnp set to "server" is active).  If you manually set a service_url the "client" mode is ignored.

## Example YAML file

This configuraton section can be included for each device configuration.

```yaml
service:
  id:             my-id-string-goes-here
  host:           0.0.0.0
  port:           9700
  service_url:    https://127.0.0.1:9700
  api_key:        my-api-key-goes-here
  upnp.type:      server
  upnp.timeout:   45
  ssl.enable:     true
  ssl.cert_file:  /etc/ssl/cert/snakeoil.crt
  ssl.key_file:   /etc/ssl/private/snakeoil.key
```


-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.dev](https://kenzy.dev)