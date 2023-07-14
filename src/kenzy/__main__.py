#import time 

#time.sleep(10)

import os
import json
import kenzy.settings
data = kenzy.settings.load("/home/lnxusr1/git/kenzy/examples/watcher.yml")
print(json.dumps(data, indent=4))
quit()

#kenzy.settings.save({ "type": "kenzy.image", "component": {  }, "device": {  }, "server": { } })
#settings = kenzy.settings.load()
#print(settings)

#import kenzy.extras

#print(kenzy.extras.get_local_ip_address())

import kenzy.core
from kenzy.image.device import VideoDevice

httpd = kenzy.core.KenzyHTTPServer(("0.0.0.0", 8080))

dev = VideoDevice(server=httpd)
dev.start()

try:
    httpd.devices["id"] = dev
    httpd.serve_forever()
except KeyboardInterrupt:
    httpd.shutdown()
    dev.stop()