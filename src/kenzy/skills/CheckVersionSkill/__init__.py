from kenzy import GenericSkill
import logging 


class CheckVersionSkill(GenericSkill):
    """
    Skill to check version
    """
    
    def __init__(self):
        """
        Check Version Skill Initialization
        """
        
        self._name = "CheckVersionSkill"
        self.logger = logging.getLogger("SKILL")
        self.logger.debug(self._name + "loaded successfully.")
    
    def initialize(self):
        """
        Load intent files for Check Version Skill
        
        Returns:
            (bool): True on success else raises an exception
        """

        self.register_intent_file("checkversion.intent", self.handle_checkversion_intent)
        return True
        

    def handle_checkversion_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        if self.brain is not None:
            return self.say("The active version is " + str(self.brain.version), context=context)
        
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
        (object): CheckVersionSkill instantiated class object
    """
    
    return CheckVersionSkill()
