"""
More info at Kenzy.Ai
"""

import sys
import os
import logging 
import traceback
from .templates import GenericContainer, GenericDevice

try:
    from .skillmanager import GenericSkill
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("GenericSkill not available due to missing libraries.")


__app_name__ = "kenzy"
__app_title__ = "KENZY.Ai"

with open(os.path.join(os.path.dirname(__file__), "VERSION"), "r", encoding="UTF-8") as fp:
    __version__ = fp.readline().strip()

VERSION = [(int(x) if x.isnumeric() else x) for x in __version__.split(".")]