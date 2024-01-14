# WeatherSkill &middot; Kenzy.Ai

The WeatherSkill provides a way to get the current weather conditions through Kenzy's voice request/response mechanisms.

## Prompts

* What's the current weather like outside?
* What is the weather right now?
* What's the current temperature like outside?
* What is the temperature right now?

## Example Responses

* The temperature is 28 degrees with clear skies.  The wind is blowing at about 5 miles per hour from the northwest with gusts up to 30 miles per hour.
* The temperature is 28 degrees.

## Configuration

```
device:
  WeatherSkill:
    api_key: *enter your API key from openweathermap.org*
    units: imperial
    lat:   43.878708
    lon:   -103.458935
```

Get your free API key from [openweathermap.org](http://openweathermap.org)