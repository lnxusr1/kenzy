import os
import threading
import logging
import sys
import traceback
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.stt.core import read_from_device
from kenzy.extras import get_status


class AudioProcessor:
    type = "kenzy.stt"
    logger = logging.getLogger("KNZY-STT")

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.service = None

        self.stop_event = threading.Event()
        self.main_thread = None
        self.callback_queue = None
        self.restart_enabled = False
        self.muted_event = threading.Event()

        self.location = kwargs.get("location", "Kenzy's Room")
        self.group = kwargs.get("group", "Kenzy's Group")

        self.audio_consumers = []
        self.stream = None

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

    def _process_callback(self, text):
        if text is None or not isinstance(text, str):
            return
        
        self.logger.info(f"HEARD: {text}")

        self.service.collect(data={
            "type": "kenzy.stt",
            "text": text
        }, wait=False, timeout=2)

    def _read_from_device(self):

        if self.settings.get("offline"):
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_DATASETS_OFFLINE"] = "1"

        audio_consumers=[]
        
        from kenzy.stt.consumers import SpeechModel
        sm = SpeechModel(**self.settings)
        sm.initialize(stop_event=self.stop_event, muted_event=self.muted_event, callback=self._process_callback, **self.settings)
        sm.start()
        audio_consumers.append(sm)

        try:
            self.logger.debug("Attempting to start stream...")
            self.stream = read_from_device(self.stop_event, muted_event=self.muted_event, audio_consumers=audio_consumers, **self.settings)
        except KeyboardInterrupt:
            self.stop()
        except Exception:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            self.logger.error("Unable to read from listener device.")
            self.stop_event.set()
            self.restart_enabled = True

        if self.stream is not None:
            self.logger.debug("STT is listening...")
        else:
            self.logger.error("Failed to start stream")


        import time
        while not self.stop_event.is_set():
            time.sleep(0.1)

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
        
        if (self.main_thread is not None and self.main_thread.is_alive()):

            self.stop()

        self.main_thread = threading.Thread(target=self._read_from_device, daemon=True)
        self.main_thread.start()

        if self.is_alive():
            self.logger.info("Started Audio Processor")
            return KenzySuccessResponse("Started Audio Processor")
        else:
            self.logger.error("Unable to start Audio Processor")
            return KenzyErrorResponse("Unable to start Audio Processor")
        
    def stop(self, **kwargs):
        if (self.main_thread is None or not self.main_thread.is_alive()):

            self.logger.error("Audio Processor is not running")
            return KenzyErrorResponse("Audio Processor is not running")
        
        self.stop_event.set()
        self.stream.close()

        if self.main_thread.is_alive():
            self.main_thread.join()

        if not self.is_alive():
            self.logger.info("Stopped Audio Processor")
            return KenzySuccessResponse("Stopped Audio Processor")
        else:
            self.logger.error("Unable to stop Audio Processor")
            return KenzyErrorResponse("Unable to stop Audio Processor")
    
    def restart(self, **kwargs):
        if (self.main_thread is not None and self.main_thread.is_alive()):
            
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
        return KenzySuccessResponse(get_status(self))
