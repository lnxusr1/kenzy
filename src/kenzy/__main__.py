#from kenzy.image.device import VideoDevice
#import time 

#dev = VideoDevice()
#dev.start()

#time.sleep(10)

#dev.stop()

import kenzy.settings

kenzy.settings.save({ "type": "kenzy.image", "component": {  }, "device": {  }, "server": { } })
settings = kenzy.settings.load()
print(settings)

import kenzy.extras

print(kenzy.extras.get_local_ip_address())