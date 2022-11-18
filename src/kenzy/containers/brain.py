import os
import logging
import time
from kenzy.shared import getFileContents, \
    sendHTTPRequest, \
    TCPStreamingClient, \
    UPNPServer, \
    KContext
from kenzy.templates import GenericContainer
from kenzy.skillmanager import SkillManager
from urllib.parse import urljoin
from cgi import parse_header
import sys
import traceback 


class Brain(GenericContainer):
    """
    Brain
    """
    def initialize(self, **kwargs):
        self.args = kwargs
        
        self.tcp_port = 8080
        self.skill_manager = SkillManager(self, skill_folder=self.args.get("skillFolder"))
        self.skill_manager.initialize()

        self._upnpServer = None

        self.webFilePath = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
        
        # Add API methods allowed
        self.accepts.append("register")
        self.accepts.append("shutdown")
        self.accepts.append("collect")
        self.accepts.append("upgradeAll")

        for i, item in enumerate(self.accepts):
            if item == "stopDevices":
                self.accepts.pop(i)
                break

        self.id = "brain"
        self.type = "brain"
        self.isBrain = True
        self.logger = logging.getLogger("BRAIN")

        # Data Storage
        self.clients = {}
        self.data = {}

        # Open streaming clients
        self.streams = {}

        self.callbacks = {"ask": None}

        super().initialize(**kwargs)

        upnp_server = UPNPServer(parent=self)
        self.addDevice("UPNP_Service", upnp_server, autoStart=self.args.get("startUPNP", True))

        return True

    def _processRequest(self, httpRequest):

        headers = None
        if self.authenticationKey is not None:
            headers = {"Cookie": "token=" + self.authenticationKey}

        if httpRequest.isAuthRequest:
            return super()._processRequest(httpRequest)

        if httpRequest.isFileRequest:
            return self._processFileRequest(httpRequest)

        if not httpRequest.authenticated:
            return httpRequest.sendRedirect("/admin/login.html")

        if httpRequest.isDeviceRequest \
                and httpRequest.item in self.clients \
                and self.my_url != self.clients[httpRequest.item]["url"]:

            # Forward request to client
            reqUrl = urljoin(self.clients[httpRequest.item]["url"], 
                             httpRequest.path)

            if reqUrl not in self.streams:
                self.streams[reqUrl] = {"contentType": None, "clients": []}

            if reqUrl in self.streams \
                    and len(self.streams[reqUrl]["clients"]) > 0:

                # We're just pass thru here so resend exactly what we get
                sclient = TCPStreamingClient(httpRequest.socket,
                                             includeHeader=True,
                                             includeBoundary=True,
                                             includeImageHeader=True)

                sclient.start()
                self.streams[reqUrl]["clients"].append(sclient)

                return True

            if httpRequest.isJSON:
                ret, retType, retData = sendHTTPRequest(
                    urljoin(self.clients[httpRequest.item]["url"],
                            httpRequest.path),
                    type=httpRequest.command,
                    jsonData=httpRequest.JSONData,
                    headers=headers,
                    context=httpRequest.context)

            else:
                ret, retType, retData = sendHTTPRequest(
                    urljoin(self.clients[httpRequest.item]["url"], 
                            httpRequest.path),
                    type=httpRequest.command,
                    headers=headers,
                    context=httpRequest.context)

            if retType == "application/json":
                return httpRequest.sendJSON(
                    contentBody=retData,
                    contentType="application/json")

            elif retType.startswith("multipart/x-mixed-replace"):

                if self.streams[reqUrl]["contentType"] is None:
                    self.streams[reqUrl]["contentType"] = retType

                cType, cParam = parse_header(retData.headers.get("content-type"))
                boundary = cParam["boundary"]
                boundary = ("\n--" + boundary).encode()
                endHeaders = "\n\n".encode()
                sock = httpRequest.socket

                # Relaying messages that already have this info
                sclient = TCPStreamingClient(
                    sock,
                    includeHeader=True,
                    includeBoundary=True,
                    includeImageHeader=True)

                sclient.start()

                self.streams[reqUrl]["clients"].append(sclient)

                part = None
                try:
                    for data in retData.iter_content(chunk_size=64):
                        if len(self.streams[reqUrl]["clients"]) == 0:
                            break

                        p = data.find(boundary)
                        if p > 0:

                            part += data[:p]

                            iEnd = part.find(endHeaders)
                            if iEnd >= 0:
                                # Parse Headers:
                                #
                                # hdrs = part[len(boundary):iEnd].decode().strip().split("\n")
                                # for item in hdrs:
                                #     if item.lower().startswith("x-timestamp: "):
                                #         timeStamp = item[12:]
                                #         import datetime
                                #         print(datetime.datetime.fromtimestamp(float(timeStamp.strip())).strftime('%Y-%m-%d %H:%M:%S.%f'))
                                #         break

                                part = part[iEnd + len(endHeaders):]

                            for c in self.streams[reqUrl]["clients"]:
                                if c.connected:

                                    # Prevent overloading stream to
                                    # keep output current
                                    if c.streamQueue.empty():
                                        c.bufferStreamData(part)
                                    else:
                                        # Dropping frame
                                        pass
                                else:
                                    c.logger.debug("Streaming client disconnected.")
                                    self.streams[reqUrl]["clients"].remove(c)

                            part = data[p:]

                        else:
                            if part is None:
                                part = data
                            else:
                                part += data

                except Exception:
                    return True

            return httpRequest.sendHTTP(
                contentBody=retData,
                contentType=retType)

        # Default
        return super()._processRequest(httpRequest)
    
    def _processFileRequest(self, httpRequest):
        try:
            
            if not httpRequest.authenticated and httpRequest.item.lower() not in ["login.html", "jquery.js", "favicon.svg"]:

                if httpRequest.item.lower() in ["index.html"]:
                    return httpRequest.sendRedirect("/admin/login.html")
                else:
                    return httpRequest.sendError()
            
            if httpRequest.item is None:
                return httpRequest.sendError()
            
            fileName = os.path.abspath(os.path.join(self.webFilePath, httpRequest.item))
            if not fileName.startswith(self.webFilePath) or not os.path.isfile(fileName):
                return httpRequest.sendError()
    
            contentType = "text/html"
            readMode = "r"
            if "." in httpRequest.item:
                fileExtension = str(os.path.splitext(httpRequest.item)[1]).lower()
                if fileExtension in httpRequest.mimeTypes:
                    contentType = httpRequest.mimeTypes[fileExtension]
                    readMode = "rb"
            
            contentBody = getFileContents(fileName, readMode)
            if readMode == "r": 
                contentBody = contentBody.replace("__APP_VERSION__", self.version)
            
            return httpRequest.sendHTTP(contentBody=contentBody, contentType=contentType)
                
        except Exception:
            self.logger.error(str(sys.exc_info()[0]))
            self.logger.error(str(traceback.format_exc()))
            return httpRequest.sendError()
        
    def register(self, httpRequest):

        if httpRequest.JSONData is not None:
            data = httpRequest.JSONData
            ctrs = {}

            for c in data:
                if str(c).lower().startswith("http"):
                    if isinstance(data[c], dict):
                        for devId in data[c]:
                            self.clients[devId] = data[c][devId]
                            self.clients[devId]["url"] = str(c)
                            if self.clients[devId]["type"] == "container":
                                ctrs[str(c)] = devId
                    
                    for item in self.clients:
                        if self.clients[item]["url"] == str(c) and item not in data[c]:
                            del self.clients[item]
            
            for devId in self.clients:
                self.clients[devId]["containerId"] = ctrs[self.clients[devId]["url"]] if self.clients[devId]["url"] in ctrs else None

        return httpRequest.sendJSON({ "error": False, "message": "Complete" })
    
    def collect(self, httpRequest):
        
        if httpRequest.isJSON:
            data = httpRequest.JSONData
            if data is not None and isinstance(data, dict):
                if "type" in data and "data" in data:
                    if data["type"] not in self.data:
                        self.data[data["type"]] = []
                        
                    self.data[data["type"]].append(data["data"])
                    
                    if len(self.data[data["type"]]) > 50:
                        self.data[data["type"]].pop()

                    if data["type"] == "AUDIO_INPUT":
                        if "ask" in self.callbacks and isinstance(self.callbacks["ask"], dict):
                            if httpRequest.context.originContainerId is not None and httpRequest.context.originContainerId in self.callbacks["ask"]:
                                if self.callbacks["ask"][httpRequest.context.originContainerId]["timeout"] == 0 or \
                                        self.callbacks["ask"][httpRequest.context.originContainerId]["timeout"] > time.time():
                                    self.callbacks["ask"][httpRequest.context.originContainerId]["timeout"] = time.time() - 1
                                    self.callbacks["ask"][httpRequest.context.originContainerId]["function"](data["data"], httpRequest.context)
                                    return httpRequest.sendJSON({ "error": False, "message": "Complete" }) 
                        
                        self.skill_manager.parseInput(data["data"], httpRequest.context)

                    return httpRequest.sendJSON({ "error": False, "message": "Complete" })   
    
        return httpRequest.sendError()
    
    def status(self, httpRequest):
        headers = None
        if self.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.authenticationKey}
            
        stat = self._getStatus()

        context = httpRequest.context
        context.targetDevType = "container"
        
        # Real-time
        for devId in self.clients:
            item = self.clients[devId]
            if item["url"] not in stat and item["type"] == "container":
                context.targetDevType = item["type"]
                ret, retType, retData = sendHTTPRequest(
                    urljoin(item["url"], "container/" + str(devId) + "/status"), 
                    "GET", 
                    headers=headers,
                    context=context)
                if ret: 
                    stat[item["url"]] = retData[item["url"]]
                    
        return httpRequest.sendJSON(stat) 

    def shutdown(self, httpRequest):

        self.logger.info("Shutdown detected.")
        headers = None
        if self.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.authenticationKey}
            
        context = httpRequest.context
        context.targetDevType = "container"

        for devId in self.clients:
            item = self.clients[devId]
            if item["type"] == "container":
                self.logger.debug("Shutting down device: " + str(devId))
                ret, retType, retData = sendHTTPRequest(
                    urljoin(item["url"], "container/" + str(devId) + "/stop"), 
                    "GET",
                    headers=headers,
                    context=context)
        
        return self.stop(httpRequest)
    
    def restart(self, httpRequest=None):
        headers = None
        if self.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.authenticationKey}
        
        if httpRequest is not None:
            context = httpRequest.context
        else:
            context = KContext()
            context.originDevId = self.id
            context.originDevType = self.type
            context.originContainerId = self.id
            context.originGroup = self.groupName

        context.targetDevType = "container"

        for devId in self.clients:
            item = self.clients[devId]
            if item["type"] == "container":
                ret, retType, retData = sendHTTPRequest(
                    urljoin(item["url"], "container/" + str(devId) + "/restart"), 
                    "GET", 
                    headers=headers)
                
        return super().restart(httpRequest)
    
    def upgradeAll(self, httpRequest=None):
        headers = None
        if self.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.authenticationKey}
        
        if httpRequest is not None:
            context = httpRequest.context
        else:
            context = KContext()
            context.originDevId = self.id
            context.originDevType = self.type
            context.originContainerId = self.id
            context.originGroup = self.groupName

        context.isInput = True
        
        finalRet = True 
        
        for devId in self.clients:
            item = self.clients[devId]
            if "upgrade" in item["accepts"]:
                context.targetDevId = devId
                context.targetDevType = self.clients[devId]["type"]

                ret, retType, retData = sendHTTPRequest(
                    urljoin(item["url"], "device/" + str(devId) + "/upgrade"),
                    "GET",
                    headers=headers,
                    context=context)
                if not ret:
                    self.logger.error("Upgrade failed for device " + str(devId))
                    finalRet = False
    
        if not self.upgrade(httpRequest):
            finalRet = False
            
        return finalRet
    
    def say(self, text, context=None):

        headers = None
        if self.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.authenticationKey}

        targetDevId = None
        targetDevType = None
        targetContainerId = None
        targetGroup = None

        if context is not None:
            targetDevId = context.targetDevId
            targetDevType = context.targetDevType
            targetContainerId = context.targetContainerId
            targetGroup = context.targetGroup

        bFound = False
        speakerId = None

        # Check levels
        # * Device (if specified)
        # * Container (if specified) + Type (if specified)
        # * Group (if specified) + Type (if specified)
        # * Anywhere
        
        # Check if we are targeting a specific speaker
        if targetDevId is not None and targetDevId in self.clients:
            if targetDevType is None or targetDevType == self.clients[targetDevId]["type"]:
                if "speak" in self.clients[targetDevId]["accepts"]:
                    speakerId = targetDevId
                    
                    if targetGroup is not None:
                        groupName = targetGroup  
                    elif "groupName" in self.clients[speakerId]:
                        groupName = self.clients[speakerId]["groupName"] 
                    else: 
                        None

                    bFound = True

        # Check if we are targeting a specific container
        if not bFound and targetContainerId is not None:
            for devId in self.clients:
                item = self.clients[devId]
                if targetDevType is None or targetDevType == item["type"]:
                    if (targetContainerId == item["containerId"]) and "speak" in item["accepts"]:
                        speakerId = devId
                        groupName = item["groupName"] if "groupName" in item else None
                        bFound = True

        # Check if we are targeting a group
        if not bFound and targetGroup is not None:
            for devId in self.clients:
                item = self.clients[devId]
                if targetDevType is None or targetDevType == item["type"]:
                    if (targetGroup == item["groupName"]) and "speak" in item["accepts"]:
                        speakerId = devId
                        groupName = item["groupName"] if "groupName" in item else None
                        bFound = True

        # Fall back: Check for a speaker even if it isn't at the origin device, container, or in the group
        if not bFound:
            for devId in self.clients:
                item = self.clients[devId]
                if "speak" in self.clients[devId]["accepts"]:
                    speakerId = devId
                    groupName = self.clients[devId]["groupName"] if "groupName" in self.clients[devId] else None
                    bFound = True
                
        if not bFound:
            return False

        for devId in self.clients:
            item = self.clients[devId]
            if "audioOutStart" in self.clients[devId]["accepts"] and (groupName is None or groupName == self.clients[devId]["groupName"]):
                sendHTTPRequest(
                    urljoin(item["url"], "device/" + str(devId) + "/audioOutStart"), 
                    "GET", 
                    headers=headers)[0]
        
        ret = sendHTTPRequest(
            urljoin(self.clients[speakerId]["url"], "device/" + str(speakerId) + "/speak"), 
            "POST", 
            jsonData={ "text": text }, 
            headers=headers)[0]

        for devId in self.clients:
            item = self.clients[devId]
            if "audioOutEnd" in self.clients[devId]["accepts"] and (groupName is None or groupName == self.clients[devId]["groupName"]):
                sendHTTPRequest(
                    urljoin(item["url"], "device/" + str(devId) + "/audioOutEnd"), 
                    "GET", 
                    headers=headers)[0]
            
        return ret
    
    def ask(self, text, callback, timeout=0, context=None):

        retVal = self.say(text, context=context)

        if timeout > 0:
            timeout = timeout + time.time()
        else:
            timeout = 0
            
        if context is not None and context.originContainerId is not None:
            self.callbacks["ask"] = { context.originContainerId: { "function": callback, "timeout": timeout } }

        return retVal