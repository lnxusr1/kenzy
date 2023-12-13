import threading
import logging
import queue
import sys
import traceback
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.stt.core import read_from_device


class AudioProcessor:
    type = "kenzy.stt"

    location = None
    group = None
    service = None

    logger = logging.getLogger("KNZY-STT")
    settings = {}
 
    stop_event = threading.Event()
    main_thread = None
    callback_thread = None
    callback_queue = None
    restart_enabled = False
    muted_event = threading.Event()

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

    @property
    def accepts(self):
        return ["start", "stop", "restart", "status", "get_settings", "set_settings", "mute", "unmute"]

    def mute(self, **kwargs):
        self.muted_event.set()
        self.logger.debug("Device is muted.")
        return KenzySuccessResponse("Mute command successful")

    def unmute(self, **kwargs):
        self.muted_event.clear()
        self.logger.debug("Device is unmuted.")
        return KenzySuccessResponse("Unmute command successful")

    def _process_callback(self):
        while True:
            text = self.callback_queue.get()
            if text is None or not isinstance(text, str):
                break

            self.service.collect(data={
                "type": "kenzy.stt",
                "text": text
            })

    def _read_from_device(self):

        try:
            for text in read_from_device(self.stop_event, muted_event=self.muted_event, **self.settings):
                self.logger.debug(f"HEARD: {text}")
                self.callback_queue.put(text)
        except KeyboardInterrupt:
            self.stop()
        except Exception:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            self.logger.error("Unable to read from listener device.")
            self.stop_event.set()
            self.restart_enabled = True

    def is_alive(self, **kwargs):
        if self.main_thread is not None and self.main_thread.is_alive():
            return True
        
        return False
    
    def start(self, **kwargs):
        self.restart_enabled = False
        self.muted_event.clear()

        if self.is_alive():
            self.logger.error("Audio Processor already running")
            return KenzyErrorResponse("Audio Processor already running")
        
        if (self.main_thread is not None and self.main_thread.is_alive()) \
                or (self.callback_thread is not None and self.callback_thread.is_alive()):

            self.stop()

        self.callback_queue = queue.Queue()
        self.callback_thread = threading.Thread(target=self._process_callback, daemon=True)
        self.callback_thread.start()

        self.main_thread = threading.Thread(target=self._read_from_device, daemon=True)
        self.main_thread.start()

        if self.is_alive():
            self.logger.info("Started Audio Processor")
            return KenzySuccessResponse("Started Audio Processor")
        else:
            self.logger.error("Unable to start Audio Processor")
            return KenzyErrorResponse("Unable to start Audio Processor")
        
    def stop(self, **kwargs):
        if (self.main_thread is None or not self.main_thread.is_alive()) \
                and (self.callback_thread is None or self.callback_thread.is_alive()):

            self.logger.error("Audio Processor is not running")
            return KenzyErrorResponse("Audio Processor is not running")
        
        self.stop_event.set()

        if self.main_thread.is_alive():
            self.main_thread.join()

        if self.callback_thread.is_alive():
            self.callback_queue.put(None)
            self.callback_thread.join()

        if not self.is_alive():
            self.logger.info("Stopped Audio Processor")
            return KenzySuccessResponse("Stopped Audio Processor")
        else:
            self.logger.error("Unable to stop Audio Processor")
            return KenzyErrorResponse("Unable to stop Audio Processor")
    
    def restart(self, **kwargs):
        if (self.main_thread is not None and self.main_thread.is_alive()) \
                or (self.callback_thread is not None and self.callback_thread.is_alive()):
            
            ret = self.stop()
            if not ret.is_success():
                return ret
        
        return self.start()

    def set_service(self, service):
        self.service = service

    def get_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")
    
    def status(self, **kwargs):
        return KenzySuccessResponse({
            "active": self.is_alive(),
            "type": self.type,
            "accepts": self.accepts,
            "location": self.location,
            "group": self.group,
            "data": {
            }
        })
