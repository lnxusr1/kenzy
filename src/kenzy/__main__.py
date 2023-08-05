import logging
import argparse
import os
import kenzy.core
from kenzy.extras import clean_string
from . import __app_title__, __version__
from . import settings


parser = argparse.ArgumentParser(
    description=__app_title__ + " " + __version__,
    formatter_class=argparse.RawTextHelpFormatter,
    epilog='''For more information visit:\nhttp://kenzy.ai''')

parser.add_argument('-c', '--config', default=None, help="Configuration file")
parser.add_argument('-v', '--version', action="store_true", help="Print Version")
parser.add_argument('-d', '--set-device', action="append", help="Override settings as: name=value")
parser.add_argument('-s', '--set-service', action="append", help="Override settings as: name=value")
parser.add_argument('--offline', action="store_true", help="Run in offline mode.")

device_options = parser.add_argument_group('Device Options')
device_options.add_argument('-t', '--type', default=None, help="Specify instance type (override config value if set)")

service_options = parser.add_argument_group('Service Options')
service_options.add_argument('--upnp', default=None, help="Enable UPNP as server, client, or leave blank to disable")

logging_group = parser.add_argument_group('Logging Options')

logging_group.add_argument('--log-level', default="info", help="Options are full, debug, info, warning, error, and critical")
logging_group.add_argument('--log-file', default=None, help="Redirects all logging messages to the specified file")

ARGS = parser.parse_args()

# VERSION 
if ARGS.version:
    print(__app_title__, __version__)
    quit()

# LOG LEVEL

logLevel = logging.INFO
if ARGS.log_level is not None and ARGS.log_level.strip().lower() in ["debug", "info", "warning", "error", "critical"]:
    logLevel = eval("logging." + ARGS.log_level.strip().upper())
elif ARGS.log_level is not None and ARGS.log_level.strip().lower() == "full":
    logLevel = logging.DEBUG

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    datefmt='%Y-%m-%d %H:%M:%S %z',
    filename=ARGS.log_file,
    format='%(asctime)s %(name)-12s - %(levelname)-9s - %(message)s',
    level=logLevel)

# OFFLINE MODE
if ARGS.offline:
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"

# INSTANCE START

logger = logging.getLogger("STARTUP")
cfg = settings.load(ARGS.config)

if ARGS.type is not None:
    cfg["type"] = ARGS.type

if ARGS.upnp is not None:
    if str(ARGS.upnp).lower() in ["server", "client", "standalone"]:
        if not isinstance(cfg.get("service"), dict):
            cfg["service"] = {}
        cfg["service"]["upnp"] = str(ARGS.upnp).lower()
    else:
        logger.critical("Unable to start.  Invalid UPNP value specified.  Must be one of server, client, or standalone.")
        quit(1)

if cfg.get("type") is None:
    logger.critical("Unable to identify instance type. (Use --type to dynamically specify)")
    quit(1)

app_type = str(clean_string(cfg.get("type"))).replace("..", ".").replace("/", "").replace("\\", "").replace("-", "_")

if app_type not in ["kenzy.core"]:
    exec(f"import {app_type}")

if ARGS.set_device is not None:
    if "device" not in cfg:
        cfg["device"] = {}
    
    if isinstance(ARGS.set_device, list):
        for item in ARGS.set_device:
            if "=" in item:
                parent_type = "device"
                setting_name = item.split("=", 1)[0]
                setting_value = item.split("=", 1)[1]

                if "." in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
                    setting_value = float(setting_value.replace(",", ""))
                elif "." not in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
                    setting_value = int(setting_value.replace(",", ""))
                elif setting_value.lower().strip() in ["true", "false"]:
                    setting_value = bool(setting_value.lower().strip())
                elif setting_value.startswith("{") and setting_value.endswith("}"):
                    setting_value = dict(setting_value)
                elif setting_value.startswith("[") and setting_value.endswith("]"):
                    setting_value = list(setting_value)

                cfg[parent_type][setting_name] = setting_value
            else:
                logging.critical("Invalid setting provided.  Must be in form: name=value")
                quit(1)

if ARGS.set_service is not None:
    if "service" not in cfg:
        cfg["service"] = {}
    
    if isinstance(ARGS.set_service, list):
        for item in ARGS.set_service:
            if "=" in item:
                parent_type = "service"
                setting_name = item.split("=", 1)[0]
                setting_value = item.split("=", 1)[1]

                if "." in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
                    setting_value = float(setting_value.replace(",", ""))
                elif "." not in setting_value and setting_value.replace(",", "").replace(".", "").isdigit():
                    setting_value = int(setting_value.replace(",", ""))
                elif setting_value.lower().strip() in ["true", "false"]:
                    setting_value = bool(setting_value.lower().strip())
                elif setting_value.startswith("{") and setting_value.endswith("}"):
                    setting_value = dict(setting_value)
                elif setting_value.startswith("[") and setting_value.endswith("]"):
                    setting_value = list(setting_value)

                cfg[parent_type][setting_name] = setting_value
            else:
                logging.critical("Invalid setting provided.  Must be in form: name=value")
                quit(1)

device = eval(f"{app_type}.device(**cfg.get('device', dict()))")
service = kenzy.core.KenzyHTTPServer(**cfg.get('service', dict()))

# Interlinking objects
device.set_service(service)      # Tell device about service wrapper
service.set_device(device)       # Add device to service

try:
    service.serve_forever()
except KeyboardInterrupt:
    service.shutdown()
