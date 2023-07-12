import sys
import logging 
import traceback

try:
    from kenzy.image import core

    class detector(core.detector):
        pass
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to start analyzer due to missing libraries")