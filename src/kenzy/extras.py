import socket
import logging
import os
import re
import mimetypes
import threading
import time
import uuid
import requests
import xml.etree.ElementTree as ET
from . import __app_title__, __version__
import json


SSDP_DEVICE_TYPE = "urn:schemas-upnp-org:device:Kenzy-Core:1"


def get_raw_value(setting_value):
    if "." in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
        setting_value = float(setting_value.replace(",", ""))
    elif "." not in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
        setting_value = int(setting_value.replace(",", ""))
    elif setting_value.lower().strip() in ["true", "false"]:
        setting_value = bool(setting_value.lower().strip())
    elif setting_value.startswith("{") and setting_value.endswith("}"):
        setting_value = json.loads(setting_value)
    elif setting_value.startswith("[") and setting_value.endswith("]"):
        setting_value = json.loads(setting_value)

    return setting_value


def clean_string(input_string):
    pattern = r"[^a-zA-Z0-9_\-./]"
    return re.sub(pattern, "", input_string)


def get_file(requested_file):
    file_path = requested_file
    file_path = file_path if "?" not in file_path else file_path[0:file_path.find("?")]
    file_path = file_path if "#" not in file_path else file_path[0:file_path.find("#")]
    file_path = str(clean_string(file_path).replace("..", "").replace("//", "/"))
    file_path = file_path.lower().lstrip("/")

    if file_path == "" or file_path.endswith("/"):
        file_path += "index.html"

    if not os.path.exists(os.path.join(os.path.dirname(__file__), "web", file_path)):
        return None, None
        
    mime_type, _ = mimetypes.guess_type(file_path)
    with open(os.path.join(os.path.dirname(__file__), "web", file_path), "rb") as fp:
        return mime_type, fp.read()


def get_local_ip_address():
    try:
        # Create a temporary socket to retrieve the local IP address
        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_socket.connect(("192.168.0.1", 0))  # Connect to a dummy IP address

        # Get the local IP address from the temporary socket
        local_ip_address = temp_socket.getsockname()[0]

        # Close the temporary socket
        temp_socket.close()

        return local_ip_address
    except socket.error as e:
        logging.warning(f"Failed to retrieve the local IP address: {e}")
        return None
    

def update_defaults(settings_in, settings_default):
    for key, value in settings_default.items():
        if key not in settings_in or settings_in[key] is None and value is not None:
            settings_in[key] = value
        elif not isinstance(settings_in[key], (dict, list)):
            if isinstance(value, dict) and not isinstance(settings_in[key], dict):
                settings_in[key] = {}
                update_defaults(settings_in[key], value)
            elif isinstance(value, list) and not isinstance(settings_in[key], list):
                settings_in[key] = []
                settings_in[key] = value
        elif isinstance(value, dict) and isinstance(settings_in[key], dict):
            update_defaults(settings_in[key], value)
            

class SSDPServer:

    def __init__(self, usn_uuid=None, server_ip="0.0.0.0", service_url="http://192.168.1.100:8000"):
        self.server_ip = server_ip
        self.server_port = 1900
        self.service_url = service_url
        self.ssdp_ip = "239.255.255.250"
        self.ssdp_port = 1900
        self.server_socket = None
        self.server_thread = None
        self.notify_interval = 30
        self.stop_event = threading.Event()
        self.logger = logging.getLogger("UPNP-SRV")
        self.usn_uuid = usn_uuid if usn_uuid is not None else str(uuid.uuid4())

    def get_notify_message(self, type_text="ssdp:alive"):
        notify_message = "NOTIFY * HTTP/1.1\r\n"
        notify_message += f"HOST: {self.ssdp_ip}:{self.ssdp_port}\r\n"
        notify_message += "CACHE-CONTROL: max-age=1800\r\n"
        notify_message += f"LOCATION: {self.service_url}\r\n"
        notify_message += f"NT: {SSDP_DEVICE_TYPE}\r\n"
        notify_message += f"NTS: {type_text}\r\n"
        notify_message += f"SERVER: {__app_title__}/{__version__} UPnP/1.0 {__app_title__}/{__version__}\r\n"
        notify_message += f"USN: uuid:{self.usn_uuid}::{SSDP_DEVICE_TYPE}\r\n"
        notify_message += "X-KENZY-SERVICE: core\r\n"
        notify_message += "\r\n"

        return notify_message

    def run_server(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("0.0.0.0", self.server_port))

        mcast_req = socket.inet_aton(self.ssdp_ip) + socket.inet_aton("0.0.0.0")
        self.server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mcast_req)

        self.logger.info(f"UPnP/SSDP server listening on udp://{self.server_ip}:{self.server_port}...")

        last_notify_time = 0

        while not self.stop_event.is_set():
            try:
                data, addr = self.server_socket.recvfrom(1024)

                if "M-SEARCH" in data.decode("utf-8"):
                    # Respond to M-SEARCH requests
                    headers = get_ssdp_headers(data)
                    if "ST" in headers and headers["ST"].lower() in ["ssdp:all", SSDP_DEVICE_TYPE.lower()]:
                        response = "HTTP/1.1 200 OK\r\n"
                        response += "CACHE-CONTROL: max-age=1800\r\n"
                        response += f"LOCATION: {self.service_url}\r\n"
                        response += f"SERVER: {__app_title__}/{__version__} UPnP/1.0 {__app_title__}/{__version__}\r\n"
                        response += f"ST: {SSDP_DEVICE_TYPE}\r\n"
                        response += f"USN: uuid:{self.usn_uuid}::{SSDP_DEVICE_TYPE}\r\n"
                        response += "X-KENZY-SERVICE: core\r\n"
                        response += "\r\n"

                        self.server_socket.sendto(response.encode("utf-8"), addr)

                current_time = time.time()
                if current_time - last_notify_time > self.notify_interval:
                    # Send periodic NOTIFY messages
                    notify_message = self.get_notify_message("ssdp:alive")
                    self.server_socket.sendto(notify_message.encode("utf-8"), (self.ssdp_ip, self.ssdp_port))
                    last_notify_time = current_time

            except (KeyboardInterrupt, OSError):
                pass

        # Send byebye message
        notify_message = self.get_notify_message("ssdp:byebye")
        self.server_socket.sendto(notify_message.encode("utf-8"), (self.ssdp_ip, self.ssdp_port))
        self.logger.debug("SSDP BYE message sent")

    def start(self):
        if self.server_thread is None:
            self.stop_event.clear()
            self.server_thread = threading.Thread(target=self.run_server)
            self.server_thread.start()
            self.logger.info("UPnP/SSDP server started.")
        else:
            self.logger.info("UPnP/SSDP server is already running.")

    def stop(self):
        if self.server_thread is not None:
            self.stop_event.set()
            try:
                self.server_socket.shutdown(socket.SHUT_RD)
            except OSError:
                pass
            self.server_thread.join()
            self.server_thread = None
            self.logger.info("UPnP/SSDP server stopped.")
        else:
            self.logger.info("UPnP/SSDP server is not currently running.")


def get_ssdp_headers(content):
    headers = {}
    for line in content.decode("utf-8").split("\n"):
        if ":" in line:
            item = line.split(":", 1)
            headers[str(item[0]).upper()] = str(item[1]).strip("\r ")

    return headers


def find_presentation_url(element):
    presentation_url = None

    for child in element:
        if child.tag == "{urn:schemas-upnp-org:device-1-0}presentationURL":
            presentation_url = child.text
            break
        else:
            presentation_url = find_presentation_url(child)
            if presentation_url:
                break

    return presentation_url


def parse_presentation_url(xml_data):
    root = ET.fromstring(xml_data)
    presentation_url = find_presentation_url(root)
    return presentation_url


def discover_ssdp_services(search_text=SSDP_DEVICE_TYPE):
    logger = logging.getLogger("SSDP-CLT")
    ssdp_ip = "239.255.255.250"
    ssdp_port = 1900

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_socket.settimeout(5)
    client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    client_socket.bind(("", ssdp_port))

    search_message = "M-SEARCH * HTTP/1.1\r\n"
    search_message += "HOST: {}:{}\r\n".format(ssdp_ip, ssdp_port)
    search_message += "MAN: \"ssdp:discover\"\r\n"
    search_message += "MX: 1\r\n"
    search_message += f"ST: {search_text}\r\n"
    search_message += "\r\n"

    client_socket.sendto(search_message.encode("utf-8"), (ssdp_ip, ssdp_port))

    logger.debug("Waiting for SSDP responses...")

    url = None
    while True:
        try:
            response, addr = client_socket.recvfrom(1024)
            if b"X-KENZY-SERVICE" in response:
                headers = get_ssdp_headers(response)
                loc = headers["LOCATION"]
                response = requests.get(loc)
                if response.status_code == 200:
                    p = parse_presentation_url(response.text)
                    if p is not None:
                        url = p
                        break
                else:
                    logger.error(f"Failed to retrieve webpage. Status code: {response.status_code}")
                    return None
        except socket.timeout:
            logger.debug("No more responses. Exiting...")
            break

    client_socket.close()
    if url is None:
        logger.critical("No UPNP Server identified via SSDP.")

    return url
