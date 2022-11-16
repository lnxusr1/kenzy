# Creating your own Skill

The basic structure for a skill is laid out below:

```
from kenzy_brain import Skill

class MyCustomSkill(Skill):
 	def __init__(self):
 		self._name = "My Custom Skill"
 		super().__init__()
    
 	def initialize(self):
 		self.register_intent_file("customskill.intent", self.handle_custom_intent)
 		return True
       
 	def handle_custom_intent(self, message, context=None):
 		text = self.getMessageFromDialog("customskill.dialog")
 		self.say(text, context=context)
 		return True
         
 	def stop(self):
 		return True
        
def create_skill():
 	return MyCustomSkill()
```

You can also create folders as follows:
```
/skills
    /MyCustomSkill
        /__init__.py
        /vocab
            /en_us
                /customskill.intent
                /customskill.dialog
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)