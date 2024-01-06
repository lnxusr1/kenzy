import sys
import traceback
import time
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse, KenzyContext
from kenzy.skillmanager.core import SkillManager, SpeakCommand, PlayCommand, GenericSkill
from kenzy.extras import get_status, get_skills_package, KenzyLogger


class SkillsDevice:
    type = "kenzy.skillmanager"

    def __init__(self, **kwargs):

        self.logger = KenzyLogger("KNZY-SKM")

        if not isinstance(kwargs.get("timeout", {}), dict):
            kwargs["timeout"] = {}

        self.settings = kwargs
        self.service = None
        self.skill_manager = None

        self.location = kwargs.get("location", "Kenzy's Room")
        self.group = kwargs.get("group", "Kenzy's Group")
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
            activation_timeout=self.activation_timeout,
            logger=self.logger
        )

        self.skill_manager.initialize()
        self.skill_manager.service = self.service

    @property
    def accepts(self):
        return ["status", "get_settings", "set_settings", "collect", "download_skill", "relay"]
    
    def is_alive(self, **kwargs):
        return True
    
    def download_skill(self, **kwargs):
        try:
            self.logger.info("Downloading skills")
            if not get_skills_package(skill_name=None, skill_dir=self.settings.get("folder.skills", "~/.kenzy/skills")):
                return KenzyErrorResponse("Download failed.")
            
            self.initialize()

        except Exception:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            return KenzyErrorResponse("Download failed.")
        
        return KenzySuccessResponse("Download successful.")
    
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
                self.skill_manager.parse(text=text, context=context)
        else:
            self.logger.debug(f"COLLECT: {data}")

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
                try:
                    skill_list[obj.name] = {
                        "description": obj.description,
                        "settings": self.settings.get(obj.name),
                        "version": obj.version
                    }
                except AttributeError:
                    pass

        st = get_status(self)
        st["url"] = self.service.service_url
        st["data"]["skills"] = skill_list
        st["data"]["devices"] = self.service.remote_devices
        st["data"]["logs"] = list(self.logger.entries)
        st["data"]["logs"].reverse()
        
        return KenzySuccessResponse(st)
    
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
    
    def relay(self, data, **kwargs):
        url = data.get("url", self.service.service_url)
        request = data.get("command")
        headers = data.get("headers")
        
        self.logger.debug("==============")
        self.logger.debug(f"URL: {url}")
        self.logger.debug(f"request: {request}")
        self.logger.debug(f"headers: {headers}")

        if request is not None:
            self.service.send_request(payload=request, url=url, headers=headers, wait=False)
            return KenzySuccessResponse("Command received successfully")
        
        return KenzyErrorResponse("No command received")
