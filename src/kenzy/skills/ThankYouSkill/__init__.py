from kenzy import GenericSkill
import logging 


class ThankYouSkill(GenericSkill):
    """
    Skill to say "Thank You"
    """
    
    def __init__(self):
        """
        Thank You Skill Initialization
        """
        
        self._name = "ThankYouSkill"
        self.logger = logging.getLogger("SKILL")
        self.logger.debug(self._name + "loaded successfully.")
    
    def initialize(self):
        """
        Load intent files for Thank You Skill
        
        Returns:
            (bool): True on success else raises an exception
        """

        self.register_intent_file("thankyou.intent", self.handle_thankyou_intent)
        return True
        
    def handle_thankyou_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (obj):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
        
        text = self.getMessageFromDialog("thankyou.dialog")
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
        (object): ThankYouSkill instantiated class object
    """
    
    return ThankYouSkill()
