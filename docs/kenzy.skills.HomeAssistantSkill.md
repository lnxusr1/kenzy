# HomeAssistantSkill &middot; Kenzy.Ai

The HomeAssistantSkill enables Kenzy to interact with your installation of HomeAssistant.  It requires configuration in order to work properly.

## Prompts

* Turn on the lights in the office.
* Unlock the front door.

## Example Responses

* Office lights are now on.
* Front door is unlocked.

## Other Interactions

* Trigger input_boolean entities via image analysis (using kenzy.image).  (e.g. Turn on an input_boolean if Kenzy sees a person walk in front of a camera.)

## Configuration
```
device:
  HomeAssistantSkill:
    url: *enter your home assistant URL here*
    token: *enter your long-lived access token here*
    locks: disable
    lights: enable
    covers: enable
    area_aliases:
      "back_yard": backyard
      "out_building": Storage Shed
    entity_aliases:
      light.light_293029: 
        - Nightstand
    triggers:
      - type: camera
        source_name: Camera 1
        entity_id: input_boolean.human_detected
        filters:
          - person
      - type: camera
        source_name: Camera 1
        entity_id: input_boolean.animal_detected
        filters:
          - dog
          - cat
          - horse
          - bird
```