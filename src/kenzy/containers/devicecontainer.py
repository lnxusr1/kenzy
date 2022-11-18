"""
Kenzy.Ai: Containers
"""

import time
from kenzy.shared import threaded, sendSDCPRequest
from kenzy.templates import GenericContainer
from urllib.parse import urljoin


class DeviceContainer(GenericContainer):
    def initialize(self, **kwargs):

        self.args = kwargs
        self.registerThread = None
        super().initialize(**kwargs)

        return True
    
    def start(self, useThreads=True):
        ret = super().start(useThreads)

        self.registerThread = self.stayConnected()
        return ret
    
    def checkForBrain(self):
        res = sendSDCPRequest()
        if isinstance(res, list):
            for item in res:
                if item.get("headers", {}).get("X-KENZY-TYPE", '') == "BRAIN":
                    self.brain_url = item.get("headers", {}).get("LOCATION", self.brain_url)
                    self.logger.info("Using BRAIN @ " + str(self.brain_url))
                    return True

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
                    self.logger.debug("Checking for BRAIN via SDCP.")
                    self.checkForBrain()
                
            time.sleep(1)

        return True
    
    def _processRequest(self, httpRequest):
        if httpRequest.isFileRequest:
            return httpRequest.sendRedirect(urljoin(self.brain_url, httpRequest.path)) 
            
        return super()._processRequest(httpRequest)
    