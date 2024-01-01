import socket
import logging
import os
import re
import mimetypes
import threading
import time
import uuid
import json
import tempfile
import zipfile
import collections
import requests
import xml.etree.ElementTree as ET
from . import __app_title__, __version__
import psutil

SKILLS_BASE_URL = "https://kenzy.ai/download/skills/"
SSDP_DEVICE_TYPE = "urn:schemas-upnp-org:device:Kenzy-Core:1"
ones = {
    0: '', 1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six',
    7: 'seven', 8: 'eight', 9: 'nine', 10: 'ten', 11: 'eleven', 12: 'twelve',
    13: 'thirteen', 14: 'fourteen', 15: 'fifteen', 16: 'sixteen',
    17: 'seventeen', 18: 'eighteen', 19: 'nineteen'}

tens = {
    2: 'twenty', 3: 'thirty', 4: 'forty', 5: 'fifty', 6: 'sixty',
    7: 'seventy', 8: 'eighty', 9: 'ninety'}

illions = {
    1: 'thousand', 2: 'million', 3: 'billion', 4: 'trillion', 5: 'quadrillion',
    6: 'quintillion', 7: 'sextillion', 8: 'septillion', 9: 'octillion',
    10: 'nonillion', 11: 'decillion'}


class GenericCommand:

    def __init__(self, action=None, context=None, url=None, **kwargs):
        self.pre_cmds = []
        self.post_cmds = []
        self.action = action
        self.payload = dict(kwargs)
        self.context = context
        self.url = url
        self.timeout = kwargs.get("timeout")
        self.wait = kwargs.get("wait")

    def set(self, name, value):
        self.payload[name] = value

    def to_json(self):
        return self.get()

    def get_url(self):
        return self.url

    def set_url(self, url=None):
        self.url = url
    
    def get(self, name=None):
        if name is not None:
            return self.payload.get(name)
        else:
            context = self.context.get() if self.context is not None and not isinstance(self.context, dict) else self.context
            return { "action": self.action, "payload": self.payload, "context": context }
        
    def pre(self, cmd=None):
        if cmd is not None:
            self.pre_cmds.append(cmd)
        
        return self.pre_cmds

    def post(self, cmd=None):
        if cmd is not None:
            self.post_cmds.append(cmd)

        return self.post_cmds
        
    def set_context(self, context, overwrite=False):
        self.context = context if self.context is None or overwrite else self.context

    def get_context(self):
        return self.context
        

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


def get_raw_value(setting_value):
    if not isinstance(setting_value, str):
        return setting_value

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


def apply_vars(cfg, vars):
    for item in vars:
        if "=" in item:
            setting_name = item.split("=", 1)[0]
            setting_value = item.split("=", 1)[1]

            setting_value = get_raw_value(setting_value)

            cfg[setting_name] = setting_value
        else:
            logging.critical("Invalid setting provided.  Must be in form: name=value")
            quit(1)


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


def discover_ssdp_services(search_text=SSDP_DEVICE_TYPE, timeout=45):
    logger = logging.getLogger("SSDP-CLT")
    ssdp_ip = "239.255.255.250"
    ssdp_port = 1900

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_socket.settimeout(timeout)
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


def strip_punctuation(text):
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$.'"
    text = "".join([str(x if x in allowed else " ") for x in [*text]]).replace("  ", " ").replace(". ", " ").strip(".")
    return text.strip()


def numbers_in_string(text):
    return [i for i in text.replace(":", " ").split() if (i.strip("$?!.:;").replace(",", "").replace(".", "").isdigit())]


def number_to_words(i):
    """
    Convert an integer in to it's word representation.

    say_number(i: integer) -> string
    """
    if i < 0:
        return _join('negative', _say_number_pos(-i))
    if i == 0:
        return 'zero'
    return _say_number_pos(i)


def _say_number_pos(i):
    if i < 20:
        return ones[i]
    if i < 100:
        return _join(tens[i // 10], ones[i % 10])
    if i < 1000:
        return _divide(i, 100, 'hundred')
    for illions_number, illions_name in illions.items():
        if i < 1000**(illions_number + 1):
            break
    return _divide(i, 1000**illions_number, illions_name)


def _divide(dividend, divisor, magnitude):
    return _join(
        _say_number_pos(dividend // divisor),
        magnitude,
        _say_number_pos(dividend % divisor),
    )


def _join(*args):
    return ' '.join(filter(bool, args))


def get_sys_info():
    sys_info = { "cpu": {}, "memory": { "virtual": {}, "swap": {} }, "disk": { "partitions": []}, "network": {} }

    sys_info["cpu"]["percent"] = psutil.cpu_percent(interval=1)
    sys_info["cpu"]["count"] = psutil.cpu_count()
    sys_info["cpu"]["count_physical"] = psutil.cpu_count(logical=False)
    sys_info["cpu"]["frequency"] = psutil.cpu_freq().max
    sys_info["cpu"]["load"] = psutil.getloadavg()

    vm = psutil.virtual_memory()
    sys_info["memory"]["virtual"]["total"] = vm.total
    sys_info["memory"]["virtual"]["available"] = vm.available
    sys_info["memory"]["virtual"]["used"] = vm.used
    sys_info["memory"]["virtual"]["free"] = vm.free
    sys_info["memory"]["virtual"]["percent"] = vm.percent

    sm = psutil.swap_memory()
    sys_info["memory"]["swap"]["total"] = sm.total
    sys_info["memory"]["swap"]["used"] = sm.used
    sys_info["memory"]["swap"]["free"] = sm.free
    sys_info["memory"]["swap"]["percent"] = sm.percent

    dp = psutil.disk_partitions(all=False)
    for part in dp:
        try:
            if part.device.startswith("/dev/") \
                    and not part.device.startswith("/dev/loop") \
                    and not part.mountpoint.startswith("/var/snap/") \
                    and not part.mountpoint.startswith("/boot/"):
                
                du = psutil.disk_usage(part.mountpoint)
                sys_info["disk"]["partitions"].append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "total": du.total,
                    "free": du.free,
                    "used": du.used,
                    "percent": du.percent
                })
        except PermissionError:
            pass

    dio = psutil.disk_io_counters(perdisk=False)
    sys_info["disk"]["io"] = {
        "read_bytes": dio.read_bytes,
        "read_count": dio.read_count,
        "read_time": dio.read_time,
        "write_bytes": dio.write_bytes,
        "write_count": dio.write_count,
        "write_time": dio.write_time
    }

    nio = psutil.net_io_counters(pernic=True)
    for nic in nio:
        sys_info["network"][nic] = {
            "bytes_recv": nio[nic].bytes_recv,
            "bytes_sent": nio[nic].bytes_sent,
            "packets_recv": nio[nic].packets_recv,
            "packets_sent": nio[nic].packets_sent,
            "dropin": nio[nic].dropin,
            "dropout": nio[nic].dropout,
            "errin": nio[nic].errin,
            "errout": nio[nic].errout
        }

    return sys_info


def get_status(obj):
    return {
        "active": obj.is_alive(),
        "type": obj.type,
        "accepts": obj.accepts,
        "location": obj.location,
        "group": obj.group,
        "version": obj.service.version,
        "info": get_sys_info(),
        "settings": obj.settings,
        "data": {}
    }


def get_skills_package(skill_name=None, skill_dir=None):
    if skill_dir is None:
        return False
    
    if skill_name is None:
        skill_pack = "skill-package-" + __version__ + ".zip"
    else:
        skill_pack = skill_name.strip().lower()

        if not skill_pack.endswith(".zip"):
            skill_pack = skill_pack + ".zip"

    skill_pack_url = SKILLS_BASE_URL + skill_pack
    
    resp = requests.get(skill_pack_url, allow_redirects=True)
    if resp.ok:
        file_name = os.path.join(tempfile.gettempdir(), skill_pack)
        
        with open(file_name, "wb") as sw:
            sw.write(resp.content)

        os.makedirs(os.path.expanduser(skill_dir), exist_ok=True)
        with zipfile.ZipFile(file_name) as zf:
            zf.extractall(os.path.expanduser(skill_dir))
        
        return True
    
    return False


class KenzyLogger:
    def __init__(self, name="root", maxlen=50):
        self.name = name
        self.entries = collections.deque(maxlen=maxlen)
        if name == "root":
            self.logger = logging.getLogger()
        else:
            self.logger = logging.getLogger(name)

    def _log(self, log_type, msg, *args):

        if len(args) > 0:
            if "{" in msg and "}" in msg:
                f_msg = msg.format(*args)
            else:
                return
        else:
            f_msg = msg

        self.entries.append({
            "timestamp": time.time(),
            "type": log_type,
            "message": f_msg
        })

    def debug(self, msg, *args):
        self._log("debug", msg, *args)
        self.logger.debug(msg, *args)

    def info(self, msg, *args):
        self._log("info", msg, *args)
        self.logger.info(msg, *args)

    def warning(self, msg, *args):
        self._log("warning", msg, *args)
        self.logger.info(msg, *args)

    def error(self, msg, *args):
        self._log("error", msg, *args)
        self.logger.info(msg, *args)

    def critical(self, msg, *args):
        self._log("critical", msg, *args)
        self.logger.info(msg, *args)