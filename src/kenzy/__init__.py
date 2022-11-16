"""
More info at Kenzy.Ai
"""

import sys
import logging 
import traceback
from .templates import GenericContainer, GenericDevice

try:
    from .skillmanager import GenericSkill
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("GenericSkill not available due to missing libraries.")


VERSION = (0, 9, 1)

__app_name__ = "kenzy"
__app_title__ = "KENZY.Ai"
__version__ = ".".join([str(x) for x in VERSION])
