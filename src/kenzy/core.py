import json
import logging
import uuid
import os
import sys
import traceback
import copy
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import ssl
from urllib.parse import parse_qs
import requests
# from requests.adapters import HTTPAdapter
import urllib3
import threading
import time
import concurrent.futures
from . import VERSION, __app_name__, __app_title__, __version__
from .extras import SSDPServer, discover_ssdp_services, get_file, get_local_ip_address, GenericCommand
        

class RegisterCommand(GenericCommand):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.action = "register"


class KenzyContext:

    def __init__(self, url=None, type=None, location=None, group=None):
        self.url = url
        self.type = type
        self.location = location
        self.group = group

    def to_json(self):
        return self.get()

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

    def to_json(self):
        return self.get()

    def get(self):
        return {
            "status": self.status,
            "errors": self.errors,
            "data": self.data
        }


class KenzyResponse:

    def __init__(self, status=None, data=None, errors=None, request=None):
        self.status = status
        self.errors = errors
        self.data = data
        self.request = request

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
    def __init__(self, data=None, errors=None):
        super().__init__(data=data, errors=errors)
        self.status = "success"


class KenzyErrorResponse(KenzyResponse):
    def __init__(self, errors=None, data=None):
        super().__init__(data=data, errors=errors)
        self.status = "failed"


class KenzyRequestHandler(BaseHTTPRequestHandler):
    logger = logging.getLogger("HTTP-REQ")

    def log_message(self, format, *args):
        # if self.path.lower() != "/upnp.xml":
        try:
            if hasattr(self, "headers"):
                self.logger.debug(str(str(self.client_address[0]) + " - " + str(format % args)) + " - " + str(self.headers.get("User-Agent")))
            else:
                self.logger.debug(str(str(self.client_address[0]) + " - " + str(format % args)))
        except AttributeError:
            pass

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
        try:
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
            self.wfile.flush()

        except BrokenPipeError:
            pass
        
        except Exception as e:
            self.send_error(500, str(e))
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))

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

        except BrokenPipeError:
            pass

        except Exception as e:
            if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
                self.send_error(500, str(e))
            else:
                self.send_error(500, "An internal error occurred")
                self.logger.debug(str(sys.exc_info()[0]))
                self.logger.debug(str(traceback.format_exc()))
                self.logger.debug(str(e))

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
                            response_data = self.server.command(data.get("action"), data.get("payload"), context)
                            if not isinstance(response_data, KenzyResponse):
                                logging.error(str(response_data))
                                response_data = KenzyErrorResponse("Unrecognized response from device.")
                        else:
                            response_data = KenzyResponse("failed", None, "Unrecognized request")

                    response_body = json.dumps(response_data.get()).encode('utf-8')

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

        except BrokenPipeError:
            pass

        except Exception as e:
            self.send_error(500, str(e))
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))


class KenzyHTTPServer(ThreadingMixIn, HTTPServer):
    logger = logging.getLogger("HTTP-SRV")

    def __init__(self, **kwargs) -> None:
        self.device = None
        self.ssdp_server = None
        self.remote_devices = {}
        self.restart_event = threading.Event()
        self.restart_thread = None
        self.register_event = threading.Event()
        self.register_thread = None
        self.upnp = "client"
        self.upnp_timeout = 45
        self.timers = {}

        self.thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=20)
        self.active = False

        self.settings = kwargs
        self.service_url = kwargs.get("service_url")
        self.id = kwargs.get("id", uuid.uuid4())
        self.api_key = kwargs.get("api_key")

        # Get Service URL and start UPNP/SSDP server is appropriate
        proto = "https" if kwargs.get("ssl.enable", False) else "http"
        host = kwargs.get("host", "0.0.0.0")
        port = kwargs.get("port", 9700)

        try:
            ip_addr = host if host != "0.0.0.0" else get_local_ip_address()
        except Exception:
            ip_addr = "127.0.0.1"
            
        self.local_url = kwargs.get("service_url", "%s://%s:%s" % (proto, ip_addr, port))

        self.upnp = str(kwargs.get("upnp.type", "client")).lower().strip() 
        self.upnp_timeout = int(kwargs.get("upnp.timeout", 45))

        if self.upnp == "server":
            # start UPNP
            if self.service_url is None:
                self.service_url = self.local_url 
                self.logger.info(f"Service URL set to {self.service_url}")
            self.ssdp_server = SSDPServer(usn_uuid=self.id, service_url="%s/upnp.xml" % self.service_url)
        
        elif self.upnp == "client":
            self._set_service_url()

        if self.service_url is None:
            self.service_url = self.local_url
            self.logger.info(f"Service URL set to {self.service_url}")
        
        super().__init__((host, port), KenzyRequestHandler)

        if kwargs.get("ssl.enable", False):
            cert_file = os.path.expanduser(kwargs.get("ssl.cert_file"))
            key_file = os.path.expanduser(kwargs.get("ssl.key_file"))
            self.socket = ssl.wrap_socket(self.socket, certfile=cert_file, keyfile=key_file, server_side=True)

    def _set_service_url(self):
        # search for UPNP service
        if self.upnp == "client" and self.service_url is None:
            url = discover_ssdp_services(timeout=self.upnp_timeout)
            if url is not None and (self.service_url is None or self.service_url != url):
                self.service_url = url
                self.logger.info(f"Service URL set to {self.service_url}")

    @property
    def version(self):
        return __version__
    
    def command(self, action=None, payload=None, context=None):
        if context is None:
            context = KenzyContext()

        if str(action).strip().lower() == "register":
            return self.register(data=payload, context=context)

        for item in self.device.accepts:
            if str(action).strip().lower() == "shutdown":
                t = threading.Thread(target=self.shutdown)
                t.daemon = True
                t.start()
                self.timers["shutdown"] = t

                return KenzySuccessResponse("Shutdown commencing.")

            if str(item).strip().lower() == str(action).strip().lower():
                ret = eval("self.device." + str(item).strip().lower() + "(data=payload, context=context)")
                return ret

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

    def collect(self, data=None, context=None, wait=True, timeout=None):
        if not isinstance(context, KenzyContext):
            context = self.get_local_context()
    
        local_url = self.local_url
        service_url = self.service_url

        if service_url != local_url:
            # Send to service_url
            req = {
                "action": "collect",
                "payload": data,
                "context": context.get()
            }

            self.send_request(req, wait=wait, timeout=timeout)
        else:
            if self.device is not None and not hasattr(self.device, "accepts") and "collect" in self.device.accepts:
                self.device.collect(data, context)
            else:
                self.logger.debug(f"{data}, {context.get()}")

        return True

    def _register(self, **kwargs):
        cnt = 0
        self.register_event.clear()

        while not self.register_event.is_set():
            if cnt > 40:
                self.register()
                cnt = 0

            cnt += 1
            time.sleep(0.5)

    def register(self, **kwargs):
        local_url = self.local_url
        service_url = self.service_url

        if service_url == local_url:
            data = kwargs.get("data", {})
            url = data.get("url")
            if url is not None:
                if url not in self.remote_devices:
                    self.logger.info(f"Registered remote device {url}")
                else:
                    self.logger.debug(f"Registered remote device {url}")

                self.remote_devices[url] = data

            return KenzySuccessResponse("Register completed successfully.")
        else:
            if self.device is not None:
                cmd = RegisterCommand()
                cmd.set("url", self.local_url)

                if "status" in self.device.accepts:
                    st = self.device.status().get().get("data", {})
                    for item in st:
                        cmd.set(item, st.get(item))

                # Send to service_url
                if not self.send_request(cmd):
                    self._set_service_url()

    def send_request(self, payload, headers=None, url=None, wait=True, timeout=None):
        if isinstance(payload, dict):
            if wait:
                return self._send_request(payload=payload, headers=headers, url=url, timeout=timeout)
            else:
                self.thread_pool.submit(self._send_request, payload=payload, headers=headers, url=url, timeout=timeout)
                return True

        if isinstance(payload, GenericCommand):
            if payload.get_url() is None:
                ctx = payload.get_context()
                if isinstance(ctx, KenzyContext) and ctx.location is not None:

                    ret = False
                    for device_url in self.remote_devices:
                        device = self.remote_devices.get(device_url)
                        if device.get("active", False) and device.get("location") == ctx.location:
                            if payload.action in device.get("accepts", []):
                                payload.set_url(device_url)
                                ret = self._send_command(copy.copy(payload))
                    
                    return ret
                
            if timeout is not None:
                payload.timeout = timeout

            return self._send_command(payload, wait=wait)

    def _send_command(self, payload, wait=True):
        ret = True

        payload.set_context(self.get_local_context())
        
        # use payload context to get group/location

        pre_cmds = payload.pre()
        post_cmds = payload.post()

        # Send PRE
        for cmd in pre_cmds:
            cmd.set_context(payload.get_context())
            url = cmd.get_url()
            x_payload = cmd.get()
            if not self.send_request(payload=cmd, url=url, wait=wait, timeout=payload.timeout):
                ret = False

        # Send Primary
        url = payload.get_url()
        x_payload = payload.get()
        if not self.send_request(payload=x_payload, url=url, wait=wait, timeout=payload.timeout):
            ret = False

        # Send POST
        for cmd in post_cmds:
            cmd.set_context(payload.get_context())
            url = cmd.get_url()
            x_payload = cmd.get()
            if not self.send_request(payload=cmd, url=url, wait=wait, timeout=payload.timeout):
                ret = False
    
        return ret

    def _send_request(self, payload, headers=None, url=None, timeout=None):
        token = uuid.uuid4()

        if isinstance(payload, dict):
            payload["context"] = payload.get("context", self.get_local_context().get())
        else:
            return False
            
        try:
            if headers is None or not isinstance(headers, dict):
                headers = {}

            headers["Authorization"] = f"Bearer {token}"
            headers["Content-Type"] = "application/json"

            if url is None:
                url = self.service_url

            kwargs = { "verify": False }
            if timeout is not None:
                kwargs["timeout"] = timeout

            # s = requests.Session()
            # s.mount(url.split(":", 1)[0] + "://", HTTPAdapter(max_retries=1, pool_block=False))
            response = requests.post(url, json=payload, headers=headers, **kwargs)
            response_data = response.json()
            self.logger.debug(f"Response: {response_data}")

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, TimeoutError, urllib3.exceptions.ReadTimeoutError):
            self.logger.error("Request timed out")
            self.logger.debug(f"Timeout error: url={url} action={payload.get('action')}")
            return False
        except (requests.exceptions.ConnectionError, ConnectionRefusedError, urllib3.exceptions.NewConnectionError, urllib3.exceptions.MaxRetryError):
            self.logger.error("Request connection error")
            self.logger.debug(f"Connection error: url={url} action={payload.get('action')}")
            return False
        except requests.exceptions.RequestException:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            self.logger.error("An error occurred")
            return False

        return True
    
    def _restart_watcher(self):
        self.restart_event.clear()

        try:
            if "restart" in self.device.accepts:
                if hasattr(self.device, 'restart_enabled'):
                    self.logger.debug("Restart watcher enabled.")
                    while not self.restart_event.is_set():
                        do_restart = self.device.restart_enabled
                        if do_restart:
                            time.sleep(2)
                            self.logger.debug("Restart Flag Identified.")
                            self.device.restart()
                        else:
                            time.sleep(.5)

        except KeyboardInterrupt:
            pass

    def serve_forever(self, poll_interval: float = 0.5, *args, **kwargs):
        self.active = True

        if not self.device.is_alive():
            self.device.start()
            
        if self.ssdp_server is not None:
            self.ssdp_server.start()

        self.restart_thread = threading.Thread(target=self._restart_watcher, daemon=True)
        self.restart_thread.start()

        if self.service_url != self.local_url:
            self.register_event.clear()
            self.register_thread = threading.Thread(target=self._register, daemon=True)
            self.register_thread.start()

        self.logger.info("Server started on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")
        super().serve_forever(poll_interval)

    def set_device(self, device):
        self.device = device

    def shutdown(self, **kwargs):
        for item in self.remote_devices:
            cmd = GenericCommand("shutdown", context=self.get_local_context(), url=item)
            self.send_request(cmd, url=item)

        self.logger.info("SHUTDOWN: %s", self.device.type)
        if self.restart_thread is not None and self.restart_thread.is_alive():
            self.restart_event.set()

        if self.ssdp_server is not None:
            self.ssdp_server.stop()
            self.ssdp_server = None

        if self.register_thread is not None and self.register_thread.is_alive():
            self.register_event.set()

        if self.device.is_alive():
            self.device.stop()

        self.logger.info("Server attempting shutdown on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")
        self.socket.close()
        
        if self.active:
            self.active = False
            self.timers["shutdown"] = threading.Thread(target=self.shutdown, daemon=True)
            self.timers["shutdown"].start()
            return
        
        super().shutdown()
        self.logger.info("Server stopped on " + str("%s:%s" % self.server_address) + " (" + str(self.server_name) + ")")

    def status(self, **kwargs):
        # TODO: Local vs. Remote Status
        r = []
        for item in self.devices:
            r.append(item)
        return r

    def upgrade(self, **kwargs):
        # TODO: Add feature
        raise NotImplementedError("Feature not implemented")
