"""
Kenzy: __main__
"""

import logging
import sys
import os
import argparse
import json
import traceback
from . import __app_name__, __version__


def _getImport(libs, val):
    """
    Dynamically imports a library into memory for referencing during configuration file parsing.

    Args:
        libs (list):  The list of libraries already imported.
        val (str):  The name of the new library to import.

    Returns:
        (str): The name of the newly imported library.  If library is already imported then returns None.
    """

    if val is not None and isinstance(val, str) and val.strip() != "":
        if "." in val:
            parts = val.split(".")
            parts.pop()
            ret = ".".join(parts)
            if ret in libs:
                return None

            libs.append(ret)
            return ret

    return None


parser = argparse.ArgumentParser(
    description=__app_name__ + " v" + __version__,
    formatter_class=argparse.RawTextHelpFormatter,
    epilog='''To start the services try:\npython3 -m kenzy\n\nMore information available at:\nhttp://kenzy.ai''')
# parser.add_argument('--locale', default="en_us", help="Language Locale")

parser.add_argument('-c', '--config', default=None, help="Configuration file")
parser.add_argument('-v', '--version', action="store_true", help="Print Version")

startup_group = parser.add_argument_group('Startup Options')

startup_group.add_argument('--disable-builtin-speaker', action="store_true", help="Disable the built-in Speaker device")
startup_group.add_argument('--disable-builtin-listener', action="store_true", help="Disable the built-in Listener device")
startup_group.add_argument('--disable-builtin-watcher', action="store_true", help="Disable the built-in Watcher device")
startup_group.add_argument('--disable-builtin-panels', action="store_true", help="Disable the built-in Panel devices")
startup_group.add_argument('--disable-builtin-brain', action="store_true", help="Disable the built-in Brain container")
startup_group.add_argument('--disable-builtin-container', action="store_true", help="Disable the built-in Device container")

watcher_group = parser.add_argument_group('Watcher Options')

watcher_group.add_argument('--training-source-folder', default=None, help="Specify the path to the image input directory structure")
watcher_group.add_argument('--force-train', action="store_true", help="Force the recognition profile to be retrained")

listener_group = parser.add_argument_group('STT Options')

listener_group.add_argument('--download-models', action="store_true", help="Download listener models")
listener_group.add_argument('--model-version', default=None, help="Coqui STT Model Version")
listener_group.add_argument('--model-type', default="tflite", help="Coqui STT Model Type as pbmm or tflite")
listener_group.add_argument('--include-scorer', action="store_true", help="Include scorer model")
listener_group.add_argument('--overwrite', action="store_true", help="Overwrite models")

logging_group = parser.add_argument_group('Logging Options')

logging_group.add_argument('--log-level', default="info", help="Options are full, debug, info, warning, error, and critical")
logging_group.add_argument('--log-file', default=None, help="Redirects all logging messages to the specified file")

ARGS = parser.parse_args()

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

logger = logging.getLogger("STARTUP")

# Hide some debug level logging to cut down on noise
if ARGS.log_level is not None and ARGS.log_level.strip().lower() == "debug":
    logging.getLogger("UPNP-SRV").setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.INFO)

if ARGS.version:
    print(__app_name__, "v" + __version__)
    quit()

if ARGS.download_models:
    try:
        from .extras import download_models
        if download_models(version=ARGS.model_version, model_type=ARGS.model_type, include_scorer=ARGS.include_scorer, overwrite=ARGS.overwrite):
            print("Models downloaded successfully.")
            quit()
    except Exception:
        print("Error downloading models.")
        raise

# Overly simplified default configuration
cfg = {
    "appVersion": __version__,
    "settings": {
        "modulesFolder": None
    },
    "containers": [
        {
            'module': 'kenzy.containers.Brain',
            'autoStart': True,
            'settings': {
                'tcpPort': 8080
            }
        },
        {
            "module": 'kenzy.containers.DeviceContainer',
            'autoStart': True,
            'settings': {
                'tcpPort': 8081
            },
            'devices': [
                {
                    'autoStart': True,
                    'module': 'kenzy.devices.Listener'
                },
                {
                    'autoStart': True,
                    'module': 'kenzy.devices.Speaker'
                },
                {
                    'autoStart': True,
                    'module': 'kenzy.devices.Watcher'
                },
                {
                    'autoStart': True,
                    'module': 'kenzy.devices.KasaPlug'
                },
                {
                    'autoStart': True,
                    'isPanel': True,
                    'module': 'kenzy.panels.RaspiPanel'
                }
            ]
        }
    ]
}

configFile = ARGS.config
if configFile is None:
    configFile = os.path.join(os.path.expanduser("~/.kenzy"), 'config.json')
    if not os.path.isfile(configFile):
        try:
            # If the config doesn't exist then try to create it.
            os.makedirs(os.path.dirname(configFile), exist_ok=True)
            with open(configFile, "w") as fp:
                json.dump(cfg, fp, indent=4)
        except Exception:
            pass

else:
    configFile = os.path.abspath(ARGS.config)
    if not os.path.isfile(configFile):
        raise Exception("Configuration file does not exist.")
        quit(1)

with open(configFile, "r") as fp:
    cfg = json.load(fp)

hasPanel = False
try:
    for c_idx, container in enumerate(cfg["containers"]):
        if (ARGS.disable_builtin_brain and container["module"] == "kenzy.containers.Brain") \
                or (ARGS.disable_builtin_container and container["module"] == "kenzy.containers.DeviceContainer"):
            
            cfg["containers"][c_idx] = None
            continue

        if "devices" in container:
            for d_idx, device in enumerate(container["devices"]):
                if (ARGS.disable_builtin_speaker and device["module"] == "kenzy.devices.Speaker") \
                        or (ARGS.disable_builtin_watcher and device["module"] == "kenzy.devices.Watcher") \
                        or (ARGS.disable_builtin_listener and device["module"] == "kenzy.devices.Listener") \
                        or (ARGS.disable_builtin_panels and device["module"].startswith("kenzy.panels")):

                    cfg["containers"][c_idx]["devices"][d_idx] = None
                    continue 

                if device["module"] == "kenzy.devices.Watcher":
                    if ARGS.training_source_folder is not None:
                        cfg["containers"][c_idx]["devices"][d_idx]["trainingSourceFolder"] = ARGS.training_source_folder

                    if ARGS.force_train:
                        recognizerFile = None
                        namesFile = None
                        hPath = os.path.join(os.path.expanduser("~/.kenzy"), "data", "models", "watcher")

                        if "parameters" in device and "recognizerFile" in device["parameters"]:
                            recognizerFile = device["parameters"]["recognizerFile"] 

                        if recognizerFile is None:
                            recognizerFile = os.path.abspath(os.path.join(hPath, "recognizer.yml"))

                        if "parameters" in device and "namesFile" in device["parameters"]:
                            namesFile = device["parameters"]["namesFile"]

                        if namesFile is None:
                            namesFile = os.path.abspath(os.path.join(hPath, "names.json"))

                        if os.path.exists(recognizerFile):
                            os.unlink(recognizerFile)

                        if os.path.exists(namesFile):
                            os.unlink(namesFile)

                if "isPanel" in device and bool(device["isPanel"]):
                    hasPanel = True

except Exception:
    print("Invalid configuration provided.")
    raise

app = None
if hasPanel:
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication(sys.argv)
    except ModuleNotFoundError:
        pass

try:
    modulesFolder = cfg["settings"]["modulesFolder"]
    if modulesFolder is not None:
        if os.path.exists(str(modulesFolder).strip()):
            sys.path.insert(0, str(modulesFolder).strip())
except KeyError:
    pass

libs = []
objs = []
for container in cfg["containers"]:
    if container is None:
        continue 

    if "module" not in container or container["module"] is None:
        raise Exception("Value for module not defined for container.")

    try:
        m = _getImport(libs, str(container["module"]).strip())
        if m is not None:
            exec("import " + m.strip())

        setting_args = container["settings"] if "settings" in container and isinstance(container["settings"], dict) else {}
        obj = eval(str(container["module"]).strip() + "(**setting_args)")
        obj.app = app

        init_args = container["initialize"] if "initialize" in container and isinstance(container["initialize"], dict) else {}
        exec("obj.initialize(**init_args)")

        if "autoStart" not in container or not isinstance(container["autoStart"], bool) or bool(container["autoStart"]):
            obj.start()

        objs.append(obj)

        # Load up devices
        if "devices" in container and isinstance(container["devices"], list):
            for device_config in container["devices"]:
                try:
                    if device_config is None or "module" not in device_config: 
                        continue

                    isPanel = bool(device_config["isPanel"]) if "isPanel" in device_config and device_config["isPanel"] is not None else False
                    if isPanel and app is None:
                        raise Exception("QtApplication unavailable.  Unable to start panel.")

                    m = _getImport(libs, str(device_config["module"]).strip())
                    if m is not None:
                        exec("import " + m.strip())
                
                    setting_args = device_config["parameters"] if "parameters" in device_config and isinstance(device_config["parameters"], dict) else {}
                    autoStart = bool(device_config["autoStart"]) if "autoStart" in device_config and device_config["autoStart"] is not None else True
                    groupName = str(device_config["groupName"]) if "groupName" in device_config and device_config["groupName"] is not None else None
                    
                    devId = str(device_config["uuid"]) if "uuid" in device_config and device_config["uuid"] is not None else None

                    dev = eval(str(device_config["module"]).strip() + "(callback=obj.callbackHandler, **setting_args)")
                    obj.addDevice(device_config["module"], dev, id=devId, autoStart=autoStart, isPanel=isPanel, groupName=groupName)
                except Exception:
                    logging.debug(str(sys.exc_info()[0]))
                    logging.debug(str(traceback.format_exc()))
                    logger.error("Unable to start device: " + str(device_config["module"]).strip())
                    
    except Exception:
        logging.debug(str(sys.exc_info()[0]))
        logging.debug(str(traceback.format_exc()))
        logger.error("Unable to start container: " + str(container["module"]).strip())
        raise

doRestart = False
for obj in reversed(objs):
    obj.wait()
    if obj._doRestart:
        doRestart = True

if doRestart:
    if sys.argv[0].endswith("kenzy/__main__.py"):
        cmd = sys.executable + " -m kenzy " + " ".join(sys.argv[1:])
    else:
        cmd = sys.executable + " " + " ".join(sys.argv)

    logger = logging.getLogger("RESTART")

    import time
    logger.info("Waiting for processes to close")
    time.sleep(5)
    logger.info("Restarting")

    myEnv = dict(os.environ)

    if "QT_QPA_PLATFORM_PLUGIN_PATH" in myEnv:
        del myEnv["QT_QPA_PLATFORM_PLUGIN_PATH"]

    import subprocess
    subprocess.Popen(
        cmd,
        shell=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
        stdin=sys.stdin,
        env=myEnv)