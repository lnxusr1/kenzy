import logging
import traceback
import sys
from .speaker import Speaker

try:
    from .listener import Listener
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Listener disabled due to missing libraries.")

try:
    from .watcher import Watcher
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Watcher disabled due to missing libraries.")

try:
    from .kasaplug import KasaPlug
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("KasaPlug disabled due to missing libraries.")