#!/usr/bin/env python3
"""HELLO PAGE - the smallest complete Open500 add-on: a native page with a value, DO IT, a message and a view.

    python3 sdk/examples/hello_page.py          -> builds HELLO.WAV and tests it in the emulator

On the MPC500: copy HELLO.WAV to the CF card, LOAD it (after HUB.WAV), then MODE -> OTHER -> "Hello".
  row 0: "HELLO OPEN500"           row 1: "Times: 3"  (turn the wheel to change it)
  DO IT -> message "Said hello 03x" for ~2 s, and the counter is published as view 23 for the remote screen.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti
from view import View

pg = PageAddon("hello1", open_press=None)     # 8-char internal name; open_press=None: opened only from MODE -> OTHER
pg.other_menu("Hello")                        # entry in MODE -> OTHER (max 16 chars)
pg.label("HELLO OPEN500", 0, 0)               # static text at column 0, row 0
TIMES = pg.value("Times:", 0, 1, fmt=" 0", lo=1, hi=9, default=3)   # editable number: the OS handles cursor and wheel
pg.message()                                  # enables the ~2 s message line (row 0)
SAID = pg.alloc_slot()                        # a 32-bit variable in your add-on's private RAM ("scratch")
view = View(23, ver=1)                        # views 12-23 are free for new add-ons (see docs/PROTOCOL.md)

def fire(m):
    """runs ~300 times per second while the page is open. Keep it short. r0-r14 are free here."""
    view.publish(m, SAID, 4)                  # cheap: only copies when the value changed
    m.a.movl_pool('__hello_end', 2); m.a.jmp(2); m.a.nop(); m.pool_jump()
    view.emit_subs(m)                         # the view's subroutines must live outside the normal flow
    m.a.deflabel('__hello_end')
pg.on_fire(fire)

def doit(m):
    """runs when DO IT is pressed on your page"""
    a = m.a
    ld(m, SAID, 0); ld(m, TIMES, 1); a.add_rr(1, 0); st(m, 0, SAID)       # SAID += TIMES
    pg.emit_message_dec(m, "Said hello ", TIMES, "x")
pg.on_doit(doit)

if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', '..', 'HELLO.WAV'); pg.build_wav(out)
    from testkit import PageTest
    t = PageTest(pg, out); t.open()
    assert t.slot(TIMES) == 3, "default value"
    assert t.doit() == 1 and t.msg() == "Said hello 03x", t.msg()
    t.set_slot(TIMES, 5); t.doit(); t.fire(1)
    assert t.slot(SAID) == 8 and t.view(23) == (1, (8).to_bytes(4, 'little')), t.view(23)
    print("OK: HELLO.WAV built and tested (DO IT, message, value, view 23)")
