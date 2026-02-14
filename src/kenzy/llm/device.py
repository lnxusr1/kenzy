import os
import logging
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse, KenzyContext
from kenzy.extras import number_to_words, numbers_in_string, get_status


class LLMDevice:
    type = "kenzy.llm"
    logger = logging.getLogger("KNZY-LLM")

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.service = None
        self._is_running = False

        self.location = kwargs.get("location", "Kenzy's Room")
        self.group = kwargs.get("group", "Kenzy's Group")

        self.initialize()

    def initialize(self):
        pass

    @property
    def accepts(self):
        return ["start", "stop", "restart", "status", "get_settings", "set_settings", "fallback"]
    
    def is_alive(self, **kwargs):
        return self._is_running

    def start(self, **kwargs):
        self._is_running = True
        return KenzySuccessResponse("LLM started")
    
    def stop(self, **kwargs):
        self._is_running = False
        return KenzySuccessResponse("LLM stopped")

    def restart(self, **kwargs):
        self._is_running = True
        return KenzySuccessResponse("LLM restarted")

    def fallback(self, data=None, **kwargs):
        if not self._is_running:
            return KenzyErrorResponse("Device is stopped.")

        caller_context = kwargs.get("context")

        context = KenzyContext()
        context.load(data.get("context", {}))

        #TODO: DO SOMETHING HERE
        self.logger.info("COMMAND RECEIVED")
        
        in_text = data.get("text")
        raw_text = data.get("raw")

        self.logger.debug(f"TEXT: {in_text}")
        self.logger.debug(f"RAW: {raw_text}")
        self.logger.debug(f"ROOT: {context.get()}")
        self.logger.debug(f"CALLER: {caller_context.get()}")

        return KenzySuccessResponse("Complete")

    def get_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")

    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")
    
    def status(self, **kwargs):
        return KenzySuccessResponse(get_status(self))
    
    def set_service(self, service):
        self.service = service
