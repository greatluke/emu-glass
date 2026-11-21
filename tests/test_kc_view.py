import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kc_view as kv

INI = """grpc.port=8558
avd.id=KC_Phone_3
avd.name=KC Phone 3
port.serial=5558
"""


def _mk(tmp, pid, body):
    d = Path(tmp) / "avd" / "running"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"pid_{pid}.ini").write_text(body)
    return d


def test_discover_by_name_and_serial():
    with tempfile.TemporaryDirectory() as t:
        proc = Path(t) / "proc"
        (proc / "123").mkdir(parents=True)
        d = _mk(t, 123, INI + "grpc.token=abc\n")
        for name in ("kc_phone_3", "KC_Phone_3", "KC Phone 3", "emulator-5558"):
            r = kv.discover(name, [d], proc)
            assert r["port"] == 8558 and r["token"] == "abc" and r["serial"] == "emulator-5558"
        try:
            kv.discover("kc_phone", [d], proc)
            assert False
        except LookupError:
            pass


def test_discover_skips_stale_and_noauth():
    with tempfile.TemporaryDirectory() as t:
        proc = Path(t) / "proc"
        (proc / "9").mkdir(parents=True)
        d = _mk(t, 9, INI)
        _mk(t, 77, INI.replace("8558", "9999"))          # pid 77 not alive -> stale
        r = kv.discover("kc_phone_3", [d], proc)
        assert r["port"] == 8558 and r["token"] is None


def test_fit_and_map_roundtrip():
    dest = kv.fit_rect(1000, 1000, 1900, 1800)           # width-limited
    assert dest[2] == 1000 and dest[3] == 947 and dest[0] == 0 and dest[1] == 26
    crop = (0, 0, 1900, 1800)
    assert kv.window_to_device(500, 513, dest, crop) == (950, 925)
    assert kv.window_to_device(0, 10, dest, crop) is None            # letterbox
    assert kv.window_to_device(0, 26, dest, crop) == (0, 0)
    x, y = kv.window_to_device(999, 972, dest, crop)
    assert 1896 <= x <= 1899 and 1795 <= y <= 1799


def test_pane_crop_mapping():
    crop = kv.clamp_crop("right", 1900, 1800)
    assert crop == (960, 0, 940, 1800)
    dest = kv.fit_rect(470, 900, crop[2], crop[3])
    assert kv.window_to_device(0, 0, dest, crop) == (960, 0)
    x, y = kv.window_to_device(469, 899, dest, crop)
    assert 1895 <= x <= 1899 and 1795 <= y <= 1799
    assert kv.clamp_crop("left", 1900, 1800) == (0, 0, 940, 1800)
    assert kv.clamp_crop("both", 1800, 1900) == (0, 0, 1800, 1900)


def test_key_map():
    assert kv.map_key("escape") == ("key", "GoBack")
    assert kv.map_key("b", alt=True) == ("key", "GoBack")
    assert kv.map_key("h", alt=True) == ("key", "GoHome")
    assert kv.map_key("s", alt=True) == ("key", "AppSwitch")
    assert kv.map_key("a") is None                    # plain letters arrive as TEXTINPUT
    assert kv.map_key("return") == ("key", "Enter")
    assert kv.map_key("backspace") == ("key", "Backspace")
    assert kv.map_key("f1") == ("local", "overlay")
    assert kv.map_key("x", alt=True) is None


def test_wheel_swipe_clamped():
    pts = kv.wheel_swipe(100, 1700, -1, (0, 0, 940, 1800))
    assert all(2 <= y < 1800 for _, y in pts) and pts[0] == (100, 1700)


def test_frame_to_native_rotations():
    # KC_Phone_3 measured: SkinRotation 3 -> k=1, 1900x1800 frame, native 1800x1900
    assert kv.frame_to_native(310, 590, 1900, 1800, 1) == (590, 1589)
    assert kv.frame_to_native(0, 0, 1900, 1800, 1) == (0, 1899)
    assert kv.frame_to_native(1899, 1799, 1900, 1800, 1) == (1799, 0)
    assert kv.frame_to_native(5, 7, 1800, 1900, 0) == (5, 7)
    assert kv.frame_to_native(5, 7, 1800, 1900, 2) == (1794, 1892)
    # k=3 is the inverse quarter turn of k=1
    fx, fy = 310, 590
    nx, ny = kv.frame_to_native(fx, fy, 1900, 1800, 3)
    assert (nx, ny) == (1800 - 1 - fy, fx)


def test_emulator_default_shortcuts():
    m = lambda n, **k: kv.map_key(n, **k)
    assert m("h", ctrl=True) == ("key", "GoHome")
    assert m("backspace", ctrl=True) == ("key", "GoBack")
    assert m("o", ctrl=True) == ("key", "AppSwitch")
    assert m("p", ctrl=True) == ("key", "Power")
    assert m("=", ctrl=True) == ("key", "AudioVolumeUp") and m("-", ctrl=True) == ("key", "AudioVolumeDown")
    assert m("left", ctrl=True) == ("local", "rotate_left") and m("right", ctrl=True) == ("local", "rotate_right")
    assert m("s", ctrl=True) == ("local", "save_shot")
    assert m("up", ctrl=True) is None                     # zoom: not mappable
    assert m("h") is None and m("o") is None              # plain typing is text, never a shortcut
    assert m("escape") == ("key", "GoBack") and m("b", alt=True) == ("key", "GoBack")
