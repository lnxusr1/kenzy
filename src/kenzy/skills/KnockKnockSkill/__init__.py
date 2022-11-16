from kenzy import GenericSkill 
import logging 


class KnockKnockSkill(GenericSkill):
    """
    Skill to respond to knock knock jokes
    """
    
    def __init__(self):
        """
        Knock Knock Skill Initialization
        """
        
        self._name = "KnockKnockSkill"
        self.logger = logging.getLogger("SKILL")
        self.logger.debug(self._name + "loaded successfully.")
    
    def initialize(self):
        """
        Load intent files for Tell Date Time Skill
        
        Returns:
            (bool): True on success else raises an exception
        """
        
        self.register_intent_file("knockknock.intent", self.handle_knockknock_intent)
        return True

    def handle_knockknock_q2(self, message, context=None):
        """
        Last step in knock knock job (e.g. the last laugh).
        
        Args:
            message (str): The text triggering this step.
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool):  True on success or False on failure
        """

        text = self.getMessageFromDialog("knockknock.dialog")
        if text != "":
            return self.say(text, context=context)
        else:
            return self.say("ha ha ha.", context=context)
        
    def handle_knockknock_q1(self, message, context=None):
        """
        Second step in knock knock job (e.g. "VOICE_PROMPT who?").
        
        Args:
            message (str): The text triggering this step.
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool):  True on success or False on failure
        """
        
        return self.ask(str(message) + " who?", self.handle_knockknock_q2, timeout=10, context=context)

    def handle_knockknock_intent(self, message, context=None):
        """
        Primary function for intent matches.  Called by skill manager.
        
        Args:
            message (str):  text that triggered the intent
            context (KContext): Context surrounding the request. (optional)
            
        Returns:
            (bool): True on success or False on failure
        """
            
        return self.ask("Who's there?", self.handle_knockknock_q1, timeout=10, context=context)
    
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
        (object): KnockKnockSkill instantiated class object
    """

    return KnockKnockSkill()