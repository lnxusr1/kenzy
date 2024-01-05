# Creating your own Skill

The basic structure for a skill is laid out below:

```python
from kenzy import GenericSkill

class MyCustomSkill(GenericSkill):
 	def __init__(self, **kwargs):
 		super().__init__(**kwargs)

 		self.name = "My Custom Skill"
		self._version = [1, 0, 0]
    
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
        
def create_skill(**kwargs):
 	return MyCustomSkill(**kwargs)
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

## Creating Intents

Kenzy utilizes the [Padatious](https://mycroft-ai.gitbook.io/docs/mycroft-technologies/padatious) libriary which in turn uses a series of example sentences to train a machine learning model to identify an intent.

In the example above the file ending in the file extension .intent is in __Intent__ file. For example, if you were to create a tomato Skill to respond to questions about a tomato, you would create the file

```
vocab/en_us/what.is.intent
```

This file would contain examples of questions asking what a tomato is.
```
What would you say a tomato is?
What's a tomato?
Describe a tomato
What defines a tomato
```
and
```
vocab/en_us/do.you.like.intent
```

with examples of questions about Kenzy's opinion about tomatoes:

* Are you fond of tomatoes?
* Do you like tomatoes?
* What are your thoughts on tomatoes?
* Are you fond of ```{type}``` tomatoes?
* Do you like ```{type}``` tomatoes?
* What are your thoughts on ```{type}``` tomatoes?

Note the {type} in above examples these are wild-cards where matching content is forwarded to the skill's intent handler.

Each file should contain at least 4 examples for good modeling.

-----

## Creating Entities

In the above example, ```{type}``` will match anything. While this makes the intent flexible, it will also match if we say something like Do you like eating tomatoes?. It would think the type of tomato is eating which doesn't make much sense. Instead, we can specify what type of things the {type} of tomato should be. We do this by defining the type entity file here:

```
vocab/en_us/type.entity
```

Which would contain something like:

```
red
reddish
green
greenish
yellow
yellowish
ripe
unripe
pale
```

Now, we can say things like Do you like greenish red tomatoes? and it will tag type: as greenish red.

-----

## Advanced Usage

### Parentheses Expansion

Sometimes you might find yourself writing a lot of variations of the same thing. For example, to write a skill that orders food, you might write the following intent:

```
Order some {food}.
Order some {food} from {place}.
Grab some {food}.
Grab some {food} from {place}.
```

Rather than writing out all combinations of possibilities, you can embed them into a single line by writing each possible option inside parentheses with | in between each part. For example, that same intent above could be written as:

```
(Order | Grab) some {food} (from {place} | )
```

Nested parentheses are supported to create even more complex combinations, such as the following:

```
(Look (at | for) | Find) {object}.
```

Which would expand to:

```
Look at {object}
Look for {object}
Find {object}
```

### Number matching

Let's say you are writing an __Intent__ to call a phone number. You can make it only match specific formats of numbers by writing out possible arrangements using # where a number would go. For example, with the following intent:

```
Call {number}.
Call the phone number {number}.
```

the number.entity could be written as:

```
+### (###) ###-####
+## (###) ###-####
+# (###) ###-####
(###) ###-####
###-####
###-###-####
###.###.####
### ### ####
##########
```

### Entities with unknown tokens
Let's say you wanted to create an intent to match places:

```
Directions to {place}.
Navigate me to {place}.
Open maps to {place}.
Show me how to get to {place}.
How do I get to {place}?
```

This alone will work, but it will still get a high confidence with a phrase like "How do I get to the boss in my game?". We can try creating a ```.entity``` file with things like:

```
New York City
#### Georgia Street
San Francisco
```

The problem is, now anything that is not specifically a mix of New York City, San Francisco, or something on Georgia Street won't match. Instead, we can specify an unknown word with :0. This would would be written as:

```
:0 :0 City
#### :0 Street
:0 :0
```

Now, while this will still match quite a lot, it will match things like "Directions to Baldwin City" more than "How do I get to the boss in my game?"

*NOTE: Currently, the number of :0 words is not fully taken into consideration so the above might match quite liberally.*

-----

## Credits

Complete MyCroft Padatious documentation available on [their site](https://mycroft-ai.gitbook.io/docs/mycroft-technologies/padatious).  Do note that not all features are fully supported in Kenzy as we have taken a slightly different approach to intent parsing and device management.

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)