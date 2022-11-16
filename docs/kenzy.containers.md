# Custom Containers

While it isn't necessary, you can build your own devices by inheriting ```kenzy.GenericContainer``` which has the following basic structure:

```
import kenzy.GenericContainer

class MyCustomContainer(kenzy.GenericContainer):
    def initialize(self):

        super().initialize()
        
        self.registerThread = None
        self.version = "1.0.0"

        return True
    
    def start(self, useThreads=True):
        ret = super().start(useThreads)
        
        self.registerThread = self.stayConnected()
        return ret
    
    @threaded
    def stayConnected(self):
        delay = 30
        interval = 0
        while self.isRunning():
            interval = interval + 1
            
            if interval >= delay:
                interval = 0
                try:
                    
                    ret = self.registerWithBrain()
                    if ret:
                        self.logger.debug("Re-register thread alive")
                except Exception:
                    pass 
                
            time.sleep(1)

        return True
    
    def _processRequest(self, httpRequest):
        if httpRequest.isFileRequest:
            return httpRequest.sendRedirect(urljoin(self.brain_url, httpRequest.path)) 
            
        return super()._processRequest(httpRequest)
```

# Starting/Stopping a Container

This then gives you the ability to start/stop the container like all other Kenzy device containers.
```
container = MyCustomContainer()
container.intialize()

container.start()

container.wait()  # Wait until container is shutdown before exiting
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)