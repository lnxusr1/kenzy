import sys
import logging 
import traceback

try:
    from kenzy.tts import device

    class device(device.SpeakerDevice):
        pass

except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to start speaker device due to missing libraries")