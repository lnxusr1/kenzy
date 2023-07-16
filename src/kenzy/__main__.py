import logging
import argparse
import kenzy.core
from kenzy.extras import clean_string
from . import __app_name__, __version__
from . import settings


parser = argparse.ArgumentParser(
    description=__app_name__ + " v" + __version__,
    formatter_class=argparse.RawTextHelpFormatter,
    epilog='''For more information visit:\nhttp://kenzy.ai''')

parser.add_argument('-c', '--config', default=None, help="Configuration file")
parser.add_argument('-v', '--version', action="store_true", help="Print Version")

startup_group = parser.add_argument_group('Startup Options')
parser.add_argument('-t', '--type', default=None, help="Specify instance type (override config value if set)")
parser.add_argument('--upnp', default=None, help="Enable UPNP as server, client, or leave blank to disable")

logging_group = parser.add_argument_group('Logging Options')

logging_group.add_argument('--log-level', default="info", help="Options are full, debug, info, warning, error, and critical")
logging_group.add_argument('--log-file', default=None, help="Redirects all logging messages to the specified file")

ARGS = parser.parse_args()

# VERSION 
if ARGS.version:
    print(__app_name__, "v" + __version__)
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

logging.getLogger("DETECT_TIME").setLevel(logging.INFO)

# INSTANCE START

logger = logging.getLogger("STARTUP")
cfg = settings.load(ARGS.config)

if ARGS.type is not None:
    cfg["type"] = ARGS.type

if ARGS.upnp is not None and str(ARGS.upnp).lower() in ["server", "client", "standalone"]:
    if not isinstance(cfg.get("service"), dict):
        cfg["service"] = {}
    cfg["service"]["upnp"] = str(ARGS.upnp).lower()

if cfg.get("type") is None:
    logger.critical("Unable to identify instance type. (Use --type to dynamically specify)")
    quit(1)

app_type = str(clean_string(cfg.get("type"))).replace("..", ".").replace("/", "").replace("\\", "").replace("-", "_")

if app_type not in ["kenzy.core"]:
    exec(f"import {app_type}")

component = eval(f"{app_type}.component(**cfg.get('component', dict()))")
device = eval(f"{app_type}.device(**cfg.get('device', dict()))")
service = kenzy.core.KenzyHTTPServer(**cfg.get('service', dict()))

# Interlinking objects
device.set_component(component)  # Add component to device
device.set_service(service)      # Tell device about service wrapper
service.set_device(device)       # Add device to service

try:
    service.serve_forever()
except KeyboardInterrupt:
    service.shutdown()
