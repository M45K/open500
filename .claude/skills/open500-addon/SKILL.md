---
name: open500-addon
description: Build, test and package add-ons for the Akai MPC500 with the Open500 SDK (Python that generates SH-3 code, native OS pages, emulator testing, WAV packaging, remote-screen views). Use when the user wants to create or modify an Open500 add-on, a page in MODE -> OTHER, a pad/sequence/sample tool for the MPC500, or mentions Open500, HUB, PageAddon, testkit, or an add-on WAV.
---

# Open500 add-on development

You are helping someone (often a musician, not a programmer) build an add-on for the Akai MPC500 running Open500.
The SDK is in `sdk/` of the Open500 repository; read `sdk/README.md` and `sdk/examples/hello_page.py` first, every time.

## Workflow (always in this order)
1. **Understand the musical goal** in one or two sentences. Ask only what changes the design: which buttons, what
   the 16x2 display shows, what DO IT does, what must never be destroyed (samples, programs, sequences).
2. **Design the page on paper** first and show it to the user as two 16-character rows, e.g.
   ```
   SCALE PADS      
   MINOR   C  >B   
   ```
   plus the button flow (DO IT -> "Hit source pad" -> pad -> confirm). Anything destructive needs a confirmation
   step and CANCEL.
3. **Copy the structure of the closest official add-on**: `hello_page.py` for a simple page; for pads, programs,
   sequences, samples, audio or MIDI read the matching official source in `sdk/addons/` (table in `sdk/README.md`:
   Pad Tools, Scale Pads, Pad Mute, Seq Tools, Grid Seq, Chop Shop and its modes, Sample FX, Master FX, MIDI Screen,
   USB MIDI) and reuse its addresses and patterns rather than guessing. Several pages in one add-on: Chop Shop.
4. **Write the add-on** as `sdk/examples/<name>.py` (or where the user wants it), with a header comment describing
   the page, the flow, and every OS address used and where it came from.
5. **Write the emulator test in the same file** (`if __name__ == '__main__':` builds the WAV, then `testkit.PageTest`
   exercises every path: normal use, CANCEL, edge cases, "nothing else in memory changed").
6. **Run it** (`python3 sdk/examples/<name>.py`) and fix until it prints OK. Never tell the user it works if you
   did not see the test pass.
7. **Hand over a hardware checklist**: what to copy to the CF card, what to press, what they should see/hear, what to
   do if something goes wrong (SHIFT+ERASE reloads the HUB; power-cycle; the original OS can always be reflashed).
   Be explicit that emulator-tested is not hardware-tested.

## Hard rules
- **Never put Akai firmware in the repository or in messages.** The emulator uses the user's own patched
  `MPC500.bin` (from the Open500 website), via `OPEN500_FIRMWARE`.
- Only use OS addresses that come from the SDK README, the official add-ons, or something you verified in the
  emulator against the user's firmware. If you need a new OS address, say so; do not invent one.
- Never block in fire (no waiting loops), keep fire short, mask interrupts only around memory moves of OS data,
  and restore them.
- Handlers preserve r8-r15. Fire may use r0-r14.
- New add-ons publish a view (`view.py`) so the remote screen can show them; views 1-11 are taken, 12-23 are for
  new add-ons: tell the user to request a number in an issue before sharing.
- Multi-step flows: **CANCEL goes back one step** (`pg.cancel_fn`), only from the first step it leaves the tool.
- Long OS work (Extract, Copy, moving many events) goes **one piece per fire** with a progress message, never in one
  loop inside a handler: the whole MPC is frozen while your code runs.
- Extract / Copy of samples only through `m.os_extract(src_reg, cmd)` (the OS freezes on the 11th copy of a sample
  with a 15-16 character name otherwise). Never Extract with end < start.
- The audition player plays samples 1:1 at 44.1 kHz, applies the sample Tune (+0x30) and ignores start/end when the
  u16 at +0x3E is not 0: clear it around the play call and put it back (see Tap in `sdk/addons/wav_tapchop.py`).
- Do not touch the HUB, the loader or other add-ons' memory. Buffers between the code and the scratch need
  `pg.reserve = SLOT_CODE_MAX`, otherwise the HUB puts another add-on there.

## SH-3 and assembler gotchas (check these first when something fails)
- `pool troppo lontana` → literal pool more than 1 KB away: add `m.pool_jump()` after a block of code.
- `bra/bsr lbl ... fuori range` → use `m.bt_far/m.bf_far`, `a.movl_pool(label, 2); a.jsr(2); a.nop()`, and
  `pg.far_hend = True` for big add-ons.
- Branch delay slots: the instruction after `rts`, `bra`, `jsr`, `jmp` always executes (put `nop` if unsure).
- `cmpgt(rm, rn)`: T = rn > rm (signed). `cmphs(rm, rn)`: T = rn >= rm (unsigned). `cmpge` signed.
- `movb_ld` and `movw_ld` sign-extend: use `extu_b` / `extu_w` for unsigned values.
- `movl_ld_disp` / `movl_st_disp` displacement is at most 60 bytes.
- `mov_imm` is 8-bit signed (-128..127); bigger constants go through `m.imm32`.
- `alloc_slot()` emits start-up code for its default: for large arrays allocate with care.
- Fixed messages are space-padded to 16 chars: compare `msg().rstrip()` in tests.
- Set the redraw flag (EVQ+0x70 = 1) only when your text changed, or the display flickers.

## Explaining to non-programmers
Describe what the add-on does on the machine, not the code. Report test results plainly ("the emulator test
passed: 12 pads tuned, other banks untouched, CANCEL writes nothing"). Keep code details for when they ask.
