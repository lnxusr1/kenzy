# Creating your own Skill

The basic structure for a skill is laid out below:

```
from kenzy import GenericSkill

class MyCustomSkill(GenericSkill):
 	def __init__(self):
 		self._name = "My Custom Skill"
 		super().__init__()
    
 	def initialize(self):
 		self.register_intent_file("customskill.intent", self.handle_custom_intent)
 		return True
       
	def handle_custom_intent(self, message, context=None, **kwargs):
 		text = self.getMessageFromDialog("customskill_question.dialog")
 		self.ask(text, self.handle_custom_response, context=context, timeout=10)
 		return True

	def handle_custom_response(self, message, context=None, **kwargs):
 		text = self.getMessageFromDialog("customskill_response.dialog")
 		self.say(text, context=context)
 		return True
     
 	def stop(self):
 		return True
        
def create_skill():
 	return MyCustomSkill()
```

You then must also create folders as follows:
```
/skills
    /MyCustomSkill
        /__init__.py
        /vocab
            /en_us
                /customskill.intent
				/customskill_question.dialog
                /customskill_response.dialog
```

Contents of ```customskill.intent```:

```
how are you (doing |) (today |)
```

Contents of ```customskill_question.dialog```:
```
I'm doing well, thank you.  How are you?
I'm online and functioning properly.  How are you?
```

Contents of ```customskill_response.dialog```:
```
Thank you for the update.
```
-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)