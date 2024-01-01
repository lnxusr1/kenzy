import sys
import logging 
import traceback

try:
    from kenzy.stt import device

    class device(device.AudioProcessor):
        pass

except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to start audio processor due to missing libraries")