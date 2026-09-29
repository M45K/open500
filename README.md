# Open500

[![Support Open500](https://img.shields.io/badge/Support-PayPal-0070ba?logo=paypal)](https://paypal.me/SupportOpen500)

New native pages for the **Akai MPC500**, loaded from a WAV file. No hardware mod, fully reversible.

**Video:** [Open500 on the MPC500](https://youtu.be/ETZ7N6kh8hg)

**Website & installer:** `docs/index.html` (GitHub Pages) — drop the official OS 1.31 file in the page and it adds
the Open500 loader in your browser. Nothing is uploaded, and this repository contains no Akai code.

## Add-ons
| Add-on | File | |
|---|---|---|
| HUB | `HUB.WAV` | add-on manager, load it first (up to 8 add-ons at the same time) |
| Auto Chop | `CHOPAGE.WAV` | equal slices by BPM onto the pads; pad preview, hit the first pad, mono + one mute group |
| Trans-Chop | `TRANSCHP.WAV` | slices on the transients (sensitivity), cut just before each hit; pad preview, hit the first pad |
| Auto Warp | `AUTOWARP.WAV` | fit a loop to the sequence tempo with the MPC's own time stretch; bars guessed; pad preview |
| Sample FX | `SAMPLEFX.WAV` | chain of up to 8 effects (fades, gain, bit crush, downsample, drive, vinyl crackle, silence trim) printed into a new sample; pad preview |
| Pad Tools | `PADTOOLS.WAV` | swap / copy / clear pads (sample, whole pad, or pad + MIDI note); SHIFT+STOP |
| Master FX | `MASTERFX.WAV` | 12 live SP-404-style effects on the whole mix, one per pad (held = on), slider = main parameter, tempo-synced |
| Grid Seq | `GRID.WAV` | 16-step grid sequencer on the display, edits the current sequence while it plays; arrows + QLINK |
| Input Thru | `INTHRU.WAV` | line input always open to the outputs (analog pass-through) with level |

## How it works
- The loader (1.3 KB) is written into an unused, all-zero area of the MPC500 OS 1.31 and hooked into the OS main
  loop. It reserves the last 896 KB of sample memory: 384 KB for add-ons, 512 KB of effect buffers (Master FX).
- Add-ons are WAV files with a signed, relocatable payload. After a LOAD, the HUB finds them in the sample list,
  installs them and adds their entry to MODE → OTHER. Their pages are real objects of the OS page framework.
- The bootloader is never touched: flashing the original OS removes everything.

## Repository layout
- `docs/` — website, browser patcher (`patcher.js`), patch data (`patch.js`), add-on WAVs
- `tools/make_patch.py` — builds `docs/patch.js` from the tested firmware and checks the base against the Akai zip
- `tools/test_patcher.js` — `node tools/test_patcher.js`: original OS 1.31 + patch must equal the hardware-tested
  firmware byte for byte, and wrong files must be refused

Unofficial project, not affiliated with Akai Professional or inMusic. Tested on one MPC500 with OS 1.31. Use at your own risk.
