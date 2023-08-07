import os
import logging
import sys
import traceback

__app_name__ = "kenzy"
__app_title__ = "KENZY"

with open(os.path.join(os.path.dirname(__file__), "VERSION"), "r", encoding="UTF-8") as fp:
    __version__ = fp.readline().strip()

VERSION = [(int(x) if x.isnumeric() else x) for x in __version__.split(".")]

try:
    from kenzy.skillmanager.core import GenericSkill
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to start speaker device due to missing libraries")