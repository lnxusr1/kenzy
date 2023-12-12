import logging
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.skillmanager.core import SkillManager, SpeakCommand


class SkillsDevice:
    type = "kenzy.skillmanager"

    location = None
    group = None
    service = None

    skill_manager = None

    logger = logging.getLogger("KNZY-SKM")
    settings = {}

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

        self.initialize()

    def initialize(self):
        skill_folder = self.settings.get("folder", "~/.kenzy/skills")
        temp_folder = self.settings.get("temp_folder", "/tmp/intent_cache")
        self.skill_manager = SkillManager(device=self, skill_folder=skill_folder, temp_folder=temp_folder)
        self.skill_manager.initialize()

    @property
    def accepts(self):
        return ["status", "get_settings", "set_settings", "collect"]
    
    def is_alive(self, **kwargs):
        return True
    
    def collect(self, **kwargs):
        # print(kwargs)
        data = kwargs.get("data", {})
        context = kwargs.get("context")

        print(data)
        if data.get("type", "") == "kenzy.stt":
            text = data.get("text")
            self.skill_manager.parse(text=text, context=context)

        return KenzySuccessResponse("Collect complete")
    
    def start(self, **kwargs):
        return KenzySuccessResponse("Skill Manager started")
    
    def stop(self, **kwargs):
        return KenzySuccessResponse("Skill Manager stopped")

    def restart(self, **kwargs):
        return KenzySuccessResponse("Skill Manager restarted")

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
        self.skill_manager.service = service

    def say(self, text, **kwargs):
        # text, context=None
        cmd = SpeakCommand(context=kwargs.get("context"))
        cmd.text(text)

        self.service.send_request(payload=cmd)

        return KenzySuccessResponse("Say command complete")
    
    def ask(self, text, **kwargs):
        # text, in_callback, timeout=0, context=None
        cmd = SpeakCommand(context=kwargs.get("context"))
        cmd.text(text)
        
        # use context = location (door), group (living room), all
        self.service.send_request(payload=cmd)

        return KenzySuccessResponse("Ask command complete")
