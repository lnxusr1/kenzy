import os
import logging
import random
import sys
import pathlib
import traceback
import importlib 
from kenzy.extras import dayPart, GenericCommand, strip_punctuation


try:
    from padatious import IntentContainer
except ModuleNotFoundError:
    logging.debug(str(sys.exc_info()[0]))
    logging.debug(str(traceback.format_exc()))
    logging.info("SkillManager disabled due to missing libraries.")
    pass


class SpeakCommand(GenericCommand):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.action = "speak"
        self.pre(GenericCommand(action="mute", context=kwargs.get("context")))
        self.post(GenericCommand(action="unmute", context=kwargs.get("context")))

    def text(self, value):
        self.payload["text"] = value


class SkillManager:
    logger = logging.getLogger("SKILLMANAGER")

    def __init__(self, device=None, skill_folder="~/.kenzy/skills", temp_folder="/tmp/intent_cache"):
        self.service = None
        self.skills = []

        self.skill_folder = os.path.expanduser(skill_folder)
        self.device = device
        self.temp_folder = temp_folder
        
    def initialize(self):
        """
        Loads all skills into memory for referencing as required.
        """
        
        self.logger.debug("Initalizing")
        
        # TODO: Download Skills (or unzip from included package)

        try:
            self.intent_parser = IntentContainer(self.temp_folder)
        except NameError:
            self.intent_parser = None

        if self.skill_folder not in sys.path:
            sys.path.insert(0, str(pathlib.Path(self.skill_folder).absolute()))
        
        skillModules = [os.path.join(self.skill_folder, o) for o in os.listdir(self.skill_folder) 
                        if os.path.isdir(os.path.join(self.skill_folder, o))]
        
        for f in skillModules:
            s = os.path.basename(f)
            if str(s).lower() == "__pycache__" or str(s).endswith(".py"):
                continue 

            mySkillName = os.path.basename(f)
            self.logger.debug("Loading " + mySkillName) 
            mySkillModule = importlib.import_module(mySkillName)
            mySkillClass = mySkillModule.create_skill()
            mySkillClass.device = self.device
            mySkillClass.initialize()
            
        self.logger.debug("Skills load is complete.")
        
        try:
            self.intent_parser.train(False)  # False = be quiet and don't print messages to stdout
            self.logger.debug("Training completed.")
        except AttributeError:
            self.logger.error("Training failed.  intent_parser not loaded.")
            pass

        self.logger.info("Initialization completed.")

    def parse(self, text=None, context=None):
        """
        Parses inbound text leveraging skills and fallbacks to produce a response if possible.
        
        Args:
            text (str):  Input text to process for intent.
            context (KHTTPHandler): Context surrounding the request. (optional)
            
        Returns:
            (bool):  True on success and False on failure.
        """
        
        clean_text = strip_punctuation(text)

        def audio_fallback(in_text, context):
            self.logger.debug("fallback: " + in_text)
            return False

        if clean_text is None or str(clean_text).strip() == "":
            return False

        try:
            
            # This one line explains it all... link incoming command into actionable intent using Padatious library
            intent = self.intent_parser.calc_intent(clean_text)
         
            # I need to be at least 60% likely to be correct before I try to process the request.
            if intent.conf >= 0.6:
                for s in self.skills:
                    if intent.name == s["intent_file"]:
                        ret_val = s["callback"](intent, context=context, raw=text)
                        if isinstance(ret_val, bool):
                            return ret_val
                        else:
                            return True  # Default return is True in case the returned value isn't boolean
            else:
                return audio_fallback(text, context)  # Old code for hardcoded responses
        except Exception:
            self.logger.debug(str(sys.exc_info()[0]))
            self.logger.debug(str(traceback.format_exc()))
            self.logger.error("An error occurred")
            
            return False

        return False
    
    def stop(self):
        """
        Calls the stop method of all opened skills to close any daemon processes opened.
        
        Returns:
            (bool): True on success else raises an exception.
        """
        
        if (self.skills is not None and len(self.skills) > 0):
            for s in self.skills:
                try:
                    s["object"].stop()
                except Exception:
                    pass

        return True
    

class GenericSkill:
    """
    Class for inheriting to generate new skills.  Includes basic functionality for generic skills.
    """
        
    def __init__(self):
        """
        Skill Initialization
        """
        
        self._name = "Learned Skill"
        self.device = None 

    @property
    def service(self):
        return self.device.service if self.device is not None else None
    
    def ask(self, text, callback, timeout=0, context=None):
        """
        Encapsulates the frequently used function of "ask" in order to make it easier for new skill development.  Makes self.ask() method available.
        
        Args:
            in_text (str): The text to speak to start the question/answer phase.
            in_callback (function):  The function to call when the subject responds.
            timeout (int):  The length of time to wait for a response before giving up.  A value of zero will wait forever.
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success and False on failure.
        """

        if self.device is not None:
            ret = self.device.ask(text=text, callback=callback, timeout=timeout, context=context)
            return ret.is_success()
        else:
            self.logger.error("Device not referenced")

        return False
    
    def getMessageFromDialog(self, dialog_file, **args):
        """
        Retrieves a randomized line from the specified dialog file beneath the vocab/locale folder.
        
        Args:
            dialog_file (str):  Filename of the dialog for the skill from which to pull a response.
            
        Returns:
            (str): Randomized string response from the specified dialog file.
        """
        
        text = ""
        df = os.path.join(self.device.skill_manager.skill_folder, self.__class__.__name__, "vocab", "en_us", dialog_file)
        
        if os.path.exists(df):
            with open(df, "r") as s:
                m = []
                for line in s:
                    if line.strip() != "":
                        m.append(line.strip())

                text = random.choice(m)
                    
            if ("*dayPart*" in text):
                text = text.replace("*dayPart*", dayPart())
            
            return text
        else:
            return ""
        
    def getContentsFromVocabFile(self, filename):
        """
        Retrieves all text in the specified file beneath the vocab/locale folder.
        
        Args:
            filename (str):  Filename for the skill from which to read data.
            
        Returns:
            (str): Full text of the specified file.
        """
        filename = os.path.join(self.device.skill_manager.skill_folder, self.__class__.__name__, "vocab", "en_us", filename)
        if os.path.exists(filename):
            with open(filename, "r") as f:
                text = f.read()
                
            return text
        else:
            return ""

    def initialize(self):
        """
        Function to be overridden in child classes.  This function is where intents can be registered for the padatious runtime.
        
        Returns:
            (bool):  True on success or False on failure.
        """
        return True
        
    def register_intent_file(self, filename, callback):
        """
        Registers an intent file with the Padatious neural network engine.
        
        Args:
            filename (str): The file in the vocab/local folder whose contents should be registered.
            callback (function): The function to call when the determined intent matches this data set.
            
        Returns:
            (bool):  True on success and False on failure.
        """
        
        fldr = os.path.join(self.device.skill_manager.skill_folder, self.__class__.__name__)
        if os.path.exists(fldr):
            if os.path.exists(fldr):
                if self.device is not None:
                    try:
                        self.device.skill_manager.intent_parser.load_file(filename, os.path.join(fldr, "vocab", "en_us", filename), reload_cache=True)
                        self.device.skill_manager.skills.append({ "intent_file": filename, "callback": callback, "object": self })
                    except AttributeError:
                        self.logger.error("Error registering intent file due to AttributeError.")
                        return False
                else:
                    self.logger.error("Device not referenced")
            else:
                self.logger.error("Error registering intent file (" + str(filename) + ")")
        else:
            self.logger.error("Intent file not found (" + str(filename) + ")")
            return False
        
        return True
    
    def say(self, text, context=None):
        """
        Encapsulates the frequently used function of "say" in order to make it easier for new skill development.  Makes self.say() method available.
        
        Args:
            in_text (str): The text to speak to start the question/answer phase.
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success and False on failure.
        """
        if self.device is not None:
            ret = self.device.say(text=text, context=context)
            return ret.is_success()
        else:
            self.logger.error("Device not referenced")

        return False

    def stop(self):
        """
        Method to stop any daemons created during startup/initialization for this skill.
        
        Returns:
            (bool):  True on success and False on failure
        """
        return True