import json
import logging
import uuid
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
import ssl
from urllib.parse import parse_qs
import requests
import threading
import time
from . import VERSION, __app_name__, __app_title__
from .extras import SSDPServer, discover_ssdp_services, get_file, get_local_ip_address
        

class KenzyContext:
    url = None
    type = None
    location = None
    group = None

    def __init__(self, url=None, type=None, location=None, group=None):
        self.url = url
        self.type = type
        self.location = location
        self.group = group

    def get(self):
        return {
            "url": self.url,
            "type": self.type,
            "location": self.location,
            "group": self.group
        }


class KenzyRequest:
    action = None
    payload = None
    context = None

    def __init__(self, action=None, payload=None, context=None):
        self.action = action
        self.payload = payload
        self.context = context

    def get(self):
        return {
            "status": self.status,
            "errors": self.errors,
            "data": self.data
        }


class KenzyResponse:
    status = None
    errors = None
    data = None

    def __init__(self, status=None, data=None, errors=None):
        if status is not None:
            self.status = status
        
        if errors is not None:
            self.errors = errors
        
        if data is not None:
            self.data = data

    def is_success(self):
        if self.status is not None and str(self.status) == "success":
            return True
        
        return False

    def get(self):
        return {
            "status": self.status,
            "errors": self.errors,
            "data": self.data
        }


class KenzySuccessResponse(KenzyResponse):
    status = "success"
    data = None
    errors = None

    def __init__(self, data=None, errors=None):
        super().__init__(data=data, errors=errors)


class KenzyErrorResponse(KenzyResponse):
    status = "failed"

    def __init__(self, errors=None, data=None):
        super().__init__(data=data, errors=errors)


class KenzyRequestHandler(BaseHTTPRequestHandler):
    logger = logging.getLogger("HTTP-REQ")

    def log_message(self, format, *args):
        # if self.path.lower() != "/upnp.xml":
        self.logger.debug(str(str(self.client_address[0]) + " - " + str(format % args)) + " - " + str(self.headers.get("User-Agent")))

    def set_vars(self, content):
        return content.replace(
            b"{service_url}", 
            self.server.service_url.encode()
        ).replace(
            b"{server_uuid}", 
            self.server.settings.get("id", "").encode()
        ).replace(
            b"{VERSION}", 
            ".".join([str(x) for x in VERSION]).encode()
        ).replace(
            b"{APP_NAME}", 
            __app_name__.encode()
        ).replace(
            b"{APP_TITLE}", 
            __app_title__.encode()
        )

    def send_file(self, file_name=None):
        file_path = self.path if file_name is None else file_name
        mime_type, content = get_file(file_path)

        if mime_type is None and content is None:
            self.send_response(404)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'File Not Found')
            return

        if file_path.lower().endswith("upnp.xml") or file_path.lower().endswith(".html"):
            content = self.set_vars(content)

        self.send_response(200)
        self.send_header('Content-type', mime_type)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        try:

            if self.server.local_url != self.server.service_url and not self.path.lower().startswith("/api/"):
                self.send_response(302)    
                self.send_header('Location', self.server.service_url.rstrip("/") + self.path)
                self.end_headers()
                return

            if self.path.lower() == "/" or self.path.lower().startswith("/admin/") or self.path.lower() == "/admin":
                self.send_response(302)    
                self.send_header('Location', '/index.html')
                self.end_headers()
                return

            # Yeah, I know... but it works with most browsers
            if self.path == "/favicon.ico":
                self.path = "/favicon.svg"
            
            if not self.path.lower().startswith("/api/"):
                self.send_file(self.path)
                return
            
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"<html><head><title>Error: Unsupported Request</title></head><body>")
            self.wfile.write(b"<h1>Unsupported Request</h1><p>Please use POST for data transmission.</p></body></html>")

        except Exception as e:
            if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
                self.send_error(500, str(e))
            else:
                self.send_error(500, "An internal error occurred")
                self.logger.error(str(e))

    def do_POST(self):
        try:
            content_type = self.headers['Content-Type']
            content_length = int(self.headers['Content-Length'])

            if not self.server.authenticate(self.headers["Authorization"]):
                response_data = KenzyResponse("failed", None, "Unauthorized").get()
                response_body = json.dumps(response_data).encode('utf-8')

                # Send response status code
                self.send_response(200)

                # Send headers
                self.send_header('Content-type', 'application/json')
                self.end_headers()

                # Send response body
                self.wfile.write(response_body)
                return

            payload = self.rfile.read(content_length)

            if content_type.startswith('application/json'):
                try:
                    data = json.loads(payload.decode('utf-8'))
                    # Process the JSON data as needed
                    if not isinstance(data, dict):
                        response_data = KenzyResponse("failed", None, "Invalid request")
                    else:
                        context = KenzyContext()
                        if isinstance(data.get("context"), dict):
                            context = KenzyContext(**data.get("context"))

                        if data.get("action") is not None:
                            response_data = self.server.command(data.get("action"), data.get("payload"), context).get()
                        else:
                            response_data = KenzyResponse("failed", None, "Unrecognized request")

                    response_body = json.dumps(response_data).encode('utf-8')

                    # Send response status code
                    self.send_response(200)

                    # Send headers
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()

                    # Send response body
                    self.wfile.write(response_body)
                except json.JSONDecodeError:
                    self.send_error(400, "Invalid JSON payload")

            elif content_type.startswith('multipart/form-data'):
                # Parse the multipart/form-data payload
                form_data = parse_qs(payload.decode('utf-8'))
                # Process the form data as needed
                response_data = {'message': 'Received form data', 'data': form_data}
                response_body = json.dumps(response_data).encode('utf-8')

                # Send response status code
                self.send_response(200)

                # Send headers
                self.send_header('Content-type', 'application/json')
                self.end_headers()

                # Send response body
                self.wfile.write(response_body)

            else:
                self.send_error(415, "Unsupported Media Type")

        except Exception as e:
            self.send_error(500, str(e))


class KenzyHTTPServer(HTTPServer):
    logger = logging.getLogger("HTTP-SRV")
    settings = {}
    device = None
    ssdp_server = None
    service_url = None
    local_url = None
    api_key = None
    restart_thread = None
    restart_event = threading.Event()

    def __init__(self, **kwargs) -> None:

        self.settings = kwargs
        self.service_url = kwargs.get("service_url")
        self.id = kwargs.get("id", uuid.uuid4())
        self.api_key = kwargs.get("api_key")

        # Get Service URL and start UPNP/SSDP server is appropriate
        proto = "https" if kwargs.get("ssl.enable", False) else "http"
        host = kwargs.get("host", "0.0.0.0")
        port = kwargs.get("port", 8080)

        try:
            ip_addr = host if host != "0.0.0.0" else get_local_ip_address()
        except Exception:
            ip_addr = "127.0.0.1"
            
        self.local_url = kwargs.get("service_url", "%s://%s:%s" % (proto, ip_addr, port))

        if str(kwargs.get("upnp", "client")).lower().strip() == "server":
            # start UPNP
            if self.service_url is None:
                self.service_url = self.local_url 
            self.ssdp_server = SSDPServer(usn_uuid=self.id, service_url="%s/upnp.xml" % self.service_url)
        
        elif str(kwargs.get("upnp", "client")).lower().strip() == "client":
            # search for UPNP service
            url = discover_ssdp_services()
            if url is not None:
                self.service_url = url

        if self.service_url is None:
            self.service_url = self.local_url

        self.logger.info(f"Service URL set to {self.service_url}")
        
        super().__init__((host, port), KenzyRequestHandler)

        if kwargs.get("ssl.enable", False):
            cert_file = os.path.expanduser(kwargs.get("ssl.cert_file"))
            key_file = os.path.expanduser(kwargs.get("ssl.key_file"))
            self.socket = ssl.wrap_socket(self.socket, certfile=cert_file, keyfile=key_file, server_side=True)

    def command(self, action=None, payload=None, context=None):
        if context is None:
            context = KenzyContext()

        for item in self.device.accepts:
            if str(item).strip().lower() == str(action).strip().lower():
                return eval("self.device." + str(item).strip().lower() + "(data=payload, context=context)")

        return KenzyErrorResponse("Unrecognized command.")

    def authenticate(self, api_key):
        if str(api_key).lower().startswith("bearer "):
            api_key = str(api_key)[7:].strip().strip("\"").strip("'")
        
        server_key = self.api_key
        if server_key is None or server_key == api_key:
            return True
        
        return False

    def get_local_context(self):
        return KenzyContext(
            url=self.local_url,
            type=self.device.type,
            location=self.device.location,
            group=self.device.group
        )

    def collect(self, data=None, context=None):
        if not isinstance(context, KenzyContext):
            context = self.get_local_context()
    
        local_url = self.local_url
        service_url = self.service_url

        print(service_url, local_url)
        if service_url != local_url:
            # Send to service_url
            req = {
                "action": "collect",
                "payload": data,
                "context": context.get()
            }

            self.send_request(req)
        else:
            if self.device is not None and not hasattr(self.device, "accepts") and "collect" in self.device.accepts:
                self.device.collect(data, context)
            else:
                self.logger.debug(f"{data}, {context.get()}")

        return True

    def register(self, **kwargs):
        if str(self.settings.get("type", "core")).lower().strip() == "core":
            # Save local
            pass
        else:
            # Send to service_url
            pass

        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")
    
    def send_request(self, payload, headers=None, url=None):
        token = uuid.uuid4()

        if not isinstance(payload, dict):
            return False
        
        try:
            if headers is None or not isinstance(headers, dict):
                headers = {}

            headers["Authorization"] = f"Bearer {token}"
            headers["Content-Type"] = "application/json"

            if url is None:
                url = self.service_url

            response = requests.post(url, json=payload, headers=headers, verify=False)

            response_data = response.json()
            self.logger.debug(f"{response_data}")
        except requests.exceptions.RequestException as e:
            print("An error occurred:", e)
            return False

        return True
    
    def _restart_watcher(self):
        self.restart_event.clear()

        try:
            if "restart" in self.device.accepts:
                if hasattr(self.device, 'restart_enabled'):
                    while not self.restart_event.is_set():
                        do_restart = self.device.restart_enabled
                        if do_restart:
                            time.sleep(2)
                            self.device.restart()
                        else:
                            time.sleep(.5)

        except KeyboardInterrupt:
            pass

    def serve_forever(self, poll_interval: float = 0.5, *args, **kwargs):
        if not self.device.is_alive():
            self.device.start()
            
        if self.ssdp_server is not None:
            self.ssdp_server.start()

        self.restart_thread = threading.Thread(target=self._restart_watcher, daemon=True)
        self.restart_thread.start()
        
        self.logger.info("Server started on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")
        super().serve_forever(poll_interval)

    def set_device(self, device):
        self.device = device

    def shutdown(self, **kwargs):
        if self.device.is_alive():
            self.device.stop()

        if self.ssdp_server is not None:
            self.ssdp_server.stop()
            self.ssdp_server = None

        if self.restart_thread is not None and self.restart_thread.is_alive():
            self.restart_event.set()
        
        self.logger.info("Server stopped on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")

        super().shutdown()

    def status(self, **kwargs):
        # TODO: Local vs. Remote Status
        r = []
        for item in self.devices:
            r.append(item)
        return r

    def upgrade(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")
