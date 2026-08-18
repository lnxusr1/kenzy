#!/bin/bash
# Paired-wake trial driver for the co-audible lab rig.
#
# Plays wake (+ optional command) clips through the talker speaker and — the
# part that makes trials VALID — waits between trials until every listening
# node has been journal-quiet for QUIET_S seconds. Two lessons are encoded
# here, both paid for in invalid data:
#
#   * Fixed sleeps are never enough. The pipeline can take ~25 s to answer,
#     and there is a latency HOLE between capture-end and the answer starting
#     where everything LOOKS quiet — a trial started there measures the
#     previous trial's answers. Poll the nodes, don't guess.
#   * The BY Y02 talker sleep-clips its first ~0.5 s after ~20 s idle — it
#     eats "Hey Ken—". Warmup silence plays before EVERY clip burst.
#
# Config via environment (defaults = the lab as of 2026-08):
#   TALKER_DEV   aplay device            (default plughw:0,0 — check `aplay -l`,
#                                         the Y02's card number moves between boots)
#   NODES        space-separated node hosts to poll   (default "pi-a.lan pi-b.lan")
#   WAKE_CLIP    wake-word wav           (default: cached "Hey Kenzy!" synth)
#   CMD_CLIP     command wav, "" = wake-only trials   (default: "What time is it?")
#   TRIALS       how many                (default 4)
#   QUIET_S      journal-quiet threshold (default 20)
#   GAP_S        wake→command gap        (default 3; rig lore: 2 clipped onsets)
#
# Clips come from voice_probe's cache (synthesize with:
#   python -c "import voice_probe as vp; print(vp.synth('TEXT', vp.VOICE))" )
#
# Evidence comes out of the SERVER journal:
#   journalctl --user -u kenzy-server | grep -E "wake_pending|arbitration|engagement"
#
# For scripted who-hears-what (force one node deaf, force-wake another) compose
# with scripts/dash_mutate.py.

set -u
C=~/.cache/kenzy-voice-probe
TALKER_DEV=${TALKER_DEV:-plughw:0,0}
NODES=${NODES:-"pi-a.lan pi-b.lan"}
WAKE_CLIP=${WAKE_CLIP:-$C/am_adam_df163e27663f851e.wav}
CMD_CLIP=${CMD_CLIP:-$C/am_adam_1218d4b9ff935ad1.wav}
TRIALS=${TRIALS:-4}
QUIET_S=${QUIET_S:-20}
GAP_S=${GAP_S:-3}

[ -f "$WAKE_CLIP" ] || { echo "wake clip missing: $WAKE_CLIP" >&2; exit 1; }

wait_quiet() {
  for _ in $(seq 1 36); do   # up to ~3 min
    local now quiet host last
    now=$(date +%s); quiet=1
    for host in $NODES; do
      last=$(ssh -o ConnectTimeout=3 "$host" \
        "journalctl -q --no-pager --user-unit kenzy-node -n 1 -o short-unix 2>/dev/null | cut -d. -f1")
      [ -n "$last" ] && [ $((now - last)) -lt "$QUIET_S" ] && quiet=0
    done
    [ "$quiet" -eq 1 ] && return 0
    sleep 5
  done
  echo "  (gave up waiting for quiet)" >&2
}

play() {  # busy-retry: a desktop's audio server can hold the device briefly
  for _ in 1 2 3; do
    aplay -q -D "$TALKER_DEV" "$1" 2>/dev/null && return 0
    sleep 0.5
  done
  echo "  (aplay failed 3x: $1)" >&2
  return 1
}

for i in $(seq 1 "$TRIALS"); do
  wait_quiet
  echo "trial $i $(date +%H:%M:%S)"
  play "$C/warmup_silence.wav"   # EVERY burst: the talker sleep-clips after idle
  play "$WAKE_CLIP"
  if [ -n "$CMD_CLIP" ]; then
    sleep "$GAP_S"
    play "$CMD_CLIP"
  fi
  sleep 15   # let the exchange begin before polling for its end
done
wait_quiet
echo "done $(date +%H:%M:%S)"
