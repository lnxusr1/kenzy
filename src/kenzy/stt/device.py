import threading
import logging
import time
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse


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

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

    @property
    def accepts(self):
        return ["start", "stop", "restart", "status", "get_settings", "set_settings", "collect"]

    def _process_callback(self):
        while True:
            data = self.callback_queue.get()
            if data is None or not isinstance(data, list):
                break

            self.service.collect(data={
                "type": "kenzy.stt"
            })

    def _read_from_device(self):
        pass

    def is_alive(self, **kwargs):
        if self.main_thread is not None and self.main_thread.is_alive():
            return True
        
        return False
    
    def start(self, **kwargs):
        if self.main_thread is None or not self.main_thread.is_alive():
            self.logger.error("Video Processor is not running")
            return KenzyErrorResponse("Video Processor is not running")

        self.main_thread = threading.Thread(target=self._read_from_device, daemon=True)
        self.main_thread.start()

        if self.is_alive():
            self.logger.info("Started Video Processor")
            return KenzySuccessResponse("Started Video Processor")
        else:
            self.logger.error("Unable to start Video Processor")
            return KenzyErrorResponse("Unable to start Video Processor")
        
    def stop(self, **kwargs):
        if self.main_thread is None or not self.main_thread.is_alive():
            self.logger.error("Video Processor is not running")
            return KenzyErrorResponse("Video Processor is not running")
        
        self.record_event.clear()
        self.stop_event.set()
        self.main_thread.join()

        self.callback_queue.put(None)
        self.callback_thread.join()

        if not self.is_alive():
            self.logger.info("Stopped Video Processor")
            return KenzySuccessResponse("Stopped Video Processor")
        else:
            self.logger.error("Unable to stop Video Processor")
            return KenzyErrorResponse("Unable to stop Video Processor")
    
    def restart(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            ret = self.stop()
            if not ret.is_success():
                return ret
        
        return self.start()

    def set_service(self, service):
        self.service = service

    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")
    
    def status(self, **kwargs):
        return KenzySuccessResponse({
            "active": self.is_alive(),
            "type": self.type,
            "accepts": self.accepts,
            "data": {
            }
        })
