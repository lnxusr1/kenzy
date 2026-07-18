# Memory

Kenzy can remember things — per person. "Hey Kenzy, remember that the gate
code is 4312" stores a fact; "what's the gate code?" gets it back. Facts
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
| **About them** | Anyone Kenzy recognizes | The middle tier for facts *about* a person that others may ask ("Nicki's birthday is in May") — settable via the memory API today; dashboard re-tiering arrives with the hardening phase |
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

## What she remembers on her own

Nothing, in this release. Kenzy only stores what you explicitly ask her to
remember — there's no ambient transcript-mining. What she *does* keep is
short-term conversational context: the last few minutes of back-and-forth in
a room, plus your own recent exchanges across rooms (so a conversation you
started in the kitchen still makes sense from the office). Both expire on
their own and respect the same privacy rules.

## Memory stays tidy by itself

Saying roughly the same thing twice doesn't leave two facts lying around:

- **Moments after each "remember…"**, Kenzy compares the new fact against
  similar ones from the same person at the same tier — using the language
  model *you* configured, so facts never leave your setup on account of
  memory. Restatements merge; corrections supersede ("the plumber is Sam
  now" retires "the plumber is Joe").
- Superseded facts leave recall instantly but stay on disk for 30 days
  (`memory.superseded_keep_days`) before a maintenance sweep removes them —
  so a wrong merge is recoverable.
- An hourly no-model sweep handles the mechanical rest: exact duplicates,
  expired facts, old tombstones. Every removal is logged.

## Seeing and managing it — the dashboard

The **People** tab is memory's admin surface: each person's page lists what
Kenzy holds for them (with tier, age, and per-fact **Forget**), the People
page carries **Household memory** and a search across every fact, and each
person's **Privacy & data** section covers the bigger hammers:

- **Export their data** — one downloadable file: their person record, voice
  profile info, and every remembered fact. The "what does Kenzy know about
  me" answer, in writing.
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
fact per line. No database, nothing leaves your machines, and it rides
[backups](backup-restore.md) automatically. If you're curious, open it in a
text editor; if a fact ever needs surgery, you can fix it there (the format
is tolerant of hand edits — restart `kenzy-llm` afterwards).

Configuration keys (intervals, retention, disabling memory entirely) are in
the [LLM configuration reference](configuration/llm.md).
