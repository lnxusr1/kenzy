import sys
import logging 
import traceback

try:
    from kenzy.skillmanager import device

    class device(device.SkillsDevice):
        pass

except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to start skill manager due to missing libraries")