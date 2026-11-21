<p align="center"><img src="assets/icon_256.png" width="128" alt="emu-glass"></p>

# emu-glass

A low-latency alternative to **scrcpy** for the **local Android Emulator**.

**scrcpy** encodes the screen to video inside the emulated device, using the guest's software
encoder. **emu-glass** instead reads frames straight from the emulator's GPU output over gRPC and
shared memory: full resolution, no compression artifacts, about 33 ms from touch to pixel (measured).

It only works with the Android Emulator on the same machine. For real devices or remote machines,
use **scrcpy**.

## Dependencies

- Android Emulator started with gRPC enabled (`-grpc <port>`)
- Python 3.10+
- `grpcio`, `protobuf`, `pygame-ce`, `numpy`, `opencv-python`

```
pip install grpcio protobuf pygame-ce numpy opencv-python
```

## Run

```
emulator -avd MyAvd -no-window -gpu host -grpc 8554
python emuglass.py --emu MyAvd            # or --emu emulator-5554, or --port 8554
```

Options: `--pane left|right`, `--frame-fps 30|60`, `--sharpen 0..1`, `--view-only`, `--debug`.

## Controls

| Input | Action |
|---|---|
| Left click / drag | Touch |
| Wheel | Swipe |
| Right click, Esc, Ctrl+Backspace | Back |
| Middle click, Ctrl+H | Home |
| Ctrl+O | Recents |
| Ctrl+M / Ctrl+P | Menu / Power |
| Ctrl+= / Ctrl+- | Volume up / down |
| Ctrl+Left / Ctrl+Right | Rotate |
| Ctrl+S | Save screenshot |
| Ctrl+F | Fullscreen |
| F1 | Overlay (fps, frame age, help) |

`emu_grpc/` is generated from the emulator's `emulator_controller.proto` (Android SDK, Apache 2.0).
