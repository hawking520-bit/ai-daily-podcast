#!/usr/bin/env python3
"""Generate ORIGINAL royalty-free lo-fi music assets with numpy (no samples, no copyrighted material).

Outputs (44.1 kHz mono WAV) into ./music/:
  intro.wav   ~10 s  warm lo-fi theme with drums, fades out
  stinger.wav ~2 s   short chime + soft hit for segment transitions
  bed.wav     ~32 s  seamless loop, sparse pads + soft keys, no drums (sits under voices)
  outro.wav   ~14 s  theme reprise with long fade
Deterministic (fixed seed) so every episode sounds the same = show identity.
"""
import numpy as np, wave, os, sys

SR = 44100
rng = np.random.default_rng(451)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from show_config import CFG
OUT = CFG["music"]["dir"]   # show.json music.dir
os.makedirs(OUT, exist_ok=True)

def midi(n): return 440.0 * 2 ** ((n - 69) / 12)

def env_adsr(n, a=0.01, d=0.2, s=0.6, r=0.3):
    t = np.arange(n) / SR; dur = n / SR
    e = np.ones(n) * s
    ai = t < a; e[ai] = t[ai] / a
    di = (t >= a) & (t < a + d); e[di] = 1 - (1 - s) * (t[di] - a) / d
    ri = t > dur - r; e[ri] *= np.clip((dur - t[ri]) / r, 0, 1)
    return e

def lowpass(x, cutoff):
    # one-pole lowpass, run forward
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x); acc = 0.0
    b = 1 - a
    for i in range(len(x)):  # small assets only, fine
        acc = b * x[i] + a * acc; y[i] = acc
    return y

def lp_fast(x, cutoff):
    # vectorised approximation: FFT brickwall-ish smooth lowpass
    X = np.fft.rfft(x); f = np.fft.rfftfreq(len(x), 1 / SR)
    X *= 1 / (1 + (f / cutoff) ** 4)
    return np.fft.irfft(X, len(x))

def pad_note(freq, dur, amp=0.12):
    n = int(dur * SR); t = np.arange(n) / SR
    sig = np.zeros(n)
    for det in (-0.12, 0.0, 0.11):  # detuned triangle-ish
        f = freq * 2 ** (det / 12)
        sig += (2 / np.pi) * np.arcsin(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)))
    sig += 0.4 * np.sin(2 * np.pi * freq / 2 * t)
    lfo = 1 + 0.15 * np.sin(2 * np.pi * 0.25 * t)
    return amp * sig / 3 * lfo * env_adsr(n, a=0.6, d=0.5, s=0.8, r=min(1.0, dur / 2))

def key_note(freq, dur, amp=0.18):
    # soft electric-piano: sine + few harmonics with exp decay
    n = int(dur * SR); t = np.arange(n) / SR
    sig = (np.sin(2*np.pi*freq*t) + 0.35*np.sin(2*np.pi*2*freq*t)*np.exp(-t*3)
           + 0.12*np.sin(2*np.pi*3*freq*t)*np.exp(-t*5))
    trem = 1 + 0.08 * np.sin(2*np.pi*4.5*t)
    return amp * sig * np.exp(-t * 1.6) * trem * env_adsr(n, a=0.005, d=0.1, s=1, r=0.08)

def kick(amp=0.5):
    n = int(0.35 * SR); t = np.arange(n) / SR
    f = 50 + 70 * np.exp(-t * 30)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return amp * np.sin(ph) * np.exp(-t * 9)

def snare(amp=0.18):
    n = int(0.25 * SR); t = np.arange(n) / SR
    noise = lp_fast(rng.standard_normal(n), 5000)
    return amp * (0.7 * noise * np.exp(-t * 18) + 0.3 * np.sin(2*np.pi*190*t) * np.exp(-t * 25))

def hat(amp=0.05):
    n = int(0.06 * SR); t = np.arange(n) / SR
    noise = rng.standard_normal(n); noise = noise - lp_fast(noise, 7000)
    return amp * noise * np.exp(-t * 60)

def place(buf, sig, start):
    i = int(start * SR); j = min(len(buf), i + len(sig))
    if i < len(buf): buf[i:j] += sig[: j - i]

def vinyl(n, amp=0.006):
    crackle = np.zeros(n)
    idx = rng.integers(0, n, size=n // 4000)
    crackle[idx] = rng.uniform(-1, 1, size=len(idx))
    hiss = lp_fast(rng.standard_normal(n), 3000) * 0.3
    return amp * (crackle * 3 + hiss)

# Progression (in D): Dmaj9 - Bm9 - Gmaj7 - A6/9 ; lo-fi feel at 78 BPM
BPM = 78; BEAT = 60 / BPM; BAR = 4 * BEAT
CHORDS = [
    [50, 57, 61, 64, 66],  # D A C# E F#
    [47, 54, 57, 61, 62],  # B F# A C# D
    [43, 50, 54, 57, 59],  # G D F# A B
    [45, 52, 54, 59, 61],  # A E F# B C#
]
MELODY = [  # (beat offset within 4-bar phrase, midi, beats)
    (0, 78, 1.5), (1.5, 76, 0.5), (2, 73, 1), (3, 76, 1),
    (4, 74, 2), (6, 73, 1), (7, 71, 1),
    (8, 69, 1.5), (9.5, 71, 0.5), (10, 73, 1), (11, 74, 1),
    (12, 73, 3), (15, 69, 1),
]

def section(bars, drums=True, melody=True, keys=True, pad_amp=0.10):
    dur = bars * BAR; n = int(dur * SR) + SR
    buf = np.zeros(n)
    for b in range(bars):
        ch = CHORDS[b % 4]; t0 = b * BAR
        for m in ch: place(buf, pad_note(midi(m), BAR + 0.4, pad_amp / len(ch) * 3), t0)
        place(buf, pad_note(midi(ch[0] - 12), BAR, 0.08), t0)  # bass
        if keys:
            for k, m in enumerate(ch[1:]):
                place(buf, key_note(midi(m + 12), 1.4, 0.05), t0 + k * BEAT * 0.5 + (BEAT * 2 if k > 1 else 0))
        if drums:
            for beat in range(4):
                tt = t0 + beat * BEAT
                if beat in (0,): place(buf, kick(), tt)
                if beat == 2: place(buf, kick(0.35), tt + BEAT * 0.5)
                if beat in (1, 3): place(buf, snare(), tt + 0.02)
                place(buf, hat(), tt); place(buf, hat(0.03), tt + BEAT * 0.55)  # swing
    if melody:
        for p in range(bars // 4):
            for off, m, ln in MELODY:
                place(buf, key_note(midi(m), ln * BEAT + 0.6, 0.11), p * 4 * BAR + off * BEAT)
    buf += vinyl(n)
    buf = lp_fast(buf, 6500)  # warm lo-fi top end
    return buf

def fade(x, fin=0.0, fout=0.0):
    x = x.copy()
    if fin: k = int(fin * SR); x[:k] *= np.linspace(0, 1, k)
    if fout: k = int(fout * SR); x[-k:] *= np.linspace(1, 0, k) ** 1.5
    return x

def norm(x, peak_db=-3.0):
    return x / (np.max(np.abs(x)) + 1e-9) * 10 ** (peak_db / 20)

def write(name, x):
    x = np.clip(x, -1, 1)
    with wave.open(os.path.join(OUT, name), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    print(name, f"{len(x)/SR:.1f}s")

# intro: 3 bars ≈ 9.2 s + tail → trim to ~10 s
intro = section(4, drums=True, melody=True)[: int(10.2 * SR)]
write("intro.wav", norm(fade(intro, 0.3, 2.5)))

# stinger ~2 s: rising arpeggio chime + soft kick
st = np.zeros(int(2.2 * SR))
for i, m in enumerate([62, 66, 69, 74, 78]):
    place(st, key_note(midi(m), 1.6, 0.16), i * 0.09)
place(st, kick(0.35), 0.0)
place(st, pad_note(midi(50), 2.0, 0.08), 0.0)
st = lp_fast(st + vinyl(len(st), 0.004), 7000)
write("stinger.wav", norm(fade(st, 0.0, 0.9), -4))

# bed: 8 bars (~24.6 s) seamless loop: render 9 bars, crossfade last bar into the first
raw = section(9, drums=False, melody=False, keys=True, pad_amp=0.09)
L = int(8 * BAR * SR); xf = int(1.5 * SR)
bed = raw[:L].copy()
tail = raw[L:L + xf]
ramp = np.linspace(0, 1, xf)
bed[:xf] = bed[:xf] * ramp + tail * (1 - ramp)
write("bed.wav", norm(bed, -6))

# outro: theme with drums, long fade
outro = section(4, drums=True, melody=True)[: int(14 * SR)]
write("outro.wav", norm(fade(outro, 0.5, 6.0)))
