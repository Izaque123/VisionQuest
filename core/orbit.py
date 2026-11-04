"""
Sand orbit for the HUD centre — a tilted disc of grains around a looking eye.

This replaces the holographic head. The motion is the one from the orbit
prototype: each assistant state has its own radius, speed, thickness and
brightness, and the eye glances, blinks and changes colour with that state.

* Idle drifts. Listening tightens and breathes with the microphone.
* Thinking spins fast and thin, and looks around quickly.
* Speaking widens with the voice. A gold flash marks the moment an answer
  is ready. An error shakes the eye and pulls it toward red.

Software rendered, like the rest of the HUD: grains land in a small image,
the eye is ordinary QPainter. No OpenGL and no extra dependency.
"""

from __future__ import annotations

import math

import numpy as np
from PyQt6.QtCore import QLineF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QRadialGradient

# Disc tilt, in radians. The squash below turns the circle into the flat
# ring; the tilt is what gives it a near and a far edge.
_TILT = -0.32
_CA = math.cos(_TILT)
_SA = math.sin(_TILT)

# Dark-theme palette from the prototype. The HUD background is already
# near black, so the warm sand is the one that reads.
_SAND = (236, 198, 134)
_IRIS = (236, 140, 52)
_PUPIL = (255, 246, 226)
_SAND_ERR = (255, 92, 80)
_IRIS_ERR = (226, 40, 34)
_PUPIL_ERR = (255, 205, 198)
_SAND_GOLD = (255, 226, 150)
_IRIS_GOLD = (255, 196, 70)
_PUPIL_GOLD = (255, 250, 228)

# R radius, spd spin, amp wobble, pulse mic-breath, bright, thick ring width.
_STATES = {
    "ocioso":    dict(R=1.00, spd=0.16, amp=0.000, pulse=0.0, bright=0.85, thick=1.00),
    "ouvindo":   dict(R=0.80, spd=0.09, amp=0.015, pulse=1.0, bright=1.05, thick=0.75),
    "pensando":  dict(R=0.92, spd=0.95, amp=0.020, pulse=0.0, bright=1.15, thick=0.55),
    "falando":   dict(R=1.00, spd=0.26, amp=0.070, pulse=0.0, bright=1.00, thick=1.15),
    "erro":      dict(R=0.96, spd=0.10, amp=0.035, pulse=0.0, bright=0.80, thick=1.10),
    "pronto":    dict(R=1.00, spd=0.22, amp=0.000, pulse=0.0, bright=1.10, thick=0.95),
    "dormindo":  dict(R=0.94, spd=0.07, amp=0.000, pulse=0.0, bright=0.48, thick=0.90),
    "muted":     dict(R=0.90, spd=0.05, amp=0.000, pulse=0.0, bright=0.32, thick=0.85),
}

# Per-channel gain on the sand. Listening cools it, thinking warms it.
_TONE = {
    "ocioso":   (1.00, 1.00, 1.00),
    "ouvindo":  (0.97, 1.03, 1.14),
    "pensando": (1.02, 0.95, 0.72),
    "falando":  (1.04, 1.04, 0.90),
    "erro":     (0.98, 0.80, 0.82),
    "pronto":   (1.04, 1.10, 0.95),
    "dormindo": (0.90, 0.92, 0.96),
    "muted":    (0.70, 0.78, 0.86),
}

# min wait, max wait, how far the pupil may travel, pupil size.
_GAZE = {
    "ocioso":   (1.2, 2.6, 1.00, 0.40),
    "ouvindo":  (1.8, 3.2, 0.30, 0.50),
    "pensando": (0.3, 0.7, 1.00, 0.32),
    "falando":  (0.9, 1.8, 0.75, 0.42),
    "erro":     (0.12, 0.3, 0.60, 0.30),
    "pronto":   (1.4, 2.4, 0.15, 0.55),
    "dormindo": (2.6, 5.0, 0.25, 0.26),
    "muted":    (2.2, 4.4, 0.15, 0.32),
}

# Blink gap scale. Thinking and errors blink more; sleep blinks less.
_BLINK_K = {
    "pensando": 0.6,
    "erro": 0.25,
    "dormindo": 1.8,
    "muted": 1.7,
}

_QUIET = frozenset({"ocioso", "dormindo", "muted"})
_N = 3000
_N_FIBERS = 170
_FIBER_SEGS = 12


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    if k <= 0.01:
        return a
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def _q(rgb: tuple[int, int, int], alpha: float = 1.0) -> QColor:
    c = QColor(int(rgb[0]), int(rgb[1]), int(rgb[2]))
    c.setAlpha(int(max(0.0, min(255.0, alpha * 255.0))))
    return c


class SandOrbit:
    """Animated sand orbit. One instance per HUD canvas.

    Lifecycle:
        orb = SandOrbit()
        orb.step(dt, amp, speaking=..., muted=..., state=...)
        orb.paint(painter, cx, cy, unit)
    """

    def __init__(self) -> None:
        rng = np.random.default_rng(7)
        n = _N
        # Three uniform draws give a soft pile-up around r = 1, same as the
        # prototype, so the ring has a bright middle and a thin outer edge.
        self.r = 1.0 + (rng.random(n) + rng.random(n) + rng.random(n) - 1.5) * 0.3
        self.th = rng.random(n) * math.tau
        self.w = 1.0 / np.power(np.maximum(self.r, 0.4), 1.5)
        self.ph = rng.random(n) * math.tau
        self.sz = 0.6 + rng.random(n) * 1.5
        self.lift = rng.random(n) - 0.5
        self.al = 0.3 + rng.random(n) * 0.7

        nf = _N_FIBERS
        ns = _FIBER_SEGS
        fa = rng.random(nf) * math.tau
        fl = 0.55 + rng.random(nf) * 0.45
        fspeed = 0.5 + rng.random(nf) * 0.6
        total = nf * ns
        self.fa = np.repeat(fa, ns)
        self.flen = np.repeat(fl, ns)
        self.fs = np.repeat(fspeed, ns) * (0.8 + rng.random(total) * 0.4)
        self.fu = rng.random(total)
        self.foff = (rng.random(total) - 0.5) * 0.02
        self.fsz = 1.1 + rng.random(total) * 0.9
        self.fal = 0.45 + rng.random(total) * 0.55
        self.fc = np.repeat(np.arange(nf) % 4 == 0, ns)

        self.spike_a = np.linspace(0.0, math.tau, 64, endpoint=False)
        self.spike_a += (rng.random(64) - 0.5) * 0.05
        self.spike_l = 0.12 + rng.random(64) * 0.2
        self.spike_ph = rng.random(64) * math.tau

        self.t = 0.0
        self.mode = "ocioso"
        self.cur = {k: float(v) for k, v in _STATES["ocioso"].items()}
        self.target = dict(self.cur)
        self.tn = np.ones(3, dtype=np.float64)
        self.gx = self.gy = 0.0
        self.tx = self.ty = 0.0
        self.next_gaze = 0.6
        self.pr = 0.40
        self.blink_at = 1.6
        self.er = 0.0
        self.fl = 0.0
        self.wave_at = -10.0
        self.vol = 0.0
        self.lvl = 0.0
        self.sp = 0.0
        self.amp = 0.0
        self.glance_x = 0.0
        self.glance_y = 0.0
        self.glance_until = -1.0

        self._key: tuple[int, int] | None = None
        self._alpha: np.ndarray | None = None
        self._rgba: np.ndarray | None = None
        self._rgb_of: dict[int, tuple[int, int, int]] = {}
        self._eye_key: tuple[int, int] | None = None
        self._eye_alpha: np.ndarray | None = None
        self._eye_rgba: np.ndarray | None = None

    def fit_unit(self, width: float, height: float) -> float:
        """Largest unit that keeps the tilted disc inside a width × height box.

        Horizontal reach is about 1.95 units, vertical about 1.48, measured
        from the tilted and squashed ring at its loudest.
        """
        return max(36.0, min((width - 12.0) * 0.5 / 1.95,
                             (height - 8.0) * 0.5 / 1.48))

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Look somewhere for `hold` seconds, then wander again.

        `dx` and `dy` are about −1..1. Negative `dy` looks down, which is the
        convention the HUD already uses when a panel opens below the orbit.
        """
        self.glance_x = max(-1.0, min(1.0, float(dx)))
        self.glance_y = max(-1.0, min(1.0, -float(dy)))
        self.glance_until = self.t + max(0.1, float(hold))

    def _resolve(self, speaking: bool, muted: bool, state: str) -> str:
        if muted:
            return "muted"
        st = (state or "").upper()
        if st == "MUTED":
            return "muted"
        if "ERR" in st or "FAIL" in st:
            return "erro"
        if speaking or st == "SPEAKING":
            return "falando"
        if st in ("THINKING", "PROCESSING"):
            return "pensando"
        if st == "LISTENING":
            return "ouvindo"
        if st == "SLEEPING":
            return "dormindo"
        return "ocioso"

    def step(self, dt: float, amp: float = 0.0, speaking: bool = False,
             muted: bool = False, state: str = "") -> None:
        """Advance the orbit. Safe to call faster than the canvas paints."""
        dt = max(0.001, min(0.05, float(dt)))
        self.t += dt
        t = self.t
        try:
            amp = float(amp)
        except (TypeError, ValueError):
            amp = 0.0
        self.amp = max(0.0, min(1.0, amp))

        mode = self._resolve(speaking, muted, state)
        if mode != self.mode:
            # A ripple on the way into a busier state. Settling back to idle,
            # and the hop from "ready" into speech, stay quiet.
            if mode not in _QUIET and not (self.mode == "pronto" and mode == "falando"):
                self.wave_at = t
            if self.mode == "pensando" and mode == "falando":
                self.fl = 1.0
            self.mode = mode
            self.target = {k: float(v) for k, v in _STATES[mode].items()}

        k = 1.0 - math.exp(-dt * 3.0)
        for key, goal in self.target.items():
            self.cur[key] = self.cur[key] + (goal - self.cur[key]) * k
        tone = _TONE[mode]
        self.tn += (np.array(tone, dtype=np.float64) - self.tn) * k

        self.th = np.mod(self.th + self.w * float(self.cur["spd"]) * dt, math.tau)

        flow = 0.6 + float(self.cur["spd"]) * 1.6
        self.fu *= np.maximum(0.0, 1.0 - self.fs * flow * dt)
        self.fu = np.where(self.fu < 0.02, 1.0, self.fu)

        v = self.amp if mode == "ouvindo" else 0.0
        self.vol += (v - self.vol) * (1.0 - math.exp(-dt * 8.0))
        talking = mode == "falando"
        self.sp += ((1.0 if talking else 0.0) - self.sp) * (1.0 - math.exp(-dt * 6.0))
        self.lvl += ((self.amp if talking else 0.0) - self.lvl) * (
            1.0 - math.exp(-dt * (12.0 if talking else 6.0)))

        cfg = _GAZE[mode]
        if t < self.glance_until:
            self.tx = self.glance_x * 0.92
            self.ty = self.glance_y * 0.92
            self.next_gaze = self.glance_until
        elif t >= self.next_gaze:
            ang = float(np.random.random() * math.tau)
            reach = (0.65 + float(np.random.random()) * 0.35) * cfg[2]
            self.tx = math.cos(ang) * reach
            self.ty = math.sin(ang) * reach
            span = cfg[1] - cfg[0]
            self.next_gaze = t + cfg[0] + float(np.random.random()) * span
        kg = 1.0 - math.exp(-dt * 10.0)
        self.gx += (self.tx - self.gx) * kg
        self.gy += (self.ty - self.gy) * kg
        self.pr += (cfg[3] - self.pr) * (1.0 - math.exp(-dt * 4.0))

        want_err = 1.0 if mode == "erro" else 0.0
        self.er += (want_err - self.er) * (1.0 - math.exp(-dt * 8.0))
        self.fl *= math.exp(-dt * 1.4)

        if t > self.blink_at + 0.24:
            scale = _BLINK_K.get(mode, 1.0)
            gap = (0.3 if np.random.random() < 0.2 else 2.5 + float(np.random.random()) * 4.0)
            self.blink_at = t + gap * scale

    def _blink(self) -> float:
        bp = (self.t - self.blink_at) / 0.24
        if 0.0 <= bp <= 1.0:
            return math.sin(bp * math.pi)
        return 0.0

    def _buffer(self, side: int) -> tuple[np.ndarray, np.ndarray]:
        side = max(8, int(side))
        key = (side, side)
        if self._key != key or self._alpha is None or self._rgba is None:
            if self._rgba is not None:
                self._rgb_of.pop(id(self._rgba), None)
            self._alpha = np.zeros((side, side), np.uint8)
            self._rgba = np.zeros((side, side, 4), np.uint8)
            self._key = key
        else:
            self._alpha.fill(0)
        return self._alpha, self._rgba

    def _stamp(self, alpha: np.ndarray, xs: np.ndarray, ys: np.ndarray,
               aval: np.ndarray, size: np.ndarray) -> None:
        h, w = alpha.shape
        xi = np.floor(xs).astype(np.int32)
        yi = np.floor(ys).astype(np.int32)
        ai = np.clip(aval * 255.0, 0.0, 255.0).astype(np.uint8)

        def splat(x: np.ndarray, y: np.ndarray, a: np.ndarray, m: np.ndarray) -> None:
            mm = m & (x >= 0) & (y >= 0) & (x < w) & (y < h)
            if np.any(mm):
                np.maximum.at(alpha, (y[mm], x[mm]), a[mm])

        m = ai > 6
        splat(xi, yi, ai, m)
        big = m & (size >= 1.35)
        if np.any(big):
            splat(xi + 1, yi, ai, big)
            splat(xi, yi + 1, ai, big)
            splat(xi + 1, yi + 1, ai, big)

    def _eye_layer(self, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
        """Small buffer for the iris fibres. The orbit buffer is far larger
        than the eye, and copying it twice per blink was most of the frame."""
        h = max(8, int(h))
        w = max(8, int(w))
        key = (h, w)
        if (self._eye_key != key or self._eye_alpha is None
                or self._eye_rgba is None):
            if self._eye_rgba is not None:
                self._rgb_of.pop(id(self._eye_rgba), None)
            self._eye_alpha = np.zeros((h, w), np.uint8)
            self._eye_rgba = np.zeros((h, w, 4), np.uint8)
            self._eye_key = key
        else:
            self._eye_alpha.fill(0)
        return self._eye_alpha, self._eye_rgba

    def _blit(self, p: QPainter, alpha: np.ndarray, rgba: np.ndarray,
              rgb: tuple[int, int, int], x0: int, y0: int) -> None:
        # Colour changes only when the state tone settles on a new value, so
        # the three colour planes stay put and a frame only rewrites alpha.
        if self._rgb_of.get(id(rgba)) != rgb:
            rgba[:, :, 0] = rgb[0]
            rgba[:, :, 1] = rgb[1]
            rgba[:, :, 2] = rgb[2]
            self._rgb_of[id(rgba)] = rgb
        rgba[:, :, 3] = alpha
        h, w = alpha.shape
        # drawImage reads the buffer before it returns, so the clear below
        # cannot blank what was just painted.
        img = QImage(rgba.data, w, h, int(rgba.strides[0]),
                     QImage.Format.Format_RGBA8888)
        p.drawImage(x0, y0, img)
        alpha.fill(0)

    def paint(self, p: QPainter, cx: float, cy: float, unit: float,
              narrow: bool = False) -> None:
        """Draw the orbit centred at (cx, cy). `unit` is the ring's radius."""
        if unit < 8.0:
            return
        cur = self.cur
        t = self.t
        U = float(unit)
        breath = (1.0
                  + cur["pulse"] * self.vol * 0.22
                  + 0.012 * math.sin(t * 0.9)
                  + self.sp * self.lvl * 0.07)
        bri = (cur["bright"]
               + cur["pulse"] * self.vol * 0.30
               + self.sp * self.lvl * 0.35
               + self.fl * 0.8)
        sand = tuple(int(max(0, min(255, round(_SAND[i] * self.tn[i])))) for i in range(3))

        age = t - self.wave_at
        wr = age * 1.7
        wa = max(0.0, 1.0 - wr / 2.2) if age >= 0.0 else 0.0

        # Soft body of light, then the ripple that marks a state change.
        gr = U * cur["R"] * 0.9
        glow = QRadialGradient(cx, cy, gr)
        glow.setColorAt(0.0, _q(sand, min(0.55, 0.16 * bri)))
        glow.setColorAt(1.0, _q(sand, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2.0, gr * 2.0))

        if wa > 0.02:
            p.save()
            p.translate(cx, cy)
            p.rotate(math.degrees(_TILT))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_q(sand, 0.30 * wa), 1.6))
            rx, ry = wr * U, wr * U * 0.4
            p.drawEllipse(QRectF(-rx, -ry, rx * 2.0, ry * 2.0))
            p.restore()

        base = (1.0 + (self.r - 1.0) * cur["thick"]) * cur["R"] * breath
        u2 = np.mod(self.th / math.tau + 0.25, 1.0)
        fold = np.where(u2 < 0.5, u2, 1.0 - u2)
        sv = np.clip(self.amp * (0.45 + 0.55 * np.sin(fold * math.tau + t * 7.0)), 0.0, 1.0)
        rr = (base
              + cur["amp"] * np.sin(self.th * 5.0 + t * 6.0)
              + 0.012 * np.sin(self.ph + t * 0.8)
              + self.sp * sv * sv * 0.26) * U
        wg = np.zeros(self.th.shape, dtype=np.float64)
        if wa > 0.0:
            dd = (rr / U - wr) / 0.16
            wg = np.exp(-(dd * dd)) * wa
            rr = rr + wg * 0.1 * U

        x = np.cos(self.th) * rr
        y = np.sin(self.th) * rr * 0.4 + self.lift * U * 0.08 * cur["thick"]
        X = x * _CA - y * _SA
        Y = x * _SA + y * _CA
        d = np.sin(self.th)
        depth = 0.5 + 0.5 * (d * 0.5 + 0.5)
        grain_a = np.minimum(1.0, self.al * bri * (1.0 + wg * 1.2) * depth)
        grain_s = self.sz * (0.75 + 0.45 * d) * (1.0 + wg * 0.8)

        pad = int(U * 2.2) + 4
        side = pad * 2
        x0 = int(math.floor(cx - pad))
        y0 = int(math.floor(cy - pad))
        alpha, rgba = self._buffer(side)
        sx = (cx + X) - x0
        sy = (cy + Y) - y0

        hint = p.testRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        back = d < 0.0
        self._stamp(alpha, sx[back], sy[back], grain_a[back], grain_s[back])
        self._blit(p, alpha, rgba, sand, x0, y0)

        self._draw_eye(p, cx, cy, U, narrow, sand)
        self._stamp(alpha, sx[~back], sy[~back], grain_a[~back], grain_s[~back])
        self._blit(p, alpha, rgba, sand, x0, y0)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, hint)
        p.setOpacity(1.0)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_eye(self, p: QPainter, cx: float, cy: float, unit: float,
                  narrow: bool, sand: tuple[int, int, int]) -> None:
        t = self.t
        E = unit * 0.20 * (1.45 if narrow else 1.0)
        Ri = E * 0.6
        amt = self._blink()
        ef = 0.5 + 0.5 * math.tanh(math.sin(t * 14.0) * 3.0)
        kr = self.er * (0.15 + 0.85 * ef)
        kg = min(1.0, self.fl) * 0.85
        eye_sand = _mix(_mix(sand, _SAND_ERR, kr), _SAND_GOLD, kg)
        iris = _mix(_mix(_IRIS, _IRIS_ERR, kr), _IRIS_GOLD, kg)
        pupil = _mix(_mix(_PUPIL, _PUPIL_ERR, kr), _PUPIL_GOLD, kg)

        ecx = cx + math.sin(t * 26.0) * E * 0.1 * self.er
        ecy = cy
        span = E - Ri
        gx = self.gx * span
        gy = self.gy * span
        ix, iy = ecx + gx, ecy + gy
        Rp = Ri * max(0.12, min(0.72, self.pr))
        px, py = ix + gx * 0.12, iy + gy * 0.12

        # Lashes sit outside the blink, so they are drawn before the squash.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_q(eye_sand, 0.40), 1.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        lashes = []
        for i in range(self.spike_a.shape[0]):
            a = float(self.spike_a[i]) + t * 0.03
            r0 = E * 1.2
            r1 = r0 + E * float(self.spike_l[i]) * (1.0 + 0.3 * math.sin(t * 1.4 + float(self.spike_ph[i])))
            ca, sa = math.cos(a), math.sin(a)
            lashes.append(QLineF(ecx + ca * r0, ecy + sa * r0,
                                 ecx + ca * r1, ecy + sa * r1))
        p.drawLines(lashes)

        sy = 1.0 - 0.94 * amt
        p.save()
        p.translate(0.0, ecy * (1.0 - sy))
        p.scale(1.0, sy)

        p.setPen(QPen(_q(eye_sand, 0.30), 1.0))
        p.drawEllipse(QRectF(ecx - E, ecy - E, E * 2.0, E * 2.0))

        gR = Ri * (1.5 + self.fl * 1.3)
        glow = QRadialGradient(px, py, max(1.0, gR))
        glow.setColorAt(0.0, _q(pupil, min(1.0, 0.40 + self.fl * 0.55)))
        glow.setColorAt(1.0, _q(pupil, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QRectF(px - gR, py - gR, gR * 2.0, gR * 2.0))

        p.setBrush(QBrush(_q(iris, 0.18)))
        p.drawEllipse(QRectF(ix - Ri, iy - Ri, Ri * 2.0, Ri * 2.0))
        p.restore()

        # Fibres stream from the iris into the pupil. Positions are squashed
        # by hand so they can be stamped as grains without a second transform.
        ang = self.fa + self.foff + t * 0.04
        span_r = Ri - Rp * 1.04
        rr2 = Rp * 1.04 + self.fu * self.flen * span_r
        fx = ix + np.cos(ang) * rr2
        fy = iy + np.sin(ang) * rr2
        fy = ecy + (fy - ecy) * sy
        fa = (self.fal
              * np.minimum(1.0, self.fu * 14.0)
              * np.minimum(1.0, (1.0 - self.fu) * 8.0 + 0.2))
        fs = self.fsz * (0.8 + 0.3 * self.fu) * (1.3 if narrow else 1.0)
        xmin = int(np.floor(np.min(fx))) - 2
        ymin = int(np.floor(np.min(fy))) - 2
        ew = int(np.ceil(np.max(fx))) + 3 - xmin
        eh = int(np.ceil(np.max(fy))) + 3 - ymin
        ealpha, ergba = self._eye_layer(eh, ew)
        iris_m = ~self.fc
        sand_m = self.fc
        self._stamp(ealpha, fx[iris_m] - xmin, fy[iris_m] - ymin,
                    fa[iris_m], fs[iris_m])
        self._blit(p, ealpha, ergba, iris, xmin, ymin)
        self._stamp(ealpha, fx[sand_m] - xmin, fy[sand_m] - ymin,
                    fa[sand_m], fs[sand_m])
        self._blit(p, ealpha, ergba, eye_sand, xmin, ymin)

        p.save()
        p.translate(0.0, ecy * (1.0 - sy))
        p.scale(1.0, sy)
        rad = max(1.0, Rp * 1.1)
        disc = QRadialGradient(px, py, rad)
        disc.setColorAt(0.0, _q(pupil, 1.0))
        disc.setColorAt(0.8, _q(pupil, 0.92))
        disc.setColorAt(1.0, _q(pupil, 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(disc))
        p.drawEllipse(QRectF(px - rad, py - rad, rad * 2.0, rad * 2.0))
        p.restore()

        if amt > 0.25:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            col = _q(eye_sand, min(1.0, amt * 1.1) * 0.85)
            lg = 1.3
            for i in range(64):
                lx = ecx - E + (i / 63.0) * 2.0 * E
                ly = ecy + math.sin(i * 7.3) * 1.1
                p.fillRect(QRectF(lx, ly - lg * 0.5, lg, lg), col)
