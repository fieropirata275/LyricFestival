<p align="center">
  <img src="assets/banner.svg" alt="LyricFestival" width="100%">
</p>

<p align="center">
  <img alt="Platform" src="https://img.shields.io/badge/host-Windows%2010%20%2F%2011-8b5cf6?style=for-the-badge">
  <img alt="Version" src="https://img.shields.io/badge/version-v18-ec4899?style=for-the-badge">
  <img alt="Visual AI" src="https://img.shields.io/badge/visual%20AI-none%20%C2%B7%20deterministic-22d3ee?style=for-the-badge">
  <img alt="Transport" src="https://img.shields.io/badge/transport-WebSocket%20%C2%B7%20WebRTC-a78bfa?style=for-the-badge">
</p>

<p align="center">
  <b>Real-time synced lyrics, beat-reactive visuals and LAN "rave companions" for every screen in the room.</b><br>
  Whatever plays on the host PC is turned into a live lyric show that phones, tablets and TVs join by scanning a QR code.
</p>

---

## Contents

- [Highlights](#highlights)
- [Architecture](#architecture)
- [Minimum hardware requirements](#minimum-hardware-requirements)
- [Semantic dictionary (customizable)](#semantic-dictionary-customizable)
- [Timing and sync](#timing-and-sync)
- [Beat Oracle](#beat-oracle)
- [Host vs Rave Companions](#host-vs-rave-companions)
- [Private headphone audio](#private-headphone-audio)
- [Device-specific companions](#device-specific-companions)
- [Samsung TV adaptive profiles](#samsung-tv-adaptive-profiles)
- [Changelog](#changelog)

---

## Highlights

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="assets/icons/lexicon.svg" width="26" align="left">&nbsp;<b>Deterministic semantic lexicon</b><br>
      13,394 words across 79 categories drive the visuals. No AI model, zero model latency, and fully editable.
    </td>
    <td width="50%" valign="top">
      <img src="assets/icons/target.svg" width="26" align="left">&nbsp;<b>Word-level timing</b><br>
      Musixmatch RichSync as the authority, LRCLIB and local alignment as fallbacks, with Whisper-powered manual resync.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <img src="assets/icons/pulse.svg" width="26" align="left">&nbsp;<b>Beat Oracle</b><br>
      Server-side BPM, phase and 4/4 bar tracking, broadcast as one shared beat clock to every client.
    </td>
    <td valign="top">
      <img src="assets/icons/users.svg" width="26" align="left">&nbsp;<b>Rave Companions</b><br>
      Any LAN device joins via QR or <code>lyricfestival.local</code> and gets RAVE or KARAOKE mode.
    </td>
  </tr>
  <tr>
    <td valign="top">
      <img src="assets/icons/headphones.svg" width="26" align="left">&nbsp;<b>Private headphone audio</b><br>
      48 kHz stereo over WebRTC/Opus straight to a phone's headphones, with no STUN/TURN needed on the LAN.
    </td>
    <td valign="top">
      <img src="assets/icons/tv.svg" width="26" align="left">&nbsp;<b>Adaptive TV rendering</b><br>
      Samsung Tizen TVs are benchmarked and auto-assigned STANDARD, LITE or ULTRA-LITE.
    </td>
  </tr>
</table>

---

## Architecture

<p align="center">
  <img src="assets/architecture.svg" alt="LyricFestival architecture" width="100%">
</p>

The core principle: **every layer is isolated.** Visual semantics, beat detection and private audio never touch lyric timing, and a failure in one of them cannot disturb the others.

<details>
<summary><b>Preserved core components</b></summary>

- Media Oracle (including the stale-SMTC fix)
- Musixmatch authoritative word timing
- LRCLIB timing
- Local alignment fallback
- WebSocket transport
- LAN + mDNS
- QR join card
- Visual animation engine

</details>

<details>
<summary><b>Removed in v12 (local AI visual director)</b></summary>

- llmster startup and `lms` commands
- Local LLM HTTP calls
- `/api/visual-ai` endpoint
- Visual AI cache
- LLMSTER badge

</details>

---

## Minimum hardware requirements

<img src="assets/icons/cpu.svg" width="24" align="left">&nbsp;LyricFestival is **not a lightweight app.** The host PC runs real-time audio capture, deep Whisper passes, beat analysis, a WebSocket hub, one WebRTC stream per companion and the full visual stage, all at the same time. A reasonably powerful machine is required for a smooth experience.

| Component | Minimum | Recommended |
|---|---|---|
| **OS** | Windows 10 64-bit (22H2) | Windows 11 64-bit |
| **CPU** | 6 cores / 12 threads, e.g. AMD Ryzen 5 5600 or Intel Core i5-12400 | 8 cores / 16 threads, e.g. AMD Ryzen 7 7700 or Intel Core i7-13700 |
| **RAM** | 16 GB DDR4 | 32 GB DDR4/DDR5 |
| **GPU** | NVIDIA RTX 3060 (8 GB VRAM) with CUDA | NVIDIA RTX 4070 (12 GB VRAM) or better |
| **Storage** | SSD with 10 GB free | NVMe SSD with 20 GB free |
| **Network** | Gigabit Ethernet or Wi-Fi 5 (802.11ac) | Host on Gigabit Ethernet + Wi-Fi 6 router for companions |
| **Audio** | Any WASAPI-compatible output device | Same |

> [!NOTE]
> The GPU mainly accelerates the Whisper resync passes. It can run on CPU only, but FORCE RESYNC and SYNC MUSIXMATCH will be noticeably slower.

> [!TIP]
> Each private-audio companion opens its own WebRTC peer connection. With many guests at once, a wired host and a good Wi-Fi 6 router make a bigger difference than extra CPU.

---

## Semantic dictionary (customizable)

<img src="assets/icons/lexicon.svg" width="24" align="left">&nbsp;The visualizer reacts to lyrics through a **static, deterministic dictionary**: every word it recognizes is mapped to a semantic category, and each category drives a specific palette, effect and animation style. The same word always produces the same visual reaction.

**File:** `app/static/semantic_lexicon.js`

| Stat | Value |
|---|---:|
| Categories | 79 |
| Unique exact words / variants | 13,394 |
| Phrase patterns | 375 |
| Root / stem rules | 1,257 |

### <img src="assets/icons/edit.svg" width="22" align="center"> Make it yours

The dictionary is plain JavaScript and **meant to be edited.** Whoever downloads the project can tune it to their own music, language or event:

| | Action | Example use |
|:-:|---|---|
| <img src="assets/icons/plus.svg" width="20"> | **Add words** to an existing category | Local slang, artist names, words in your language |
| <img src="assets/icons/minus.svg" width="20"> | **Remove words** from a category | Stop the visuals reacting to words you don't want highlighted |
| <img src="assets/icons/edit.svg" width="20"> | **Move words** between categories | Change which visual style a word triggers |
| <img src="assets/icons/trash.svg" width="20"> | **Remove whole categories** | e.g. drop profanity, drugs or weapons for a family-friendly event |

Open `app/static/semantic_lexicon.js`, find the category you want to change and add or delete entries in its word list. Keep the existing format of the file, save it and reload the page. Nothing needs recompiling, and **timing is never affected**, since the lexicon only controls how words look, not when they appear.

> [!TIP]
> Words are normalized before matching, and inflections, phrases and root/stem rules are checked afterwards. You usually only need to add the base form of a word.

### Coverage

Ordinary English plus large Spanish/Portuguese coverage and an extensive lyric/slang vocabulary:

<table>
  <tr>
    <td valign="top">
      <b>Feelings & people</b><br>
      emotions · relationships · identity / pronouns · family · friendship · betrayal · body / sex
    </td>
    <td valign="top">
      <b>Language</b><br>
      ordinary verbs · questions · negation / affirmation · internet slang · ad-libs · profanity
    </td>
  </tr>
  <tr>
    <td valign="top">
      <b>Lifestyle</b><br>
      money / wealth · luxury / drip / flex · cars · work / grind · fame / social media · phones / internet · music / dance · food
    </td>
    <td valign="top">
      <b>Street</b><br>
      trap / drill slang · drugs / smoke / alcohol · weapons / crime / police
    </td>
  </tr>
  <tr>
    <td valign="top">
      <b>World</b><br>
      weather · nature · water · fire · ice · travel · home · city · colors
    </td>
    <td valign="top">
      <b>Beyond</b><br>
      time / memory · movement / direction · space / cosmic · light / dark · religion / supernatural
    </td>
  </tr>
</table>

### Matching pipeline

```
lyric word
  │
  ├─ 1. normalized exact word
  ├─ 2. simple base / inflection form
  ├─ 3. phrase context
  ├─ 4. root / stem rules
  ├─ 5. dedicated slang heuristics
  └─ 6. deterministic contextual fallback
  │
  ▼
semantic category → palette · effects · animation
```

---

## Timing and sync

Two manual sync buttons live in the lower-right corner of the host screen. **Only one is enabled at a time**, depending on the active lyric provider.

| Provider active | FORCE RESYNC | SYNC MUSIXMATCH |
|---|:-:|:-:|
| Musixmatch RichSync (word-level) | <img src="assets/icons/x-red.svg" width="18"> | <img src="assets/icons/check-green.svg" width="18"> |
| LRCLIB / local alignment | <img src="assets/icons/check-green.svg" width="18"> | <img src="assets/icons/x-red.svg" width="18"> |

### <img src="assets/icons/bolt.svg" width="22" align="center"> FORCE RESYNC &nbsp;<sub>v13</sub>

For LRCLIB / locally aligned lyrics that have drifted.

1. Captures the last ~8 s of WASAPI loopback audio.
2. Runs a deep Whisper pass immediately.
3. Searches a much wider lyric-word region than the automatic background pass.
4. Sequence-matches heard words against the lyrics.
5. Performs a robust affine fit from lyric timestamps to real vocal timing.
6. Replaces the local timing offset/speed immediately.
7. Purges stale learned word timings around the current playback position.
8. Commits strong newly observed word timestamps.
9. Pushes the corrected timing to every connected LAN client.

The button shows *scanning*, *locked* and *failed* states. Provider-authoritative timing is never overwritten.

### <img src="assets/icons/target.svg" width="22" align="center"> SYNC MUSIXMATCH &nbsp;<sub>v14</sub>

Calibrates Musixmatch RichSync **without rewriting its timestamps**.

1. Listens to ~8 s of recent WASAPI loopback audio.
2. Runs a deep Whisper pass.
3. Matches heard words against the provider wordmap.
4. Computes a robust global audio-vs-provider offset.
5. Stores it separately as `provider_offset_ms`.
6. The browser evaluates each word as `provider_timestamp + provider_offset_ms`.

Musixmatch's internal word spacing is preserved; the whole timeline simply shifts left or right.

<img src="assets/icons/shield.svg" width="20" align="left">&nbsp;**Safety rules**
- Correction clamped to **±4000 ms**
- Repeated-phrase outliers rejected
- At least **two strong matches** required
- The original wordmap is never modified

---

## Beat Oracle

<img src="assets/icons/pulse.svg" width="24" align="left">&nbsp;`app/beat_oracle.py` reuses the host's existing WASAPI loopback capture. Guests **never** request microphone or audio permissions.

**Pipeline:** ~8 s audio window → onset-energy envelope → BPM via autocorrelation → beat phase from recent transients → continuous tempo/phase smoothing → beats scheduled against the Media Oracle timeline → grouped into an estimated 4/4 bar → broadcast over WebSocket.

<table>
  <tr>
    <th><code>beat_state</code></th>
    <th><code>beat</code></th>
  </tr>
  <tr>
    <td valign="top">BPM · confidence · period · estimated meter · current bar/beat</td>
    <td valign="top">beat index · bar beat 1–4 · downbeat flag · BPM · intensity · media timeline timestamp</td>
  </tr>
</table>

The host analyzes audio **once**, and every companion follows the same server-side beat clock, so phones never drift out of time with each other.

> [!IMPORTANT]
> Beat detection is a visual layer only. It never modifies Media Oracle, Musixmatch, LRCLIB, Whisper alignment or lyric timing.

---

## Host vs Rave Companions

<img src="assets/icons/network.svg" width="24" align="left">&nbsp;The role is decided by **how the page is opened.**

| | Access via | Gets |
|---|---|---|
| **Host** | `localhost` · `127.0.0.1` · `::1` | Full UI: lyric visualizer, QR join card, provider status/debug badges, FORCE RESYNC, SYNC MUSIXMATCH, beat-reactive background |
| **Rave Companion** | `lyricfestival.local` · the PC's LAN IP · any other hostname | Clean guest UI with no controls, QR or debug badges |

Companions choose between two modes:

- **RAVE**: lyrics hidden; large beat-reactive orb with rings, beams, particles and the semantic palette.
- **KARAOKE**: synchronized lyrics stay visible while beat effects continue in the background.

---

## Private headphone audio

<img src="assets/icons/headphones.svg" width="24" align="left">&nbsp;**PRIVATE AUDIO** (companions only, never on localhost) streams the host's audio to a phone's headphones.

```
Spotify / Windows output
  → dedicated WASAPI loopback (48 kHz stereo)
  → WebRTC
  → Opus
  → mobile browser
  → headphones
```

- The existing 16 kHz mono capture for Whisper and Beat Oracle is untouched; a second capture handles music-quality stereo.
- 20 ms blocks; consumers that fall behind jump to the live edge instead of buffering old music.
- Host ICE candidates only (`iceServers: []`), so no public STUN/TURN is needed on the same LAN.
- One WebRTC peer connection per companion, closed when the page is left.
- Audio starts only after a tap (mobile autoplay policy), and a mute/unmute control then appears.

> [!NOTE]
> Literal zero latency is physically impossible. Total delay depends on Wi-Fi quality, WASAPI buffering, Opus/WebRTC packetization, the phone's jitter buffer and, above all, **Bluetooth**. Wired headphones or the phone speaker will feel noticeably tighter.

The audio path is fully separate from lyric sync and the Beat Oracle, so a WebRTC failure cannot disturb the main show.

---

## Device-specific companions

<img src="assets/icons/devices.svg" width="24" align="left">&nbsp;Remote clients are classified automatically:

| Device | Layout |
|---|---|
| **Phone** | Portrait-first with landscape fallback, larger center words, tight beat rings, bottom audio controls |
| **Tablet** | Wider stage, larger ring system, roomier karaoke geometry |
| **Desktop / TV** | Large stage composition |

RAVE mode includes animated semantic backdrops, vignette, film/noise texture, a radial beam fan, triple beat rings, device-scaled particle bursts, downbeat choreography and the current lyric word as giant center typography.

---

## Samsung TV adaptive profiles

<img src="assets/icons/tv.svg" width="24" align="left">&nbsp;Samsung Smart TV browsers are detected by their SMART-TV / Tizen user agent. The client then tries (best-effort, since ordinary websites aren't guaranteed access to privileged Tizen APIs) to read the Tizen version, model, firmware, display data, memory status, `hardwareConcurrency` and `deviceMemory`, and runs a **~450 ms compositor benchmark**.

| Profile | Target | What changes |
|---|---|---|
| **STANDARD** | Modern, high-performing TVs | Most cinematic effects kept; lighter film/noise |
| **LITE** | Mid-range / older Tizen | No heavy blur, no shadow-heavy rings, no beat grid, fewer particles |
| **ULTRA-LITE** | Tizen 4 and older / weak engines | Static radial background, one simple ring, large typography, simplified pulse. No noise, beams, particles, grid, `backdrop-filter` or text shadows |

A badge shows the assigned profile, approximate model year and measured FPS.

---

## Changelog

| Version | Change |
|---|---|
| **v18** | Samsung TV adaptive companion (STANDARD / LITE / ULTRA-LITE) |
| **v17** | Cinematic device-specific companions (phone / tablet / desktop-TV) |
| **v16** | Private headphone audio over WebRTC/Opus |
| **v15** | Beat Oracle + Rave Companions (host vs guest split) |
| **v14** | SYNC MUSIXMATCH manual calibration (`provider_offset_ms`) |
| **v13** | FORCE RESYNC for LRCLIB / local timing |
| **v12** | Local AI visual director removed; mega deterministic semantic lexicon |

<p align="center"><sub>Every presentation-layer release leaves Media Oracle, lyric timing, Beat Oracle and the server core untouched.</sub></p>
