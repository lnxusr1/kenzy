import json
import logging
import uuid
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs
from . import VERSION, __app_name__, __app_title__
from .extras import SSDPServer, discover_ssdp_services, get_file
        

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
                #if self.server.devices["id"].is_alive():
                #    self.server.devices["id"].stop()
                #else:
                #    self.server.devices["id"].start()

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
            payload = self.rfile.read(content_length)

            if content_type.startswith('application/json'):
                try:
                    data = json.loads(payload.decode('utf-8'))
                    # Process the JSON data as needed
                    resp = None
                    if isinstance(data, dict):
                        if data.get("action") == "status":
                            resp = self.server.status()

                    response_data = {'message': 'Received JSON data', 'data': data, 'response': resp }
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
    devices = {}
    settings = {}
    config_file = os.path.join(os.path.expanduser("~"), ".kenzy", "config.yml")
    ssdp_server = None
    service_url = None
    local_url = None

    def __init__(self, server_address, RequestHandlerClass: BaseHTTPRequestHandler = KenzyRequestHandler, 
                 bind_and_activate: bool = True, settings: dict = {}) -> None:

        self.settings = settings if isinstance(settings, dict) else {}

        settings["id"] = settings.get("id", str(uuid.uuid4()))
        self.service_url = settings.get("service_url")

        # Get Service URL and start UPNP/SSDP server is appropriate
        http_settings = settings.get("http", {})
        proto = "https" if http_settings.get("ssl", {}).get("enable", False) else "http"

        self.local_url = http_settings.get("url", "%s://%s:%s" % (proto, server_address[0], server_address[1]))

        if self.settings.get("upnp", False):
            
            if str(self.settings.get("type", "core")).lower().strip() == "core":
                # start UPNP
                if self.service_url is None:
                    self.service_url = self.local_url 
                self.ssdp_server = SSDPServer(usn_uuid=settings.get("id"), service_url="%s/upnp.xml" % self.service_url)
            
            else:
                # search for UPNP service
                url = discover_ssdp_services()
                if url is not None:
                    self.service_url = url

        if self.service_url is None:
            self.service_url = self.local_url

        self.logger.info(f"Service URL set to {self.service_url}")

        # load devices
        
        #speaker = Speaker()
        #self.add_device(id="speaker-device", device=speaker)
        
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    def add_device(self, id=None, device=None, url=None, **kwargs):

        try:
            if id is None and device is not None:
                v = device.id
                if v is not None and str(v).strip() > 0:
                    id = str(v).strip()
        except Exception:
            pass

        if url is None:
            url = self.local_url

        self.devices[id] = device

    def collect(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")

    def delete_device(self, id):
        if id in self.devices and self.devices[id].get("active", False):
            self.stop(id=id)

            # if device_url != local_url then tell device_url and remove (this is on core)
            # if device_url == local_url and device_url != service_url then remove
            del self.devices[id]

    def notify(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")
    
    def register(self, **kwargs):
        if str(self.settings.get("type", "core")).lower().strip() == "core":
            # Save local
            pass
        else:
            # Send to service_url
            pass

        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")
    
    def serve_forever(self, poll_interval: float = 0.5, *args, **kwargs):
        if self.ssdp_server is not None:
            self.ssdp_server.start()
        
        self.logger.info("Server started on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")
        super().serve_forever(poll_interval)

    def shutdown(self, **kwargs):
        if self.ssdp_server is not None:
            self.ssdp_server.stop()
            self.ssdp_server = None
        
        self.logger.info("Server stopped on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")

        super().shutdown()

    def start(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")

    def status(self, **kwargs):
        # TODO: Local vs. Remote Status
        r = []
        for item in self.devices:
            r.append(item)
        return r
    
    def stop(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")

    def upgrade(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")
