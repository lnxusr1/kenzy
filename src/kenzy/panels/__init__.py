import logging
import sys
import traceback

try:
    from .example_panel.panel import PanelApp as RaspiPanel
except Exception:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("RaspiPanel disabled due to missing libraries.")