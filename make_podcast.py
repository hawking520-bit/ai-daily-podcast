#!/usr/bin/env python3
"""Turn a podcast script.md into a finished MP3 (TTS voices + original music, ducked bed, loudness-normalised).

Script format (one turn per line; blank lines and markdown headings are ignored):
    ALEX: Okay so here's the thing...
    MAYA: Wait, really?
    [INTRO]            -> intro music (cold open goes above it)
    [STINGER]          -> 2 s transition chime
    [BED ON] / [BED OFF] -> start/stop the soft background loop under the talk
    [PAUSE 1.5]        -> silence in seconds
    [OUTRO]            -> outro music (overlaps the final line's tail)
Text in (parentheses) or *asterisks* is a stage direction and is not spoken.

Voices: front-matter lines at the top of the script, e.g.
    voice ALEX = en-US-AndrewMultilingualNeural
    voice MAYA = en-US-AvaMultilingualNeural
or --voice ALEX=... on the command line (CLI wins). Optional rate/pitch: --rate ALEX=+4%.

Usage:
    python3 make_podcast.py script.md -o episode.mp3 [--default-rate -13%] [--gap-extra 0.2] [--bitrate 128k]
    Glosses: write {中文} right after a word, e.g. "leverage{杠杆/借力}" -> not spoken, shown in transcript.
    [BEAT] = 0.9 s pause after a key point.
    python3 make_podcast.py script.md --mode briefing -o jobs.mp3   # single-host, lighter music
"""
import argparse, asyncio, hashlib, json, os, re, subprocess, sys, time, wave
import numpy as np

SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
from show_config import CFG
MUSIC = CFG["music"]["dir"]
CACHE = os.path.join(HERE, ".tts-cache")

DEFAULT_VOICES = {h["id"]: h["voice"] for h in CFG["hosts"]}          # from show.json "hosts"
HOST_RATES = {h["id"]: h["rate"] for h in CFG["hosts"] if h.get("rate")}
HOST_NAMES = {h["id"]: h.get("name") or h["id"].title() for h in CFG["hosts"]}

LINE_RE = re.compile(r"^\s*\**([A-Z][A-Z ]{1,20})\**\s*:\s*(.+)$")
DIR_RE = re.compile(r"^\s*\[(INTRO|OUTRO|STINGER|BED ON|BED OFF|BEAT|PAUSE[^\]]*)\]\s*$", re.I)
VOICE_RE = re.compile(r"^\s*voice\s+([A-Z ]+?)\s*=\s*(\S+)\s*$", re.I)

def parse(path):
    events, voices = [], {}
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("## "):
            events.append(("chapter", line[3:].strip())); continue
        if not line.strip() or line.lstrip().startswith("#") or line.strip().startswith(">"):
            continue
        m = VOICE_RE.match(line)
        if m: voices[m.group(1).strip().upper()] = m.group(2); continue
        m = re.match(r"^\s*\[STORY\s+([^\]\s]+)\]\s*$", line, re.I)
        if m: events.append(("story", m.group(1))); continue
        m = DIR_RE.match(line)
        if m:
            d = m.group(1).upper()
            if d.startswith("PAUSE"):
                secs = float((d.split() + ["1"])[1]); events.append(("pause", secs))
            elif d == "BEAT":
                events.append(("pause", BEAT_SEC))
            else:
                events.append(("dir", d))
            continue
        m = LINE_RE.match(line)
        if m:
            spk = m.group(1).strip().upper()
            body = re.sub(r"\([^)]*\)|\*[^*]*\*", "", m.group(2)).strip()
            text = re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}", "", body)).strip()      # spoken: glosses removed
            shown = re.sub(r"\s*\{([^}]*)\}", r" (\1)", body)                       # transcript: word (中文释义)
            if text: events.append(("say", spk, text, shown))
    return events, voices

def decode(path):
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(out, dtype=np.float32).astype(np.float64)

def load_wav(name):
    """name = intro/outro/stinger/bed; file comes from show.json music.<name>. Music off -> short silence."""
    m = CFG["music"]
    if not m.get("enabled", True): return np.zeros(int(0.8 * SR) if name != "stinger" else int(0.6 * SR))
    p = m.get(name) or f"{name}.wav"
    p = p if os.path.isabs(p) else os.path.join(MUSIC, p)
    if not os.path.exists(p): sys.exit(f"music file missing: {p} (run make_music.py, fix show.json music.{name}, or set music.enabled false)")
    return decode(p)

def expected_sec(text):
    zh = len(re.findall(r"[\u4e00-\u9fff]", text))
    en = len(re.findall(r"[A-Za-z0-9]+", text))
    return en * 0.28 + zh * 0.2

def audio_sec(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    try: return float(out)
    except ValueError: return 0.0

def clip_ok(path, text):
    # edge-tts occasionally returns a near-empty clip (~0.3 s) for a long line; treat that as a failure
    return os.path.exists(path) and audio_sec(path) >= max(0.5, 0.45 * expected_sec(text))

async def tts_one(sem, text, voice, rate, pitch, out):
    import edge_tts
    err = None
    async with sem:
        for attempt in range(6):
            try:
                c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
                await c.save(out + ".part")
                if os.path.getsize(out + ".part") > 1000 and clip_ok(out + ".part", text):
                    os.replace(out + ".part", out); return
                err = f"clip too short ({audio_sec(out + '.part'):.2f}s)"
            except Exception as e:
                err = e
                # 2026-10-12: Microsoft is moving Read Aloud to api.msedgeservices.com; the legacy
                # speech.platform.bing.com host now answers 403 from this box. On a 403, switch this
                # process to the new host (legacy token/version still accepted there). edge-tts issue #490.
                if getattr(e, "status", None) == 403:
                    import edge_tts.communicate as _cm
                    from edge_tts.constants import TRUSTED_CLIENT_TOKEN as _tok
                    _cm.WSS_URL = ("wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
                                   f"?Ocp-Apim-Subscription-Key={_tok}")
            await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f"TTS failed for {voice}: {text[:60]}... ({err})")

async def synth_all(jobs, conc):
    sem = asyncio.Semaphore(conc)
    await asyncio.gather(*[tts_one(sem, *j) for j in jobs])

def trim(x, thr=0.004):
    idx = np.where(np.abs(x) > thr)[0]
    if len(idx) == 0: return x
    a = max(0, idx[0] - int(0.02 * SR)); b = min(len(x), idx[-1] + int(0.06 * SR))
    return x[a:b]

BLOCK = 441  # 10 ms analysis blocks (keeps memory low on hour-long episodes)

def block_rms(x):
    n = len(x) // BLOCK * BLOCK
    b = np.sqrt(np.mean(x[:n].reshape(-1, BLOCK).astype(np.float64) ** 2, axis=1))
    return np.append(b, 0.0) if n < len(x) else b

def smooth_blocks(g, attack=0.05, release=0.6):
    y = np.empty_like(g); acc = g[0] if len(g) else 0.0
    aa = np.exp(-BLOCK / (attack * SR)); ra = np.exp(-BLOCK / (release * SR))
    for i, v in enumerate(g):
        acc = aa * acc + (1 - aa) * v if v > acc else ra * acc + (1 - ra) * v
        y[i] = acc
    return y

def upsample_blocks(g, n):
    return np.repeat(g.astype(np.float32), BLOCK)[:n] if len(g) * BLOCK >= n else \
        np.pad(np.repeat(g.astype(np.float32), BLOCK), (0, n - len(g) * BLOCK), mode="edge")

# Pacing defaults (v2, 2026-10-12): tuned for fluent non-native listeners -> slower voices, roomier gaps.
DEFAULT_RATE = CFG["voice_defaults"].get("rate", "-10%")   # edge-tts speaking rate for every host unless --rate overrides
GAP_EXTRA = float(CFG["voice_defaults"].get("gap_extra", 0.2))       # seconds added to every turn gap (v1 had none)
BEAT_SEC = 0.9          # [BEAT] directive: pause after a key point
TAKEAWAY_SEC = 0.6      # extra pause after a line that starts "So the takeaway"

def gap_after(prev_spk, spk, text, rng, extra=GAP_EXTRA):
    if prev_spk is None: return 0.0
    if prev_spk == spk: return rng.uniform(0.35, 0.55) + extra
    short = len(text.split()) <= 4  # quick reactions still come in a bit faster
    return (rng.uniform(0.12, 0.25) if short else rng.uniform(0.25, 0.45)) + extra

# ---- time-synced captions (WebVTT + SRT) and chapter list -------------------------------------------
CUE_MAX_CHARS = 90

def _weight(t):
    zh = len(re.findall(r"[\u4e00-\u9fff]", t)); en = len(re.findall(r"[A-Za-z0-9]+", t))
    return max(0.3, en * 0.28 + zh * 0.2)

def split_cues(text, start, end):
    """Split one spoken line into sentence-level cues, timed proportionally to expected speaking time."""
    sents = [x.strip() for x in re.split(r"(?<=[.!?。！？])\s+|(?<=[。！？])", text) if x.strip()]
    groups, cur = [], ""
    for x in sents:
        if cur and len(cur) + 1 + len(x) > CUE_MAX_CHARS: groups.append(cur); cur = x
        else: cur = (cur + " " + x).strip() if cur and not re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]$", cur) else cur + x
    if cur: groups.append(cur)
    w = [_weight(g) for g in groups]; tot = sum(w); out = []; t = start
    for g, wi in zip(groups, w):
        d = (end - start) * wi / tot; out.append((t, t + d, g)); t += d
    return out

def _ts(sec, sep="."):
    ms = int(round(sec * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s_, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s_:02d}{sep}{ms:03d}"

def write_captions(a, tlines, chapters, duration, stories=()):
    base = os.path.splitext(a.out)[0]
    cues = []
    for (t, spk, shown, end, spoken) in tlines:
        for cs, ce, txt in split_cues(spoken, t, end):
            cues.append((cs, ce, HOST_NAMES.get(spk, spk.title()), txt))
    with open(base + ".vtt", "w", encoding="utf-8") as fh:
        fh.write("WEBVTT\n\n")
        for i, (cs, ce, spk, txt) in enumerate(cues, 1):
            fh.write(f"{i}\n{_ts(cs)} --> {_ts(ce)}\n<v {spk}>{txt}\n\n")
    with open(base + ".srt", "w", encoding="utf-8") as fh:
        for i, (cs, ce, spk, txt) in enumerate(cues, 1):
            fh.write(f"{i}\n{_ts(cs, ',')} --> {_ts(ce, ',')}\n{spk}: {txt}\n\n")
    json.dump({"duration_sec": round(duration, 2),
               "chapters": [{"start_sec": round(t, 2), "title": ti} for t, ti in chapters],
               "stories": [{"start_sec": round(t, 2), "key": k} for t, k in stories],
               "lines": [{"start": round(t, 3), "end": round(e, 3), "speaker": HOST_NAMES.get(spk, spk.title()), "text": sp}
                         for (t, spk, sh, e, sp) in tlines]},
              open(base + ".chapters.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"captions: {base}.vtt / .srt ({len(cues)} cues), chapters: {len(chapters)}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="show.json to use (default ./show.json)")
    ap.add_argument("script"); ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--mode", choices=["conversation", "briefing"], default="conversation")
    ap.add_argument("--voice", action="append", default=[], help="SPEAKER=voice")
    ap.add_argument("--rate", action="append", default=[], help="SPEAKER=+5%%")
    ap.add_argument("--bed-db", type=float, default=None, help="bed level under speech, dB (default -26 conv / -28 briefing)")
    ap.add_argument("--lufs", type=float, default=float(CFG["voice_defaults"].get("lufs", -16)))
    ap.add_argument("--bitrate", default=CFG["voice_defaults"].get("bitrate", "128k"))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--default-rate", default=DEFAULT_RATE, help="rate for hosts without --rate (default %(default)s)")
    ap.add_argument("--gap-extra", type=float, default=GAP_EXTRA, help="seconds added to every turn gap")
    ap.add_argument("--transcript", default=None, help="write a timestamped transcript .md (glosses shown as word (释义)); default <out>.transcript.md")
    ap.add_argument("--no-transcript", action="store_true")
    ap.add_argument("--no-captions", action="store_true", help="skip <out>.vtt / .srt / .chapters.json")
    ap.add_argument("--timings-only", action="store_true",
                    help="no TTS, no mixing: recompute the timeline from cached clips (same script/voices/rate/seed/gap as the build) and write .vtt/.srt/.chapters.json")
    a = ap.parse_args()
    t0 = time.time()
    events, voices = parse(a.script)
    V = dict(DEFAULT_VOICES); V.update(voices)
    for kv in a.voice: k, v = kv.split("=", 1); V[k.strip().upper()] = v.strip()
    R = dict(HOST_RATES)
    for kv in a.rate: k, v = kv.split("=", 1); R[k.strip().upper()] = v.strip()
    bed_db = a.bed_db if a.bed_db is not None else float(CFG["music"].get("bed_db", -26 if a.mode == "conversation" else -28))

    os.makedirs(CACHE, exist_ok=True)
    jobs, files = [], []
    for ev in events:
        if ev[0] != "say": files.append(None); continue  # dir / pause / chapter / story
        spk, text = ev[1], ev[2]
        if spk not in V: sys.exit(f"No voice for speaker {spk}; add it to show.json hosts, or a 'voice {spk} = ...' line in the script")
        rate = R.get(spk, a.default_rate); key = hashlib.sha1(f"{V[spk]}|{rate}|{text}".encode()).hexdigest()[:16]
        f = os.path.join(CACHE, key + ".mp3"); files.append(f)
        if not clip_ok(f, text): jobs.append((text, V[spk], rate, "+0Hz", f))  # also re-does bad cached clips
    nsay = sum(1 for e in events if e[0] == "say")
    words = sum(len(e[2].split()) for e in events if e[0] == "say")
    print(f"{nsay} lines, ~{words} words, {len(jobs)} to synthesise", flush=True)
    if jobs and a.timings_only:
        sys.exit(f"--timings-only needs every clip in .tts-cache; {len(jobs)} missing/bad (wrong script, voice or rate?)")
    if jobs:
        asyncio.run(synth_all(jobs, a.concurrency))
    print(f"TTS done in {time.time()-t0:.0f}s", flush=True)

    intro, outro, sting, bed = (load_wav(n) for n in ("intro", "outro", "stinger", "bed"))
    rng = np.random.default_rng(a.seed)
    voice = []      # list of (start_sample, array)
    music = []      # list of (start_sample, array, gain)
    bed_spans = []  # [start, end) samples where bed plays
    tlines = []     # (start_sec, speaker, text-with-glosses) for the transcript
    cur = 0; prev = None; bed_start = None; music_g = 0.8 if a.mode == "conversation" else 0.55

    chapters = []   # (start_sec, title) -- start = first spoken line after the "## " heading
    pending_ch = None; pending_st = None; stories = []   # [STORY key] markers -> (start_sec, key)
    for ev, f in zip(events, files):
        if ev[0] == "chapter":
            pending_ch = ev[1]; continue
        if ev[0] == "story":
            pending_st = ev[1]; continue
        if ev[0] == "pause":
            cur += int(ev[1] * SR); prev = None; continue
        if ev[0] == "dir":
            d = ev[1]
            if d == "INTRO":
                # intro music; voices resume under its tail
                music.append((cur, intro, music_g)); cur += len(intro) - int(2.0 * SR); prev = None
            elif d == "STINGER":
                cur += int(0.35 * SR); music.append((cur, sting, music_g * 0.9)); cur += len(sting) - int(0.5 * SR); prev = None
            elif d == "OUTRO":
                cur += int(0.6 * SR); music.append((cur, outro, music_g)); cur += len(outro); prev = None
            elif d == "BED ON":
                bed_start = cur
            elif d == "BED OFF" and bed_start is not None:
                bed_spans.append((bed_start, cur + int(1.0 * SR))); bed_start = None
            continue
        spk, text, shown = ev[1], ev[2], ev[3]
        x = trim(decode(f)).astype(np.float32)
        cur += int(gap_after(prev, spk, text, rng, a.gap_extra) * SR)
        tlines.append((cur / SR, spk, shown, (cur + len(x)) / SR, text))
        if pending_ch: chapters.append((cur / SR, pending_ch)); pending_ch = None
        if pending_st: stories.append((cur / SR, pending_st)); pending_st = None
        voice.append((cur, x)); cur += len(x); prev = spk
        if text.lower().startswith("so the takeaway"): cur += int(TAKEAWAY_SEC * SR)
    if bed_start is not None: bed_spans.append((bed_start, cur))
    if a.timings_only:
        total = cur + int(1.0 * SR)
        for s_, m, g in music: total = max(total, s_ + len(m))
        write_captions(a, tlines, chapters, total / SR, stories)
        print(json.dumps({"timings_only": True, "duration_sec": round(total / SR, 1), "lines": len(tlines),
                          "chapters": len(chapters)}))
        return

    total = cur + int(1.0 * SR)
    for s, m, g in music: total = max(total, s + len(m))
    vt = np.zeros(total, np.float32); mt = np.zeros(total, np.float32); bt = np.zeros(total, np.float32)
    for s, x in voice:
        # normalise each clip's RMS so both hosts sit at the same level
        r = np.sqrt(np.mean(x ** 2)) + 1e-9; vt[s:s + len(x)] += x * np.float32(0.1 / r)
    for s, m, g in music: mt[s:s + len(m)] += (m * g).astype(np.float32)
    bed32 = bed.astype(np.float32)
    for s, e in bed_spans:
        n = e - s
        for o in range(0, n, len(bed32)):
            m = min(len(bed32), n - o); bt[s + o:s + o + m] += bed32[:m]
        k = min(int(3 * SR), n // 3)
        bt[s:s + k] *= np.linspace(0, 1, k, dtype=np.float32); bt[e - k:e] *= np.linspace(1, 0, k, dtype=np.float32)

    # sidechain duck: bed sits at bed_db relative to speech while talking, rises ~6 dB in pauses
    env = smooth_blocks((block_rms(vt) > 0.01).astype(float), attack=0.08, release=0.8)
    talk_g = 10 ** (bed_db / 20); idle_g = 10 ** ((bed_db + 6) / 20)
    bed_rms = np.sqrt(np.mean(bed ** 2)) + 1e-9
    bt *= upsample_blocks((0.1 / bed_rms) * (idle_g + (talk_g - idle_g) * env), total)
    mt *= np.float32(0.1 / (np.sqrt(np.mean(intro ** 2)) + 1e-9) * 0.7)
    mix = vt; mix += bt; mix += mt; del bt, mt
    peak = float(np.max(np.abs(mix)))
    if peak > 0.95: mix *= np.float32(0.95 / peak)

    tmp = a.out + ".tmp.wav"
    os.makedirs(os.path.dirname(os.path.abspath(tmp)), exist_ok=True)
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        np.clip(mix, -1, 1, out=mix); mix *= 32767; w.writeframes(mix.astype(np.int16).tobytes())
    # two-pass loudnorm
    p1 = subprocess.run(["ffmpeg", "-hide_banner", "-i", tmp, "-af", f"loudnorm=I={a.lufs}:TP=-1.5:LRA=11:print_format=json",
                         "-f", "null", "-"], capture_output=True, text=True).stderr
    js = json.loads(p1[p1.rfind("{"): p1.rfind("}") + 1])
    af = (f"loudnorm=I={a.lufs}:TP=-1.5:LRA=11:measured_I={js['input_i']}:measured_TP={js['input_tp']}"
          f":measured_LRA={js['input_lra']}:measured_thresh={js['input_thresh']}:offset={js['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", tmp, "-af", af, "-ar", "44100", "-ac", "1",
                    "-c:a", "libmp3lame", "-b:a", a.bitrate,
                    "-metadata", f"title={os.path.splitext(os.path.basename(a.out))[0]}", a.out], check=True)
    os.remove(tmp)
    if not a.no_transcript:
        tp = a.transcript or os.path.splitext(a.out)[0] + ".transcript.md"
        with open(tp, "w", encoding="utf-8") as fh:
            fh.write(f"# Transcript: {os.path.basename(a.out)}\n\n")
            if CFG["language"].get("glosses"): fh.write("Some words have a gloss in brackets, e.g. leverage (杠杆/借力).\n\n")
            for t, spk, shown, *_ in tlines:
                fh.write(f"**[{int(t//60):02d}:{int(t%60):02d}] {spk.title()}:** {shown}\n\n")
        print(f"transcript: {tp}", flush=True)
    if not a.no_captions: write_captions(a, tlines, chapters, total / SR, stories)
    dur = total / SR
    print(json.dumps({"out": a.out, "duration_sec": round(dur, 1), "duration": f"{int(dur//60)}:{int(dur%60):02d}",
                      "size_mb": round(os.path.getsize(a.out) / 1e6, 2), "lines": nsay, "words": words,
                      "voices": {k: V[k] for k in sorted({e[1] for e in events if e[0]=='say'})},
                      "elapsed_sec": round(time.time() - t0)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
