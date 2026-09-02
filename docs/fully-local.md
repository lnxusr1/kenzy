# Running Fully Local

Kenzy was architected so that **every stage of the voice pipeline can run on your
own hardware** — nothing spoken in your home has to leave your network.

So why does [Getting Started](getting-started.md) walk you through an OpenAI
key? Because that default is a **pragmatic quick-start, not a philosophy**: one
key, zero model downloads, and it works even when your whole install is a
single Raspberry Pi — which can run everything *except* a capable language
model. It gets you from install to "Hey Kenzy" in minutes so you can feel out
what she can do before investing in hardware.

Running in your own house is the point of Kenzy — and going local is more than
swapping the brain: the ears, the voice, and the skills each have their own
story. This page is the whole recipe, stage by stage — flip everything local,
or mix cloud and local deliberately.

!!! note "This page vs the service pages"
    **This page is the journey**: the order to do things in, what each stage
    costs, and how to verify the result end to end. Each service page —
    [the brain](configuration/llm.md), [her voice](configuration/tts.md),
    [her ears](configuration/stt.md) — has a *Cloud or local?* comparison and a
    tabbed **Set it up** block covering that one service either way, plus the
    full key reference. Come here to decide and to walk it; go there to look
    something up.

## What "fully local" means here

The **voice pipeline** — wake word, speech-to-text, reasoning, text-to-speech,
and speaker identification — runs entirely on your machines. Two honest caveats:

- **Skills that fetch internet content still fetch it.** Weather, news, stocks,
  and web search exist to bring the outside world in; asking for them makes an
  outbound request (with the *query*, never room audio). Disable any of them in
  the dashboard's **Skills** tab if you want zero outbound traffic.
- **First-time setup downloads models once** (`kenzy-setup`, pip installs, the
  first Ollama pull). After that, day-to-day operation needs no internet.

## Stage by stage

| Stage | Local engine | Status |
|---|---|---|
| Wake word | openwakeword on the node | **Always local** — no config needed |
| Speaker ID | SpeechBrain ECAPA on `kenzy-speaker` | **Always local** — no config needed |
| Speech-to-text | faster-whisper on `kenzy-stt` | **Local by default** (`provider: whisper`) |
| Reasoning (LLM) | Ollama / LM Studio via LiteLLM | One config change (below) |
| Text-to-speech | Kokoro on `kenzy-tts` | One extra + one config change (below) |
| Conversations (follow-up mode) | `kenzy-s2s` composing the stages above | **Local engine by default** — its model provider follows the same split (below) |
| Dashboard, node links, HA control, lists | — | Already LAN-only by design |

So on a default install, most stages are already local — only the LLM
and the voice need switching.

### Configuring the brain (the LLM)

On the server (or any box with the horsepower):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:14b        # or another tool-calling-capable model
```

Then in the dashboard, **Fleet → llm**:

```yaml
model: "ollama/qwen2.5:14b"
base_url: "http://127.0.0.1:11434"
```

No API key is needed — Kenzy deliberately sends no cloud credential to a custom
`base_url`. Skill sub-calls (news summaries, the HA resolver) follow the same
model unless you override their per-skill `model`.

!!! warning "The hardware reality"
    This is the one stage where local costs real iron. Kenzy's skills depend on
    **tool calling**, and small models are noticeably worse at it. A ~7–14B
    model on a GPU with 8–16 GB VRAM (or an Apple-silicon Mac) gives a good
    experience; CPU-only LLM inference is technically possible and practically
    frustrating. Whisper and Kokoro, by contrast, are perfectly happy on CPU.

### The voice → Kokoro

On the TTS host:

```bash
sudo apt-get install espeak-ng
pip install 'kenzy[kokoro]'     # in Kenzy's venv (adds Kokoro + PyTorch)
kenzy-setup                     # pre-download the voice weights
```

Then in the dashboard, **Fleet → tts**: set `provider: kokoro` (voice/speed
under the `kokoro:` block). Output format is identical to the cloud provider,
so nothing else changes — except one payoff: local speech is what unlocks
**spoken [lockbox secrets](memory.md#secrets-the-lockbox)** (a cloud voice
gets a deflection to the dashboard instead).
See [TTS Configuration](configuration/tts.md#kokoro-provider).

### Speech-to-text — verify, don't change

`provider: whisper` is the default; just confirm nobody switched it to
`openai` (**Fleet → stt**). Pick the model size for your CPU in the same
place ([guide](configuration/stt.md#model-size-guide)).

### Skills housekeeping

In the dashboard's **Skills** tab, for a strictly-no-outbound setup disable
`web_search`, `get_news`/`get_news_article`, `get_current_weather`/`get_forecast`,
and `get_stock_info` — or keep them and accept those queries going out. If you
want web search without a cloud dependency, point it at a self-hosted
[SearXNG](configuration/llm.md) (`skills.web_search.provider: searxng`).

## Verifying it

The pull-the-plug test, in order of what it proves:

1. Disconnect your router's WAN (leave the LAN up).
2. **"Hey Kenzie… what time is it?"** — proves wake word → STT → fast path →
   TTS, end to end, no internet.
3. **"Turn on the kitchen lights."** — adds Home Assistant control (all LAN).
4. **Ask something open-ended** ("tell me a joke") — proves the local LLM path.
5. Check the dashboard's **Activity** tab: every interaction should show
   normal latencies, and the fleet view all-green, with the WAN still dark.

If step 2 works but 4 doesn't, the LLM config is the issue; if nothing speaks,
check TTS — the [Troubleshooting](troubleshooting.md) page takes it from there.
And if a stage does fail, Kenzy now says so out loud rather than going silent
(the pre-recorded failure cue — `sound_error` in a node's settings).

## The hybrid option: cloud primary, local fallback

You don't have to choose. Keep the cloud providers as your day-to-day setup and
let the local engines catch failures automatically — each retry is silent, and
only a double failure reaches the error cue:

- **LLM:** set [`fallback.model` / `fallback.base_url`](configuration/llm.md)
  to a local Ollama model — an internet outage degrades to a dumber-but-present
  assistant instead of an apology.
- **Memory:** set [`memory.classifier_model`](configuration/llm.md) to a local
  Ollama model — the third local-model lever. With a cloud brain it's what
  classifies secret-shaped memories automatically (a cloud model is never
  consulted about secrecy) *and* runs the duplicate-merging pass over
  private-tier facts, which are otherwise withheld from cloud consolidation
  entirely.
- **TTS:** `openai.fallback: true` (the default) uses local Kokoro when the
  cloud fails — if you've installed the `kokoro` extra.
- **STT:** `openai.fallback: true` (the default) retries with local whisper —
  relevant only if you switched STT to the cloud provider.

## The conversation engine

[Conversation mode](talking-to-kenzy.md#conversation-mode-experimental)'s engine,
`kenzy-s2s`, is **local by default** and composes the stages above — so going
local for STT, TTS, and the model makes conversations fully local too. Its
model provider is its own setting (it may legitimately differ from the classic
pipeline's — pick for realtime latency): point
[`provider.base_url`](configuration/s2s.md) at a local vLLM or llama.cpp
server and conversations never leave the house. A validated local recipe —
model, serving command, measured latencies — is planned for this page once
benchmarking on reference hardware completes. The OpenAI Realtime cloud
engine (`s2s.profile: openai-realtime`) is the deliberate opposite of this
page — see [its caveat](configuration/s2s.md#using-a-cloud-realtime-engine-instead).
