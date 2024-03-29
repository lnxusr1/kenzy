# KENZY: Skill Manager

The Skill Manager device acts as the Brain for all collecting, storing, processing, and responding to events.  It uses an intent determination routine to translate text into actions/responses.  Those actions are then defined by Skills that can be added or created to meet any preference.

## Parameters
| Parameter           | Type    | Default                       | Description                       |
| :------------------ | :------ | :---------------------------- | :-------------------------------- |
| group               | str     | *None*                        | Device membership group           |
| location            | str     | *None*                        | Location, e.g. "Living Room"      |
| folder.skills       | str     | ~/.kenzy/skills               | Skills folder                     |
| folder.temp         | str     | /tmp/intent_cache             | Trained intents storage           |
| wake_words          | list    | ["Kenzie", "Kenzy", "Kinsey"] | Wake words                        |
| timeout.wake        | float   | 15                            | Idle time between wake words      |
| timeout.ask         | float   | 10                            | Wait time after "ask" commands    |
| log_level           | str     | info                          | Log level to include in logs      |

## Example YAML file

See [Service Settings](kenzy.containers.md) for options in the *service* group.
```yaml
type: kenzy.skillmanager

device: 
  log_level:                debug
  location:                 Living Room
  group:                    Primary
  folder.skills:            ~/.kenzy/skills
  folder.temp:              /tmp/intent_cache
  wake_words:
    - Kenzy
    - Kenzie
    - Kensey
  timeout.wake:             45
  timeout.ask:              10

service:
  upnp.type:                server
  host:                     0.0.0.0
  port:                     9700
```

## Basic Skills List

| Name               | Example                 | Description                            |
| :----------------- | :---------------------- | :------------------------------------- |
| HelloSkill         | "Hello"                 | Simple greeting skill                  |
| AboutSkill         | "How are you?"          | Provides general information           |
| CheckVersionSkill  | "What is your version?" | Gives the current version              |
| KnockKnockSkill    | "Knock, Knock"          | Responds to "Knock, Knock" jokes       |
| PowerDownSkill     | "Please Power Down"     | Shutsdown the server                   |
| TellDateTimeSkill  | "What is the date?"     | Gives the date, time or day of week.   |
| TellJokeSkill      | "Tell me a joke."       | Tells a random joke                    |
| ThankYouSkill      | "Thank you"             | Displays manners                       |
| HomeAssistantSkill | "Turn on the lights"    | Interacts with home assistant devices. |

Some skills allow for additional configuration.  These configurations can be specified under the ```type: kenzy.skillmanager``` device using the skill name as its parent.

For example:
```yaml
type: kenzy.skillmanager

device:
    group:                 My Group
    location:              Family Room

    HomeAssistantSkill:
      url:                 http://homeassistant.local:8123
      token:               <long-lived-access-token>
```

-----

## Skills Settings

### HomeAssistantSkill

The HomeAssistantSkill allows for you to specify the following parameters:

| Parameter       | Type | Description                                                                  |
| :-------------- | :--- | :--------------------------------------------------------------------------- |
| url             | str  | The complete URL to your HA server                                           |
| token           | str  | A long-lived access token generated from HA                                  |
| area_aliases    | dict | A dictionary of name/value pairs of Area IDs and friendly names to use       |
| entity_aliases  | dict | A dictionary of name/list pairs of Entity IDs and friendly name alternatives |

Example:
```yaml
type: kenzy.skillmanager

device:
    HomeAssistantSkill:
      url:                 http://homeassistant.local:8123
      token:               <long-lived-access-token>
      area_aliases:
        ha_area_id_here:   My First Area
        ha_area2_id_here:  My Second Area
      entity_aliases:
        light.my_switch_list:
          - My Switch Light
          - My Ceiling Light
```

### WeatherSkill

The WeatherSkill allows for you to specify the following parameters:

| Parameter       | Type  | Description                                                                  |
| :-------------- | :---- | :--------------------------------------------------------------------------- |
| api_key         | str   | Current Weather API Key from openweathermap.org                              |
| lat             | float | Lattitude of location to provide weather update                              |
| lon             | float | Longitude of location to provide weather update                              |
| units           | str   | Default: imperial; Options:  metric, imperial, standard (a.k.a. kelvin)      |

Example:
```yaml
type: kenzy.skillmanager

device:
    WeatherSkill:
      api_key:            <api_key_here>
      lat:                43.878708
      lon:                -103.458935
      units:              imperial
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)