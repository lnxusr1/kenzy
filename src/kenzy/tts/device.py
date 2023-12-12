import logging
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.tts.core import model_type, create_speech


class SpeakerDevice:
    type = "kenzy.tts"

    location = None
    group = None
    service = None

    logger = logging.getLogger("KNZY-TTS")
    settings = {}

    model_type = None
    speaker = None
    cache_folder = None 

    def __init__(self, **kwargs):
        print(kwargs)
        self.settings = kwargs
        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

        self.initialize()

    def initialize(self):
        self.model = model_type(self.settings.get("model_type", "speecht5"), target=self.settings.get("model_target"))
        self.speaker = self.settings.get("speaker", "slt")
        self.cache_folder = self.settings.get("cache_folder", "~/.kenzy/cache/speech")

    @property
    def accepts(self):
        return ["start", "stop", "restart", "status", "get_settings", "set_settings", "speak"]
    
    def is_alive(self, **kwargs):
        return True
    
    def start(self, **kwargs):
        return KenzySuccessResponse("Speaker started")
    
    def stop(self, **kwargs):
        return KenzySuccessResponse("Speaker stopped")

    def restart(self, **kwargs):
        return KenzySuccessResponse("Speaker restarted")

    def speak(self, **kwargs):
        text = kwargs.get("data", {}).get("text")
        self.logger.debug(f"SPEAK: {text}")
        create_speech(self.model, text, speaker=self.speaker, cache_folder=self.cache_folder)
        return KenzySuccessResponse("Complete")

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
    
    def set_service(self, service):
        self.service = service
