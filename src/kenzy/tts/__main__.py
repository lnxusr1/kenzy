from kenzy import __app_title__, __version__
import argparse
import logging
import os
from kenzy.extras import apply_vars
from kenzy.tts.core import model_type, create_speech


parser = argparse.ArgumentParser(
    description=__app_title__ + " " + __version__,
    formatter_class=argparse.RawTextHelpFormatter,
    epilog='''For more information visit:\nhttp://kenzy.ai''')

parser.add_argument('-v', '--version', action="store_true", help="Print Version")
parser.add_argument('-t', '--text', default=None, help="Text to Speak")
parser.add_argument('-s', '--set', action="append", help="Override settings as: name=value")
parser.add_argument('--offline', action="store_true", help="Run in offline mode.")

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

# OFFLINE
if ARGS.offline:
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"

# CORE FUNCTION

cfg = { }

if ARGS.set is not None:
    if isinstance(ARGS.set, list):
        apply_vars(cfg, ARGS.set)

m = model_type(cfg.get("model.type", "speecht5"), target=cfg.get("model.target", "cpu"))
create_speech(m, ARGS.text, speaker=cfg.get("speaker", "slt"))