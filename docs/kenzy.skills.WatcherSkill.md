# WatcherSkill &middot; Kenzy.Ai

The WatcherSkill leverages any attached cameras through kenzy.image to describe what is shown in total or in part.

## Prompts

* What do you see?

## Example Responses

* I see 3 people with motion in 2 areas.
* I see motion in 3 areas.
* I don't see anything at the moment.

## Configuration

You can filter out unwanted objects from the responses using the filters option.  This will limit the results returned from this skill to only the objects matching the descriptions provided in the list.

```
device:
  WatcherSkill:
    filters:
      - person
      - car
      - motorcycle
      - bicycle
      - truck
      - bus
      - dog
      - cat
```