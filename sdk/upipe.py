#!/usr/bin/env python3
# PIPE USB (2026-10-16): due code di byte MIDI grezzi in RAM tra MIDI Screen (protocollo) e USB MIDI (trasporto), cosi'
# lo schermo remoto e il telecomando passano anche dal cavo USB dell'MPC (telefono/computer, niente interfaccia MIDI).
#   B+UPIPE_OFF (fra la cassetta delle viste e la vecchia cassetta di Trans-Chop):
#   +0x00 'O5U1' (scritto da USB MIDI quando ha la porta)   +0x04 battito = contatore frame (USB MIDI a ogni fire, 0 = OFF)
#   +0x08 TXW +0x0C TXR  coda MPC -> USB (MIDI Screen scrive SOLO pacchetti SysEx interi, USB MIDI legge)
#   +0x10 RXW +0x14 RXR  coda USB -> MPC (USB MIDI scrive i byte MIDI ricevuti, MIDI Screen li legge come il MIDI IN)
#   +0x40 dati TX (U_TXSZ)   +0x840 dati RX (U_RXSZ). Indici = offset, scritti DOPO i dati. Un solo produttore e un
#   solo consumatore per coda, tutti e due nel giro principale (fire): niente interrupt in mezzo.
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from mpcmod import SCRATCH_MAGIC, VBOX_OFF, VBOX_N, VSLOT, MBOX_OFF

MGR, SIG2, FRAMES = 0x8848ED30, 0x8B2A3D01, 0x881C05F0
UPIPE_OFF = 0x5E000
UPIPE_MAGIC = 0x3155354F                     # 'O5U1'
U_MAGIC, U_ALIVE, U_TXW, U_TXR, U_RXW, U_RXR = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14
U_TX, U_TXSZ, U_RX, U_RXSZ = 0x40, 0x800, 0x840, 0x400
U_ALIVE_MAX = 22050                          # battito piu' vecchio di 0,5 s = USB spento
assert VBOX_OFF + VBOX_N * VSLOT <= UPIPE_OFF and UPIPE_OFF + U_RX + U_RXSZ <= MBOX_OFF

def emit_base(m, lbl, need_alive=False):
    """subroutine lbl: r0 = B+UPIPE_OFF (firme del loader ok; con need_alive anche 'O5U1' e battito recente), 0 se no.
    Usa r0-r3."""
    a = m.a; no = m.newlabel('upn')
    a.deflabel(lbl)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); m.bt_far(no)
    a.mov_rm_rn(2, 1); a.add_imm(0x40, 1)
    a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 3); a.cmpeq(3, 0); m.bf_far(no)
    a.movl_ld_disp(1, 4, 0); m.imm32(SIG2, 3); a.cmpeq(3, 0); m.bf_far(no)
    m.imm32(UPIPE_OFF, 0); a.add_rr(2, 0)
    if need_alive:
        a.movl_ld(0, 3); m.imm32(UPIPE_MAGIC, 1); a.cmpeq(1, 3); m.bf_far(no)
        a.movl_ld_disp(0, U_ALIVE, 3); a.tst_rr(3, 3); m.bt_far(no)
        m.imm32(FRAMES, 1); a.movl_ld(1, 1); a.sub(3, 1); m.imm32(U_ALIVE_MAX, 3); a.cmphs(3, 1); m.bt_far(no)
    a.rts(); a.nop()
    a.deflabel(no); a.rts(); a.mov_imm(0, 0)
    m.pool_jump()
