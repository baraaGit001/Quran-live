# quran-live

24/7 YouTube Live streaming of Quran recitation with the verses shown on screen,
sized for a 2-OCPU Oracle Ampere A1 server that also hosts other applications.

## Content rights

This system ships **no audio and no visuals**. You supply them.

Recitations, background footage, photographs and fonts are all copyrightable.
Before broadcasting, confirm you have the right to do so — a public-domain or
explicitly licensed recording, footage you shot or licensed, and a font whose
licence permits embedding in video. Fonts marked "free for personal use" are
generally **not** licensed for a public broadcast, and font aggregator sites
frequently host relabelled commercial fonts whose stated licence is not the real
one. A strike lands on your channel, not on this software. Nothing here
downloads content for you, by design.

The Quran text itself comes from [Tanzil](https://tanzil.net) and is fetched by
`scripts/fetch-quran-text.sh` into `data/`.

---

## Why this architecture

The server has **no hardware video encoder** — its GPU is `virtio-pci`, a
paravirtual display adapter with no encode engine — so every H.264 frame would
be produced by x264 on the same two cores that serve the web apps.

Showing the verse being recited means the picture changes during playback, which
normally forces a live encode:

| Approach | CPU, 5 channels @720p30 | Fits 2 cores? |
|---|---|---|
| Live encode, one ffmpeg per channel | ~2.5–4.0 cores | No |
| Shared compositor, still 5 encoded outputs | ~2.3–3.8 cores | No |
| **Pre-render each track once, then stream-copy** | **~0.015 core** | **Yes** |

So each recitation is rendered **once** into a finished track — background fitted,
verses burned in at their timings, audio muxed — and remuxed to MPEG-TS. MPEG-TS
concatenates by appending bytes, so the live stream is one long-lived ffmpeg
reading a pipe that the streamer keeps writing tracks into:

```
track1.ts + track2.ts + ... → pipe → ffmpeg -c copy → RTMP → YouTube
```

Nothing is encoded while streaming and there is no reconnect between tracks.

**Measured on this server:** streaming ffmpeg uses **0.3% of one core and 18 MB
RSS** per channel. Five channels ≈ 1.5% of one core.

The one-off cost is real: rendering a track takes roughly as long as the
recitation plays (~1.2× on this box). `./live build` does that ahead of time, and
the streamer will never render while live — it plays only what is already
finished, and prepares the rest on a background thread.

### Verse timings

An mp3 carries no timing metadata, so `./live sync` infers ayah boundaries from
the pauses in the recitation (ffmpeg `silencedetect`), then groups the segments
onto ayahs using the length of each verse's text as the guide — a long verse
takes longer to recite. That is far more accurate than merging on pause length,
which on mujawwad recitation hands a two-word verse half a minute.

**It is a draft, not an answer.** Mujawwad repeats verses, and nothing based on
silence can tell a repeat from a new verse. Every result reports a confidence
level and flags suspect ayahs; the output is editable JSON. Verify before
broadcasting — a mistimed verse is worse than no verse.

For exact timings, use **per-ayah audio files** (one file per verse) instead:
then each file *is* one ayah and no inference is needed.

### Arabic text

`ffmpeg drawtext` renders Arabic wrongly — it draws code points in order with no
shaping and no bidi, producing disconnected, backwards letters. Captions are
rendered through **Pango/HarfBuzz/FriBidi** to transparent PNGs, which ffmpeg
then composites as pixels. `./live validate` reports which backend is in use.

---

## Install

```bash
cd ~/quran-live
bash scripts/install-ffmpeg.sh          # static build into vendor/, no system packages
bash scripts/fetch-quran-text.sh        # Tanzil Uthmani text into data/
cp .env.example .env && chmod 600 .env
mkdir -p ~/.config/systemd/user
cp systemd/quran-live@.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER"          # survive logout and reboot
```

Add content, then the stream key (never in a YAML file, never in git):

```bash
cp /path/to/*.mp3  ~/quran-live/assets/audio/minshawi/
cp /path/to/*.jpg  ~/quran-live/assets/visuals/images/
cp /path/to/Font.ttf ~/quran-live/assets/fonts/

read -rsp "key: " k && echo "CHANNEL01_STREAM_KEY=$k" >> .env && chmod 600 .env
```

## Usage

```bash
./live validate                 # full pre-flight; per channel: ./live validate channel01
./live sync channel01           # infer ayah timings for every recitation
./live sync channel01 --show    # print timings against the verse text to check them
./live build channel01          # pre-render tracks (the expensive, one-off step)
./live start channel01          # start | stop | restart | status take a channel
./live status                   # no argument = all channels
./live logs channel01 -f        # follow the journal
./live enable channel01         # start automatically on boot
./live list
```

## How to…

**Add a new Sheikh** — new folder, point a channel at it:
```bash
mkdir -p assets/audio/husary && cp *.mp3 assets/audio/husary/
# channel YAML:  audio_dir: "assets/audio/husary"
```

**Add new audio** — drop it in, then sync + build. It joins the rotation at the
next track change; no restart:
```bash
cp 018.mp3 assets/audio/minshawi/
./live sync channel01 018 && ./live build channel01
```
Name files with the surah number (`018.mp3`, `surah-018.mp3`) so the verses are
matched automatically; otherwise pass `--surah`.

**Add new images/videos** — drop it in. New recitations will pair with it; to use
it for existing ones, delete their cached tracks and rebuild.

**Change the font**
```bash
cp NewFont.ttf assets/fonts/arabic.ttf     # set `font:` in the channel YAML
rm -rf cache/tracks cache/captions cache/ts && ./live build channel01
```

**Add a sixth channel**
```bash
cp channels/channel01.yaml channels/channel06.yaml   # edit name/dirs/stream_key_env
echo 'CHANNEL06_STREAM_KEY=...' >> .env
./live validate channel06 && ./live build channel06
./live enable channel06 && ./live start channel06
```

**Change a stream key** — edit `.env`, then `./live restart channel01`.

**Monitor**
```bash
./live status                                   # state, restarts, uptime, current track
systemd-cgtop --user                            # live CPU/RAM per channel
```

**Troubleshoot**
```bash
./live validate channel01
./live logs channel01 -n 200
```
Common causes: stream key unset or rotated; no rendered tracks yet (run
`./live build`); `vendor/ffmpeg` missing after a fresh checkout.

---

## Resource usage

| | per channel | 5 channels |
|---|---|---|
| CPU while streaming | **0.3% of one core** | ~1.5% |
| RAM while streaming | **18 MB** | ~90 MB |
| Egress @720p 3 Mbps | ~3.1 Mbps | ~15.5 Mbps ≈ **4.9 TB/month** |

Oracle's Always Free tier includes 10 TB/month egress, so five 720p channels use
about half. 1080p @4.5 Mbps would be ~7.3 TB/month — inside the limit but tight,
which is the real argument for 720p here, more than CPU is.

**Disk.** Nothing is recorded, but rendered tracks are kept: roughly **12 MB per
minute** of recitation at 3 Mbps (a 3-minute surah ≈ 35 MB, plus its MPEG-TS copy
≈ 38 MB). A full 30-hour Quran on one channel is therefore on the order of
**40–50 GB**. Storage during streaming itself does not grow. Clear `cache/` any
time; it rebuilds from the pools.

## Coexisting with other apps

The systemd unit makes the streams yield: `Nice=15`, `CPUWeight=20`,
`IOSchedulingClass=idle`, with `CPUQuota=120%` and `MemoryMax=1500M` as ceilings
so a bug cannot starve the box. `Restart=always` recovers from crashes and
network drops. Streams run as **user** units under an unprivileged account,
never root.

## Security

- Stream keys live only in `.env` (`chmod 600`), read from the environment at
  launch — never in a config file, never logged, never in `./live status`.
- `.gitignore` excludes `.env`, all content, and all generated media.
- Channel paths are resolved and confined to the project root.
- The CLI shells out only to `systemctl`/`journalctl` with argument lists built
  in code — no shell string is assembled from input.
- systemd hardening: `ProtectSystem=strict`, `ProtectHome=read-only`,
  `NoNewPrivileges`, writes limited to `cache/`, `state/` and `logs/`.

## Known limitations

- **Ayah timings are inferred, not exact.** See "Verse timings" above.
- One background is rendered per recitation (chosen at random), not every
  combination — rendering audio × visuals would be a combinatorial amount of
  encoding and disk. Variety comes from having several recitations.
- Changing the font, resolution or overlay settings invalidates every cached
  track, so they must all be re-rendered.
