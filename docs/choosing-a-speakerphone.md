# Choosing a Speakerphone

The microphone-and-speaker in each room is the single hardware choice that most
affects how well Kenzy hears you. The compute side is ordinary — a small Linux
box for the "brain," a Raspberry Pi per room — and covered elsewhere
([Getting Started](getting-started.md), [Deployment](deployment.md)). This page
is only about the audio device.

It's a living list from our own research and testing — short, honest, and
incomplete. **The guidance below matters more than any single product name**: it
lets you judge a device we've never seen, and it doesn't go out of stock.

## Guidance

**Look for:**

- **Wired USB.** This is the one people get wrong. A wired device stays awake
  and always listening.
- **Echo cancellation** (often written "full duplex"). It's what lets Kenzy
  hear you *while she is speaking* — the basis of barge-in, room-to-room
  intercom, and stopping a ringing alarm by voice. Calibration detects it; a
  device without it still works but runs half-duplex (no interrupting her, no
  intercom, alarms deliver once).
- **A fixed, desktop puck.** It's an appliance that lives on a shelf, not a
  gadget you carry to meetings.

**Buying for multiple rooms?** If any two rooms are close enough that both
devices could hear the same "Hey Kenzy" — an open floor plan, large rooms with
two nodes — we **strongly recommend using the same model for all of them**. Not
a hard requirement, but the difference is real: when co-audible nodes share an
[`audio_group`](configuration/node.md), Kenzy picks the device that heard you
best, and different models judge loudness differently — each has its own
automatic gain control, and we've measured gaps of several dB between models
hearing the same voice. Mixed hardware can make the farther device "sound"
closer and win the pick. Identical devices judge alike, and the whole
experience gets noticeably better. (Nodes that stand alone, out of earshot of
each other, can be anything — this only matters within earshot.)

**Avoid:**

- **Bluetooth or battery-powered speakerphones.** They sleep to save power, and
  a room node that naps goes deaf between uses — a wake word to a dozing mic is
  simply never heard. This is the most common mistake, by far. (A few devices
  offer a genuine wired-USB mode that stays awake while plugged in; that *can*
  work, but confirm it before trusting it.)
- **"Noise cancellation" with no mention of echo.** Different feature, not a
  substitute — it won't give you full duplex.
- **Anything sold as portable or "for meetings on the go."** A different product
  for a different job, and almost always wireless.

**Checking a specific listing:**

- Read the **maker's own page** for the word *echo* (or *full duplex*), not a
  reseller's keyword list.
- **Match the exact model string.** Wired and wireless siblings routinely share
  a name with one word or letter different — a "Wired" beside a "Wireless," an
  "…U" beside a "…B." The wireless one is the one you don't want.
- **Plain beats fancy.** An ordinary USB conference puck is the target; the big
  enterprise units work but are built for a room larger than a home needs.

## The list

- ✅ **Recommended** — meets the criteria. The note says whether we run it, it's
  still sold, or we simply haven't tested it. Discontinued-but-sound units are
  here too: if you can find one, there's no reason not to buy it.
- ⚠️ **Maybe — check first** — meets most of the criteria, but one point is
  genuinely in question. Fine to try; confirm the open point before you rely on
  it.
- ❌ **Avoid** — fails a criterion (Bluetooth, which auto-sleeps, or no echo
  cancellation). Being discontinued never, on its own, lands a device here.

| | Speakerphone | Notes |
|:---:|---|---|
| ✅ | **Anker PowerConf S330** | **We run this** — our main device and the safe default. Echo cancellation, working volume buttons, and Kenzy can manage its mic gain directly. |
| ✅ | **EMEET OfficeCore M1A** | We've run this. USB-C, no Bluetooth. Works well; sets its own mic gain automatically (calibration adapts), so the input level isn't yours to hand-tune. |
| ✅ | **Kaysuda SP300U** | **We run this.** USB-A budget puck. Works — but ours has occasionally dropped off the USB connection and needed a reconnect, worth weighing for round-the-clock use. |
| ✅ | **Dell Pro Desktop Speakerphone (SP3022)** | USB-A/C, no Bluetooth, names echo cancellation. Meets the criteria on the maker's spec; not tested by us. |
| ✅ | **Dell Pro Wired Speakerphone (SP325)** | USB-A/C, no Bluetooth, full duplex. The larger sibling of the SP3022. Not tested by us. |
| ✅ | **Lenovo Go Wired Speakerphone** | USB-C/A, no Bluetooth, names echo cancellation. Not tested by us. |
| ✅ | **Poly Sync 10** | USB-A/C, no Bluetooth, full duplex. Not tested by us. |
| ✅ | **Poly Calisto 3200** | USB-A or USB-C (two versions), no Bluetooth, names echo cancellation. Not tested by us. |
| ✅ | **ClearOne CHAT 50** | USB, no Bluetooth. The strongest echo-cancellation pedigree on this list. Not tested by us. |
| ✅ | **NUROUM A05U** | **We run this.** USB-C/A, no Bluetooth. Echo cancellation, and — unusually for a budget puck — it exposes a host mic-gain control Kenzy can manage. Calibrate it, as with any device. |
| ✅ | **Anker PowerConf S360** | *Discontinued by Anker* — but it meets the criteria, so buy it if you find one, new or used. |
| ✅ | **EMEET OfficeCore M0** (wired) | *Discontinued.* Meets the criteria; just confirm it's the wired M0, not the Bluetooth **M0 Plus** — they share a listing. |
| ✅ | **Jabra Speak 410** | *Discontinued by Jabra* — but a solid wired unit (the predecessor to the Bluetooth 510). Buy it if you find one. |
| ✅ | **Sennheiser SP 10** | *Discontinued.* Wired USB with echo cancellation; old, so condition matters. |
| ⚠️ | **EPOS EXPAND SP 20** | USB + 3.5 mm, names echo cancellation — but it has an internal battery and *may auto-sleep*. Confirm it stays awake while wired before relying on it. |
| ⚠️ | **Unbranded marketplace pucks** | The $30–60 tier. Some claim echo cancellation, but there's no manufacturer page to confirm it and the models churn under new names. Only worth it if you'll test one yourself. |
| ❌ | **Jabra Speak2 40** | Bluetooth (auto-sleep). |
| ❌ | **Jabra Speak 510 / 750** | Bluetooth. |
| ❌ | **Anker PowerConf S3** | Bluetooth + battery. |
| ❌ | **Poly Sync 20 / Sync 40** | Bluetooth. |
| ❌ | **EMEET Luna / Luna Lite / Luna Plus** | Bluetooth, and a wireless dongle. |
| ❌ | **EMEET OfficeCore M0 Plus** (E1103) | Bluetooth. (Not the wired M0 above.) |
| ❌ | **EPOS EXPAND SP 30+** | Bluetooth. |
| ❌ | **SVBONY SVHub Omni2P / Omni2** | Bluetooth + NFC + battery — a portable device that also takes USB. |
| ❌ | **Yamaha YVC-200** | Bluetooth + battery. |
| ❌ | **Yealink CP50** | Bluetooth. |
| ❌ | **MAXHUB BM20 / BM45** | Bluetooth. |
| ❌ | **Logitech P710e** | Bluetooth/NFC mobile unit. (Also discontinued — but the Bluetooth is the reason it's here.) |
| ❌ | **BY Y02** | Not a speakerphone — a plain USB speaker/mic with no echo cancellation. (We use one only as a test noise source.) |

When in doubt, go back to the guidance. A device that is wired USB, names echo
cancellation, and stays put is almost always a safe bet, whether or not it's on
this list.
