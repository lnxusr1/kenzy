import sys
import logging 
import traceback

try:
    from kenzy.llm import device

    class device(device.LLMDevice):
        pass

except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("Unable to LLM device due to missing libraries")