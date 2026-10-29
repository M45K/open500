# Open500 add-on SDK

Write your own native pages for the Akai MPC500. An add-on is a WAV file: the user LOADs it like a sample and
the HUB installs it into MODE → OTHER. You write it in **Python**, which generates **SH-3 machine code** through a
small assembler, and you test it in an **emulator** before touching the hardware.

> Status: early. The API works and runs every official add-on, but names and details may still change.
> The **source of every official add-on** is in `sdk/addons/` (Open500 0.2). Comments and test output inside the
> core files and the add-ons are mostly in Italian (the project started that way); this README and the example are
> the reference.

## With Claude Code
Open the repository folder in Claude Code: the `open500-addon` skill (`.claude/skills/`) loads by itself and follows
this README, the example and the official add-ons. Describe the tool; it builds it, runs the emulator test and tells
you what to try on the MPC.

## What you need
- Python 3.9+ (no other packages).
- **Your patched firmware**: open the Open500 website, drop the official Akai OS 1.31 file, save the result as
  `MPC500.bin` in the repository root (or set `OPEN500_FIRMWARE=/path/to/it`). The emulator runs this file.
  Nothing from Akai is in this repository: never commit firmware (`*.bin` is git-ignored).

## Quick start
```
python3 sdk/examples/hello_page.py
```
Builds `HELLO.WAV` and tests it in the emulator. Copy it to the CF card next to `HUB.WAV`, LOAD both, then
MODE → OTHER → "Hello". Read `examples/hello_page.py`: it is 40 lines and shows every basic piece.

## Official add-ons (source)
Each file builds its WAV and runs its full emulator test (same `MPC500.bin` / `OPEN500_FIRMWARE` as above):
```
python3 sdk/addons/wav_padtools.py        # -> sdk/PADTOOLS.WAV, identical to docs/addons/PADTOOLS.WAV
```
| Source | WAV | Good example of |
|---|---|---|
| `addons/wav_chopshop.py` | `CHOPSHOP.WAV` | several pages in one add-on (menu + 4 modes) |
| `addons/wav_autochop2.py`, `wav_transchop.py`, `wav_tapchop.py`, `wav_autowarp.py` | (pages of Chop Shop) | samples: start/end, Extract, audition, pads during playback |
| `addons/wav_samplefx.py` | `SAMPLEFX.WAV` | processing a sample into a new one (Copy/Extract, preview) |
| `addons/wav_padtools.py` | `PADTOOLS.WAV` | program pads, multi-step flows with confirmation and CANCEL |
| `addons/wav_scalepads.py`, `wav_padmute.py` | `SCALEPAD.WAV`, `PADMUTE.WAV` | pads, views for the remote screen |
| `addons/wav_grid.py`, `wav_seqtools.py` | `GRID.WAV`, `SEQTOOLS.WAV` | reading and editing the sequence |
| `addons/wav_masterfx.py`, `wav_inthru.py` | `MASTERFX.WAV`, `INTHRU.WAV` | audio: effects on the mix, line input |
| `addons/wav_midiscreen.py`, `wav_usbmidi.py` | `MIDISCR.WAV`, `USBMIDI.WAV` | MIDI and USB, the remote-screen protocol |
| `hub.py` | `HUB.WAV` | the add-on manager itself |
They are the best documentation of the OS: every address they use is commented where it was found.

## How it works (one minute)
- The Open500 loader (in the patched OS) loads the **HUB**; the HUB scans the sample list after every LOAD and
  installs add-on WAVs (magic `MPCADDN3` + checksum, relocatable code) into free memory pages. Up to 16 add-ons.
- An add-on with a page gets an entry in MODE → OTHER. The page and its widgets are **real OS objects**: the OS
  draws them and handles cursor, wheel, DO IT, CANCEL. You only write what is special: what DO IT does, what
  happens on each frame, how pads/keys behave.
- Your code runs ~300 times per second ("fire") from the OS main loop, plus in the page handler when an event arrives.
- There is no cryptographic signature: anyone can build add-ons.

## Anatomy of a page add-on
```python
from pages import PageAddon, ld, st, sti
pg = PageAddon("myaddon1", open_press=None)   # 8-char unique name. open_hold/open_press = optional key combo
pg.other_menu("My Tool")                      # MODE -> OTHER entry (16 chars)
pg.label("MY TOOL", 0, 0)                     # static text (x, y)
L   = pg.dlabel(10, 0, 6)                     # dynamic text: you write bytes at pg.addr['dlabels'][L]
N   = pg.value("N:", 0, 1, fmt=" 0", lo=1, hi=99, default=8)    # number field, OS edits it, returns its slot
OP  = pg.choice(" ", 6, 1, ["COPY", "SWAP"])  # list field (stored as a byte at its slot)
pg.selector()                                 # optional: the OS sample selector on row 0
pg.message()                                  # optional: 2-second message line
X   = pg.alloc_slot()                         # private 32-bit variable (scratch RAM, survives between fires)
pg.on_fire(fn)        # fn(m): every frame while the page is open
pg.on_inactive(fn)    # fn(m): every frame while it is NOT open (keep it tiny)
pg.on_doit(fn)        # fn(m): DO IT pressed
pg.on_event(0x38, fn) # fn(m, hret0, hret1): pad events (code = pad 0..11, data = velocity, 0 = release)
pg.on_event(2, fn)    #                       key presses (code = key, see mpcmod.EVCODE)
pg.cancel_fn = fn     # fn(m, hret1): CANCEL inside a multi-step flow (go back one step, see Rules)
pg.build_wav("MYTOOL.WAV")
```
In an event handler, `r8` points to the event `{u16 type, u16 code, s32 data}`; finish with a jump to `hret1`
("I consumed it") or `hret0` ("let the OS handle it"), like `examples` and the official add-ons do.

### Writing the code
`m` is the add-on builder, `m.a` the assembler (`mov_imm`, `movl_ld`, `movl_st`, `add_rr`, `cmpeq`, `bt_lbl`…, see
`shasm.py`). Helpers:
- `ld(m, slot, r)` / `st(m, r, slot)` / `sti(m, value, slot)`: read/write your variables.
- `m.imm32(value_or_label, r)`: load any 32-bit constant or label address.
- `m.newlabel('x')`, `a.deflabel(l)`, `m.bt_far(l)` / `m.bf_far(l)`: branches of any length.
- `m.pool_jump()`: drop the literal pool here (needed every ~1 KB of code, see Pitfalls).
- `pg.emit_message(m, "Done")`, `pg.emit_message_dec(m, "Copied ", slot, " pads")`.

### Several pages in one add-on
`p = pg.add_page("name")` returns a page with the same API (selector, value, choice, label, dlabel, message,
on_doit, on_fire, on_inactive, on_event, cancel_fn). Same code and same scratch; its labels get the prefix `name:`
and it has its own message timer (`p.SHOWMSG`). The OTHER entry opens the first page; open the others with
`OS_PUSH_PAGE(EVQ, p.page, 0)`, and CANCEL goes back (the OS page stack). A module can build alone **or** as a page of
another add-on: `pg = page_or_addon("name", ...)` (when `pages.HOST` is set it returns `HOST.add_page(...)`). Chop Shop
does exactly this with Auto Chop, Trans-Chop, Tap and Auto Warp. `p.alloc_buf(n)` reserves scratch bytes without a
start value (tables). The scratch is 4 KB, checked at build time.

### Views (remote screen / companion app)
New add-ons should publish their state for the remote screen (protocol notes: `docs/PROTOCOL.md`, receiver:
`docs/o5link.js`):
```python
from view import View
v = View(23, ver=1)          # views 1-11 are taken, 12-23 are free: ask in an issue to get a number assigned
v.publish(m, BUF_SLOT, n)    # in fire/inactive: copies only when the bytes changed
v.emit_subs(m)               # once, outside the normal code flow
```

## Useful OS facts (MPC500 OS 1.31)
| What | Where |
|---|---|
| Current program | `0x8837B5AC + idx*0x1F98`, idx = u16 at `0x8847C0E2` |
| Pad block (48 pads) | program + `0x18 + pad*0xA4`: 4 sample layers × 0x18 (name16, +0x11 level, +0x14 tune s16 cents), +0x62 poly/mono, +0x63 mute group, +0x8F mixer level |
| Pad → MIDI note | program + `0x1ED8` (48 bytes); note → pad at + `0x1F08` |
| Current bank | byte 0 of `0x8849C98C` (0 = A) |
| Sample list | manager `0x8848ED30`: +8 current sample block (+4 name, +0x18 length, +0x20 start, +0x24 end, +0x30 tune s16 cents, PCM from +0x100) |
| Current sequence | `*(u32*)0x8847CBC8`: +0x1C bars, +0x30 bar table, events 16 B from +0x1C10 (tick, track, 0x90, note, vel, dur) |
| UI event queue | `0x88483EE4` (+0x68 current page, +0x70 = 1 → redraw) |
Addresses are for OS 1.31 only. More in the official add-ons' source headers.

## Rules
- **Never block.** No waiting loops on hardware in fire; keep each fire short (the audio and MIDI clock run from
  interrupts, but the UI and the other add-ons share your time).
- Only touch memory the OS will not move under you; when you move OS data (sequence arena, sample list) do it with
  interrupts masked (`stc sr` / `or #0xF0` / `ldc sr`, see Seq Tools) and restore them.
- Handlers must preserve `r8-r15` (the testkit checks it). Fire code may use `r0-r14`.
- Code + objects ≤ 28 KB; private scratch ≤ 4 KB. The HUB packs add-ons in 4 KB pages: memory between the end of your
  code and your scratch belongs to OTHER add-ons, unless you reserve it (`pg.reserve = SLOT_CODE_MAX`, as Master FX
  and Trans-Chop do for their buffers).
- **DO IT and CANCEL** (12 LEVELS / FULL LEVEL) work as DO IT / CANCEL only while the OS flags are on; the
  framework keeps both on while one of your pages is open and restores FULL LEVEL when you leave. In a flow with
  several steps **CANCEL goes back one step** (`pg.cancel_fn`); only from the first step it leaves.
- **Long OS work, one piece per fire.** While your handler or fire runs, the MPC is frozen (display, keys,
  sequencer). Extracting 12 slices is 12 fires with a progress message (see Tap), not one loop.
- **Extract / Copy only through `m.os_extract(src_reg, cmd)`.** The OS names the new sample by bumping the digits
  at the end of the source name; with a 15-16 character name there is room for one digit only, and the 11th copy of
  the same sample searches forever (MPC frozen). `os_extract` shortens the source name to 13 characters during the
  call and puts it back. Never Extract with end < start (negative length = the OS copies ~4 G samples).
- **The audition player** (what you hear in TRIM and from the official add-ons' previews) plays samples 1:1 at
  44.1 kHz whatever their rate, applies the sample Tune (+0x30), and plays the WHOLE sample ignoring start/end when
  the u16 at +0x3E is not 0 (WAVs from sample packs): clear it around the play call and put it back.
- Test in the emulator first; then on hardware with a backup of your work. Flashing the original OS always restores
  the machine (the bootloader is never touched).

## Pitfalls (each one cost us an evening)
- `pool troppo lontana` (pool too far): literals must be within 1 KB → add `m.pool_jump()` in long code.
- `bra/bsr ... fuori range`: short branches reach ±4 KB → use `m.bt_far`, `movl_pool(label)+jsr/jmp`, and
  `pg.far_hend = True` for add-ons bigger than ~4 KB.
- `alloc_slot()` also generates start-up code for its default value: allocate big arrays with care.
- `cmpgt(rm, rn)` sets T when **rn > rm** (SH operand order).
- Fixed messages are padded to 16 characters with spaces.
- The OS reads the display text from your dynamic labels: set the redraw flag (`EVQ+0x70 = 1`) only when the text
  changed, or the LCD flickers.

## Testing
`testkit.PageTest` loads firmware + HUB + your WAV and lets you open the page, press DO IT/CANCEL, hit pads, read
the message line, your variables and your view. See the end of `examples/hello_page.py`.

## Sharing
Open an issue or a pull request with your add-on source. Add-ons are distributed as WAV files; please include the
`.py` so others can learn from it.
