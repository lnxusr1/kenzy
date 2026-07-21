# Talking to Kenzy

Everything on this page works out of the box — no configuration. It's the
five-minute guide to hand anyone in the house (or read aloud to a houseguest).

## Waking her up

Say **"Hey Kenzy."** You'll hear a short chime — that's her saying *I'm
listening*. Ask your question or give your instruction; a soft ticking sound
means she's thinking, and she answers out loud.

The chime is also the privacy line: until the wake word fires, audio is
processed on the room's device and **never leaves it**. Only after the chime
does your speech travel (to your own server).

```text
You:    Hey Kenzy
Kenzy:  ♪ (chime — listening)
You:    what's the weather tomorrow?
Kenzy:  Tomorrow looks sunny with a high of 84…
```

## Making her stop

- **Interrupt her mid-sentence:** say **"Hey Kenzy"** while she's talking —
  the wake word is the **only** thing she reacts to while speaking, so a bare
  *"stop"* shouted over her does nothing. "Hey Kenzy" cuts her off instantly
  and she listens; follow with *"stop"* to end it there (or say nothing and
  she'll go back to idle on her own).
- **The stop words** — *"stop"*, *"stop it"*, *"stop talking"*, *"be quiet"*,
  *"quiet"*, *"hush"*, *"shut up"*, *"silence"*, *"that's enough"* — end the
  exchange **silently** whenever she's listening: right after the wake word
  ("Hey Kenzy" … *"stop"*), or as your answer when she's holding the mic open
  for a follow-up. No reply, no acknowledgment: you asked for quiet, so quiet
  is what you get. These are handled at the lowest level, before any thinking
  happens, so they're instant even if your AI model is slow or down.
- **The polite bail-outs** — *"never mind"*, *"forget it"*, *"cancel"* — do the
  same thing but with a brief spoken "Okay, no problem," which fits the cases
  where you changed your mind rather than demanded silence.
- **A ringing alarm** stops the moment you say the wake word — no other words
  needed.

!!! note "“Stop” vs. “stop the timer”"
    A bare *"stop"* ends the current exchange. *"Stop the timer"* / *"cancel
    the alarm"* cancel the thing you named — see
    [Timers, Alarms & Reminders](skills/builtin.md#timers-alarms-reminders).

## Muting a room

*"Hey Kenzy, mute"* **mutes that room's speaker** — and it stays muted until
you say *"Hey Kenzy, unmute"* (or *"turn the sound back on"*). That's the
difference from the stop words above: *"stop"* ends one exchange; *"mute"*
silences the room until further notice.

Two sounds intentionally survive muting, at a low volume: the wake-word chime
(so you always know when she's listening) and alert chimes like the
[doorbell](integrations/home-assistant.md#driving-kenzy-from-automations)
(someone at your door outranks mute). A restart also un-mutes.

Volume is voice-controlled too: *"volume up"*, *"turn it down"*, *"set the
volume to 60"*, *"louder"*, *"quieter"*.

## Having a conversation

When **Kenzy asks you something** — a follow-up question, "should I create
one called Shopping list?", the next line of a knock-knock joke — **just
answer**. No wake word, no chime: when she stops talking in a conversation
she started, she's listening. If you say nothing for about eight seconds,
a soft cue plays meaning *"I stopped waiting"* and the conversation ends.
Any bail-out phrase (*"never mind"*) ends it politely too.

On rooms with an echo-cancelling speaker you don't even have to wait for her
to finish: **start talking over her** during a conversation and she'll drop
her volume, then yield the floor to you mid-sentence.

## What the sounds mean

| Sound | Meaning |
|---|---|
| Chime after "Hey Kenzy" | She's listening (plays even when muted, quietly) |
| Soft ticking | She heard you and is working on it |
| Short cue after silence | She stopped waiting for a reply — the conversation ended |
| Spoken apology | Something in the pipeline failed (see [Troubleshooting](troubleshooting.md)) |
| Lead-in tone before an announcement | A timer or alarm you set is firing |
| Doorbell chime | An automation rang the house (plays even when muted, quietly) |
| Ringing tone after "call the kitchen" | The other room is being asked to accept your [intercom call](skills/builtin.md#intercom) |

## Things to try

- *"What time is it?"* · *"What's the weather this week?"* — instant, and the
  time works with no internet at all
- *"Turn off the lights"* — your [Home Assistant devices](skills/home-assistant.md),
  room-aware: in the kitchen, that means the kitchen lights
- *"Add milk and eggs to the shopping list"* — [lists](skills/builtin.md#lists)
  that show up on everyone's phone
- *"Set a timer for 10 minutes"* · *"Wake me at 7"* · *"Remind me to call Mom
  at 8"*
- *"Remember that the spare key is under the blue pot"* · *"What do you know
  about the spare key?"* — per-person [memory](memory.md) with real privacy:
  your private facts are only ever spoken back to *you* (and a spoken *code*
  or *password* is auto-vaulted into the encrypted lockbox)
- *"Play some jazz"* · *"Put on The Beatles in the loft"* — music by name via
  [Music Assistant](skills/home-assistant.md), if you run it
- *"Is Mom home?"* · *"Who's home?"* — live from Home Assistant's
  [presence tracking](skills/builtin.md#presence), once a person's card links
  their HA account
- *"Tell everyone dinner's ready"* — spoken in every room at once
- *"Call the living room"* — a live two-way [intercom](skills/builtin.md#intercom)
  (the other side has to say "yes")
- *"Enroll me as Alice"* — teach her [your voice](speaker-enrollment.md), so
  "my reminders" means *yours*
- *"Calibrate"* — she [tunes her hearing](dashboard.md#calibrating-a-nodes-audio)
  to your room, by voice

The full list of built-in abilities is in [Built-in Skills](skills/builtin.md).
