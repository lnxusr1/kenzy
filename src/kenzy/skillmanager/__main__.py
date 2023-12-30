import os
import argparse
import logging
from kenzy import __app_title__, __version__
from kenzy.extras import get_skills_package


parser = argparse.ArgumentParser(
    description=__app_title__ + " " + __version__,
    formatter_class=argparse.RawTextHelpFormatter,
    epilog='''For more information visit:\nhttp://kenzy.ai''')

parser.add_argument('-v', '--version', action="store_true", help="Print Version")
parser.add_argument('--skill-dir', default="~/.kenzy/skills", help="Local skill folder")
parser.add_argument('--get-skills', action="store_true", help="Download skills")
parser.add_argument('--get-skill', action="append", help="Download skill by name")

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

skill_folder = os.path.expanduser(ARGS.skill_dir)

if ARGS.get_skills:
    logging.info("Downloading skills package.")
    if not get_skills_package(skill_name=None, skill_dir=skill_folder):
        logging.error("Unable to download skill package.")
    else:
        logging.info("Download complete.")

if isinstance(ARGS.get_skill, list):
    for skill_name in ARGS.get_skill:
        logging.info(f"Downloading {skill_name} package.")
        if not get_skills_package(skill_name=skill_name, skill_dir=skill_folder):
            logging.error(f"Unable to download {skill_name} package.")
        else:
            logging.info("Download complete.")