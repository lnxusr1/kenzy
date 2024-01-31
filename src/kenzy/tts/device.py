import os
import logging
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.tts.core import model_type, create_speech, play_wav_file
from kenzy.extras import number_to_words, numbers_in_string, get_status


class SpeakerDevice:
    type = "kenzy.tts"
    logger = logging.getLogger("KNZY-TTS")

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.service = None

        self.model_type = None
        self.speaker = None
        self.cache_folder = None 
        self._is_running = False

        self.location = kwargs.get("location", "Kenzy's Room")
        self.group = kwargs.get("group", "Kenzy's Group")

        self.initialize()

    def initialize(self):
        if self.settings.get("offline", False):
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["HF_DATASETS_OFFLINE"] = "1"

        self.model = model_type(
            self.settings.get("model.type", "speecht5"), 
            target=self.settings.get("model.target"), 
            offline=self.settings.get("offline", False)
        )
        
        self.speaker = self.settings.get("speaker", "slt")
        self.cache_folder = self.settings.get("cache.folder", "~/.kenzy/cache/speech")
        self.ext_prg = self.settings.get("external_player")

    @property
    def accepts(self):
        return ["start", "stop", "restart", "status", "get_settings", "set_settings", "speak", "play"]
    
    def is_alive(self, **kwargs):
        return self._is_running

    def play(self, **kwargs):
        # print(kwargs)
        if kwargs.get("data", {}).get("file_name") is not None:
            file_name = kwargs.get("data", {}).get("file_name")
            play_wav_file(file_name, ext_prg=self.ext_prg)
        return KenzySuccessResponse("Complete")

    def start(self, **kwargs):
        self._is_running = True
        return KenzySuccessResponse("Speaker started")
    
    def stop(self, **kwargs):
        self._is_running = False
        return KenzySuccessResponse("Speaker stopped")

    def restart(self, **kwargs):
        self._is_running = True
        return KenzySuccessResponse("Speaker restarted")

    def speak(self, **kwargs):
        if not self._is_running:
            return KenzyErrorResponse("Device is stopped.")
        
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
        create_speech(self.model, text, speaker=self.speaker, cache_folder=self.cache_folder, ext_prg=self.ext_prg)
        return KenzySuccessResponse("Complete")

    def get_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")
    
    def status(self, **kwargs):
        return KenzySuccessResponse(get_status(self))
    
    def set_service(self, service):
        self.service = service
