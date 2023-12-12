import logging
import argparse
import os
import threading
import time
import kenzy.core
from kenzy.extras import clean_string, apply_vars, get_raw_value
from . import __app_title__, __version__
from . import settings

global service
services = []


def startup(cfg):
    app_type = str(clean_string(cfg.get("type"))).replace("..", ".").replace("/", "").replace("\\", "").replace("-", "_")

    if app_type not in ["kenzy.core"]:
        exec(f"import {app_type}")

    device = eval(f"{app_type}.device(**cfg.get('device', dict()))")
    service = kenzy.core.KenzyHTTPServer(**cfg.get('service', dict()))

    # Interlinking objects
    device.set_service(service)      # Tell device about service wrapper

    service.set_device(device)       # Add device to service
    services.insert(0, service)

    device.start()

    try:
        service.serve_forever()
    except KeyboardInterrupt:
        service.shutdown()


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

if str(cfg.get("type", "")).lower() in ["multi", "multiple", "many"]:
    # Multiple
    thread_pool = []

    last_port = 0

    defaults = cfg.get("default", {})
    defaults["device"] = defaults.get("device", {})
    defaults["service"] = defaults.get("server", {})

    for grp in cfg:
        if str(grp).lower() in ["type", "default"]:
            continue

        cfg[grp]["device"] = cfg[grp].get("device", {})
        cfg[grp]["service"] = cfg[grp].get("service", {})

        port = get_raw_value(cfg[grp].get("service", {}).get("port", defaults["service"].get("port", "9700")))

        for item in defaults.get("device", {}):
            cfg[grp]["device"][item] = cfg[grp].get("device", {}).get(item, defaults.get("device", {}).get(item))
        
        for item in defaults.get("service", {}):
            cfg[grp]["service"][item] = cfg[grp].get("service", {}).get(item, defaults.get("service", {}).get(item))

        grp_type = get_raw_value(cfg[grp].get("type", "kenzy.skillmanager"))
        if grp_type == "kenzy.skillmanager":
            cfg[grp]["service"]["upnp"] = cfg[grp].get("service", {}).get("upnp", "server")
            print(cfg[grp]["service"]["upnp"])

        if port <= last_port:
            port = last_port + 1

        last_port = port
        if "service" not in cfg[grp]:
            cfg[grp]["service"] = {}

        cfg[grp]["service"]["port"] = port

        t = threading.Thread(target=startup, args=[cfg.get(grp)], daemon=True)
        t.start()
        thread_pool.append(t)

        if grp_type == "kenzy.skillmanager":
            # Let the main server get fully online first
            time.sleep(1)

    while True:
        alive = False
        try:
            for item in thread_pool:
                item.join()
        except KeyboardInterrupt:
            for item in services:
                item.shutdown()

        for item in thread_pool:
            if item.is_alive():
                alive = True

        if not alive:
            break

        time.sleep(.5)

else:

    # Single
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

    if ARGS.set_device is not None:
        if "device" not in cfg:
            cfg["device"] = {}
        
        if isinstance(ARGS.set_device, list):
            apply_vars(cfg["device"], ARGS.set_device)

    if ARGS.set_service is not None:
        if "service" not in cfg:
            cfg["service"] = {}
        
        if isinstance(ARGS.set_service, list):
            apply_vars(cfg["service"], ARGS.set_device)

    startup(cfg)