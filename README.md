# Audio Capture Box

A touchscreen multitrack recorder for Raspberry Pi 4. Plug in a USB mixer or audio
interface and record every input to its own WAV file, with no laptop at the gig.

Built for a Pi 4 with the Waveshare 4.3" DSI touchscreen (800×480), but any
800×480 display works.

## Features

- Finds class-compliant USB interfaces automatically, including several at once
- Level meter, clip indicator (holds for 10 s, tap to clear) and scrolling waveform per channel
- Arm or disarm each channel; tap the box above a pair to join it as one stereo track
- 44.1 / 48 / 96 kHz, 16 / 24-bit or 32-bit float
- Records to the SD card or a USB thumb drive, with time remaining calculated from free space
- Plays back takes through the headphone jack or an interface's outputs
- Joins Wi-Fi from the screen (on-screen keyboard)
- **Send to computer:** download recordings in a web browser, or copy them with `scp`

## Install on the Pi

On the Pi (over SSH):

```bash
sudo apt install -y git
git clone https://github.com/cfort6/Audio-Capture-Box.git
cd Audio-Capture-Box
./install.sh
```

The installer adds the needed packages, gives the recorder permission to mount USB
drives and change Wi-Fi, and sets it to start full-screen every time the Pi boots.
Reboot once afterwards so the group changes take effect: `sudo reboot`.

## Update

```bash
cd ~/Audio-Capture-Box
git pull
sudo systemctl restart audio-capture-box
```

## Where recordings go

Each take is a folder with one WAV per track plus a `take.json` with its settings:

```
~/recordings/2026-11-21_201530_Take-3/
    UMC_01.wav  UMC_02.wav  UMC_03-04.wav  take.json
```

On a USB drive the same folders go in `recordings/` at the top of the drive.
Files that could pass 4 GB (very long, high-resolution sets) are written as RF64,
which most DAWs open like normal WAVs.

## Getting files onto a computer

On the recorder, tap **Takes → Send to computer**, then either:

- open `http://recorder.local:8080` in a web browser on the same Wi-Fi network, or
- run `scp -r yourname@recorder.local:~/recordings .` in a terminal.

(Use your Pi's hostname and username. The screen shows the exact address and command.)

## Useful commands

```bash
systemctl status audio-capture-box       # is it running?
journalctl -u audio-capture-box -e       # see errors
sudo systemctl stop audio-capture-box    # stop it (e.g. to use the console)
sudo systemctl start audio-capture-box
```

## Try it on a computer

You can run the interface on any computer with Python, pygame, numpy and soundfile,
using simulated interfaces and Wi-Fi:

```bash
python3 -m acb --simulate --windowed
```

## Known limits (version 0.1)

- **Multiple interfaces aren't clock-synced yet.** Each records on its own clock, so on
  long sets their tracks can drift slightly against each other. The Clock source setting
  is saved and written to each take, and syncing is planned for the next version.
  A single interface is unaffected.
- Playing a take through an interface needs the interface to support the take's sample rate.
