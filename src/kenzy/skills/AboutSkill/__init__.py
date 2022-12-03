from kenzy import GenericSkill
import logging 


class AboutSkill(GenericSkill):
    """
    Skill provide "About" Information
    """
    
    def __init__(self):
        """
        About Skill Initialization
        """
        
        self._name = "AboutSkill"
        self.logger = logging.getLogger("SKILL")
        self.logger.debug(self._name + "loaded successfully.")
    
    def initialize(self):
        """
        Load intent files for About Skill
        
        Returns:
            (bool): True on success else raises an exception
        """

        self.register_intent_file("who.intent", self.handle_who_intent)
        self.register_intent_file("status.intent", self.handle_status_intent)
        self.register_intent_file("real.intent", self.handle_real_intent)
        self.register_intent_file("human.intent", self.handle_human_intent)
        self.register_intent_file("maker.intent", self.handle_maker_intent)
        return True

    def handle_who_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        if message.conf == 1.0:
            
            text = self.getMessageFromDialog("who.dialog")
            if (text != ""):
                return self.say(text, context=context)
            
        return False

    def handle_status_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        text = self.getMessageFromDialog("status.dialog")
        if (text != ""):
            return self.say(text, context=context)
            
        return False

    def handle_real_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        if message.conf == 1.0:
            
            text = self.getMessageFromDialog("real.dialog")
            if (text != ""):
                return self.say(text, context=context)
            
        return False

    def handle_human_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        if message.conf == 1.0:
            
            text = self.getMessageFromDialog("human.dialog")
            if (text != ""):
                return self.say(text, context=context)
            
        return False

    def handle_maker_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        if message.conf == 1.0:
            
            text = self.getMessageFromDialog("maker.dialog")
            if (text != ""):
                return self.say(text, context=context)
            
        return False
    
    def stop(self):
        """
        Method to stop any daemons created during startup/initialization for this skill.
        
        Returns:
            (bool):  True on success and False on failure
        """
        return True
        
    
def create_skill():
    """
    Method to create the instance of this skill for delivering to the skill manager
    
    Returns:
        (object): HelloSkill instantiated class object
    """
    
    return AboutSkill()
