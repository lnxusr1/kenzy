# Part 2 — People & Rooms

**Goal:** Teach Kenzy about **who's talking** and expand her presence into all your rooms.  

*Note:* Make sure you completed the prerequisites from [Part 1](part1-first-conversation.md) first.

## 1. Teach her who's who

Enroll each household member's voice — from the dashboard is easiest:

1. **People** tab → **+ Add person** → their name.
2. On their card, **Enroll voice**, pick the room they're standing in.
3. Kenzy reads a few sentences aloud at that room's speaker; they repeat each
   one. Done — the voice is linked to the person automatically.

!!! note "✓ Checkpoint"
    Their card now shows `voice · 5 samples`. Have them say *"hey Kenzie,
    what time is it?"* — the dashboard's **Activity** tab shows the request
    with their name on it.

Enrolling again later (different day, different distance) adds samples and
makes recognition more reliable. The full story — including the voice-command
and CLI paths — is in [Speaker Enrollment](../speaker-enrollment.md).

## 2. Try out Memory

Recognition is what makes memory personal. Have someone enrolled try:

> "Hey Kenzie… **remember that my locker code is 4321**"
>
> "Hey Kenzie… **what do you know about my locker?**"

Private facts are only ever spoken back to their owner — someone else asking
gets nothing. *"Everyone should know…"* stores a fact the whole house can ask
for. The complete guide (tiers, forgetting, the privacy tools) is
[Memory](../memory.md).

-----

!!! warning "If you have more than one device..."
    ...then you need to configure each device *node* to work with the server instance created in [Part 1](./part1-first-conversation.md).

## 3. Adding additional room nodes

A room node is a small device whose only job is to listen and speak — an
**Orange Pi Zero 3 / 2W** or **Raspberry Pi 3/4/5** with a USB speakerphone. The
thinking still happens on your server.

1. Set up the board with its standard OS (Raspberry Pi OS Lite is fine), connected
   to the same network.
2. In the dashboard, go to **Settings → Node provisioning** and copy the **join
   token**.
3. On the new device, run:

    ```bash
    curl -fsSL https://kenzy.ai/install.sh | bash -s -- --profile node --token PASTE_TOKEN_HERE
    ```

That's the whole install. The node finds your server on the network by itself,
connects with the token, and pulls all its settings from the server — there's
nothing to configure on the device.

!!! note "✓ Checkpoint"
    Within a minute the new node appears as a card in the dashboard's fleet view.
    Open its **Configure** page to name its room, then run **Set up / calibrate
    audio…** to pick its microphone and tune it — all from your browser. Then walk
    over and say "hey Kenzie."

Repeat for as many rooms as you like. With more than one room you can try:

> "Hey Kenzie… **tell everyone dinner's ready**" (speaks in every room)
>
> "Hey Kenzie… **call the living room**" (live intercom — the other room says "yes" to accept)


-----

If you run Home Assistant,
**[Continue](part3-home-assistant.md)** to **[Part 3 — Home Assistant Basics](part3-home-assistant.md)**.
