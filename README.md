# kc_view: low-latency viewer for headless Android emulators

A Looking-Glass-style viewer for the Android Emulator. Frames come from the emulator's
gRPC `streamScreenshot` API over shared memory (`/dev/shm`, MMAP transport), so nothing is
video-encoded: full resolution, ~60 fps, ~33 ms click-to-pixel. Mouse and keyboard go back
over gRPC (`streamInputEvent`).

## Requirements
- Android Emulator started with gRPC enabled, e.g.
  `emulator -avd MyAvd -port 5554 -no-window -gpu host -grpc 8554`
- Python 3.12 with `grpcio`, `protobuf`, `pygame-ce`

## Usage
```
python kc_view.py --emu MyAvd                 # or emulator-5554, or --port 8554 [--token T]
python kc_view.py --emu MyAvd --pane left     # crop to the left half at full resolution
python kc_view.py --emu MyAvd --debug         # log every input event and mapped coordinates
```

## Controls
| Input | Action |
|---|---|
| Left click / drag | Touch |
| Wheel | Swipe |
| Right click, Esc, Ctrl+Backspace, Alt+B | Back |
| Middle click, Ctrl+H, Alt+H | Home |
| Ctrl+O, Alt+S | Recents |
| Ctrl+M / Ctrl+P | Menu / Power |
| Ctrl+= / Ctrl+- | Volume up / down |
| Ctrl+Left / Ctrl+Right | Rotate |
| Ctrl+S, F2 | Save screenshot |
| Ctrl+F | Fullscreen |
| F1 | Overlay (fps, frame age, transport, help) |

Touches are sent in the display's native (unrotated) coordinates; the viewer reads the
frame's rotation and converts automatically.

## Notes
- `kc_grpc/` is generated from the Android Emulator's `emulator_controller.proto`
  (Android SDK `emulator/lib/`, Apache License 2.0). Regenerate with `grpc_tools.protoc`
  after emulator updates.
- Tests: `python -m pytest tests`
