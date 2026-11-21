"""Start the recorder:  python3 -m acb  [--simulate] [--windowed] [--screenshots DIR]"""

import argparse
import os
import sys


def main():
    p = argparse.ArgumentParser(description="Audio Capture Box touchscreen recorder")
    p.add_argument("--simulate", action="store_true", help="fake interfaces and Wi-Fi (run on any computer)")
    p.add_argument("--windowed", action="store_true", help="run in a window instead of full screen")
    p.add_argument("--screenshots", metavar="DIR", help="save a PNG of each screen and exit (testing)")
    args = p.parse_args()

    if args.simulate:
        from . import simulate
        sd = simulate.FakeSD()
        wifi = simulate.FakeWifi()
        provider = lambda: (simulate.fake_interfaces(), -1)
    else:
        try:
            import sounddevice as sd
        except OSError as e:
            sys.exit(f"Audio library missing ({e}). Run install.sh first.")
        from . import devices, wifi as wifi_mod
        wifi = wifi_mod
        provider = lambda: devices.discover(sd)

    from .ui import App
    app = App(sd, wifi, provider, simulate=args.simulate, windowed=args.windowed or bool(args.screenshots))
    app.run(screenshot_dir=args.screenshots)


if __name__ == "__main__":
    main()
