"""Синтез-примітиви для звукових пакетів (acid-бас, суперсо, гейт, сайдчейн, реверб тощо).
Використовує базові звуки з radio.py."""
import numpy as np
from scipy import signal

import radio as R

SR = R.SR
add, st, mtof, _t = R._add, R._st, R.mtof, R._t


class Grid:
    def __init__(self, bpm):
        self.bpm, self.beat = bpm, 60 / bpm
        self.bar, self.step = 4 * self.beat, self.beat / 4


# ------------------------------------------------------------------ нові примітиви
def _biquad_lp(fc, q):
    w0 = 2 * np.pi * min(fc, SR * 0.45) / SR
    a, c = np.sin(w0) / (2 * q), np.cos(w0)
    b = np.array([(1 - c) / 2, 1 - c, (1 - c) / 2])
    aa = np.array([1 + a, -2 * c, 1 - a])
    return b / aa[0], aa / aa[0]


def acid(f, d, cut_lo=280, cut_hi=2800, q=9.0, decay=12.0, accent=False, f_from=None, vol=0.5, rise=False):
    """Кислотна нота у стилі TB-303: пила+квадрат, резонансний НЧ-фільтр із обвідною, ковзання, акцент."""
    n = int(d * SR)
    t = np.arange(n) / SR
    freq = f_from * (f / f_from) ** np.clip(t / 0.07, 0, 1) if f_from else np.full(n, f)
    ph = 2 * np.pi * np.cumsum(freq) / SR
    x = 0.6 * signal.sawtooth(ph) + 0.4 * signal.square(ph)
    hi = cut_hi * (1.4 if accent else 1.0)
    qq = q * (1.25 if accent else 1.0)
    cut = cut_lo + (hi - cut_lo) * ((t / d) ** 1.6 if rise else np.exp(-t * decay))
    y, zi, blk = np.zeros(n), np.zeros(2), 64
    for i in range(0, n, blk):
        b, a = _biquad_lp(cut[min(i + blk // 2, n - 1)], qq)
        y[i:i + blk], zi = signal.lfilter(b, a, x[i:i + blk], zi=zi)
    env = np.minimum(1, t / 0.003) * np.minimum(1, (d - t) / 0.02)
    return np.tanh(y * 1.7) * env * vol * (1.25 if accent else 1.0)


def supersaw(f, d, vol=0.3, voices=7, spread=0.013, lp=7000, atk=0.01, rel=0.05, decay=0.0):
    n = int(d * SR)
    t = np.arange(n) / SR
    left, right = np.zeros(n), np.zeros(n)
    for k, o in enumerate(np.linspace(-1, 1, voices)):
        x = signal.sawtooth(2 * np.pi * f * (1 + o * spread) * t + (k * 0.37 % 1) * 2 * np.pi)
        pan = (o + 1) / 2
        left += x * (1 - pan * 0.8)
        right += x * (0.2 + pan * 0.8)
    out = R._lp(np.stack([left, right], 1) / voices, lp)
    env = np.minimum(1, t / atk) * np.minimum(1, np.maximum(d - t, 0) / rel) * np.exp(-t * decay)
    return out * env[:, None] * vol


def snare(rng, vol=0.7):
    t = _t(0.3)
    nz = R._bp(rng.standard_normal(len(t)), 1400, 6500) * np.exp(-t * 17)
    body = np.sin(2 * np.pi * 190 * t * (1 + 0.5 * np.exp(-t * 40))) * np.exp(-t * 30)
    return (nz * 0.9 + body * 0.6) * vol


def tom(f=110, d=0.35, vol=0.7):
    t = _t(d)
    ph = 2 * np.pi * np.cumsum(f * (1 + 1.2 * np.exp(-t * 25))) / SR
    return np.sin(ph) * np.exp(-t * 11) * vol


def rim(rng, vol=0.5):
    t = _t(0.07)
    return (np.sin(2 * np.pi * 1750 * t) + R._hp(rng.standard_normal(len(t)), 3000) * 0.6) * np.exp(-t * 90) * vol


def blip(f, d=0.14, vol=0.35):
    t = _t(d)
    return np.sin(2 * np.pi * f * t + 2.2 * np.exp(-t * 28) * np.sin(2 * np.pi * f * 1.5 * t)) * np.exp(-t * 26) * vol


def rolling_pad(freqs, d, vol=0.16):
    return R.pad(freqs, d, vol)


def duck(x, g, depth=0.55, rel=0.22, offset=0.0):
    """Сайдчейн-«дихання»: гучність падає на кожен кік і повертається."""
    n = len(x)
    phase = ((np.arange(n) / SR - offset) % g.beat)
    env = 1 - depth * np.exp(-phase / (rel * g.beat))
    return x * env[:, None] if x.ndim == 2 else x * env


def gate(x, g, pattern="x.x.x.x.x.x.x.x.", smooth=0.004):
    """Трансовий гейт по 16-х: x = звук, . = тиша."""
    n = len(x)
    idx = ((np.arange(n) / SR) // g.step).astype(int) % len(pattern)
    env = np.array([1.0 if c == "x" else 0.0 for c in pattern])[idx]
    k = max(1, int(smooth * SR))
    env = np.convolve(env, np.ones(k) / k, mode="same")
    return x * env[:, None] if x.ndim == 2 else x * env


def wash(x, tail=0.9, mix=0.45):
    """Проста м'яка «реверберація» з кількох декорельованих відлунь."""
    if x.ndim == 1:
        x = np.stack([x, x], 1)
    out = np.concatenate([x, np.zeros((int(tail * SR), 2))])
    wet = np.zeros_like(out)
    for i, (d, gn) in enumerate(((0.029, .5), (0.047, .42), (0.071, .33), (0.097, .26), (0.131, .2), (0.173, .15))):
        k = int(d * SR)
        src = x[:, ::-1] if i % 2 else x
        wet[k:k + len(x)] += src * gn
    return out + R._lp(wet, 3500) * mix


def chord(root, iv, octave=0):
    return [mtof(root + i + 12 * octave) for i in iv]


def noise(rng, n):
    return rng.standard_normal((n, 2))


# ------------------------------------------------------------------ загальні шматки груву
def drums(buf, g, t0, nbeats, rng, kick=1.0, clap=True, hat="off", rimshot=False, shaker=False, snare_on=False, vol=1.0):
    for b in range(int(nbeats)):
        tb = t0 + b * g.beat
        pan = 0.45 if b % 2 else -0.45
        if kick:
            add(buf, R.kick(), tb, kick * vol)
        if hat == "off":
            add(buf, R.hat(rng, True, 0.30), tb + g.beat / 2, vol, pan=pan)
        if hat in ("16", "shuf"):
            for s in range(4):
                sw = g.step * 0.2 if (hat == "shuf" and s % 2) else 0
                add(buf, R.hat(rng, False, 0.17 * (1.0 if s % 2 == 0 else 0.55)), tb + s * g.step + sw, vol, pan=-pan * (1 if s % 2 else -1))
        if shaker:
            for s in (1, 3):
                add(buf, R.hat(rng, False, 0.11), tb + s * g.step, vol, pan=0.55 if s == 1 else -0.55)
        if clap and b % 2 == 1:
            add(buf, snare(rng, 0.55) if snare_on else R.clap(rng, 0.7), tb, vol)
        if rimshot and b % 4 == 2:
            add(buf, rim(rng, 0.45), tb + g.step * 2, vol, pan=0.4)


def _corr(x):
    L, Rr = x[:, 0].astype(float), x[:, 1].astype(float)
    return float(np.corrcoef(L, Rr)[0, 1]) if np.std(L) > 0 and np.std(Rr) > 0 else 1.0


def crackle(rng, d, density=7.0, vol=0.05):
    """Вініловий шум: тихий шипіння + рідкісні клацання (для чіл-пакету)."""
    n = int(d * SR)
    hiss = R._lp(rng.standard_normal((n, 2)), 5500) * vol * 0.12
    clicks = np.zeros((n, 2))
    k = int(density * d)
    pos = rng.integers(0, max(1, n - 200), k)
    for p in pos:
        ch = rng.integers(0, 2)
        amp = rng.uniform(0.2, 1.0) * vol
        clicks[p:p + 120, ch] += amp * np.exp(-np.arange(120) / 14.0) * rng.choice([-1, 1])
    return hiss + clicks


def master(x, target, widen=1.25, min_corr=-1.0):
    """Фінішна обробка. Сумісність із моно більше не обмежуємо (слухають у стерео): min_corr=-1 вимикає обмежувач."""
    w = widen
    while True:
        y = R._finish(x.copy(), target, widen=w)
        if _corr(y) >= min_corr or w <= 1.0:
            break
        w = max(1.0, w - 0.15)
    return R.trim_silence(y, -55, 140)


def finalize(x, target):
    y = R.normalize(x.astype(np.float32), target, 0.89)
    return R.trim_silence(y, -55, 140)


def fold_loop(buf, loop_n):
    """Хвости, що вийшли за межі петлі, загортаємо на початок — петля без шва."""
    out = buf[:loop_n].copy()
    tail = buf[loop_n:]
    out[: len(tail)] += tail[: len(out)]
    return out


# ------------------------------------------------------------------ «вау»-ефекти: рух по панорамі й обʼєм
def _lp0(x, fc, order=4):
    """Фільтр низьких частот з нульовою фазою (прохід уперед і назад): його доповнення (x - lp0) —
    чистий фільтр високих без фазового зсуву, тому віднімання басу працює точно."""
    sos = signal.butter(order, fc, btype="low", fs=SR, output="sos")
    return signal.sosfiltfilt(sos, x, axis=0)


def split_low(x, fc=170):
    """Бас (нижче fc) суворо по центру: нижче fc лишаємо лише «середнє» (L+R)/2. Вище fc — окремо
    «середнє» і «бокове», щоб розширення не розфазовувало бас."""
    mid = ((x[:, 0] + x[:, 1]) / 2).astype(np.float64)
    side = ((x[:, 0] - x[:, 1]) / 2).astype(np.float64)
    low = _lp0(mid, fc)
    mid_hi = mid - low
    side_hi = side - _lp0(side, fc)
    low2 = np.stack([low, low], 1).astype(np.float32)
    high = np.stack([mid_hi + side_hi, mid_hi - side_hi], 1).astype(np.float32)
    return low2, high


def balance(x, pan):
    """Панорамування рухом: pan від -1 (лише ліво) до +1 (лише право), по відліках."""
    pan = np.clip(pan, -1, 1)
    left, right = 1 - np.maximum(0, pan), 1 + np.minimum(0, pan)
    return np.stack([x[:, 0] * left, x[:, 1] * right], 1).astype(np.float32)


def ricochet(high, taps=7, first=0.07, spacing=0.085, decay=0.66, start_left=True):
    """Відскок звуку між каналами: кожне повторення різко стрибає ліво <-> право і темнішає."""
    mono = high.mean(axis=1)
    n = len(mono)
    out = np.zeros((n + int((first + taps * spacing) * SR) + 1, 2), np.float32)
    side = -1.0 if start_left else 1.0
    for k in range(taps):
        d = int((first + k * spacing) * SR)
        pan = side * (-1) ** k
        src = R._lp(mono, max(2500, 9000 - 900 * k)) * decay ** (k + 1)
        out[d:d + n, 0] += src * (1 - max(0, pan))
        out[d:d + n, 1] += src * (1 + min(0, pan))
    return out


def mono_bass(x, fc=160):
    """Прибирає з бічної складової все нижче fc: бас суворо моно (без розфазування), решта — широка."""
    side = ((x[:, 0] - x[:, 1]) / 2).astype(np.float64)
    lf = _lp0(side, fc).astype(np.float32)
    return np.stack([x[:, 0] - lf, x[:, 1] + lf], 1).astype(np.float32)


def _wow_raw(x, kind, idx=0):
    """Додає стерео-«вау»: рух по панорамі, відскоки ліво-право і сильне розширення бази (бас лишається по центру).
    kind: sting | intro | outro | weather | bed."""
    n = len(x)
    u = np.arange(n) / n
    dur = n / SR
    low, high = split_low(x)
    if kind == "sting":  # перебивки: звук пролітає з боку в бік і відскакує
        mode = idx % 3
        pan = (np.linspace(-1, 1, n), np.linspace(1, -1, n), np.sin(2 * np.pi * 1.5 * u))[mode]
        h = balance(R._widen(high, 2.6), pan * 0.95)
        ric = ricochet(h, start_left=(mode != 0))
        out = np.zeros_like(ric)
        out[:n] += low + h
        return out + ric * 0.5
    if kind in ("intro", "outro"):  # інтро/аутро: нарощування «пролітає» через усе поле, далі повільне хитання
        pan = 0.6 * np.sin(2 * np.pi * 0.4 * dur * u)
        k = int(n * 0.22)
        pan[:k] = np.linspace(-1, 1, k) * 0.9
        h = balance(R._widen(high, 2.0), pan)
        t0 = max(0, n - int(2.2 * SR))
        tail = np.zeros_like(h)
        tail[t0:] = h[t0:]
        ric = ricochet(tail, taps=6, spacing=0.11, decay=0.7, start_left=(kind == "intro"))
        out = np.zeros_like(ric)
        out[:n] += low + h
        return out + ric * 0.45
    if kind == "weather":  # перебивка погоди: «крапельки» кружляють навколо слухача
        pan = 0.85 * np.sin(2 * np.pi * 1.4 * dur * u)
        h = balance(R._widen(high, 2.6), pan)
        ric = ricochet(h, taps=6, spacing=0.12, decay=0.68)
        out = np.zeros_like(ric)
        out[:n] += low + h
        return out + ric * 0.45
    # bed: повільне хитання, ціле число циклів на петлю (шва немає); під голосом руху небагато
    cycles = max(1, round(dur * 0.22))
    pan = 0.4 * np.sin(2 * np.pi * cycles * u)
    return (low + balance(R._widen(high, 2.6), pan)).astype(np.float32)


def wow(x, kind, idx=0):
    """Стерео-«вау» (рух по панорамі, відскоки ліво-право, сильна база) + бас суворо по центру."""
    return mono_bass(_wow_raw(x, kind, idx))
