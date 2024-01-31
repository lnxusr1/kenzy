# NewsSkill &middot; Kenzy.Ai

The NewsSkill enables Kenzy to read back the latest headlines from an RSS feed of your choice.

## Prompts

* What are the latest headlines?

## Example Responses

* The last two headlines are: <HEADLINE TEXT>, <HEADLINE TEXT>


## Configuration
```
device:
  NewsSkill:
    feeds:
      - https://feeds.a.dj.com/rss/RSSWorldNews.xml
      - https://www.cbsnews.com/latest/rss/main