# Memory

Kenzy can remember things — per person. "Hey Kenzy, remember that the spare
key is under the blue pot" stores a fact; "where's the spare key?" gets it
back. (A *gate code* would take a different path — secret-shaped facts are
auto-vaulted into the [lockbox](#secrets-the-lockbox) below.) Facts
belong to the **person** who said them (see
[Speaker Enrollment](speaker-enrollment.md) — memory is one of the things
enrolling your voice unlocks), and who can hear a fact back is a deliberate,
enforced choice.

Memory is on by default (`memory.enabled` in
[LLM configuration](configuration/llm.md)). An **unrecognized voice gets no
memory at all** — no writes, no reads, not even echoes of other people's
facts. That's the contract everything below builds on.

## Asking her to remember

```text
You:    Hey Kenzy, remember that the spare key is under the blue pot
Kenzy:  Okay, I'll remember that.

You:    Hey Kenzy, what do you know about the spare key?
Kenzy:  The spare key is under the blue pot.

You:    Hey Kenzy, forget about the spare key
Kenzy:  Forgotten: the spare key is under the blue pot.
```

Common phrasings that work: *"remember that…"*, *"don't forget that…"*,
*"what do you know / remember about…?"*, *"forget (about)…"*. Anything
fuzzier — "didn't I tell you something about the plumber?" — goes through the
language model, which has the same remember/recall/forget abilities as tools.

Two phrasings are deliberately **not** memory:

- *"Remember **to** take out the trash"* — that's a reminder (a thing that
  should happen at a time), and Kenzy treats it as one. See timers and
  reminders in [Talking to Kenzy](talking-to-kenzy.md).
- A bare *"forget it"* is taken as the colloquial "never mind," not an
  erase request.

## Who can hear a fact back — the three tiers

Every fact has a tier, and the tiers are enforced at answer time:

| Tier | Who hears it back | How a fact gets it |
|---|---|---|
| **Private** | Only the person who stored it | The default — every "remember that…" starts private |
| **About them** | Anyone Kenzy recognizes | The middle tier for facts *about* a person that others may ask ("Alice's birthday is in May") — set it by voice signal or from the People page's per-fact **Edit** |
| **Shared** | The whole household | An explicit signal — *"everyone should know…"*, *"remember for everyone…"* — or *"share that with the house"* afterwards |

```text
You:    Hey Kenzy, everyone should know the wifi password is on the fridge
Kenzy:  Okay, I'll remember that — and everyone can ask me.
```

Demoting works too: *"keep that between us"* makes a fact private again.

The walls hold everywhere: a private fact is never spoken to anyone else,
never used to answer anyone else's question, and never *echoed* — if Kenzy
just told you a private fact, someone else asking seconds later in the same
room doesn't get it replayed from conversation history.

## Secrets — the lockbox

Some things aren't just private, they're *secrets* — codes, passwords,
combinations. Those get different mechanics, not just a stricter rule:

```text
You:    Hey Kenzy, remember this secretly: the safe combo is 33-22-11
Kenzy:  Locked away — only you can ask me for it.

You:    Hey Kenzy, what's the combo for the safe?
Kenzy:  Your safe combo is 33-22-11.
```

A lockbox secret is **encrypted on disk**, **never enters any language
model**, and is **owner-only forever**: no sharing tier exists for secrets.
The dashboard shows a 🔒 label and date; the text appears only on an
explicit **Reveal** click (and hides itself again).

Two honest boundaries, stated plainly:

- **Secrets are never spoken through cloud TTS.** If your speech provider is
  a cloud service (the default OpenAI voice), asking for a secret gets an
  honest deflection to the dashboard instead; the value is spoken
  only when speech is generated on-box (the `kokoro` provider). The
  dashboard's Reveal always works.
- **A backup restores your secrets.** The archive carries the lockbox *and*
  its encryption key by default — a backup's job is to bring everything
  back. Treat it like a password file, or untick "Include the lockbox key"
  in Settings for a shareable archive that carries only unreadable
  ciphertext. (TLS keys always stay out; `.env` API keys stay out
  unless opted in.)

Under the hood each secret is a **key/value pair** — a non-sensitive key
("`shed_key_code`") and an encrypted value ("`8642`"). When you ask, in any
phrasing, the model sees only your keys and answers with a placeholder
(`[[lockbox:shed_key_code]]`); Kenzy fills in the value *after* the model
has finished, deterministically and only for the secret's owner. So the
value never enters a model in either direction — not even the reply that
speaks it — and someone else asking for your key gets nothing.

Secrets **update in place**: tell Kenzy the door code changed and the old
value is replaced under the same key — you'll never get two competing door
codes read back.

You don't have to say "secretly," either: **every new memory is briefly
quarantined** (owner-only, invisible to models) while a classifier judges it.
With a **local model** configured (`memory.classifier_model`) the model
judges **every** write — however a secret is phrased — and pattern matching
is just a fast lane for the obvious cases; a cloud model is never consulted
about secrecy. Without a local model you're down to the patterns alone
(credential words like "password"/"…code" plus a code-shaped value):
obvious secrets still vault and ambiguous ones are held for review on the
People page ("held for review" badge, one-click Release / To-lockbox) — but
a secret phrased outside the patterns will pass as an ordinary private
memory. That's the honest limit of the degraded mode, and it's the main
reason the People page nudges you toward a local classifier model.

## What she remembers on her own

By default, nothing — Kenzy only stores what you explicitly ask her to
remember; there's no ambient transcript-mining. That's a per-person choice
now: each person's page has a **Memory capture** setting — **Explicit** (the
default just described), **Suggest** (she notices a durable fact and asks
aloud — "want me to remember that?" — storing only on your spoken yes), or
**Auto** (she remembers durable personal
facts on her own and always says so; her picks carry an "auto" tag on the
People page, each one a Forget away, and they pass through the same
quarantine-and-classify pipeline as everything else). What she *always* keeps is
short-term conversational context: the last few minutes of back-and-forth in
a room, plus your own recent exchanges across rooms (so a conversation you
started in the kitchen still makes sense from the office). Both expire on
their own and respect the same privacy rules.

## Memory stays tidy by itself

Saying roughly the same thing twice doesn't leave two facts lying around:

- **Moments after each "remember…"**, Kenzy compares the new fact against
  similar ones from the same person at the same tier. Restatements merge;
  corrections supersede ("the plumber is Sam now" retires "the plumber is
  Joe").
- **Private facts don't ride to a cloud model.** If your language model is a
  cloud provider, private-tier facts are withheld from the model's context
  and from consolidation — they still answer by voice (the instant fast
  path needs no model). Configure a local `memory.classifier_model` and the
  merge pass runs on it instead, private facts included — so duplicates
  coalesce without anything leaving the house. Shared and about-them facts,
  being household-visible by design, still inject. (`memory.private_to_cloud:
  true` opts out of the protection.) The People page shows a banner when
  memory is on with no local model configured.
- Superseded facts leave recall instantly but stay on disk for 30 days
  (`memory.superseded_keep_days`) before a maintenance sweep removes them —
  so a wrong merge is recoverable.
- A no-model sweep handles the mechanical rest — exact duplicates, expired
  facts, old tombstones — kicked moments after each new fact settles (the
  hourly run is just a backstop). Every removal is logged.

## Seeing and managing it — the dashboard

The **People** tab is memory's admin surface: each person's page lists what
Kenzy holds for them (with tier, age, and per-fact **Edit** and **Forget** —
Edit changes the wording, the tier, and a retention window: keep forever, or
forget after 30/90/365 days, shown as an "expires in…" badge), the People
page carries **Household memory** and a search across every fact, and each
person's **Privacy & data** section covers the bigger hammers:

- **Export their data** — one downloadable file: their person record, voice
  profile info, every remembered fact, and (by default) their lockbox
  entries — the same access the Reveal button already grants this page.
  Untick "include lockbox secrets" for a shareable file. The "what does
  Kenzy know about me" answer, complete and in writing.
- **Don't remember…** — the per-person opt-out toggle. Kenzy keeps and reads *no*
  facts about them while they stay a fully recognized voice for device
  control and questions. Asking her to remember something gets an honest
  "memory is turned off for you at your request." Turning it on also offers
  to erase what's already stored for them (facts they shared with the house
  stay).
- **Remove completely** — the guest-departure case: erases their facts,
  deletes their voice, removes their record, in one confirmed action.
  Household-shared facts they contributed stay with the house (the gate code
  doesn't leave with the guest).

See [Dashboard → People](dashboard.md#people) for the full tour.

## Where it lives

The ledger is a plain, human-readable text file —
`data/memory/facts.jsonl` in the config home on the LLM service's host, one
fact per line; lockbox secrets live beside it (`lockbox.enc`, encrypted, with
its key in `lockbox.key` — the key is what makes a default backup sensitive).
No database, nothing leaves your machines, and it all rides
[backups](backup-restore.md) automatically. If you're curious, open it in a
text editor; if a fact ever needs surgery, you can fix it there (the format
is tolerant of hand edits — restart `kenzy-llm` afterwards).

Configuration keys (intervals, retention, disabling memory entirely) are in
the [LLM configuration reference](configuration/llm.md).
