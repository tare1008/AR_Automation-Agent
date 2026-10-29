"""Soundtrack for the AR Automation Agent launch video.

120 BPM, A minor (Am-F-C-G), every event on the 16th-note grid from t=0.
Music and effects are one piece: effects use chord tones of the bar they land in.
Pure stdlib: writes 44.1 kHz stereo 16-bit WAV.
"""

import math
import random
import struct
import sys
import wave
from array import array

SR = 44100
DUR = 20.0
BPM = 120
BEAT = 60 / BPM          # 0.5 s
BAR = BEAT * 4           # 2 s
N = int(SR * DUR)
L = array("f", [0.0]) * N
R = array("f", [0.0]) * N
random.seed(7)

def hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)

# bar -> (bass root, chord voicing)
PROG = [
    (45, [57, 60, 64]),  # Am
    (41, [57, 60, 65]),  # F
    (48, [55, 60, 64]),  # C
    (43, [55, 59, 62]),  # G
]
def chord_at(t):
    return PROG[int(t // BAR) % 4]

def add(start, samples, gain, pan=0.0):
    i0 = int(round(start * SR))
    gl, gr = gain * (1 - max(0, pan)), gain * (1 + min(0, pan))
    for k, v in enumerate(samples):
        i = i0 + k
        if i >= N:
            break
        if i >= 0:
            L[i] += v * gl
            R[i] += v * gr

def env(n, a, d):
    """attack a seconds, exponential decay with time-constant d."""
    na = max(1, int(a * SR))
    out = []
    for k in range(n):
        out.append((k / na if k < na else 1.0) * math.exp(-max(0, k - na) / (d * SR)))
    return out

def tone(f, length, a, d, harmonics=((1, 1.0),), detune=0.0):
    n = int(length * SR)
    e = env(n, a, d)
    out = []
    for k in range(n):
        t = k / SR
        s = 0.0
        for h, amp in harmonics:
            s += amp * math.sin(2 * math.pi * f * h * t)
            if detune:
                s += amp * 0.6 * math.sin(2 * math.pi * f * h * (1 + detune) * t)
        out.append(s * e[k])
    return out

def noise(length, a, d, hp=0.0, lp=1.0):
    n = int(length * SR)
    e = env(n, a, d)
    out, prev_in, hp_out, lp_out = [], 0.0, 0.0, 0.0
    for k in range(n):
        x = random.uniform(-1, 1)
        hp_out = hp * (hp_out + x - prev_in) if hp else x
        prev_in = x
        lp_out += lp * (hp_out - lp_out)
        out.append(lp_out * e[k])
    return out

def kick():
    n = int(0.35 * SR)
    out, ph = [], 0.0
    for k in range(n):
        t = k / SR
        f = 45 + 85 * math.exp(-t * 28)
        ph += 2 * math.pi * f / SR
        out.append(math.sin(ph) * math.exp(-t * 9))
    return out

KICK = kick()
HAT = noise(0.06, 0.001, 0.018, hp=0.92, lp=0.7)
SNARE_N = noise(0.22, 0.002, 0.06, hp=0.6, lp=0.5)

steps = int(DUR / (BEAT / 4))
for s in range(steps):
    t = s * BEAT / 4
    in_bar = s % 16
    bass, voicing = chord_at(t)
    # drums enter at the reveal (3 s); half-time feel through the hook
    if t >= 3.0 and t < 19.5:
        if in_bar in (0, 8) or (t >= 10 and in_bar == 10):
            add(t, KICK, 0.55)
        if in_bar % 4 == 2:
            add(t, HAT, 0.10, pan=0.3)
        if t >= 6.0 and in_bar in (4, 12):
            add(t, SNARE_N, 0.16, pan=-0.1)
            add(t, tone(190, 0.15, 0.001, 0.04), 0.08)
    # bass: 8ths from the reveal, root then octave
    if t >= 3.0 and t < 19.0 and in_bar % 2 == 0:
        m = bass + (12 if in_bar % 4 == 2 else 0)
        add(t, tone(hz(m), 0.3, 0.004, 0.11, ((1, 1.0), (2, 0.35), (3, 0.12))), 0.22)
    # pluck arpeggio: 8ths all the way through
    if in_bar % 2 == 0 and t < 18.0:
        pattern = [0, 1, 2, 1, 2, 1, 0, 2]
        note = voicing[pattern[(in_bar // 2) % 8]] + 12
        add(t, tone(hz(note), 0.5, 0.003, 0.16, ((1, 1.0), (2, 0.25), (3, 0.08))), 0.12,
            pan=-0.25 if in_bar % 4 else 0.25)

# pad: one chord per bar, slow attack, ends on the tonic
for b in range(int(DUR / BAR)):
    t = b * BAR
    _, voicing = PROG[b % 4]
    if b == 9:
        voicing = [57, 60, 64]
    for m in voicing:
        add(t, tone(hz(m), BAR + 0.3, 0.45, 1.6, ((1, 1.0), (2, 0.15)), detune=0.003), 0.045)

# --- effects, in key, under the music ---
def blip(t, gain=0.05):
    _, voicing = chord_at(t)
    add(t, tone(hz(voicing[2] + 24), 0.12, 0.001, 0.035, ((1, 1.0), (2, 0.2))), gain, pan=0.15)

for i in range(6):                       # quotes unfolding (scene 1)
    blip(0.75 + i * 0.125, 0.045)
for i in range(6):                       # thread rows (scene 3)
    blip(6.25 + i * 0.125, 0.035)
for i in range(16):                      # advice rows (scene 4)
    blip(10.5 + i * 0.125, 0.035)

def chime(t, notes, gain):
    for j, m in enumerate(notes):
        add(t + j * BEAT / 4, tone(hz(m), 1.6, 0.002, 0.5, ((1, 1.0), (2.76, 0.18), (5.4, 0.05))), gain)

chime(13.0, [69, 72, 76], 0.07)          # totals match (C bar -> A minor arpeggio)
add(15.5, noise(0.03, 0.0005, 0.006, hp=0.8, lp=0.6), 0.12)   # click on the button
chime(15.75, [72, 76], 0.05)             # match confirmed
chime(18.0, [69, 76, 81], 0.06)          # lockup

def swell(end):                          # soft filtered air into each cut
    n = int(0.5 * SR)
    src = noise(0.5, 0.0, 99, hp=0.7, lp=0.25)
    add(end - 0.5, [v * (k / n) ** 2 for k, v in enumerate(src)], 0.10)

for cut in (3.0, 6.0, 10.0, 14.0, 17.0):
    swell(cut)

# master: gentle fade-out, soft clip, normalise to -1 dBFS
fade0 = int(19.2 * SR)
for i in range(fade0, N):
    g = max(0.0, 1 - (i - fade0) / (N - fade0))
    L[i] *= g
    R[i] *= g
peak = max(max(abs(x) for x in L), max(abs(x) for x in R)) or 1.0
scale = 0.89 / math.tanh(1.0) / peak
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    frames = bytearray()
    for i in range(N):
        frames += struct.pack("<hh", int(32767 * math.tanh(L[i] * scale) * 0.98),
                              int(32767 * math.tanh(R[i] * scale) * 0.98))
    w.writeframes(bytes(frames))
print("wrote", sys.argv[1])
