import logging
import socket
import ssl
import time
import uuid
from .shared import threaded, getIPAddress, KHTTPHandler, sendHTTPRequest, upgradePackage, KContext
from urllib.parse import urljoin
import sys
import traceback


class GenericContainer():
    def __init__(self, **kwargs):
        """
        Generic Container Initialization
        
        Args:
            tcpPort (int): The TCP port on which the TCP server will listen for incoming connections
            hostName (str): The network hostname or IP address for the TCP server daemon
            sslCertFile (str): The path and file name of the SSL certificate (.crt) for secure communications
            sslKeyFile (str): The path and file name of the SSL private key (.key or .pem) for secure communications
            brainUrl (str): URL of brain device.
            groupName (str): Group or Room name for devices. (optional)
            authentication (dict): API Key required for web-based access
        
        Both the ssl_cert_file and ssl_key_file must be present in order for SSL to be leveraged.
        """

        # tcpPort=None, hostName="", sslCertFile=None, sslKeyFile=None, brainUrl=None, groupName=None, authentication=None
        self.config = kwargs
        
        from . import __app_name__, __version__, __app_title__
        self._version = __version__
        self._appName = __app_title__
        
        self._packageName = __app_name__
        self.nickname = None
        self.groupName = self.config.get("groupName")
        self.authenticationKey = self.config.get("authentication", {}).get("key")
        self.authUser = self.config.get("authentication", {}).get("username","admin")
        self.authPassword = self.config.get("authentication", {}).get("password","admin")
        
        self.app = None
        
        self._doRestart = False
        self.isBrain = False
        self.id = uuid.uuid4()
        self.type = "container"

        self._thread = None
        self._serverSocket = None             # Socket object (where the listener lives)
        self._serverThread = None             # Thread object for TCP Server (Should be non-blocking)
        self._threadPool = []           # List of running threads (for incoming TCP requests)
        
        self._isRunning = False         # Flag used to indicate if TCP server should be running
        
        self.logger = logging.getLogger("CONTAINER")
        
        # TCP Command Interface
        self.tcp_port = self.config.get("tcpPort", 8081)
        self.hostname = self.config.get("hostName", "") 
        self.use_http = True
        self.keyfile = self.config.get("sslCertFile")
        self.certfile = self.config.get("sslKeyFile")

        self.use_http = False if self.keyfile is not None and self.certfile is not None else True
        
        self.isOffline = None 
        
        # Simultaneous clients.  Max is 5.  This is probably overkill.
        # NOTE:  This does not mean only 5 clients can exist.  This is how many
        #        inbound TCP connections the server will accept at the same time.
        #        A client will not hold open the connection so this should scale
        #        to be quite large before becoming a problem.
        self.tcp_clients = 5            
        
        self.my_url = None

        self.brain_url = self.config.get("brainUrl")
        if self.brain_url is None:
            self.brain_url = "http://localhost:8080"
        
        self.accepts = ["stop", "stopDevices", "status", "restart", "upgrade"]
        self.devices = {}
        
    def initialize(self, **kwargs):
        """
        Base initialization intended to be overridden in child classes.
        """

        self.args = kwargs

        self.nickname = self.args.get("nickname")

        if self.my_url is None:
            my_ip = getIPAddress() if self.hostname is None or self.hostname == "" else self.hostname
            self.my_url = "http://"
            if not self.use_http:
                self.my_url = "https://"
            self.my_url = self.my_url + str(my_ip if my_ip is not None and my_ip != "" else "localhost") + ":" + str(self.tcp_port)

        self.addDevice(self.type, self, self.id, False, False)
        self.logger.debug("Container [" + str(self.type) + "] started with ID = " + str(self.id))
        return True
    
    def _authenticate(self, httpRequest):
        if self.authenticationKey is None:
            return httpRequest.sendJSON({ "error": False, "message": "Authentication completed successfully." })
        
        if httpRequest.isJSON:
            if httpRequest.JSONData is not None and "key" in httpRequest.JSONData:
                if httpRequest.JSONData["key"] == self.authenticationKey:
                    return httpRequest.sendJSON(
                        { "error": False, "message": "Authentication completed successfully." }, 
                        headers={ "Set-Cookie": "token=" + self.authenticationKey }
                    )
            
            if httpRequest.JSONData is not None and "username" in httpRequest.JSONData and "password" in httpRequest.JSONData:
                if httpRequest.JSONData["username"] == str(self.authUser) and httpRequest.JSONData["password"] == str(self.authPassword):
                    return httpRequest.sendJSON(
                        { "error": False, "message": "Authentication completed successfully." }, 
                        headers={ "Set-Cookie": "token=" + self.authenticationKey }
                    )
                    
        return httpRequest.sendJSON({ "error": True, "message": "Authentication failed." })
        
    def _purgeThreadPool(self):
        """
        Purges the thread pool of completed or dead threads
        """
        i = len(self._threadPool) - 1
        while i >= 0:
            try:
                if self._threadPool[i] is None or not self._threadPool[i].is_alive():
                    self._threadPool.pop(i)
            except Exception:
                self._threadPool.pop(i)
                        
            i = i - 1
            
    def _waitForThreadPool(self):
        """
        Pauses and waits for all threads in threadpool to complete/join the calling thread
        """
        i = len(self._threadPool) - 1
        while i >= 0:
            try:
                if self._threadPool[i] is not None and self._threadPool[i].is_alive():
                    self._threadPool[i].abort()
            except Exception:
                pass
                        
            i = i - 1
    
    @threaded
    def _tcpServer(self):
        """
        Internal function that creates the listener socket and hands off incoming connections to other functions.
        
        Returns:
            (thread):  The thread for the TCP Server daemon

        """
        self._isRunning = True 
                
        self._serverSocket = socket.socket()
        self._serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._serverSocket.bind((self.hostname, self.tcp_port))
        self._serverSocket.listen(self.tcp_clients)
        
        if not self.use_http:
            self.logger.debug("SSL Enabled.")
            self._serverSocket = ssl.wrap_socket(
                self._serverSocket, 
                keyfile=self.keyfile, 
                certfile=self.certfile,
                server_side=True)

        while self._isRunning:

            try:
                # Accept the new connection
                conn, address = self._serverSocket.accept()
                
                t = self._acceptConnection(conn, address)
                self._purgeThreadPool()
                self._threadPool.append(t)
                    
            except (KeyboardInterrupt):  # Occurs when we press Ctrl+C on Linux
                
                # If we get a KeyboardInterrupt then let's try to shut down cleanly.
                # This isn't expected to be hit as the primary thread will catch the Ctrl+C command first
                
                self.logger.info("Ctrl+C detected.  Shutting down.")
                self.stop()  # Stop() is all we need to cleanly shutdown.  Will call child class's method first.
                
                return True  # Nice and neat closing
                
            except (OSError):  # Occurs when we force close the listener on stop()
                
                # this error will be raised on occasion depending on how the TCP socket is stopped
                # so we put a simple "ignore" here so it doesn't fuss too much.
                pass    
        
        return True
    
    @threaded
    def _acceptConnection(self, conn, address):
        """
        Accepts inbound TCP connections, parses request, and calls appropriate handler function
        
        Args:
            conn (socket): The TCP socket for the connection
            address (tuple):  The originating IP address and port for the incoming request e.g. (192.168.0.139, 59209).
            
        Returns:
            (thread):  The thread for the request's connection
        """
        
        try:
            # Parse the inbound request
            req = KHTTPHandler(self, conn, address, conn.makefile(mode='b'))
            self.logger.debug("HTTP (" + str(address[0]) + ") " + str(req.command) + " " + str(req.path) + " [" + ("JSON" if req.isJSON else "") + "]")
            if req.validateRequest():
                self._processRequest(req)
                
        except Exception:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            self.logger.error("An error was encountered while accepting the connection.")
            raise
        
    def _getStatus(self):
        myDevices = {}
        for devId in self.devices:
            item = self.devices[devId]
            myDevices[devId] = { 
                "id": devId, 
                "nickname": None,
                "type": item["type"], 
                "accepts": item["accepts"], 
                "active": True, 
                "version": None, 
                "groupName": item["groupName"] 
            }
            
            try:
                myDevices[devId]["nickname"] = item["device"].nickname
                myDevices[devId]["active"] = item["device"].isRunning()
            except Exception:
                self.logger.debug(str(sys.exc_info()[0]))
                self.logger.debug(str(traceback.format_exc()))
                self.logger.error("Unable to parse local device status [running state].")
                pass

            try:
                myDevices[devId]["version"] = item["device"].version
            except Exception:
                self.logger.debug(str(sys.exc_info()[0]))
                self.logger.debug(str(traceback.format_exc()))
                self.logger.error("Unable to parse local device status [version].")
                pass
            
        return { self.my_url: myDevices }
    
    def _processRequest(self, httpRequest):
        try:
            if httpRequest.isAuthRequest:
                return self._authenticate(httpRequest)
            
            if httpRequest.isFileRequest:
                return httpRequest.sendRedirect(self.brain_url)
        
            if not httpRequest.authenticated:
                return httpRequest.sendError()
        
            if (not httpRequest.isTypeRequest) and httpRequest.item in self.devices:
                exec("self.devices[httpRequest.item][\"device\"]." + httpRequest.action + "(httpRequest)")
                if not httpRequest.isResponseSent:
                    return httpRequest.sendJSON({ "error": False, "message": "Request completed successfully." })
                else:
                    return True 
                
            if httpRequest.isTypeRequest:
                
                for devId in self.devices:
                    item = self.devices[devId]
                    if httpRequest.item == "all" or item["type"] == httpRequest.item:
                        if httpRequest.action in self.devices[devId]["device"].accepts():
                            exec("self.devices[devId][\"device\"]." + httpRequest.action + "(httpRequest)")

                if not httpRequest.isResponseSent:
                    return httpRequest.sendJSON({ "error": False, "message": "Request completed successfully." })
                else:
                    return True 

        except AttributeError:
            return httpRequest.sendJSON({ "error": True, "message": "Request not supported." })
        except TypeError:
            return httpRequest.sendJSON({ "error": True, "message": "Request not supported." })
        except Exception:
            raise
            return httpRequest.sendError()
        
        return httpRequest.sendError()
    
    def registerWithBrain(self):
        """
        Sends current container and child device plugin status to brain
        """

        headers = None
        if self.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.authenticationKey}
            
        ret = sendHTTPRequest(urljoin(self.brain_url, "brain/register"), jsonData=self._getStatus(), headers=headers)[0]
        return ret
    
    def addDevice(self, type, device, id=None, autoStart=True, isPanel=False, groupName=None):
        """
        Add Device to list.
        
        Args:
            type (str):  The full class path to the device.
            device (obj):  The instance of the device
            id (str):  The string representation of a unique identifier.
            autoStart (bool): Automatically start the device
            groupName (str): Override parent device container group name per device
            
        Returns:
            (bool): True on success; False on failure.
        """
        type = str(type).strip()
        groupName = self.groupName if groupName is None else groupName

        try:
            if id is None:
                id = str(uuid.uuid4())
                id = device.deviceId  # Override with the device's actual ID if available
            else:
                device.deviceId = str(id)  # Override automatic setting with specified value

        except AttributeError:
            pass
        except TypeError:
            pass
        
        accepts = None
        try:
            accepts = device.accepts()  # try as method
        except AttributeError:
            pass
        except TypeError:
            pass
        
        try:
            # Attempt to set the parent value on the device
            device.parent = self
        except Exception:
            pass
        
        if accepts is None or not isinstance(accepts, list):
            try:
                accepts = device.accepts  # try as variable
            except AttributeError:
                pass
            except TypeError:
                pass
            
        if accepts is None or not isinstance(accepts, list):
            accepts = []
            
        for i, item in enumerate(accepts):
            accepts[i] = str(item)
        
        self.devices[str(id)] = { 
            "type": str(type),
            "device": device,
            "accepts": accepts,
            "active": True,
            "isPanel": isPanel,
            "groupName": groupName
        }
        
        try:
            if autoStart and "start" in accepts and not device.isRunning():
                self.logger.info("Starting Device: " + str(id) + " (" + str(type) + ")")
                self.devices[str(id)]["device"].start()
        except Exception:
            pass
        
        if not self.isBrain:
            self.registerWithBrain()
        
        return True
    
    @property
    def version(self):
        """
        Returns the version of the service as a string value.
        """
        return self._version
    
    def isRunning(self):
        """
        Determine if device is running.
        
        Returns:
            (bool):  True if running, False if not running
        """
        return self._isRunning
        
    def start(self, useThreads=True):
        """
        Starts the container process, optionally using threads.
        
        Args:
            useThreads (bool):  Indicates if the brain should be started on a new thread.
            
        Returns:
            (bool):  True on success else will raise an exception.
        """

        if self._isRunning:
            return True 

        self._thread = self._tcpServer()
        self.logger.info("Started @ " + str(self.my_url))

        if not useThreads:
            self._thread.join()

        return True
    
    def status(self, httpRequest):
        """
        Collect status as a JSON object for container and all devices.
        
        Args:
            httpRequest (kenzy.shared.KHTTPHandler): Used to respond to status requests.
            
        Returns:
            (bool): True on success; False on Failure
        """
        
        return httpRequest.sendJSON(self._getStatus())
        
    def wait(self, seconds=0):
        """
        Waits for any active servers to complete before closing
        
        Args:
            seconds (int):  Number of seconds to wait before calling the "stop()" function
            
        Returns:
            (bool):  True on success else will raise an exception.
        """
        
        if self.app is not None:
            self.app.exec_()
                
        if seconds > 0:
            self.logger.info("Shutting down in " + str(seconds) + " second(s).")
            for i in range(0, seconds):
                if self.isRunning():  # Checking to be sure nothing killed us while we wait.
                    time.sleep(1)
            
            if self.isRunning() and self._thread is not None:
                self.stop()
        
        if self._thread is not None:
            self._thread.join()
            
        return True

    def stop(self, httpRequest=None):
        """
        Stops the brain TCP server daemon.
        
        Args:
            httpRequest (KHTTPHandler):  Not used, but required for compatibility
            
        Returns:
            (bool):  True on success else will raise an exception.
        """

        self.logger.debug(str(self.id) + ": Stop detected.")
        if httpRequest is not None:
            httpRequest.sendJSON({ "error": False, "message": "All services are being shutdown." })

        if not self.isRunning():
            return True 

        self._isRunning = False  # Kills the listener loop 
        
        self._waitForThreadPool()
        self.stopDevices()
                
        if self._serverSocket is not None:
            try:
                self._serverSocket.shutdown(socket.SHUT_RDWR)
                self._serverSocket.close()
                self._serverSocket = None
            except Exception:
                pass 
        
        if self.app is not None:
            self.app.quit()
        
        self.logger.debug(str(self.id) + ": Stopped.")
            
        return True
    
    def restart(self, httpRequest=None):
        self._doRestart = True
        return self.stop(httpRequest)
    
    def upgrade(self, httpRequest=None):
        return upgradePackage(self._packageName)
    
    def stopDevices(self, httpRequest=None):
        """
        Stops all devices currently in container's list.
        
        Args:
            httpRequest (KHTTPHandler):  Not used, but required for compatibility
            
        Returns:
            (bool): True on success; False on failure
        """
        
        self.logger.debug(str(self.id) + ": Stopping devices.")

        ret = True
        for item in self.devices:
            if item == str(self.id):
                continue 
            
            if "stop" in self.devices[item]["accepts"] and self.devices[item]["device"].isRunning():
                try:
                    self.logger.debug(str(self.id) + ": Attempting to stop local device: (" + str(self.devices[item]["type"]) + ") " + str(item))
                    self.devices[item]["device"].stop()
                    self.logger.debug(str(self.id) + ": Device stopped (" + str(self.devices[item]["type"]) + ") " + str(item))
                except Exception:
                    ret = False
                    
                try:
                    if self.devices[item]["isPanel"]:
                        self.logger.debug(str(self.id) + ": Attempting to close local panel: (" + str(self.devices[item]["type"]) + ") " + str(item))
                        self.devices[item]["device"].close()
                        self.logger.debug(str(self.id) + ": Panel closed (" + str(self.devices[item]["type"]) + ") " + str(item))
                except Exception:
                    pass
        
        self.logger.debug(str(self.id) + ": Devices stopped.")
        return ret
    
    def callbackHandler(self, inType, data, deviceId=None, context=None):
        """
        The target of all input/output devices.  Sends collected data to the brain.  Posts request to "/data".
        
        Args:
            inType (str):  The type of data collected (e.g. "AUDIO_INPUT").
            data (object):  The object to be converted to JSON and sent to the brain in the body of the message.
            deviceId (str):  The id of the device object making the callback
            context (KContext):  Context object if preferable over deviceId.

        Returns:
            (bool):  True on success or False on failure.
        """
        
        headers = None
        if context is None:
            context = KContext()
            context.isInput = True

        if context.targetDevId is None:
            context.targetDevId = "brain"
            context.targetDevType = "brain"
            context.targetGroup = "brain"

        if deviceId is not None and deviceId in self.devices:
            context.originDevId = deviceId

        if context.originDevId is not None:
            context.originDevType = self.devices[context.originDevId]["type"]
            context.originGroup = self.devices[context.originDevId]["groupName"] 
        
        context.originContainerId = self.id

        headers = context.createHeaders()

        if self.authenticationKey is not None:
            headers["Cookie"] = "token=" + self.authenticationKey
        
        jsonData = { "type": inType, "data": data }
        result = sendHTTPRequest(urljoin(self.brain_url, "/brain/collect"), jsonData=jsonData, headers=headers, context=context)[0]
        return result 
    

class GenericDevice():
    def __init__(self, **kwargs):
        
        self.args = kwargs

        if not hasattr(self, "version"):
            self.version = "0.0.0"
        
        if not hasattr(self, "_packageName"):
            self._packageName = None
        
        if not hasattr(self, "nickname"):
            self.nickname = self.args.get("nickname")

        self.deviceId = uuid.uuid4()
        self._isRunning = False

        self.updateSettings()

    def updateSettings(self):
        self.parent = self.args.get("parent")
        self._callbackHandler = self.args.get("callback")
        return True

    def callback(self, inType, data, context=None):
        """
        The target of all input/output devices.  Sends collected data to the brain.  Posts request to "/data".
        
        Args:
            inType (str):  The type of data collected (e.g. "AUDIO_INPUT").
            data (object):  The object to be converted to JSON and sent to the brain in the body of the message.
            context (KContext):  Context object if preferable over deviceId. (optional)

        Returns:
            (bool):  True on success or False on failure.
        """

        if self._callbackHandler is not None:
            return True 

        if context is None:
            context = KContext()
            context.originDevId = self.deviceId
            context.originDevType = None
            context.originGroup = None
            context.originContainerId = None 

        return self._callbackHandler(inType=inType, data=data, context=context)
        
    @property
    def accepts(self):
        return ["start", "stop"]  # Add "upgrade" if the device can be upgraded with "pip install --upgrade" command.
    
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