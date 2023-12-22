import os
import logging
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.tts.core import model_type, create_speech
from kenzy.extras import number_to_words, numbers_in_string


class SpeakerDevice:
    type = "kenzy.tts"
    logger = logging.getLogger("KNZY-TTS")

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.service = None

        self.model_type = None
        self.speaker = None
        self.cache_folder = None 

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

        self.initialize()

    def initialize(self):
        if self.settings.get("offline"):
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_DATASETS_OFFLINE"] = "1"

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
        numbers = numbers_in_string(text)
        for num in numbers:

            n = num.strip("$?!.:;").replace(",", "")
            if "." in n:
                n = n.split(".")
                joiner = " point "
                if "$" in num:
                    joiner = " dollars and "
                words = joiner.join([number_to_words(int(t)) for t in n])
                if "$" in num:
                    words = words + " cents"
            else:
                words = number_to_words(int(n.strip("$")))
                if "$" in num:
                    words = words + " dollars"

            text = text.replace(num, words.replace("  ", " "), 1)
        self.logger.debug(f"SPEAK: {text.replace(':', '-')}")
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
