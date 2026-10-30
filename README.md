# Open500

[![Support Open500](https://img.shields.io/badge/Support-PayPal-0070ba?logo=paypal)](https://paypal.me/SupportOpen500)

New native pages for the **Akai MPC500**, loaded from a WAV file. No hardware mod, fully reversible.

**Video:** [Open500 on the MPC500](https://youtu.be/ETZ7N6kh8hg)

**Website & installer:** `docs/index.html` (GitHub Pages) — drop the official OS 1.31 file in the page and it adds
the Open500 loader in your browser. Nothing is uploaded, and this repository contains no Akai code.

## What's new in 0.2
- No reflash: just copy the new WAVs (delete `CHOPAGE.WAV`, `TRANSCHP.WAV`, `AUTOWARP.WAV`: they are now in Chop Shop).
- Up to 16 tools. Startup splash.
- Chop Shop: BPM, Transient, Tap, Warp. Tap: chop live with the pads.
- Grid Seq: add steps with the pads, playhead.
- Pad Copy to many pads.
- New: Scale Pads, Pad Mute/Solo, Seq Tools (double / half).
- USB MIDI out (beta). Screen in your browser (beta).
- CANCEL goes back one step.
- Tool sources and a Claude skill for making tools.

## Add-ons
| Add-on | File | |
|---|---|---|
| HUB | `HUB.WAV` | add-on manager, load it first (up to 16 add-ons at the same time) |
| Chop Shop | `CHOPSHOP.WAV` | BPM (equal slices), Transient (slices on the hits), Tap (hit the pads while the sample plays), Warp (loop to the sequence tempo); pad preview, mono + one mute group |
| Master FX | `MASTERFX.WAV` | 12 live SP-404-style effects on the whole mix, one per pad (held = on), slider = main parameter, tempo-synced |
| Grid Seq | `GRID.WAV` | 16-step grid sequencer on the display, edits the current sequence while it plays; pads add/remove hits, playhead |
| Sample FX | `SAMPLEFX.WAV` | chain of up to 8 effects (fades, gain, bit crush, downsample, drive, vinyl crackle, silence trim) printed into a new sample; pad preview |
| Pad Tools | `PADTOOLS.WAV` | swap / copy (to many pads) / clear pads (sample, whole pad, or pad + MIDI note); SHIFT+STOP |
| Scale Pads | `SCALEPAD.WAV` | one sound on the 12 pads of a bank, tuned to a scale and key |
| Pad Mute | `PADMUTE.WAV` | mute / solo single pads; SHIFT+TRACK MUTE |
| Seq Tools | `SEQTOOLS.WAV` | double or halve the current sequence |
| Input Thru | `INTHRU.WAV` | line input always open to the outputs (analog pass-through) with level |
| USB MIDI (beta) | `USBMIDI.WAV` | the USB port as a class-compliant MIDI interface: pads and sequences to your computer |
| MIDI Screen (beta) | `MIDISCR.WAV` | display, waveform, slices, pads and mixer in the browser (`docs/screen.html`, Chrome), over USB or MIDI |

## How it works
- The loader (1.3 KB) is written into an unused, all-zero area of the MPC500 OS 1.31 and hooked into the OS main
  loop. It reserves the last 896 KB of sample memory: 384 KB for add-ons, 512 KB of effect buffers (Master FX).
- Add-ons are WAV files with a signed, relocatable payload. After a LOAD, the HUB finds them in the sample list,
  installs them and adds their entry to MODE → OTHER. Their pages are real objects of the OS page framework.
- The bootloader is never touched: flashing the original OS removes everything.

## Make your own tool with Claude Code
Clone the repository, create your patched `MPC500.bin` with the website installer and put it in the repository folder,
then open the folder in [Claude Code](https://claude.com/claude-code). The **open500-addon** skill loads by itself:
describe the tool you want ("a page that tunes the 12 pads to a chord"), and Claude designs the page, writes the add-on
with the SDK, tests it in the emulator and gives you the WAV plus a checklist for the MPC. To use the skill in other
folders, copy `.claude/skills/open500-addon` to `~/.claude/skills/`. Without Claude: start from `sdk/README.md`.

## Share your tool
Open an issue with the **[Submit a tool](https://github.com/M45K/open500/issues/new?template=submit-tool.yml)** form and
paste your `.py` (or open a pull request, see `community/README.md`). When the maintainer approves it, GitHub builds
the WAV from your source, checks it and opens a pull request; once merged it appears in *Community tools* on the website.

## Repository layout
- `docs/` — website, browser patcher (`patcher.js`), patch data (`patch.js`), add-on WAVs (`docs/addons/`), screen
  page (`screen.html`, `o5link.js`) and its protocol notes (`PROTOCOL.md`)
- `sdk/` — add-on SDK (Python → SH-3 code, emulator, testkit) and the source of every official add-on (`sdk/addons/`):
  start from `sdk/README.md`
- `.claude/skills/open500-addon/` — skill for Claude Code: helps you build and test a tool with the SDK
- `community/` — tools shared by users (source + `tool.json`); `tools/community.py` builds and checks them
- `tools/make_patch.py` — builds `docs/patch.js` from the tested firmware and checks the base against the Akai zip
- `tools/test_patcher.js` — `node tools/test_patcher.js`: original OS 1.31 + patch must equal the hardware-tested
  firmware byte for byte, and wrong files must be refused

## License
Open500 is free software under the **GNU GPL v3** (`LICENSE`): you can use, change and share it, and what you
share must stay under the same license. Copyright (C) 2026 M45K and contributors. The Akai firmware is not part of
this repository.

Unofficial project, not affiliated with Akai Professional or inMusic. Tested on one MPC500 with OS 1.31. Use at your own risk.
