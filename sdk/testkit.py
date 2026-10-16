#!/usr/bin/env python3
"""Open500 testkit: run a page add-on inside the SH emulator, no hardware needed.

    from testkit import PageTest
    t = PageTest(pg, 'MYADDON.WAV')        # loads your patched firmware + HUB + your add-on
    t.open()                               # makes your page the current page
    t.doit(); print(t.msg())               # press DO IT, read the message line
    t.pad(0, 3)                            # hit pad A04 (press + release)

Firmware: the emulator needs YOUR patched OS (the file the website installer gives you, Akai OS 1.31 + Open500
loader). Nothing from Akai is in this repository. Point to it with the environment variable OPEN500_FIRMWARE
(default: ../MPC500.bin next to this folder).
"""
import os, sys, struct
sys.path.insert(0, os.path.dirname(__file__))
from validate import Harness
from shasm import SHAsm
from mpcmod import EVQ, OS_PUSH_PAGE
from pages import ENABLE_DOIT, SET_FOCUS

FIRMWARE = os.environ.get('OPEN500_FIRMWARE', os.path.join(os.path.dirname(__file__), '..', 'MPC500.bin'))
MSG = 0x88E10000                 # free RAM used to build fake OS messages
EV_KEY, EV_PAD = 2, 0x38
K_DOIT, K_CANCEL = 0x2E, 0x2D
BANKOBJ = 0x8849C98C             # byte 0 = current pad bank (0..3)

class PageTest:
    def __init__(self, pg, wav, firmware=FIRMWARE):
        if not os.path.exists(firmware):
            raise SystemExit("patched firmware not found: %s\nCreate it with the Open500 website installer and set "
                             "OPEN500_FIRMWARE=/path/to/MPC500.bin" % firmware)
        self.pg = pg
        self.h = h = Harness(firmware); h.fill_scratch(); self.e = h.emu
        for ad in (OS_PUSH_PAGE, ENABLE_DOIT, SET_FOCUS): self.stub(ad)    # OS calls we don't need in tests
        h.load_hub(); h.load_wav(wav); h.fire(10)

    def stub(self, addr, fn=lambda s: (s.rts(), s.nop())):
        """replace the OS function at addr with your own tiny SH code (default: return immediately)"""
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): self.h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])

    def open(self, fires=3):
        self.h.ram_w32(EVQ + 0x68, self.pg.page); self.h.fire(fires)

    def close(self, fires=3):
        self.h.ram_w32(EVQ + 0x68, 0x88123456); self.h.fire(fires)

    def event(self, type_, code=0, data=0):
        """send one OS event to your page handler; returns r0 (1 = you consumed it, 0 = the OS handles it)"""
        h, e = self.h, self.e
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG + 4, data & 0xffffffff); h.ram_w32(MSG + 8, 0); h.ram_w32(MSG + 12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = self.pg.page; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = self.pg.handler; e.maxsteps = 50_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler did not return"
        for i in range(8, 16): assert e.r[i] == regs[i], "handler corrupted r%d (r8-r15 must be preserved)" % i
        return e.r[0]

    def doit(self): r = self.event(EV_KEY, K_DOIT); self.h.fire(1); return r
    def cancel(self): r = self.event(EV_KEY, K_CANCEL); self.h.fire(1); return r
    def pad(self, bank, k, vel=100):
        """hit pad k (0..11) of bank (0..3); returns (press result, release result)"""
        self.e.wr(BANKOBJ, bank, 1); return self.event(EV_PAD, k, vel), self.event(EV_PAD, k, 0)
    def fire(self, n=1): return self.h.fire(n)
    def msg(self):
        """message line text (fixed messages are space padded: compare with rstrip())"""
        return bytes(self.e.rd(self.pg.msgt + i, 1) for i in range(16)).split(b'\0')[0].decode('latin-1').rstrip()
    def slot(self, s): return self.h.ram_r32(s)
    def set_slot(self, s, v): self.h.ram_w32(s, v)
    def label(self, lbl, width):
        return bytes(self.e.rd(self.pg.dlabels[lbl] + i, 1) for i in range(width)).decode('latin-1')
    def view(self, n):
        """(ver, payload bytes) of view n in the view box, or None"""
        from mpcmod import VBOX_OFF, VSLOT, V_LEN, V_VER, V_PAY
        B = self.h.ram_r32(0x8848ED34); S = B + VBOX_OFF + n * VSLOT; ln = self.h.ram_r32(S + V_LEN)
        if ln > 0x3B0: return None
        return self.h.ram_r32(S + V_VER), bytes(self.e.rd(S + V_PAY + i, 1) for i in range(ln))
