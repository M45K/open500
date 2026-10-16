#!/usr/bin/env python3
# VISTE (docs/PROTOCOL.md §5-6, 2026-10-16): un add-on pubblica il suo stato semantico nella cassetta in RAM
# (B+VBOX_OFF, una casella da 1 KB per tipo di vista); il trasmettitore (MIDI Screen, poi USB MIDI) lo spedisce.
# L'add-on non sa nulla di MIDI/USB.
#   v = View(VIEW_MUTE, ver=1)
#   v.emit_subs(m)                    # una volta, fuori dal flusso (definisce le subroutine)
#   v.publish(m, src_addr, nbytes)    # a ogni fire: copia solo se il payload e' cambiato (seq + 1), battito sempre
# Battito: +V_ALIVE = contatore frame dell'OS all'ultimo publish. Il trasmettitore ignora le viste ferme da > 2 s
# (add-on cancellato o HUB ricaricato), cosi' nessuna vista fantasma.
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from mpcmod import (SCRATCH_MAGIC, VBOX_OFF, VBOX_MAGIC, VSLOT, VBOX_N, V_SEQ, V_LEN, V_VER, V_ALIVE, V_PAY, V_PAYMAX)

MGR, SIG2, FRAMES = 0x8848ED30, 0x8B2A3D01, 0x881C05F0
VIEW_CHOP, VIEW_GRID, VIEW_SCALE, VIEW_MUTE, VIEW_MFX, VIEW_SFX, VIEW_PADS, VIEW_THRU = range(1, 9)

class View:
    def __init__(self, view, ver=1):
        assert 1 <= view < VBOX_N
        self.view, self.ver = view, ver
        self.lbl = '__view%d' % view

    def emit_subs(self, m):
        """subroutine <lbl>_pub: r4 = payload, r6 = byte. Clobbera r0-r7, salva PR."""
        a = m.a; L = self.lbl; end = m.newlabel('ve')
        a.movl_pool(end, 2); a.jmp(2); a.nop(); m.pool_jump()
        a.deflabel(L + '_pub'); x = m.newlabel('vx')
        # r5 = casella (B dal limite sample MGR[1], valido solo con le firme del loader)
        m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); m.bt_far(x)
        a.mov_rm_rn(2, 1); a.add_imm(0x40, 1)
        a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 3); a.cmpeq(3, 0); m.bf_far(x)
        a.movl_ld_disp(1, 4, 0); m.imm32(SIG2, 3); a.cmpeq(3, 0); m.bf_far(x)
        m.imm32(VBOX_OFF, 5); a.add_rr(2, 5)
        m.imm32(VBOX_MAGIC, 0); a.movl_st(0, 5)                                              # intestazione
        m.imm32(self.view * VSLOT, 0); a.add_rr(0, 5)
        m.imm32(FRAMES, 1); a.movl_ld(1, 0); a.movl_st_disp(0, V_ALIVE, 5)                  # battito
        m.imm32(V_PAYMAX, 0); a.cmphs(0, 6); cl = m.newlabel('vcl'); a.bf_lbl(cl); a.mov_rm_rn(0, 6); a.deflabel(cl)   # (r6 > max -> max)
        # cambiato? (lunghezza, versione o un byte diverso)
        chg = m.newlabel('vch'); same = m.newlabel('vsa')
        a.movl_ld_disp(5, V_LEN, 0); a.cmpeq(6, 0); a.bf_lbl(chg)
        a.movl_ld_disp(5, V_VER, 0); a.mov_imm(self.ver, 1); a.cmpeq(1, 0); a.bf_lbl(chg)
        a.mov_rm_rn(4, 1); a.mov_rm_rn(5, 2); a.add_imm(V_PAY, 2); a.mov_rm_rn(6, 3)
        a.tst_rr(3, 3); a.bt_lbl(same)
        cp = m.newlabel('vcp'); a.deflabel(cp)
        a.movb_ld(1, 0); a.movb_ld(2, 7); a.cmpeq(7, 0); a.bf_lbl(chg); a.add_imm(1, 1); a.add_imm(1, 2); a.dt(3); a.bf_lbl(cp)
        a.bra_lbl(same); a.nop()
        a.deflabel(chg)
        a.movl_st_disp(6, V_LEN, 5); a.mov_imm(self.ver, 0); a.movl_st_disp(0, V_VER, 5)
        a.mov_rm_rn(4, 1); a.mov_rm_rn(5, 2); a.add_imm(V_PAY, 2); a.mov_rm_rn(6, 3)
        cz = m.newlabel('vcz'); a.tst_rr(3, 3); a.bt_lbl(cz)
        cw = m.newlabel('vcw'); a.deflabel(cw); a.movb_ld(1, 0); a.movb_st(0, 2); a.add_imm(1, 1); a.add_imm(1, 2); a.dt(3); a.bf_lbl(cw)
        a.deflabel(cz)
        a.movl_ld_disp(5, V_SEQ, 0); a.add_imm(1, 0); a.movl_st_disp(0, V_SEQ, 5)           # seq per ULTIMO
        a.deflabel(same)
        a.deflabel(x); a.rts(); a.nop()
        m.pool_jump()
        a.deflabel(end)

    def publish(self, m, src, nbytes):
        """asm: pubblica nbytes da src (indirizzo costante, es. uno slot della scratch). Clobbera r0-r7."""
        m.imm32(src, 4); m.imm32(nbytes, 6); m.a.movl_pool(self.lbl + '_pub', 2); m.a.jsr(2); m.a.nop()
