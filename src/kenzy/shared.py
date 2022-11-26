"""
Shared library of functions used throughout Kenzy's various modules
"""

import time 
import json
import threading 
from http.server import BaseHTTPRequestHandler 
from urllib.parse import parse_qs, urlparse, urlencode
from cgi import parse_header
import socket
import logging 
import urllib3 
import requests 
import sys
import traceback 
import os
import subprocess
import queue
from errno import ENOPROTOOPT
from email.utils import formatdate


def dayPart():
    """
    Returns the part of the day based on the system time based on generally acceptable breakpoints.
    
    Returns:
        (str):  The part of the day for the current moment (night, morning, evening, etc.).
    """
    
    # All we need is the current hour in 24-hr notation as an integer
    h = int(time.strftime("%H"))
    
    if (h < 4):
        # Before 4am is still night in my mind.
        return "night"
    elif (h < 12):
        # Before noon is morning
        return "morning"
    elif (h < 17):
        # After noon ends at 5pm
        return "afternoon"
    elif (h < 21):
        # Evening ends at 9pm
        return "evening"
    else:
        # Night fills in everything else (9pm to 4am)
        return "night"


def getFileContents(fileName, mode="r", encoding="utf-8"):
    """
    Gets the contents of a file in string or binary form based on mode.
    
    Args:
        fileName (str):  The name of the file
        mode (str):  The file open method; defaults to string reader. (defaults to "r")
        encoding (str):  Encoding to use when reading the file.  (Defaults to UTF-8.)

    Returns:
        (object):  Contents of the file in string or binary form based on mode selection
    """

    if mode != "r":
        encoding = None

    with open(fileName, mode, encoding=encoding) as fp:
        content = fp.read()
    
    return content


def getIPAddress(iface=None):
    """
    Get IP address from specified interface.
    
    Args:
        iface (str): Interface name like 'eth0'.
        
    Returns:
        (str): IP address of interface.
    """
    
    import netifaces
    if iface is None:
        
        ifaces = netifaces.interfaces()
        for iface in ifaces:
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    if addr["addr"] == "127.0.0.1":
                        continue
                    
                    return str(addr["addr"])
    
    else:
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET in addrs:
            for addr in addrs[netifaces.AF_INET]:
                if addr["addr"] == "127.0.0.1":
                    continue
                
                return str(addr["addr"])


def putFileContents(fileName, data, mode="w", encoding="utf-8"):
    """
    Writes the data provided to a file in string or binary form based on mode.
    
    Args:
        fileName (str):  The name of the file
        data (str or bytes):  The data to be written to the file.
        mode (str):  The file open method; defaults to string writer. (defaults to "w")
        encoding (str):  Encoding to use when writing the file.  (Defaults to UTF-8.)

    Returns:
        (object):  Contents of the file in string or binary form based on mode selection
    """

    if mode != "r":
        encoding = None

    with open(fileName, mode, encoding=encoding) as fp:
        fp.write(data)
    
    return True


def py_error_handler(filename, line, function, err, fmt):
    """
    Error handler to translate non-critical errors to logging messages.
    
    Args:
        filename (str): Output file name or device (/dev/null).
        line (int): Line number of error
        function (str): Function containing
        err (Exception): Exception raised for error
        fmt (str): Format of log output
    """

    # Convert the parameters to strings for logging calls
    fmt = fmt.decode("utf-8")
    filename = filename.decode("utf-8")
    fnc = function.decode('utf-8')
    
    # Setting up a logger so you can turn these errors off if so desired.
    logger = logging.getLogger("CTYPES")

    if (fmt.count("%s") == 1 and fmt.count("%i") == 1):
        logger.debug(fmt % (fnc, line))
    elif (fmt.count("%s") == 1):
        logger.debug(fmt % (fnc))
    elif (fmt.count("%s") == 2):
        logger.debug(fmt % (fnc, str(err)))
    else:
        logger.debug(fmt)
    return
    

def sendHTTPRequest(url, type="POST", postData=None, jsonData=None, isStream=True, headers=None, context=None):
    """
    Sends a HTTP request to a remote host.
    
    Args:
        url (str):  The address to submit the request.
        type (str):  The HTTP method of the request (default to POST).
        postData (str or dict): query string-like values of name/value pairs.
        jsonData (dict): Dictionary object to be converted to JSON string
        isStream (bool): indicates if the request is expected to return a stream object or a static response.
        headers (dict): Name/Value pairs for headers e.g. { "X-HDR-NAME": "MY_VALUE" }
        context (object): Context of the request
        
    Returns:
        (bool, contentType, contentObject): Status of request (True for success; HTTP content type of response; Object value or stream pointer of response.
    """
    
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    logger = logging.getLogger("HTTP")

    request_body = None
    
    try:
        if jsonData is not None:
            headers = {} if headers is None else headers
            headers["Content-Type"] = "application/json" 
            request_body = json.dumps(jsonData)
        elif postData is not None and isinstance(postData, dict):
            request_body = urlencode(postData)
        
        if context is not None:
            if headers is None:
                headers = {}
            
            h = context.createHeaders()
            for item in h:
                if item not in headers:
                    headers[item] = h[item]

        if type == "GET":  # Doesn't send request body
            res = requests.get(url, headers=headers, verify=False, stream=isStream)
        else:
            res = requests.post(url, data=request_body, headers=headers, verify=False, stream=isStream)
        
        ret_val = True
        if res.ok:
            try:
                if res.is_redirect or res.is_permanent_redirect:
                    return False, "text/html", "An error occurred in the HTTP request"
                
                ret_type, _ = parse_header(res.headers.get("content-type"))
                if ret_type == "application/json":
                    res_obj = res.json()
                    if "error" in res_obj and "message" in res_obj:
                        result = res_obj
                        ret_val = not result["error"]
                        
                    return ret_val, ret_type, res.json()  # returns as a dict
                
                # Else!
                if ret_type.startswith("text/"):
                    return True, ret_type, res.text  # returns as a string
                elif ret_type == "multipart/x-mixed-replace":
                    return True, ret_type, res  # returns as request item
                else:
                    return True, ret_type, res.content  # returns as bytes
                        
            except Exception:
                logger.error("Unable to parse response from " + str(url) + "")
                return False, ret_type, res.text  # return as string
        else:
            logger.error("Request failed for " + str(url) + "")
            return False, "text/html", "An error occurred in the HTTP request"

    except requests.exceptions.ConnectionError:
        logger.error("Connection Failed: " + url)
    except Exception:
        logger.debug(str(sys.exc_info()[0]))
        logger.debug(str(traceback.format_exc()))
        logger.error("An error occurred.")

    return False, "text/html", "An error occurred in the HTTP request"


def sendSDCPRequest():
    """
    Sends a SDCO/UPNP Request to the network.
    
    Returns:
        (list): list of all devices and their relative headers found on the network.
    """
    
    logger = logging.getLogger("UPNP-CLIENT")
    logger.info("Broadcasting UPNP search for active devices")
    
    msg = \
        'M-SEARCH * HTTP/1.1\r\n' \
        'HOST:239.255.255.250:1900\r\n' \
        'ST:upnp:rootdevice\r\n' \
        'MX:2\r\n' \
        'MAN:"ssdp:discover"\r\n' \
        '\r\n'
    
    # Set up UDP socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.settimeout(2)
    s.sendto(msg.encode(), ('239.255.255.250', 1900) )
    
    devices = []
    
    logger.info("Listening for responses")
    try:
        while True:
            data, addr = s.recvfrom(65507)
            
            d = { 
                "hostname": addr[0],
                "port": addr[1],
                "data": data.decode(),
                "headers": {}
            }
            
            for line in d["data"].split("\n"):
                if ":" in line:
                    h = line.split(":", 1)
                    if len(h) > 1:
                        d["headers"][h[0].strip().upper()] = h[1].strip()
            
            logger.debug(str(d))
            devices.append(d)

    except socket.timeout:
        pass

    return devices


def threaded(fn):
    """
    Thread wrapper shortcut using @threaded prefix
    
    Args:
        fn (function):  The function to executed on a new thread.
        
    Returns:
        (thread):  New thread for executing function.
    """

    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=fn, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread

    return wrapper


def upgradePackage(packageName):
    if packageName is None:
        logging.debug("No package name provided to upgrade.")
        return True

    logger = logging.getLogger(packageName)
    
    cmd = sys.executable + " -m pip install --upgrade --no-input " + packageName
    
    myEnv = dict(os.environ)

    if "QT_QPA_PLATFORM_PLUGIN_PATH" in myEnv:
        del myEnv["QT_QPA_PLATFORM_PLUGIN_PATH"]
    
    p = subprocess.Popen(
        cmd, 
        shell=True, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE, 
        stdin=sys.stdin,
        env=myEnv)
    
    outData, errData = p.communicate() 
    
    if outData is not None:
        if not isinstance(outData, list):
            try:
                outData = outData.decode()
            except Exception:
                pass 
            
            outData = str(outData).split("\n")

        if isinstance(outData, list):
            for line in outData:
                if str(line).strip() != "":
                    logger.info(str(line))
        else:
            if str(outData).strip() != "":
                logger.info(str(outData))

    if errData is not None and str(errData).strip() != "": 
        if not isinstance(errData, list):
            try:
                errData = errData.decode()
            except Exception:
                pass 

            errData = str(errData).split("\n")

        if isinstance(errData, list):
            for line in errData:
                if str(line).strip() != "":
                    logger.error(str(line))
        else:
            if str(errData).strip() != "":
                logger.error(str(errData))
    
    if p.returncode is not None and str(p.returncode) == "0":
        return True
    else:
        return False


class KContext:
    def __init__(self):
        self.timeStamp = None

        self.originDevId = None
        self.originDevType = None
        self.originGroup = None
        self.originContainerId = None 

        self.targetDevId = None
        self.targetDevType = None        
        self.targetGroup = None
        self.targetContainerId = None 
        self.isInput = False

        self.httpRequest = None

    def createHeaders(self):
        hdrs = {}
        if self.timeStamp is not None:
            hdrs["X-TIMESTAMP"] = str(self.timeStamp)
        
        if self.originDevId is not None:
            hdrs["X-ORIGIN-ID"] = str(self.originDevId)

        if self.originContainerId is not None:
            hdrs["X-ORIGIN-CONTAINER"] = str(self.originContainerId)
        
        if self.originDevType is not None:
            hdrs["X-ORIGIN-TYPE"] = str(self.originDevType)

        if self.originGroup is not None:
            hdrs["X-ORIGIN-GROUP"] = str(self.originGroup)

        if self.targetDevId is not None:
            hdrs["X-TARGET-ID"] = str(self.targetDevId)

        if self.targetContainerId is not None:
            hdrs["X-TARGET-CONTAINER"] = str(self.targetContainerId)
        
        if self.targetDevType is not None:
            hdrs["X-TARGET-TYPE"] = str(self.targetDevType)

        if self.targetGroup is not None:
            hdrs["X-TARGET-GROUP"] = str(self.targetGroup)

        if self.isInput:
            hdrs["X-INPUT"] = "1"

        return hdrs


class KHTTPHandler(BaseHTTPRequestHandler):
    def __init__(self, container, sock=None, address=("localhost", 0), raw_request=None, origin=None):
        
        self.container = container
        self.socket = sock
        self.address = address  # tuple (ip, port)
        self.authenticated = False 
        
        if self.container is None or self.container.authenticationKey is None:
            self.authenticated = True
             
        self.rfile = raw_request
        
        # first line of request e.g. "GET /index.html"
        self.raw_requestline = self.rfile.readline() if raw_request is not None else None
        
        self.error_code = None
        self.error_message = None
        self.path = None
        self.headers = {}

        self.isJSON = False
        self.JSON = None
        self.getVars = {}

        self.isFileRequest = False
        self.isDeviceRequest = False
        self.isBrainRequest = False
        self.isTypeRequest = False
        self.isAuthRequest = False
        
        self.item = None
        self.action = None
        self.origin = origin
        self.groupName = None
        self.context = None 

        self.isResponseSent = False
        
        self.mimeTypes = {
            ".jpg": "image/jpeg",
            ".gif": "image/gif",
            ".png": "image/png",
            ".bmp": "image/bmp",
            ".svg": "image/svg+xml",
            ".mp3": "audio/mp3",
            ".mp4": "video/mp4"
        }
        
        if self.rfile is not None:
            self.parse_request()
        
        if self.path is not None:
            self.getVars = parse_qs(urlparse(self.path).query)
            self.path = urlparse(self.path).path
            
        if self.headers.get('content-type') is not None:
            ctype = parse_header(self.headers.get('content-type'))[0]
            if ctype == "application/json":
                self.isJSON = True
        
        if not self.authenticated and self.headers.get('cookie') is not None:
            for item in self.headers.get("cookie").split(";"):
                (key, keyVal) = item.split("=")
                if key == "token" and keyVal == self.container.authenticationKey:
                    self.authenticated = True

        # Prep Context
        self.getRequestContext()

    def getRequestContext(self):
        if self.context is None:
            self.context = KContext()

        self.context.httpRequest = self 
        
        if self.headers.get("x-timestamp") is not None:
            self.context.timeStamp = float(self.headers.get("x-timestamp"))

        self.context.originDevId = self.headers.get("x-origin-id")
        self.context.originContainerId = self.headers.get("x-origin-container")
        self.context.originDevType = self.headers.get("x-origin-type")
        self.context.originGroup = self.headers.get("x-origin-group")
        
        self.context.targetDevId = self.headers.get("x-target-id")
        self.context.targetContainerId = self.headers.get("x-target-container")
        self.context.targetDevType = self.headers.get("x-target-type")        
        self.context.targetGroup = self.headers.get("x-target-group")

        if self.headers.get("x-input") is not None:
            self.context.isInput = True if str(self.headers.get("x-input")).lower() in ["1", "true"] else False
        else:
            self.context.isInput = False

        if self.context.originDevId is None:
            self.context.originGroup = self.container.groupName

    def createHeaders(self, contentType="text/html", httpStatusCode=200, httpStatusMessage="OK", headers=None):
       
        try:
            response_status = str(httpStatusCode) + " " + str(httpStatusMessage)
            response_status = response_status.replace("(", "").replace(")", "").replace("'", "").replace(",", "")
            
            if headers is None:
                headers = {}

            headers["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S %Z")
            
            if ("Content-Type" not in headers) and contentType is not None:
                headers["Content-Type"] = contentType
            
            if "Access-Control-Allow-Origin" not in headers:
                headers["Access-Control-Allow-Origin"] = "*"
            
            if "Cache-Control" not in headers:
                headers["Cache-Control"] = "no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0"

            if "Expires" not in headers:
                headers["Expires"] = "Mon, 1 Jan 2020 00:00:00 GMT"
                
            if "Pragma" not in headers:
                headers["Pragma"] = "no-cache"
            
            if self.context is not None:
                h = self.context.createHeaders()
                for item in h:
                    if item not in headers:
                        headers[item] = h[item]

            response_headers = ["HTTP/1.1 " + response_status]
            for h in headers:
                response_headers.append(str(h).strip() + ": " + str(headers[h]).strip())

            response_text = "\n".join(response_headers)

            return response_text
        except Exception:
            logging.debug(str(sys.exc_info()[0]))
            logging.debug(str(traceback.format_exc()))
            logging.error("An error occurred while attemptign to create the headers for the HTTP response")
            return None

    def sendRedirect(self, url):
        if self.isResponseSent:  # Bail if we already sent a response to requestor
            return True 

        if self.socket is None:
            return False 
        
        try:
            self.socket.send(("HTTP/1.1 307 Temporary Redirect\nLocation: " + str(url)).encode())
            self.socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

        self.socket.close()
        self.isResponseSent = True
        return True
        
    def sendError(self):
        if self.isResponseSent:  # Bail if we already sent a response to requestor
            return True 
        
        if self.socket is None:
            return False 
        
        try:
            self.socket.send("HTTP/1.1 404 NOT FOUND".encode())
            self.socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass

        self.socket.close()
        self.isResponseSent = True
        return True
    
    def sendHeaders(self, contentType="text/html", httpStatusCode=200, httpStatusMessage="OK", headers=None):
        if self.isResponseSent:  # Bail if we already sent a response to requestor
            return None 
        
        if self.socket is None:
            return None 
        
        ret = True
        try:
            response_text = self.createHeaders(contentType, httpStatusCode=httpStatusCode, httpStatusMessage=httpStatusMessage, headers=headers)
            response_text += "\n\n"
            self.socket.send(response_text.encode())
                
            ret = self.socket
        except Exception:
            self.socket.close()
            ret = None

        return ret
    
    def sendHTTP(self, contentBody=None, contentType="text/html", httpStatusCode=200, httpStatusMessage="OK", headers=None):
        if self.isResponseSent:  # Bail if we already sent a response to requestor
            return True 
        
        if self.socket is None:
            return False 
        
        ret = True
        try:

            try:
                if contentType.startswith("image/") or contentType.startswith("audio/") or contentType.startswith("video"):
                    response_body = contentBody
                else:
                    response_body = contentBody.encode()
            except (UnicodeDecodeError, AttributeError):
                raise
                response_body = contentBody

            if headers is None:
                headers = {}

            headers["Content-Length"] = str(len(response_body))

            response_text = self.createHeaders(contentType, httpStatusCode=httpStatusCode, httpStatusMessage=httpStatusMessage, headers=headers)
            
            if contentBody is not None and contentBody is not None:
                response_text += "\n\n"
                self.socket.send(response_text.encode())
                self.socket.send(response_body)
            else:
                self.socket.send(response_text.encode())
                
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()
        except Exception:
            self.socket.close()
            ret = False
    
        self.isResponseSent = True
        return ret
    
    def sendJSON(self, contentBody=None, contentType="application/json", httpStatusCode=200, httpStatusMessage="OK", headers=None):
        return self.sendHTTP(
            contentBody=json.dumps(contentBody), 
            contentType=contentType, 
            httpStatusCode=httpStatusCode, 
            httpStatusMessage=httpStatusMessage, 
            headers=headers)
    
    def validateRequest(self):
        if self.path is None:
            
            if self.command is not None and str(self.command) in ["GET", "POST"]:
                self.sendError()
            else:
                self.socket.shutdown(socket.SHUT_RDWR)
                self.socket.close()
                
            return False 
        
        if self.path.lower() == "/favicon.ico":
            self.isFileRequest = True
            self.item = "favicon.svg"  # Switched the ICON extension!!!
            self.action = "GET"
            return True
        
        if self.path.lower() == "/auth":
            self.isAuthRequest = True
            self.item = ""
            self.action = "AUTH"
            return True
        
        if not isinstance(self.path, str) or str(self.path).lower() in ["/", "/admin", "/admin/"]:
            self.sendRedirect("/admin/index.html")
            return False
        
        if isinstance(self.path, str):
            parts = self.path.strip("/").split("/")  # Trim leading/trailing spaces
            
            if len(parts) > 0:
                tgt = parts[0].lower()

                if tgt == "brain":
                    self.isBrainRequest = True
                elif tgt == "container" or tgt == "device":
                    self.isDeviceRequest = True
                elif tgt == "type":
                    self.isTypeRequest = True
                elif tgt == "admin":
                    self.isFileRequest = True
                else:
                    self.sendError()
                    return False
                
                if self.isBrainRequest:
                    if len(parts) < 2:
                        self.sendError()
                        return False
                    else:
                        self.item = "brain"
                        self.action = parts[1]
                
                elif self.isFileRequest:
                    if len(self.path) < 8:
                        self.sendError()
                        return False
                    else:
                        self.item = self.path[len("/admin/"):]
                        self.action = "GET"
                
                else:
                    if len(parts) < 3:
                        self.sendError()
                        return False
                    else:
                        self.item = parts[1]
                        self.action = parts[2]
                        
                        # Support for JSON requests in format { "command": "ACCEPT_FUNCTION" }
                        if self.action.lower() == "instance" and self.isJSON:
                            if self.JSONData is not None and "command" in self.JSON:
                                self.action = self.JSON["command"]
                        
                if self.isTypeRequest and self.item.lower() == "-":
                    self.item = "all"

            else:
                self.sendError()
                return False
                
        return True
    
    @property
    def JSONData(self):
        if self.JSON is not None:
            return self.JSON
        else:
            if self.headers is None:
                return self.JSON
            
            length = int(self.headers['content-length'])
            try:
                json_body = self.rfile.read(length)
                self.JSON = json.loads(json_body)
            except Exception:
                pass
        
        return self.JSON


class SilenceStream():
    """
    Hides C library messages by redirecting to log file
    """
    
    def __init__(self, stream, log_file=None, file_mode='a'):
        """
        Redirect stream to log file
        
        Args:
            stream (stream): Inbound stream containing errors to hide
            log_file (str): File name or device name for error log
            file_mode (str): Mode to open log_file
        """
        
        self.fd_to_silence = stream.fileno()  # Store the stream we're referening
        self.log_file = log_file  # Store the log file to redirect to
        self.file_mode = file_mode  # Append vs. Writex

    def __enter__(self):
        """
        Starts the stream redirection to the log file
        """

        if (self.log_file is None): 
            return  # No log file means we can skip this and let output flow as normal.
        
        self.stored_dup = os.dup(self.fd_to_silence)  # Store the original pointer for the stream
        self.devnull = open(self.log_file, self.file_mode)  # Get a pointer for the new target
        os.dup2(self.devnull.fileno(), self.fd_to_silence)  # Redirect to the new pointer

    def __exit__(self, exc_type, exc_value, tb):
        """
        Restore stream back to its original state before the silencer was called.
        
        Args:
            exc_type (obj): Execution type. Not used.
            exc_value (obj): Execution value. Not used.
            tb (obj): Traceback. Not used.
        """

        if (self.log_file is None): 
            return   # No log file means we can skip this as nothing needs to change.
        
        os.dup2(self.stored_dup, self.fd_to_silence)  # Restore the pointer to the original
        self.devnull = None  # Cleanup
        self.stored_dup = None  # Cleanup
        

class TCPStreamingClient(object):
    """
    TCP Streaming Media Client Class
    """
    
    def __init__(self, sock, includeHeader=True, includeBoundary=True, imageHeaders=None, includeImageHeader=True):
        """
        TCP Streaming Client Initialization
        """
        
        self.streamBuffer = ""
        self.streamQueue = queue.Queue()
        self.streamThread = threading.Thread(target=self.stream)
        self.streamThread.daemon = True
        self.connected = True
        self.kill = False

        self.logger = logging.getLogger("TCP_STREAM")
        self.sock = sock
        self.sock.settimeout(5)
        self.boundary = 'be0850c82dd4983ddc49a51a797dce49'
        self.includeHeader = includeHeader
        self.includeBoundary = includeBoundary
        self.includeImageHeader = includeImageHeader
        self.imageHeaders = imageHeaders
        
        self.logger.debug("Streaming client connected.")
        
        if includeHeader:
            self.sock.send(self.request_headers().encode())

        if includeBoundary:
            self.sock.send(("--" + self.boundary + "\n").encode())
            
    def bufferStreamData(self, data):
        """
        Adds new data to the buffer for transmission to the requestor.
        
        Args:
            data (byte): Data to be saved to buffer
        """
        # use a thread-safe queue to ensure stream buffer is not modified while we're sending it
        self.streamQueue.put(data)

    def image_headers(self, data):
        """
        Generates headers for each frame of the MJPEG Stream.
        
        Args:
            data (byte):  Data from buffer that will be sent to client
            
        Returns:
            (str):  Formatted HTTP headers for individual image frame
        """
        hdrData = [
            "X-Timestamp: " + str(time.time()),
            "Content-Length: " + str(len(data)),
            "Content-Type: image/jpeg"
        ]
        
        if self.imageHeaders is not None and isinstance(self.imageHeaders, list):
            hdrData.extend(self.imageHeaders)
            
        # Send with each frame
        return "\n".join(hdrData) + "\n\n"
        
    def request_headers(self, context=None):
        """
        Creates the headers for sending to the client on the beginning of the stream.
        
        Args:
            context (KContext):  Context object of the request

        Returns:
            (str):  Formatted HTTP headers for initial response to client
        """

        return "\n".join([
            "HTTP/1.1 200 OK",
            "Date: " + time.strftime("%a, %d %b %Y %H:%M:%S %Z"),
            "Cache-Control: no-store, no-cache, must-revalidate, pre-check=0, post-check=0, max-age=0",
            "Connection: close",
            "Content-Type: multipart/x-mixed-replace;boundary=\"" + self.boundary + "\"",
            "Expires: Mon, 1 Jan 2030 00:00:00 GMT",
            "Pragma: no-cache", 
            "Access-Control-Allow-Origin: *"]) + "\n\n"
            
    def start(self):
        """
        Starts an independent thread for the streaming client to hand off data from the buffer to avoid blocking calls on new images.
        """
        self.streamThread.start()

    def stream(self):
        """
        Thread runtime for reading data from the buffer and transmitting it to the client
        """
        
        while self.connected:
            # this call blocks if there's no data in the queue, avoiding the need for busy-waiting
            streamBuffer = self.streamQueue.get()
            
            # check if kill or connected state has changed after being blocked
            if (self.kill or not self.connected):
                self.stop()
                return

            self.transmit(streamBuffer)

    def stop(self):
        """
        Stops the client connection and related sockets.
        """
        
        self.sock.close()

        self.kill = True
        self.connected = False

    def transmit(self, data):
        """
        Sends data to client via TCP (HTTP) response.
        
        Args:
            data (byte): Data to be transmitted
        
        Returns:
            (bool): True on success
        """
        try:
            
            if self.includeImageHeader:
                self.sock.send(self.image_headers(data).encode())

            self.sock.send(data)

            if self.includeBoundary:
                self.sock.send(("\n--" + self.boundary + "\n").encode())

            return True
        except Exception:
            self.connected = False
            self.sock.close()
            
        return True
    
    
class UPNPServer(object):
    """
    Simple UPNP Server for announcing availability of services.

    Check out the link below for a more complete example of SSDP/UPNP:
    https://github.com/ZeWaren/python-upnp-ssdp-example
    """
    
    def __init__(self, tcp_port=None, hostname=None, usn=None, st='upnp:rootdevice', location=None, server=None, 
                 cache_control='max-age=1800', headers=None,
                 parent=None, callback=None):
        
        self.parent = parent
        self._serverSocket = None
        self._serverThread = None
        self.tcp_port = tcp_port if tcp_port is not None else 1900
        self.hostname = hostname if hostname is not None else "239.255.255.250"
        self.logger = logging.getLogger("UPNP-SRV")
        self._isRunning = False
        self.version = self.parent.version
        self.services = {}
        self.nickname = "UPNP Server"
    
        if usn is None:
            if self.parent is not None:
                usn = 'uuid:' + str(self.parent.id) + '::upnp:rootdevice'
            else:
                import uuid
                usn = 'uuid:' + str(uuid.uuid4()) + '::upnp:rootdevice'
        
        if location is None:
            my_ip = getIPAddress()
            proto = "http://"
            if self.parent is not None:
                if not self.parent.use_http:
                    proto = "https://"
                location = proto + str(my_ip) + ':' + str(self.parent.tcp_port)
            else:
                location = proto + str(my_ip) + ":8080"
        
        if server is None:
            if self.parent is not None and self.parent.isBrain:
                server = "Kenzy.Ai Brain UPNP v" + str(self.parent.version)
            else:
                server = "Kenzy.Ai UPNP"
           
        if self.parent is not None and self.parent.isBrain:
            if headers is None:
                headers = {}
                
            headers["X-KENZY-TYPE"] = "BRAIN" 
        
        self.register(
            usn,
            st,
            location,
            server=server,
            cache_control=cache_control,
            headers=headers)
    
    @threaded
    def _UDPServer(self):

        self._isRunning = True
        self._serverSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except socket.error as le:
                if le.errno == ENOPROTOOPT:
                    pass
                else:
                    raise

        addr = socket.inet_aton(self.hostname)
        interface = socket.inet_aton('0.0.0.0')
        cmd = socket.IP_ADD_MEMBERSHIP
        self._serverSocket.setsockopt(socket.IPPROTO_IP, cmd, addr + interface)
        self._serverSocket.bind(('0.0.0.0', self.tcp_port))
        self._serverSocket.settimeout(1)

        while self._isRunning:
            try:
                data, addr = self._serverSocket.recvfrom(1024)
                self._recv(data, addr)
            except socket.timeout:
                continue
        
        self._shutdown()
        
    def _recv(self, data, addr):

        (host, port) = addr

        try:
            header = data.decode().split('\r\n\r\n')[0]
        except ValueError as err:
            self.logger.error(err)
            return

        linesIn = header.split('\r\n')
        cmd = linesIn[0].split(' ')  # First line "NOTIFY" or "MSEARCH"

        headers = {}
        
        if len(linesIn) > 1:        
            for line in linesIn[1:]:  # skip first line
                if len(line.strip()) > 0:
                    h = line.split(":", 1)
                    if len(h) == 2 and h[0].strip() != "":
                        headers[h[0].strip().upper()] = h[1].strip()  # Convert line into name/value pairs

        self.logger.debug('Incoming "' + str(cmd[0]) + ' ' + str(cmd[1]) + '" from ' + str(host) + ':' + str(port))
        self.logger.debug('Headers: ' + str(headers))  # Note that headers are uppercased
        
        if cmd[0] == 'M-SEARCH' and cmd[1] == '*':
            self._search(headers, host, port)
        elif cmd[0] == 'NOTIFY' and cmd[1] == '*':
            pass  # We're not doing anything with NOTIFY requests at the moment
        else:
            self.logger.warning('Unknown UPNP request: ' + str(cmd[0]) + ' ' + str(cmd[1]))
            
    def _search(self, headers, host, port):

        self.logger.debug('Search request for ' + str(headers['ST']))

        for item in self.services.values():
            if item['ST'] == headers['ST'] or headers['ST'] == 'ssdp:all':
                response = ['HTTP/1.1 200 OK']

                usn = None
                for k, v in item.items():
                    if k == 'USN':
                        usn = v
                    if k not in ('HOST', 'headers', 'last-seen'):
                        response.append(str(k) + ': ' + str(v))
                        
                    if k == "headers":
                        if isinstance(v, dict):
                            for x in v:
                                response.append(str(x) + ': ' + str(v[x]))

                if usn:
                    response.append('DATE: ' + str(formatdate(timeval=None, localtime=False, usegmt=True)))
                    self._send_data(('\r\n'.join(response) + '\r\n\r\n'), (host, port))

    def register(self, usn, st, location, server=None, cache_control='max-age=1800', headers=None):

        self.logger.info('Registering ' + str(st) + " (" + str(location) + ")")

        self.services[usn] = {}
        self.services[usn]['USN'] = usn
        self.services[usn]['LOCATION'] = location
        self.services[usn]['ST'] = st
        self.services[usn]['EXT'] = ''
        self.services[usn]['SERVER'] = server if server is not None else "UPNP Server"
        self.services[usn]['CACHE-CONTROL'] = cache_control

        self.services[usn]['last-seen'] = time.time()
        self.services[usn]["headers"] = headers
        
        if self._serverSocket:
            self._notify(usn)

    def unregister(self, usn):
        del self.services[usn]
        return True

    def is_known(self, usn):
        if usn in self.services:
            return True
        
        return False
            
    def _shutdown(self):
        for st in self.services:
            self._byebye(st)
                
    def _send_data(self, response, destination):
        self.logger.debug('Send response to ' + str(destination))
        try:
            self._serverSocket.sendto(response.encode(), destination)
        except (AttributeError, socket.error) as msg:
            self.logger.warning("Respond failed to send: " + str(msg))
                
    def _byebye(self, usn):

        self.logger.info('Sending ssdp:byebye notification for ' + str(usn))

        out_response = [
            'NOTIFY * HTTP/1.1',
            'HOST: ' + str(self.hostname) + ":" + str(self.tcp_port),
            'NTS: ssdp:byebye',
        ]
        try:
            d = dict(self.services[usn].items())  # Copy the original so we don't mess with it.
            
            d['NT'] = d['ST']

            del d['ST']
            del d['last-seen']
            del d["headers"]
            
            for item in d:
                out_response.append(str(item) + ": " + str(d[item]))

            self.logger.debug(str(out_response))
        
            if self._serverSocket:
                try:
                    self._serverSocket.sendto(('\r\n'.join(out_response) + "\r\n\r\n").encode(), (self.hostname, self.tcp_port))
                except (AttributeError, socket.error) as msg:
                    self.logger.error("Failure sending byebye notification: " + str(msg))
        except KeyError as msg:
            self.logger.error("Error creating byebye notification: " + str(msg))
    
    def _notify(self, usn):

        self.logger.info('Sending alive notification for ' + str(usn))

        out_response = [
            'NOTIFY * HTTP/1.1',
            'HOST: ' + str(self.hostname) + ":" + str(self.tcp_port),
            'NTS: ssdp:alive',
        ]
        
        d = dict(self.services[usn].items())  # Copy the original so we don't mess with it.
            
        d['NT'] = d['ST']
        
        del d['ST']
        del d['last-seen']
        del d["headers"]
        
        for item in d:
            out_response.append(str(item) + ": " + str(d[item]))

        self.logger.debug(str(out_response))
        
        try:
            self._serverSocket.sendto('\r\n'.join(out_response).encode(), (self.hostname, self.tcp_port))
            self._serverSocket.sendto('\r\n'.join(out_response).encode(), (self.hostname, self.tcp_port))
        except (AttributeError, socket.error) as msg:
            self.logger.warning("Failure sending out alive notification: " + str(msg))
    
    @property
    def accepts(self):
        return ["start", "stop"]
    
    def isRunning(self):
        return self._isRunning
    
    def start(self, httpRequest=None):
        self._serverThread = self._UDPServer()
        return True
        
    def stop(self, httpRequest=None):
        self._isRunning = False
        
        if self._serverThread is not None:
            self.logger.debug("Waiting for thread to finish.")
            self._serverThread.join()
        
        self.logger.debug("Stopped.")
        return True