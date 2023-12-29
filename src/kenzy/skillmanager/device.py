import logging
import time
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse, KenzyContext
from kenzy.skillmanager.core import SkillManager, SpeakCommand, PlayCommand, GenericSkill


class SkillsDevice:
    type = "kenzy.skillmanager"
    logger = logging.getLogger("KNZY-SKM")

    def __init__(self, **kwargs):
        if not isinstance(kwargs.get("timeout", {}), dict):
            kwargs["timeout"] = {}

        self.settings = kwargs
        self.service = None
        self.skill_manager = None

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")
        self.wake_words = kwargs.get("wake_words", ["Kenzie", "Kenzi", "Kenzy", "Kinsay", "Kinsy", "Kinsie", "Kinsey"])

        try:
            self.activation_timeout = abs(float(kwargs.get("timeout.wake")))
        except TypeError:
            self.activation_timeout = 45
        
        try:
            self.ask_timeout = abs(float(kwargs.get("timeout.ask")))
        except TypeError:
            self.ask_timeout = 10

        self.timeouts = {}

        self.initialize()

    def initialize(self):
        skill_folder = self.settings.get("folder.skills", "~/.kenzy/skills")
        temp_folder = self.settings.get("folder.temp", "/tmp/intent_cache")
        
        self.skill_manager = SkillManager(
            device=self, 
            skill_folder=skill_folder, 
            temp_folder=temp_folder, 
            wake_words=self.wake_words, 
            activation_timeout=self.activation_timeout
        )

        self.skill_manager.initialize()

    @property
    def accepts(self):
        return ["status", "get_settings", "set_settings", "collect"]
    
    def is_alive(self, **kwargs):
        return True
    
    def collect(self, **kwargs):
        data = kwargs.get("data", {})
        context = kwargs.get("context")

        if data.get("type", "") == "kenzy.stt":
            text = data.get("text")
            dev_url = self.get_context_url(context)
            if time.time() < self.timeouts.get(dev_url, {}).get("timeout", 0):
                func = self.timeouts.get(dev_url, {}).get("callback")
                if func is not None:
                    self.logger.debug("Initiating callback function")
                    func(text, context=context)
                    self.skill_manager.activated = time.time()
                else:
                    self.logger.error("Callback function expected but not found.")

            else:
                self.logger.debug("Checking intent for associated skill")
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

        skill_list = {}
        for item in self.skill_manager.skills:
            obj = item.get("object")
            if isinstance(obj, GenericSkill):
                skill_list[obj.name] = {
                    "description": obj.description,
                    "settings": self.settings.get(obj.name)
                }

        return KenzySuccessResponse({
            "active": self.is_alive(),
            "type": self.type,
            "accepts": self.accepts,
            "location": self.location,
            "group": self.group,
            "version": self.service.version,
            "data": {
                "skills": skill_list,
                "devices": self.service.remote_devices
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
    
    def ask(self, text, callback=None, **kwargs):
        # text, in_callback, timeout=0, context=None
        cmd = SpeakCommand(context=kwargs.get("context"))
        cmd.text(text)
        
        context = kwargs.get("context")
        dev_url = self.get_context_url(context)

        timeout = kwargs.get("timeout")
        if timeout is None or float(timeout) <= 0:
            timeout = self.ask_timeout

        self.timeouts[dev_url] = { "timeout": time.time() + float(timeout), "callback": callback }
        # use context = location (door), group (living room), all
        self.service.send_request(payload=cmd)

        return KenzySuccessResponse("Ask command complete")
    
    def play(self, file_name, **kwargs):
        # text, context=None
        cmd = PlayCommand(context=kwargs.get("context"))
        cmd.file_name(file_name)

        self.service.send_request(payload=cmd)

        return KenzySuccessResponse("Play command complete")

    def get_context_url(self, context):
        if isinstance(context, KenzyContext) and context.url is not None:
            dev_url = context.url
        else:
            dev_url = "self"

        return dev_url
