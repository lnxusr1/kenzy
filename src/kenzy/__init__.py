import logging
import sys
import traceback

VERSION = (2, 1, 2)

__app_name__ = "kenzy"
__app_title__ = "KENZY"
__version__ = ".".join([str(x) for x in VERSION])

try:
    from kenzy.skillmanager.core import GenericSkill
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to start speaker device due to missing libraries")