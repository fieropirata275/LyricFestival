# LyricFestival Rewrite v12 — MEGA DETERMINISTIC SEMANTIC LEXICON

This build removes the local AI / llmster visual director completely.

## Preserved

- Media Oracle / stale-SMTC fix
- Musixmatch authoritative word timing
- LRCLIB timing
- local alignment fallback
- WebSocket transport
- LAN + mDNS
- QR join card
- visual animation engine

## Removed

- llmster startup
- `lms` commands
- local LLM HTTP calls
- `/api/visual-ai`
- visual AI cache
- LLMSTER badge

There is now zero model latency in the visual path.

## Semantic dictionary

Generated static lexicon stats:

- Categories: 79
- Unique exact words/variants: 13394
- Phrase patterns: 375
- Root/stem rules: 1257

The lexicon is located at:

`app/static/semantic_lexicon.js`

It covers ordinary English plus large Spanish/Portuguese coverage and a very
large lyric/slang vocabulary including:

- emotions and relationships
- ordinary verbs
- identity/pronouns
- questions/negation/affirmation
- time/memory
- movement/direction
- body/sex
- family/friendship/betrayal
- money/wealth
- luxury/drip/flex
- cars
- work/grind
- fame/social media
- phones/internet
- music/dance
- street/trap/drill slang
- profanity
- drugs/smoke/alcohol
- weapons/crime/police
- weather/nature/water/fire/ice
- space/cosmic/light/dark
- religion/supernatural
- travel/home/city
- food
- colors
- contemporary internet slang
- ad-libs

Matching pipeline:

1. normalized exact word
2. simple base/inflection form
3. phrase context
4. root/stem rules
5. dedicated slang heuristics
6. deterministic contextual fallback

The visual dictionary is fully deterministic and never modifies timing.


## v13 — Manual FORCE RESYNC

A new `⚡ FORCE RESYNC` button is available in the lower-right corner.

It is intended only for LRCLIB / locally aligned lyrics when the user notices
the lyrics have drifted.

When pressed:

1. Captures roughly the last 8 seconds of WASAPI loopback audio.
2. Runs a deep Whisper pass immediately.
3. Searches a much wider lyric-word region than the automatic background pass.
4. Sequence-matches the heard words against the lyrics.
5. Performs a robust affine fit from lyric timestamps to actual vocal timing.
6. Replaces the local timing offset/speed immediately.
7. Purges stale learned exact-word timings around the current playback region.
8. Commits strong newly observed word timestamps.
9. Pushes the corrected timing to every connected LAN client.

Musixmatch/provider-authoritative word timing is never overwritten. Pressing
FORCE RESYNC in that mode simply confirms provider lock.

The button has scanning / locked / failed visual effects, but the rest of the
visual semantics and media timing architecture remain unchanged.


## v14 — Manual Musixmatch calibration

A second button is available:

`🎯 SYNC MUSIXMATCH`

It is enabled only while an authoritative word-level provider such as
Musixmatch RichSync is active.

Unlike FORCE RESYNC, this button does NOT rewrite provider timestamps.

It:

1. Listens to about 8 seconds of recent WASAPI loopback audio.
2. Runs a deep Whisper pass.
3. Matches heard words against the provider wordmap.
4. Calculates a robust global audio-vs-provider offset.
5. Stores that offset separately as `provider_offset_ms`.
6. The browser evaluates provider word timestamps as:
   `provider_timestamp + provider_offset_ms`.

This preserves Musixmatch's exact internal word spacing and only moves the
whole RichSync timeline left or right to match the actual audio.

Safety:
- correction is clamped to ±4000 ms;
- repeated-phrase outliers are rejected;
- at least two strong matches are required;
- original Musixmatch wordmap is never modified.

When Musixmatch is active, FORCE RESYNC is disabled and SYNC MUSIXMATCH is
enabled. With LRCLIB/local timing the opposite is true.


## v15 — Beat Oracle + Rave Companions

### Host versus companion

LyricFestival now distinguishes the local control/display host from LAN guests.

Host:
- `localhost`
- `127.0.0.1`
- `::1`

The host keeps the full interface:
- lyric visualizer
- QR join card
- provider status/debug badges
- FORCE RESYNC
- SYNC MUSIXMATCH
- beat-reactive background

Any access through:
- `lyricfestival.local`
- the PC's LAN IP
- another hostname

becomes a **Rave Companion**.

Companions do not see the host controls, QR, debug/status badges or sync
buttons. They get only two modes:

`RAVE`
- lyrics are hidden
- large beat-reactive rave orb
- beat rings, beams, particles, semantic palette

`KARAOKE`
- synchronized lyrics remain visible
- beat effects continue in the background
- no host/admin controls

### Beat Oracle

The server adds `app/beat_oracle.py`.

It reuses the host's existing WASAPI loopback capture. Guests never request
microphone/audio permissions.

The Beat Oracle:
- analyzes ~8 seconds of recent audio
- builds an onset-energy envelope
- estimates BPM with autocorrelation
- estimates beat phase from recent transients
- continuously smooths tempo/phase
- schedules predicted beats against the Media Oracle timeline
- groups beats into an estimated 4/4 bar
- broadcasts each beat to all WebSocket clients

Messages:

`beat_state`
- BPM
- confidence
- period
- estimated meter
- current bar/beat

`beat`
- beat index
- bar beat 1..4
- downbeat boolean
- BPM
- intensity
- media timeline timestamp

The host analyzes audio once. Every companion receives the same server-side
beat clock, avoiding independent phone timing drift.

Important: beat detection is a visual/music-reactive layer only. It does not
modify Media Oracle, Musixmatch, LRCLIB, Whisper alignment or lyric timing.


## v16 — Private Headphone Audio over WebRTC

Rave companions now include:

`🎧 PRIVATE AUDIO`

This is intentionally available only on non-localhost companion devices.

Architecture:

`Spotify / Windows output`
→ dedicated WASAPI loopback capture at 48 kHz stereo
→ WebRTC
→ Opus audio transport
→ mobile browser
→ headphones

Important implementation details:

- The existing Whisper/Beat Oracle 16 kHz mono capture is untouched.
- A second capture is used for music-quality stereo.
- Blocks are 20 ms.
- Consumers that fall behind jump back to the live edge instead of buffering
  old music.
- LAN WebRTC uses host ICE candidates only (`iceServers: []`), so no public
  STUN/TURN server is required for normal same-LAN use.
- Each companion gets its own WebRTC peer connection.
- Audio starts only after tapping the button, which is required by mobile
  browser autoplay policies.
- A mute/unmute button appears after connection.
- Closing/leaving the page closes the peer connection.

### Latency

Literal zero latency is physically impossible.

This mode is designed for the lowest practical latency available from a normal
mobile browser. Typical total latency depends on:
- Wi-Fi quality
- Windows/WASAPI buffering
- Opus/WebRTC packetization
- phone browser jitter buffer
- Bluetooth headphone buffering

Wired headphones or phone speakers will generally have lower latency than
Bluetooth headphones.

The audio transport is fully separate from Media Oracle, lyric synchronization
and Beat Oracle, so a WebRTC failure cannot disturb the main rave.


## v17 — Cinematic device-specific companions

Remote clients are explicitly classified as PHONE, TABLET or DESKTOP/TV.
PHONE is portrait-first with dedicated landscape fallback, larger center words,
tight beat rings and bottom private-audio controls. TABLET gets a wider stage,
larger ring system and roomier karaoke geometry. Remote desktop/TV gets a large
stage composition.

RAVE mode now has animated semantic backdrops, vignette, film/noise texture,
radial beam fan, triple beat rings, device-scaled particle bursts, downbeat
choreography and the current lyric word as giant center typography.

Presentation only: Media Oracle, Beat Oracle, Musixmatch/LRCLIB, Whisper
resync and private WebRTC audio are unchanged.


## v18 — Samsung TV Adaptive Companion

Samsung Smart TV browsers are detected using their documented SMART-TV / Tizen
user-agent patterns.

The client then attempts to retrieve:
- Tizen platform version
- ProductInfo model
- firmware
- SystemInfo display data
- memory status
- browser hardwareConcurrency / deviceMemory when exposed

The Samsung browser does not guarantee that privileged Tizen ProductInfo or
SystemInfo APIs are exposed to an ordinary website, so capability retrieval is
best-effort.

A ~450 ms compositor benchmark is also performed. The TV is automatically
assigned one of:

### STANDARD
For modern/high-performing Samsung TVs.
Keeps most cinematic effects with reduced film/noise overhead.

### LITE
For mid-range / older Tizen TVs.
Removes expensive blur, box-shadow-heavy rings, beat grid and reduces
particles.

### ULTRA-LITE
For old/weak engines, including Tizen 4 and older.
Uses:
- static semantic radial background
- one simple beat ring
- large lyric typography
- no film noise
- no radial beam fan
- no particle DOM layer
- no beat grid
- no backdrop-filter
- no expensive text shadows
- simplified beat pulse

The profile badge shows Samsung TV profile, approximate model year and measured
benchmark FPS.

This affects only rendering. Media Oracle, lyric timing, Beat Oracle and the
server remain unchanged.
