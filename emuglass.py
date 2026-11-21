#!/usr/bin/env python3
"""Looking-Glass-style low-latency viewer for an Android emulator.

Frames come from the emulator gRPC API (streamScreenshot) through a shared-memory (MMAP)
file in /dev/shm, are drawn in a native pygame window (GPU-scaled when the SDL renderer is
available), and input goes back over gRPC (streamInputEvent / sendTouch / sendKey).

  emuglass.py --emu my_avd_3 [--pane left|right|both] [--no-mmap] [--port N --token T]
Mouse: left drag = touch, right = Back, middle = Home, wheel = swipe.
Keys (Android Emulator defaults): Ctrl+Backspace/Esc back, Ctrl+H home, Ctrl+O recents, Ctrl+M menu, Ctrl+P power,
Ctrl+=/- volume, Ctrl+Left/Right rotate, Ctrl+S save screenshot (~/Pictures), Ctrl+F fullscreen; also Alt+B/H/S, F1 overlay, F2 save window screenshot.
Mouse: drag = touch, wheel = swipe.  Typing sends text.
"""
from __future__ import annotations

import argparse
import mmap
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PANES = {"both": None, "left": (0, 0, 940, 1800), "right": (960, 0, 940, 1800)}
MMAP_BYTES = 4096 * 4096 * 4


# ---------------------------------------------------------------- pure helpers (unit-tested)
def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def running_dirs(environ=None, home: Path | None = None) -> list[Path]:
    environ = os.environ if environ is None else environ
    home = home or Path.home()
    out = []
    if environ.get("XDG_RUNTIME_DIR"):
        out.append(Path(environ["XDG_RUNTIME_DIR"]) / "avd" / "running")
    out.append(home / ".android" / "avd" / "running")
    out.append(Path(f"/tmp/android-{environ.get('USER', '')}") / "avd" / "running")
    return out


def parse_ini(text: str) -> dict:
    d = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d


def discover(emu: str, dirs=None, proc: Path = Path("/proc")) -> dict:
    """Find a running emulator's gRPC endpoint. emu = avd id/name (my_avd_3), My_Avd_3, or emulator-5558."""
    want = norm(emu)
    for d in dirs if dirs is not None else running_dirs():
        try:
            files = sorted(Path(d).glob("pid_*.ini"))
        except OSError:
            continue
        for f in files:
            pid = f.stem.split("_", 1)[1]
            if pid.isdigit() and proc is not None and not (Path(proc) / pid).exists():
                continue                                     # stale discovery file
            try:
                ini = parse_ini(f.read_text())
            except OSError:
                continue
            keys = {norm(ini.get("avd.id", "")), norm(ini.get("avd.name", "")),
                    norm("emulator-" + ini.get("port.serial", "x"))}
            if want in keys and ini.get("grpc.port"):
                return {"port": int(ini["grpc.port"]), "token": ini.get("grpc.token") or None,
                        "serial": "emulator-" + ini.get("port.serial", "?"), "avd": ini.get("avd.id", emu)}
    raise LookupError(f"no running emulator with gRPC found for {emu!r} "
                      "(was it started with -grpc <port>?)")


def fit_rect(win_w: int, win_h: int, src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """Aspect-preserving centred destination rect (x, y, w, h) of a src inside the window."""
    if min(win_w, win_h, src_w, src_h) <= 0:
        return 0, 0, max(win_w, 1), max(win_h, 1)
    s = min(win_w / src_w, win_h / src_h)
    w, h = max(1, round(src_w * s)), max(1, round(src_h * s))
    return (win_w - w) // 2, (win_h - h) // 2, w, h


def window_to_device(mx: float, my: float, dest: tuple, crop: tuple) -> tuple[int, int] | None:
    """Window pixel -> device pixel. dest = fit_rect; crop = (x0, y0, w, h) source region in device px.
    Returns None when outside the picture."""
    dx, dy, dw, dh = dest
    if not (dx <= mx < dx + dw and dy <= my < dy + dh):
        return None
    cx, cy, cw, ch = crop
    x = cx + int((mx - dx) * cw / dw)
    y = cy + int((my - dy) * ch / dh)
    return min(max(x, cx), cx + cw - 1), min(max(y, cy), cy + ch - 1)


def clamp_crop(pane: str, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """Crop for a pane, scaled if the frame is not the reference 1900x1800 and clipped to the frame."""
    c = PANES[pane]
    if c is None:
        return 0, 0, frame_w, frame_h
    sx, sy = frame_w / 1900, frame_h / 1800
    x, y, w, h = round(c[0] * sx), round(c[1] * sy), round(c[2] * sx), round(c[3] * sy)
    return x, y, max(1, min(w, frame_w - x)), max(1, min(h, frame_h - y))


KEY_ALIASES = {"return": "Enter", "backspace": "Backspace", "tab": "Tab", "delete": "Delete",
               "left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown",
               "home": "Home", "end": "End", "page up": "PageUp", "page down": "PageDown"}


CTRL_KEYS = {          # Android Emulator (Linux/Windows) shortcuts that map to gRPC key events
    "h": ("key", "GoHome"), "backspace": ("key", "GoBack"), "o": ("key", "AppSwitch"),
    "m": ("key", "ContextMenu"), "p": ("key", "Power"),
    "=": ("key", "AudioVolumeUp"), "-": ("key", "AudioVolumeDown"),
    "left": ("local", "rotate_left"), "right": ("local", "rotate_right"),
    "s": ("local", "save_shot"), "f": ("local", "fullscreen"),
}
# Official shortcuts with no gRPC equivalent here (skipped): Ctrl+Up/Down zoom in/out, Ctrl+Z zoom mode,
# Ctrl+Shift+Up/Down/Left/Right pan, Ctrl+Shift+E extended controls, Ctrl+Shift+A / Ctrl+Shift+... virtual-sensor and
# camera shortcuts, Ctrl+Shift+Backspace/others tied to the emulator's own Qt window.


def map_key(name: str, alt: bool = False, ctrl: bool = False):
    """pygame key name -> ('key', DOMKey) | ('text', str) | ('local', action) | None."""
    n = name.lower()
    if ctrl:
        return CTRL_KEYS.get(n)
    if alt:
        return {"b": ("key", "GoBack"), "h": ("key", "GoHome"), "s": ("key", "AppSwitch")}.get(n)
    if n == "escape":
        return ("key", "GoBack")
    if n == "f1":
        return ("local", "overlay")
    if n == "f2":
        return ("local", "screenshot")
    return ("key", KEY_ALIASES[n]) if n in KEY_ALIASES else None


def frame_to_native(fx: int, fy: int, fw: int, fh: int, k: int) -> tuple[int, int]:
    """Frame (rotated, as streamed) point -> touch space (the display's native, unrotated orientation).

    The emulator takes touch coordinates in native space. k = quarter turns clockwise that turn
    the native display into the frame; from Image.format.rotation s (SkinRotation) that is
    k = (4 - s) % 4 (s=3 reverse landscape -> k=1, measured on My_Avd_3). Native size is
    (fw, fh) for even k, (fh, fw) for odd.
    """
    k %= 4
    if k == 0:
        return fx, fy
    if k == 1:                      # native (nx,ny) -> frame (nh-ny, nx)
        return fy, fw - 1 - fx
    if k == 2:
        return fw - 1 - fx, fh - 1 - fy
    return fh - 1 - fy, fx          # k == 3: native (nx,ny) -> frame (ny, nw-nx)


def wheel_swipe(x: int, y: int, dy: int, crop: tuple, step: int = 260):
    """Points of a swipe for a wheel notch (dy>0 = wheel up = finger drags down), clamped to the crop."""
    cx, cy, cw, ch = crop
    y2 = min(max(y + dy * step, cy + 2), cy + ch - 3)
    return [(x, y + (y2 - y) * i // 5) for i in range(6)]


# ---------------------------------------------------------------- gRPC side
class Viewer:
    def __init__(self, port: int, token: str | None, mmap_on: bool = True, display: int = 0):
        import grpc
        from emu_grpc import emulator_controller_pb2 as pb, emulator_controller_pb2_grpc as rpc
        self.grpc, self.pb = grpc, pb
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=[
            ("grpc.max_receive_message_length", 64 << 20), ("grpc.max_send_message_length", 16 << 20)])
        self.stub = rpc.EmulatorControllerStub(self.channel)
        self.meta = (("authorization", f"Bearer {token}"),) if token else ()
        self.display = display
        self.want_mmap = mmap_on
        self.path = None
        self.mm = None
        self.view = None           # memoryview over the mapping
        self.path_used = "starting"
        self.lock = threading.Lock()
        self.frame = None          # (w, h, seq, ts_us, recv_wall_us, buf)
        self.rot = 0               # SkinRotation of the latest frame
        self.frames = 0
        self.dirty = False
        self.stop = threading.Event()
        self.err = None
        self.inq: queue.Queue = queue.Queue()
        self.lat_us = None
        self.debug = False

    # -- frames
    def _open_mmap(self):
        self.path = f"/dev/shm/emuglass_{os.getpid()}"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
        os.ftruncate(fd, MMAP_BYTES)
        self.mm = mmap.mmap(fd, MMAP_BYTES)
        os.close(fd)
        self.view = memoryview(self.mm)

    def _fmt(self, use_mmap):
        pb = self.pb
        f = pb.ImageFormat(format=pb.ImageFormat.RGBA8888, display=self.display)
        if use_mmap:
            f.transport.channel = pb.ImageTransport.MMAP
            f.transport.handle = "file://" + self.path
        return f

    def frame_loop(self):
        use_mmap = self.want_mmap
        if use_mmap:
            try:
                self._open_mmap()
            except OSError as e:
                print(f"[view] mmap setup failed ({e}); using inline", file=sys.stderr)
                use_mmap = False
        while not self.stop.is_set():
            try:
                got = 0
                for img in self.stub.streamScreenshot(self._fmt(use_mmap), metadata=self.meta):
                    if self.stop.is_set():
                        return
                    w, h = img.format.width, img.format.height
                    if not w or not h:
                        continue
                    now_us = int(time.time() * 1e6)
                    self.rot = img.format.rotation.rotation
                    if use_mmap and not img.image:
                        buf = self.view[: w * h * 4]
                        self.path_used = "mmap"
                    elif img.image:
                        buf = img.image
                        if use_mmap:                      # server ignored the handle
                            use_mmap = False
                        self.path_used = "inline"
                    else:
                        continue
                    got += 1
                    with self.lock:
                        self.frame = (w, h, img.seq, img.timestampUs, now_us, buf)
                        self.frames += 1
                        self.dirty = True
                if use_mmap and got == 0:
                    raise RuntimeError("no frames via mmap")
            except Exception as e:                        # noqa: BLE001
                if self.stop.is_set():
                    return
                self.err = str(e)
                if use_mmap and self.frames == 0:
                    print(f"[view] mmap path failed ({e}); falling back to inline", file=sys.stderr)
                    use_mmap = False
                time.sleep(0.5)

    # -- input
    MIN_HOLD_S = 0.04          # Android drops/ignores taps whose down and up arrive in the same instant

    def _evgen(self):
        down_t = 0.0
        while not self.stop.is_set():
            try:
                ev = self.inq.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev.HasField("touch_event"):
                t = ev.touch_event.touches[0]
                if t.pressure:
                    down_t = down_t or time.monotonic()
                else:
                    wait = down_t + self.MIN_HOLD_S - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    down_t = 0.0
            if self.debug:
                print(f"[dbg] gRPC send {str(ev).strip().replace(chr(10), ' ')}", flush=True)
            yield ev

    def input_loop(self):
        pb = self.pb
        while not self.stop.is_set():
            try:
                self.stub.streamInputEvent(self._evgen(), metadata=self.meta)
            except Exception as e:                        # noqa: BLE001
                if self.stop.is_set():
                    return
                self.err = f"input stream: {e}"
                if self.debug:
                    print(f"[dbg] input stream died, reconnecting: {e}", flush=True)
                time.sleep(0.5)

    def _put(self, **kw):
        self.inq.put(self.pb.InputEvent(**kw))

    def touch(self, x, y, down: bool):
        """x, y in frame pixels; converted to the emulator's native touch space."""
        fr = self.frame
        if fr:
            x, y = frame_to_native(x, y, fr[0], fr[1], (4 - self.rot) % 4)
        t = self.pb.Touch(x=x, y=y, identifier=0, pressure=1024 if down else 0)
        self._put(touch_event=self.pb.TouchEvent(touches=[t], display=self.display))

    def key(self, key: str):
        self._put(key_event=self.pb.KeyboardEvent(eventType=self.pb.KeyboardEvent.keypress, key=key))

    def text(self, s: str):
        self._put(key_event=self.pb.KeyboardEvent(text=s))

    def swipe(self, pts, step_s: float = 0.014):
        """Timed swipe on a helper thread (Android needs real time between move events)."""
        def go():
            self.touch(*pts[0], True)
            for p in pts[1:]:
                time.sleep(step_s)
                self.touch(*p, True)
            time.sleep(step_s)
            self.touch(*pts[-1], False)
        threading.Thread(target=go, daemon=True).start()

    def start(self):
        for fn in (self.frame_loop, self.input_loop):
            threading.Thread(target=fn, daemon=True).start()

    def close(self):
        self.stop.set()
        try:
            self.channel.close()
        except Exception:                                  # noqa: BLE001
            pass
        for o in (self.view, self.mm):
            try:
                o.release() if o is self.view else o.close()
            except Exception:                              # noqa: BLE001
                pass
        if self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def proc_cpu_s(pid: int) -> float:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return 0.0


# ---------------------------------------------------------------- UI
def run(args) -> int:
    if args.port:
        info = {"port": args.port, "token": args.token, "serial": "?", "avd": f"port {args.port}"}
    else:
        info = discover(args.emu)
        if args.token:
            info["token"] = args.token
    v = Viewer(info["port"], info["token"], not args.no_mmap)
    v.debug = args.debug
    v.start()

    os.environ.setdefault("SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR", "0")
    import pygame
    pygame.init()
    pygame.display.set_caption(f"emuglass {info['avd']}")
    t0 = time.time()
    while v.frame is None and time.time() - t0 < 15:
        time.sleep(0.02)
    if v.frame is None:
        print(f"[view] no frames from emulator: {v.err}", file=sys.stderr)
        v.close()
        return 2
    fw, fh = v.frame[0], v.frame[1]
    crop = clamp_crop(args.pane, fw, fh)
    try:
        sh = pygame.display.get_desktop_sizes()[0][1]
    except Exception:                                      # noqa: BLE001
        sh = 1000
    sc = min(1.0, (sh - 120) / crop[3])
    size = (max(200, int(crop[2] * sc)), max(200, int(crop[3] * sc)))

    # Smooth (linear) filtering when the frame is shrunk to the window; SDL's default is nearest,
    # which drops pixels and breaks up small text.  Must be set before textures are created.
    os.environ.setdefault("SDL_RENDER_SCALE_QUALITY", "best")
    from pygame._sdl2 import video
    win = video.Window(f"emuglass {info['avd']}", size=size, resizable=True)
    ren = video.Renderer(win)
    ren.draw_color = (0, 0, 0, 255)
    tex = None
    surf = None
    stex, stex_size, use_small = None, None, False
    last_up = 0.0
    cur = (0, 0)
    font = pygame.font.Font(None, 22)
    overlay = args.overlay
    toast, toast_t = "", 0.0
    fullscreen = False
    down = False
    lat_hist = []
    fps_t, fps_n, fps = time.time(), 0, 0.0
    last_frames = 0
    cpu0, wall0 = proc_cpu_s(os.getpid()), time.time()
    shot_done = False
    stats_frames0 = None
    clock = pygame.time.Clock()
    running = True

    def dest_rect():
        ww, wh = win.size
        return fit_rect(ww, wh, crop[2], crop[3])

    def to_dev(pos):
        return window_to_device(pos[0], pos[1], dest_rect(), crop)

    def save_shot(path):
        s = ren.to_surface()
        pygame.image.save(s, path)
        print(f"[view] screenshot -> {path}", flush=True)

    if args.debug:
        print(f"[dbg] video driver={pygame.display.get_driver()} window={win.size} frame={fw}x{fh} crop={crop}", flush=True)
    while running:
        for ev in pygame.event.get():
            if args.debug and ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION,
                                          pygame.MOUSEWHEEL, pygame.KEYDOWN, pygame.WINDOWFOCUSGAINED, pygame.WINDOWRESIZED, pygame.WINDOWSIZECHANGED,
                                          pygame.WINDOWFOCUSLOST):
                pos = getattr(ev, "pos", None)
                print(f"[dbg] {pygame.event.event_name(ev.type)} pos={pos} btn={getattr(ev, 'button', '')} "
                      f"win={win.size} viewport={tuple(ren.get_viewport())} dest={dest_rect()} dev={to_dev(pos) if pos else None}", flush=True)
            if ev.type == pygame.QUIT or (ev.type == pygame.WINDOWCLOSE):
                running = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (2, 3) and not args.view_only:
                v.key("GoHome" if ev.button == 2 else "GoBack")      # like scrcpy: right = Back, middle = Home
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                p = to_dev(ev.pos)
                if p and not args.view_only:
                    down, cur = True, p
                    v.touch(*p, True)
            elif ev.type == pygame.WINDOWFOCUSLOST and down:
                down = False                                  # never leave a contact stuck
                v.touch(cur[0], cur[1], False)
            elif ev.type == pygame.MOUSEMOTION and down:
                p = to_dev(ev.pos)
                if p and p != cur:
                    cur = p
                    v.touch(*p, True)
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1 and down:
                down = False
                v.touch(cur[0], cur[1], False)
            elif ev.type == pygame.MOUSEWHEEL and not args.view_only:
                p = to_dev(pygame.mouse.get_pos())
                if p and ev.y:
                    v.swipe(wheel_swipe(p[0], p[1], 1 if ev.y > 0 else -1, crop))
            elif ev.type == pygame.KEYDOWN and not args.view_only:
                m = map_key(pygame.key.name(ev.key), bool(ev.mod & pygame.KMOD_ALT), bool(ev.mod & pygame.KMOD_CTRL))
                if m is None:
                    pass
                elif m[0] == "key":
                    v.key(m[1])
                elif m == ("local", "overlay"):
                    overlay = not overlay
                elif m == ("local", "screenshot"):
                    save_shot(str(Path(args.screenshot or "/tmp/emuglass.png")))
                elif m == ("local", "save_shot"):
                    d = Path.home() / "Pictures"
                    d.mkdir(exist_ok=True)
                    p = d / f"emuglass_{info['avd']}_{time.strftime('%Y%m%d_%H%M%S')}.png"
                    save_shot(str(p))
                    toast, toast_t = f"saved {p}", time.time()
                elif m == ("local", "fullscreen"):
                    fullscreen = not fullscreen
                    win.set_fullscreen(True) if fullscreen else win.set_windowed()
                elif m[0] == "local" and m[1] in ("rotate_left", "rotate_right"):
                    n_rot = 1 if m[1] == "rotate_right" else 3
                    for _ in range(n_rot):                       # `emu rotate` steps one way only
                        subprocess.run(["adb", "-s", info["serial"], "emu", "rotate"], capture_output=True)
                    toast, toast_t = "rotated - bots expect 1900x1800 landscape", time.time()
            elif ev.type == pygame.TEXTINPUT and not args.view_only:
                if not pygame.key.get_mods() & (pygame.KMOD_ALT | pygame.KMOD_CTRL):
                    v.text(ev.text)

        with v.lock:                                          # cap processed frames at --frame-fps
            fr = v.frame
            newframe = v.dirty and (time.time() - last_up >= 1.0 / max(1, args.frame_fps))
            if newframe:
                v.dirty = False
        if newframe:
            last_up = time.time()
        w, h, seq, ts, recv_us, buf = fr
        if (w, h) != (fw, fh) or surf is None:                # first frame or rotation / resize
            fw, fh = w, h
            crop = clamp_crop(args.pane, fw, fh)
            surf = None
            tex = None
        if newframe:
            if surf is None or surf.get_size() != (w, h) or v.path_used == "inline":
                surf = pygame.image.frombuffer(buf, (w, h), "RGBA")
            if tex is None:
                tex = video.Texture(ren, (w, h), streaming=True)
            _, _, dw0, dh0 = dest_rect()
            if args.smooth and dw0 < crop[2] * 0.95:          # shrinking: area-average on the CPU
                import numpy as np, cv2                       # (GPU linear filtering still breaks up small text)
                cv2.setNumThreads(2)
                x0, y0, cw, ch = crop
                arr = np.frombuffer(buf, np.uint8, count=w * h * 4).reshape(h, w, 4)[y0:y0 + ch, x0:x0 + cw]
                small = np.ascontiguousarray(cv2.resize(arr, (dw0, dh0), interpolation=cv2.INTER_AREA))
                if args.sharpen > 0:                          # unsharp mask: crisper small text
                    blur = cv2.GaussianBlur(small, (0, 0), 1.0)
                    small = np.ascontiguousarray(cv2.addWeighted(small, 1 + args.sharpen, blur, -args.sharpen, 0))
                if stex is None or stex_size != (dw0, dh0):
                    stex, stex_size = video.Texture(ren, (dw0, dh0), streaming=True), (dw0, dh0)
                stex.update(pygame.image.frombuffer(small.tobytes(), (dw0, dh0), "RGBA"))
                use_small = True
            else:
                use_small = False
                if args.pane == "both":
                    tex.update(surf)                          # surface wraps the mmap: no CPU copy
                else:                                         # upload only the visible pane
                    r = pygame.Rect(*crop)
                    tex.update(surf.subsurface(r), area=r)
            fps_n += 1
            # latency: emulator timestamp vs our clock, only meaningful if the clocks agree
            if ts:
                d = recv_us - ts
                if -5e6 < d < 5e6:
                    lat_hist.append(d / 1000.0)
                    lat_hist[:] = lat_hist[-60:]
        now = time.time()
        if now - fps_t >= 1.0:
            fps, fps_n, fps_t = fps_n / (now - fps_t), 0, now

        ren.clear()
        dx, dy, dw, dh = dest_rect()
        if use_small and stex is not None and stex_size == (dw, dh):
            stex.draw(dstrect=pygame.Rect(dx, dy, dw, dh))
        elif tex is not None and not use_small:
            tex.draw(srcrect=pygame.Rect(*crop), dstrect=pygame.Rect(dx, dy, dw, dh))
        elif stex is not None:                                # window resized since the last frame
            stex.draw(dstrect=pygame.Rect(dx, dy, dw, dh))
        if overlay:
            lat = f"{sum(lat_hist)/len(lat_hist):.0f} ms" if lat_hist else "n/a"
            lines = [f"{fps:5.1f} fps   {v.path_used}   {w}x{h}   pane={args.pane}",
                     f"frame age (ts->recv) {lat}   seq {seq}"]
            lines.append("RMB/Esc/Ctrl+Bksp back  MMB/Ctrl+H home  Ctrl+O recents  Ctrl+M menu  Ctrl+P power")
            lines.append("Ctrl+=/- volume  Ctrl+Left/Right rotate  Ctrl+S save shot  Ctrl+F fullscreen  F1 hide")
            if (w, h) != (1900, 1800):
                lines.append("WARNING: frame is not 1900x1800 landscape - bots expect that")
            if v.err:
                lines.append("err: " + v.err[:60])
            ov = pygame.Surface((600, 18 * len(lines) + 8), pygame.SRCALPHA)
            ov.fill((0, 0, 0, 170))
            for i, t in enumerate(lines):
                ov.blit(font.render(t, True, (120, 255, 120)), (6, 4 + 18 * i))
            ot = video.Texture.from_surface(ren, ov)
            ot.blend_mode = 1
            ot.draw(dstrect=pygame.Rect(6, 6, ov.get_width(), ov.get_height()))
        if toast and time.time() - toast_t < 4:
            ts_ = font.render(toast[-90:], True, (255, 230, 120), (0, 0, 0))
            tt = video.Texture.from_surface(ren, ts_)
            tt.draw(dstrect=pygame.Rect(6, win.size[1] - ts_.get_height() - 6, ts_.get_width(), ts_.get_height()))
        ren.present()

        if args.duration:
            if stats_frames0 is None and now - t0 > 2:
                stats_frames0 = (v.frames, now, proc_cpu_s(os.getpid()))
            if args.screenshot and not shot_done and now - t0 > 3:
                save_shot(args.screenshot)
                shot_done = True
            if now - t0 > args.duration:
                running = False
        clock.tick(args.max_fps)

    if stats_frames0:
        f0, tw, c0 = stats_frames0
        dt = time.time() - tw
        print(f"[view] path={v.path_used} size={fw}x{fh} frames={v.frames - f0} over {dt:.1f}s "
              f"=> {(v.frames - f0) / dt:.1f} fps; viewer cpu {(proc_cpu_s(os.getpid()) - c0) / dt * 100:.0f}% of one core; "
              f"frame age ts->recv mean {sum(lat_hist)/len(lat_hist) if lat_hist else float('nan'):.1f} ms "
              f"(n={len(lat_hist)})", flush=True)
    v.close()
    pygame.quit()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emu", help="emulator key / AVD id, e.g. my_avd_3")
    ap.add_argument("--port", type=int, help="gRPC port (skips discovery)")
    ap.add_argument("--token", help="gRPC bearer token")
    ap.add_argument("--pane", choices=list(PANES), default="both")
    ap.add_argument("--frame-fps", type=int, default=30, help="emulator frames processed per second (default 30)")
    ap.add_argument("--sharpen", type=float, default=0.7, help="unsharp-mask amount after downscaling (0 = off; default 0.7)")
    ap.add_argument("--no-smooth", dest="smooth", action="store_false", help="GPU scaling only (no CPU area-average downscale)")
    ap.add_argument("--no-mmap", action="store_true", help="force inline image bytes")
    ap.add_argument("--view-only", action="store_true", help="ignore all input")
    ap.add_argument("--overlay", action="store_true", help="start with the stats overlay on")
    ap.add_argument("--max-fps", type=int, default=120, help="UI loop cap")
    ap.add_argument("--debug", action="store_true", help="log every mouse/key event, mapping and gRPC send")
    ap.add_argument("--duration", type=float, help="exit after N seconds and print stats (testing)")
    ap.add_argument("--screenshot", help="save a window screenshot here (3 s in with --duration; F2 otherwise)")
    args = ap.parse_args(argv)
    if not args.emu and not args.port:
        ap.error("give --emu or --port")
    try:
        return run(args)
    except LookupError as e:
        print(f"emuglass: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
