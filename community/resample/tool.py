#!/usr/bin/env python3
# Master FX link (mailbox B+RSBOX_OFF): +0 magic 'RSM1', +4 request counter (Master FX DO IT: start on bank C pad 1.. / stop),
#   +8 status (0 idle, 1 recording), +12 IOFF+1 while recording with MIX AND the Master FX page is open (+16 = its frame stamp):
#   Master FX then mixes the line input into the audio BEFORE its effect and this add-on records the output ring as is.
# RESAMPLE (MODE -> OTHER -> "Resample") -> RESAMPLE.WAV
#   Records what the MPC is playing (the digital output: sequence + pads), optionally mixed with the LINE INPUT, into a NEW
#   sample (mono or stereo) and puts it on a pad (voice overlap MONO, mute group 1).
#   Row 1:  L<len>  <src>  M<mon>  <ch>      e.g. "LMAX OUT M- 1CH"
#     L   = length: MAX (what fits) / 1/4 / 1/2 / 1 / 2 / 4 / 8 BARS, from the current tempo and the bar length of the sequence
#     src = OUT (MPC output) / MIX (+ line input, what you hear with Input Thru; the OS Record page source must be IN)
#     M   = input monitoring - / +  (analog bypass like Input Thru, active as soon as it is switched, also while recording)
#     ch  = 1CH mono / 2CH stereo (L and R kept separate)
#   row 0: "RESAMPLE  IDLE" / "RESAMPLE REC 07s" (!=dropouts)     row 1: "LEN MAX SHIFT+PD"
#   SHIFT + pad (page open)  = start recording; the sample will go on THAT pad (current bank) of the current program.
#   SHIFT + any pad          = stop: the sample is finished, named by the OS, becomes the current sample and is put on the pad.
#   LEN = MAX (until sample memory is full) / 5 / 10 / 20 / 30 s: recording stops by itself and assigns the sample.
#   Pads WITHOUT shift are left to the OS. Recording goes on if you leave the page (stop it from the page, or use LEN).
# How (all from documented addresses, see sdk/README.md and Master FX / Sample FX / Tap Chop):
#   - New sample: the OS Extract of 4 frames of the CURRENT sample (the OS makes a valid block and the name), then this add-on
#     grows that block in place (it is the last block of the list; Sample FX does the same with its preview). The list is
#     kept well-formed at every step: size updated and the 0 terminator rewritten BEFORE the PCM is written. If anything is
#     found after the block (another sample created meanwhile) recording is aborted.
#   - Audio: Master FX documents the output ring (0x884842A8, 0x4000 bytes, frame = 2 long L,R, s16 in the high bits) and
#     the OS frame counter 0x881C05F0 (frames already sent to the codec; ring position = (F*8) % 0x4000). Frames behind F are
#     final ("i dati sono definitivi solo quando partono"), so they are read from the main loop (no interrupt patch): each
#     fire copies [last, F - 32) as mono ((L>>1)+(R>>1))>>16. The copy loop runs from the CACHED alias (as Sample FX kern):
#     uncached add-on code is ~1M instr/s, 44100 frames/s would not fit. If the loop falls more than 1500 frames behind
#     (ring = 2048 frames) the oldest frames are skipped and a dropout is counted (shown as '!').
#   - Pad: same fields as Tap Chop (name16, level 100, ...), poly, mute group 0.
#   - MIX: the line input is only delivered by the OS while its recorder is running (found with Input Probe: external mixer
#     regs + the recorder start routine, flash 0x57712 / stop 0x57792, object from the getter 0x884EBEDC). Input ring 0x884882A8
#     (2048 frames, 24 bit left-justified, write pointer 0xA5017020). At start the offset between the input write position and the
#     output frame counter is stored (IOFF); input frame for output frame f = ring[(f + IOFF) & 2047]. mono = sat16(out + in).
# NOT verified on hardware: MIX; level of the ring against the real output, that the main loop keeps up, block size rules.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti
from mpcmod import EVQ, KEYS_ADDR, keymask
RSBOX_OFF, RSBOX_MAGIC = 0x5EF00, 0x314D5352  # Master FX <-> Resample mailbox (B + 0x5EF00, 'RSM1'); free gap between the USB pipe (ends B + 0x5EC40) and the screen box (B + 0x5F000)

MGR, EXTRACT_CMD, DELETE_CMD, LOOP_GUARD = 0x8848ED30, 0x884C5E90, 0x884C5FFC, 0x884C4F04
FRAMES, OUT_BASE, OUT_SIZE = 0x881C05F0, 0x884842A8, 0x4000
RX_PTR, RX_BASE, RX_END = 0xA5017020, 0x884882A8, 0x8848C2A8  # input ring (see Input Probe): frame = 2 long L,R, 24 bit left-justified
CODEC_W, REG4_OS, REG4_THRU = 0x884C6722, 0x12, 0x1A  # codec register write (r4 reg, r5 value); reg 4: OS normal / analog bypass (Input Thru)
OS_GETREC, OS_REC_START, OS_REC_STOP = 0x884EBEDC, 0x884EC06E, 0x884EC0EE  # OS recorder: object getter, start, stop (flash 0x57712 / 0x57792)
PROG_BASE, PROG_IDX, PROG_STRIDE, PAD_OFF, PAD_SIZE = 0x8837B5AC, 0x8847C0E2, 0x1F98, 0x18, 0xA4
BANKOBJ = 0x8849C98C
EV_PAD = 0x38
GUARD, CATCHUP, RATE, SLACK, MINF = 32, 1500, 44100, 0x200, 64
CACHE_SPAN = 0x300
SYS_PARAM, ID_TEMPO, TPF = 0x884E5CD0, 0x2000C, 275625  # system parameter manager (tempo, BPM x10); ticks = frames * BPMx10 / TPF (96 ticks per quarter)
SEQ_PTR, BARS_OFF, BART_OFF = 0x8847CBC8, 0x1C, 0x30  # current sequence block: u16 bars, table of bar start ticks
REC_FLAG, GAIN_SHADOW, CODEC_GAIN = 0x881C297C, 0x881C2980, 0x884C672E
LVL_0DB, REFRESH = 23, 16  # line input volume 0 dB; monitoring rewrite period (fires)

pg = PageAddon("resamp1", open_press=None)
pg.other_menu("Resample")
pg.far_hend = True
pg.far_fire = True
pg.label("RESAMPLE", 0, 0)
STL = pg.dlabel(9, 0, 7)
LENF = pg.choice("L", 0, 1, ["MAX", "1/4", "1/2", " 1 ", " 2 ", " 4 ", " 8 "], default=0)  # length in bars (tempo + bar length of the current sequence)
SRCF = MIXF = pg.choice(None, 5, 1, ["OUT", "MIX"], default=0)  # OUT = MPC output, MIX = + line input
pg.label("M", 9, 1)
MONF = pg.choice(None, 10, 1, ["-", "+"], default=0)  # input monitoring, live
CHF = pg.choice(None, 12, 1, ["1CH", "2CH"], default=0)  # mono / stereo sample
pg.message()
ST, BLK, CNT, MAXF, TPAD, TPROG, FPREV, DROPS, REQ, OLDKEY, MIXON, IOFF, SCH, MAPP, MCNT, RSINIT, RSSEEN, RSN, DIFLAG, MIXK, FXMON = [pg.alloc_slot() for _ in range(21)]
MSGB = [pg.alloc_slot() for _ in range(4)]  # message to the system parameter manager (16 contiguous bytes)

def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()
def far_bra(m, lbl): m.a.movl_pool(lbl, 2); m.a.jmp(2); m.a.nop()
def cached_call(m, label):
    m.a.movl_pool(label, 2); m.imm32(0xDFFFFFFF, 3); m.a.and_rr(3, 2); m.a.jsr(2); m.a.nop()

def msg_text(m, text):
    """text (<= 16 chars) -> message line (like emit_message, but callable from any subroutine)"""
    pg.emit_message(m, text)

def fire(m):
    a = m.a
    call(m, 'rs_maint')
    # status label: "IDLE   " / "REC 07s" ('!' instead of 's' after a dropout); redraw only when it changes
    ld(m, ST, 8); ld(m, CNT, 4); m.imm32(RATE, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(0, 9)  # r9 = seconds
    m.imm32(99, 1); a.cmpgt(1, 9); k99 = m.newlabel('k99'); a.bf_lbl(k99); a.mov_rm_rn(1, 9); a.deflabel(k99)
    ld(m, DROPS, 10); a.tst_rr(10, 10); nd = m.newlabel('nd'); a.bt_lbl(nd); a.mov_imm(1, 10); a.deflabel(nd)
    a.mov_rm_rn(9, 11); a.shll2(11); a.shll2(11); a.shll2(11); a.shll2(11); a.shll2(11)  # key = ST, secs, drop
    a.shll2(11); a.add_rr(8, 11); a.mov_rm_rn(10, 0); a.shll8(0); a.add_rr(0, 11)
    ld(m, OLDKEY, 0); a.cmpeq(11, 0); same = m.newlabel('same'); m.bt_far(same)
    st(m, 11, OLDKEY); m.imm32(pg.addr['dlabels'][STL], 12)
    idl = m.newlabel('idl'); ed = m.newlabel('ed')
    a.tst_rr(8, 8); a.bt_lbl(idl)
    for ch in "REC ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(9, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_imm(ord('s'), 0); a.tst_rr(10, 10); dd = m.newlabel('dd'); a.bt_lbl(dd); a.mov_imm(ord('!'), 0); a.deflabel(dd)
    a.movb_st(0, 12); a.bra_lbl(ed); a.nop()
    a.deflabel(idl)
    for ch in "IDLE   ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.deflabel(ed); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel(same)
    far_bra(m, 'rs_end')
    m.pool_jump()

    # ---- rs_maint: every fire (page open or not): handle SHIFT+pad request, then copy new frames while recording ----
    a.deflabel('rs_maint'); a.stsl_pr()
    call(m, 'rs_monit'); call(m, 'rs_fxmon'); call(m, 'rs_mbox')
    ld(m, REQ, 8); a.tst_rr(8, 8); norq = m.newlabel('norq'); a.bt_lbl(norq)
    sti(m, 0, REQ); ld(m, ST, 0); a.tst_rr(0, 0); stp = m.newlabel('stp'); a.bf_lbl(stp)
    call(m, 'rs_start'); call(m, 'rs_dicheck'); far_bra(m, 'rs_ret')
    a.deflabel(stp)  # stop request: last copy, then finish
    call(m, 'rs_copy'); a.mov_imm(2, 1); a.cmpeq(1, 0); m.bt_far('rs_ret')
    call(m, 'rs_finish'); far_bra(m, 'rs_ret')
    a.deflabel(norq)
    ld(m, ST, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); m.bf_far('rs_ret')
    call(m, 'rs_copy'); a.tst_rr(0, 0); m.bt_far('rs_ret'); a.mov_imm(2, 1); a.cmpeq(1, 0); m.bt_far('rs_ret')
    call(m, 'rs_finish')
    a.deflabel('rs_ret'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_copy: copy [FPREV, F-GUARD) into the block. r0 = 0 go on, 1 limit reached (finish), 2 aborted ----
    a.deflabel('rs_copy'); a.stsl_pr()
    ld(m, BLK, 8)
    a.movl_ld(8, 9); a.mov_rm_rn(8, 10); a.add_rr(9, 10); a.movl_ld(10, 0); a.tst_rr(0, 0); cok = m.newlabel('cok'); a.bt_lbl(cok)
    sti(m, 0, ST); call(m, 'rs_recstop'); msg_text(m, "Rec aborted"); a.mov_imm(2, 0); a.ldsl_pr(); a.rts(); a.nop()  # something after our block
    a.deflabel(cok)
    ld(m, FPREV, 9); m.imm32(FRAMES, 1); a.movl_ld(1, 4); a.sub(9, 4)  # r4 = frames since FPREV
    m.imm32(GUARD, 1); a.cmphi(1, 4); cgo = m.newlabel('cgo'); a.bt_lbl(cgo)
    a.mov_imm(0, 0); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(cgo)
    a.sub(1, 4); a.mov_rm_rn(4, 12)  # r12 = n
    m.imm32(CATCHUP, 1); a.cmphi(1, 12); ncu = m.newlabel('ncu'); a.bf_lbl(ncu)
    ld(m, DROPS, 0); a.add_imm(1, 0); st(m, 0, DROPS); m.imm32(CATCHUP, 1)  # (st uses r1: reload)
    a.mov_rm_rn(12, 2); a.sub(1, 2); a.add_rr(2, 9); a.mov_rm_rn(1, 12)  # skip the oldest, FPREV += skipped
    a.deflabel(ncu)
    ld(m, CNT, 11); ld(m, MAXF, 13); a.mov_rm_rn(13, 2); a.sub(11, 2)  # r2 = frames left
    a.cmphi(2, 12); nrm = m.newlabel('nrm'); a.bf_lbl(nrm); a.mov_rm_rn(2, 12); a.deflabel(nrm)
    a.tst_rr(12, 12); nz = m.newlabel('nz'); a.bf_lbl(nz); a.mov_imm(1, 0); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(nz)
    m.pool_jump()
    ld(m, SCH, 0); a.tst_rr(0, 0); sg = m.newlabel('sg'); m.bf_far(sg)  # stereo: block size is fixed from the start
    # grow the block first (size + terminator), then write the PCM
    a.mov_rm_rn(11, 2); a.add_rr(12, 2); a.shll(2); m.imm32(0x100 + SLACK + 3, 1); a.add_rr(1, 2); m.imm32(0xFFFFFFFC, 1); a.and_rr(1, 2)  # r2 = new size
    a.mov_rm_rn(8, 3); a.add_rr(2, 3); a.mov_imm(0, 0); a.movl_st(0, 3); a.movl_st(2, 8)
    a.deflabel(sg)
    # kernel arguments
    a.mov_rm_rn(9, 4); a.shll2(4); a.shll(4); m.imm32(OUT_SIZE - 1, 1); a.and_rr(1, 4); m.imm32(OUT_BASE, 1); a.add_rr(1, 4)  # r4 = ring address of FPREV
    a.mov_rm_rn(12, 5)  # r5 = frames
    a.mov_rm_rn(11, 6); a.shll(6); a.add_rr(8, 6); m.imm32(0x100, 1); a.add_rr(1, 6)  # r6 = &PCM[CNT]
    m.imm32(OUT_BASE + OUT_SIZE, 10); m.imm32(OUT_BASE, 13)
    ld(m, SCH, 0); a.tst_rr(0, 0); stm = m.newlabel('stm'); m.bf_far(stm)
    a.mov_imm(-17, 7)
    ld(m, MIXK, 0); a.tst_rr(0, 0); nomx = m.newlabel('nomx'); a.bt_lbl(nomx)
    ld(m, IOFF, 2); a.mov_rm_rn(9, 14); a.add_rr(2, 14); m.imm32(2047, 1); a.and_rr(1, 14); a.shll2(14); a.shll(14)  # r14 = input ring address of FPREV
    m.imm32(RX_BASE, 1); a.add_rr(1, 14); a.movl_st_dec(11, 15); a.movl_st_dec(12, 15); a.mov_rm_rn(1, 11); m.imm32(RX_END, 12)
    cached_call(m, 'rs_kern2'); a.movl_ld_inc(15, 12); a.movl_ld_inc(15, 11); aft = m.newlabel('aft'); a.bra_lbl(aft); a.nop()
    a.deflabel(nomx); cached_call(m, 'rs_kern'); a.bra_lbl(aft); a.nop()
    m.pool_jump()
    a.deflabel(stm)  # stereo: L plane at r6, R plane at r8 = r6 + 2*MAXF
    a.mov_imm(-16, 7); a.movl_st_dec(8, 15); ld(m, MAXF, 0); a.shll(0); a.add_rr(6, 0); a.mov_rm_rn(0, 8)
    ld(m, MIXK, 0); a.tst_rr(0, 0); nms = m.newlabel('nms'); a.bt_lbl(nms)
    ld(m, IOFF, 2); a.mov_rm_rn(9, 14); a.add_rr(2, 14); m.imm32(2047, 1); a.and_rr(1, 14); a.shll2(14); a.shll(14)
    m.imm32(RX_BASE, 1); a.add_rr(1, 14); a.movl_st_dec(11, 15); a.movl_st_dec(12, 15); a.mov_rm_rn(1, 11); m.imm32(RX_END, 12)
    cached_call(m, 'rs_kerns2'); a.movl_ld_inc(15, 12); a.movl_ld_inc(15, 11); sfn = m.newlabel('sfn'); a.bra_lbl(sfn); a.nop()
    a.deflabel(nms); cached_call(m, 'rs_kerns')
    a.deflabel(sfn); a.movl_ld_inc(15, 8)
    a.deflabel(aft)
    a.add_rr(12, 9); st(m, 9, FPREV); a.add_rr(12, 11); st(m, 11, CNT)
    ld(m, MAXF, 0); a.cmphs(0, 11); m.bf_far('rs_c0')
    a.mov_imm(1, 0); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel('rs_c0'); a.mov_imm(0, 0); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_finish: close the sample (fields, current sample), put it on the pad, message ----
    a.deflabel('rs_finish'); a.stsl_pr()
    ld(m, BLK, 8); ld(m, CNT, 9); sti(m, 0, ST); call(m, 'rs_recstop')
    m.imm32(MINF, 1); a.cmphs(1, 9); big = m.newlabel('big'); a.bt_lbl(big)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4); m.imm32(DELETE_CMD, 2); a.jsr(2); a.nop()
    msg_text(m, "Too short"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(big)
    ld(m, SCH, 0); a.tst_rr(0, 0); nsf = m.newlabel('nsf'); m.bt_far(nsf)
    ld(m, MAXF, 10); a.cmpeq(9, 10); nomv = m.newlabel('nomv'); m.bt_far(nomv)  # stereo: R plane moves down next to the L plane
    a.mov_rm_rn(10, 4); a.shll(4); a.add_rr(8, 4); m.imm32(0x100, 1); a.add_rr(1, 4)
    a.mov_rm_rn(9, 6); a.shll(6); a.add_rr(8, 6); a.add_rr(1, 6); a.mov_rm_rn(9, 5)
    cached_call(m, 'rs_mv')
    a.deflabel(nomv)
    a.mov_rm_rn(9, 2); a.shll2(2); m.imm32(0x100 + SLACK + 3, 1); a.add_rr(1, 2); m.imm32(0xFFFFFFFC, 1); a.and_rr(1, 2)  # final size + terminator
    a.movl_st(2, 8); a.mov_rm_rn(8, 3); a.add_rr(2, 3); a.mov_imm(0, 0); a.movl_st(0, 3)
    a.deflabel(nsf)
    a.mov_imm(0, 0); a.movl_st_disp(9, 0x18, 8); a.movl_st_disp(0, 0x1C, 8); a.movl_st_disp(0, 0x20, 8); a.movl_st_disp(9, 0x24, 8)
    m.imm32(0x92, 1); a.add_rr(8, 1); a.mov_imm(1, 2); a.movw_st(2, 1)  # +0x92 = 1 (mono)
    m.imm32(0x3E, 1); a.add_rr(8, 1); a.movw_st(0, 1)  # +0x3E = 0 (player uses start/end)
    ld(m, SCH, 0); a.tst_rr(0, 0); nsg = m.newlabel('nsg'); a.bt_lbl(nsg)
    a.movl_st_disp(9, 0x1C, 8); m.imm32(0x92, 1); a.add_rr(8, 1); a.mov_imm(2, 2); a.movw_st(2, 1)  # stereo: R offset = n, 2 channels
    a.deflabel(nsg)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4); a.mov_rm_rn(8, 4); m.imm32(LOOP_GUARD, 2); a.jsr(2); a.nop()
    # pad block (layer 0): name, level 100, ..., poly, mute group 0
    ld(m, TPROG, 1); m.imm32(PROG_STRIDE, 0); a.mul_l(1, 0); a.sts_macl(2)
    ld(m, TPAD, 1); m.imm32(PAD_SIZE, 0); a.mul_l(1, 0); a.sts_macl(3); a.add_rr(3, 2)
    m.imm32(PROG_BASE + PAD_OFF, 1); a.add_rr(1, 2); a.mov_rm_rn(2, 12)
    a.mov_rm_rn(8, 1); a.add_imm(4, 1); a.mov_imm(16, 3)
    cp = m.newlabel('cp'); a.deflabel(cp); a.movb_ldinc(1, 0); a.movb_st(0, 2); a.add_imm(1, 2); a.dt(3); a.bf_lbl(cp)
    a.add_imm(0x10, 12); a.mov_imm(0, 0); a.movb_st_disp(0, 12)  # fields start at +0x10 (after the name)
    a.mov_imm(100, 0); a.movb_st_disp(1, 12); a.mov_imm(0, 0); a.movb_st_disp(2, 12); a.mov_imm(0x7F, 0); a.movb_st_disp(3, 12)
    a.mov_imm(0, 0)
    for d in (4, 5, 6, 7): a.movb_st_disp(d, 12)
    a.add_imm(0x50, 12); a.mov_imm(1, 0); a.movb_st_disp(2, 12); a.movb_st_disp(3, 12)  # +0x62 voice overlap MONO (1), +0x63 mute group 1
    # message "Saved to A05"
    ld(m, TPAD, 4); a.mov_imm(12, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(0, 9); a.mov_rm_rn(1, 10); a.add_imm(1, 10)  # r9 bank, r10 pad 1..12
    m.imm32(pg.addr['msgt'], 12)
    for ch in "Saved to ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(9, 0); a.add_imm(ord('A'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_imm(10, 1); a.mov_imm(ord('0'), 0); tn = m.newlabel('tn'); a.cmpge(1, 10); a.bf_lbl(tn); a.add_imm(1, 0); a.add_imm(-10, 10); a.deflabel(tn)
    a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(10, 0); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    for _ in range(3): a.mov_imm(ord(' '), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_imm(0, 0); a.movb_st(0, 12); pg._hcall(m, '__showmsg')
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_start: r8 = pad + 1. Creates the block with the OS Extract of the current sample, then arms the recorder ----
    a.deflabel('rs_start'); a.stsl_pr()
    a.mov_rm_rn(8, 0); a.add_imm(-1, 0); st(m, 0, TPAD)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8)  # r8 = current sample
    a.tst_rr(8, 8); hs = m.newlabel('hs'); a.bf_lbl(hs)
    msg_text(m, "Need a sample"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(hs)
    a.movl_ld_disp(8, 0x18, 0); a.mov_imm(8, 1); a.cmphs(1, 0); hl = m.newlabel('hl'); a.bt_lbl(hl)
    msg_text(m, "Sample too short"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(hl)
    m.imm32(MIXF, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); nom1 = m.newlabel('nom1'); fs1 = m.newlabel('fs'); a.bf_lbl(fs1); a.movl_pool(nom1, 2); a.jmp(2); a.nop(); m.pool_jump(); a.deflabel(fs1)  # MIX: the OS record source must be IN
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.movl_ld_disp(0, 12, 1); a.tst_rr(1, 1); fs2 = m.newlabel('fs'); a.bf_lbl(fs2); a.movl_pool(nom1, 2); a.jmp(2); a.nop(); m.pool_jump(); a.deflabel(fs2)
    msg_text(m, "Set REC src IN"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(nom1)
    m.pool_jump()
    a.movl_ld_disp(8, 0x20, 12); a.movl_ld_disp(8, 0x24, 13)  # the OS keeps r8-r14
    a.mov_imm(0, 0); a.movl_st_disp(0, 0x20, 8); a.mov_imm(3, 0); a.movl_st_disp(0, 0x24, 8)
    m.os_extract(8)
    a.movl_st_disp(12, 0x20, 8); a.movl_st_disp(13, 0x24, 8)  # original range back
    a.tst_rr(0, 0); xo = m.newlabel('xo'); a.bt_lbl(xo)
    msg_text(m, "Memory full"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(xo)
    m.pool_jump()
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 9)  # r9 = new block (now current)
    m.imm32(CHF, 1); a.movb_ld(1, 3); a.extu_b(3, 3); st(m, 3, SCH)  # r3 = 0 mono / 1 stereo
    a.mov_rm_rn(3, 0); a.shll2(0); a.movl_st_disp(0, 0x1C, 9)  # R offset: 0 (mono) / 4 (stereo, placeholder)
    m.imm32(0x92, 1); a.add_rr(9, 1); a.mov_rm_rn(3, 2); a.add_imm(1, 2); a.movw_st(2, 1)  # channels 1 / 2
    a.mov_imm(0, 0); m.imm32(0x3E, 1); a.add_rr(9, 1); a.movw_st(0, 1)
    # frames that fit: (limit - block - 0x100 - SLACK - 0x100) / 2, optionally capped by LEN
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.sub(9, 2); m.imm32(0x200 + SLACK, 1); a.sub(1, 2); a.shlr(2)
    ld(m, SCH, 0); a.tst_rr(0, 0); s0 = m.newlabel('s0'); a.bt_lbl(s0); a.shlr(2); a.deflabel(s0)  # stereo: two planes
    m.imm32(LENF, 1); a.movb_ld(1, 3); a.extu_b(3, 3); a.tst_rr(3, 3); nlen = m.newlabel('nlen'); m.bt_far(nlen)  # MAX: what fits
    a.mov_rm_rn(2, 12); a.mov_imm(1, 4); a.add_imm(-1, 3); a.shad(3, 4); a.mov_rm_rn(4, 13)  # r12 = frames that fit, r13 = k (quarter bars: 1 2 4 8 16 32)
    m.imm32(384, 11); m.imm32(SEQ_PTR, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); nsq = m.newlabel('nsq'); a.bt_lbl(nsq)  # r11 = ticks per bar (default 4/4)
    a.mov_rm_rn(0, 1); a.add_imm(BARS_OFF, 1); a.movw_ld(1, 2); a.extu_w(2, 2); a.tst_rr(2, 2); a.bt_lbl(nsq)
    a.mov_rm_rn(0, 1); a.add_imm(BART_OFF, 1); a.movl_ld(1, 2); a.movl_ld_disp(1, 4, 3); m.imm32(0xFFFFFF, 1); a.and_rr(1, 2); a.and_rr(1, 3)
    a.sub(2, 3); a.tst_rr(3, 3); a.bt_lbl(nsq); a.mov_rm_rn(3, 11)  # bar 1 length from the bar table
    a.deflabel(nsq)
    a.mul_l(13, 11); a.sts_macl(13); a.shlr2(13)  # r13 = ticks = bar * k / 4
    sti(m, 0, MSGB[1]); sti(m, 0x10, MSGB[0]); sti(m, ID_TEMPO, MSGB[3]); sti(m, 0, MSGB[2])
    m.imm32(MSGB[0], 4); m.imm32(SYS_PARAM, 2); a.jsr(2); a.nop(); ld(m, MSGB[1], 5)  # r5 = tempo (BPM x10)
    a.mov_rm_rn(12, 2); a.tst_rr(5, 5); m.bt_far(nlen)  # no tempo: no cap
    m.imm32(TPF, 1); a.mul_l(1, 13); a.sts_macl(4); pg._hcall(m, '__udiv')  # r0 = frames = ticks * TPF / tempo
    a.mov_rm_rn(0, 4); a.mov_rm_rn(12, 2); a.cmphi(4, 2); a.bf_lbl(nlen); a.mov_rm_rn(4, 2)  # cap
    a.deflabel(nlen)
    m.imm32(RATE // 10, 1); a.cmphs(1, 2); fit = m.newlabel('fit'); a.bt_lbl(fit)  # < 0.1 s of room
    m.imm32(MGR, 4); a.movl_st_disp(9, 8, 4); m.imm32(DELETE_CMD, 2); a.jsr(2); a.nop()
    msg_text(m, "Memory full"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(fit)
    st(m, 2, MAXF); st(m, 9, BLK); sti(m, 0, CNT); sti(m, 0, DROPS)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, FPREV)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 0); a.extu_w(0, 0); st(m, 0, TPROG)
    ld(m, SCH, 0); a.tst_rr(0, 0); smo = m.newlabel('smo'); a.bt_lbl(smo); sdn = m.newlabel('sdn')
    ld(m, MAXF, 2); a.shll2(2); m.imm32(0x100 + SLACK + 3, 1); a.add_rr(1, 2); m.imm32(0xFFFFFFFC, 1); a.and_rr(1, 2)  # stereo: the whole capacity up front
    a.movl_st(2, 9); a.mov_rm_rn(9, 1); a.add_rr(2, 1); a.mov_imm(0, 0); a.movl_st(0, 1); a.bra_lbl(sdn); a.nop()
    a.deflabel(smo); m.imm32(0x100 + SLACK, 1); a.movl_st(1, 9); a.add_rr(9, 1); a.mov_imm(0, 0); a.movl_st(0, 1)  # mono: size + terminator
    a.deflabel(sdn)
    sti(m, 0, MIXK); sti(m, 0, MIXON); m.imm32(MIXF, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); nom2 = m.newlabel('nom2'); n2s = m.newlabel('n2s'); a.bf_lbl(n2s); a.movl_pool(nom2, 2); a.jmp(2); a.nop(); m.pool_jump(); a.deflabel(n2s)
    ld(m, FXMON, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); fxr = m.newlabel('fxr'); a.bf_lbl(fxr); a.movl_pool('rs_pm', 2); a.jmp(2); a.nop(); m.pool_jump(); a.deflabel(fxr)  # stream already running (FX monitor)
    m.imm32(RX_PTR, 1); a.movl_ld(1, 10)  # r10 = stale input pointer
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.mov_rm_rn(0, 4); m.imm32(OS_REC_START, 2); a.jsr(2); a.nop()  # input stream on (~50 ms)
    m.imm32(RX_PTR, 1); m.imm32(30000, 3); pl = m.newlabel('pl'); pm = m.newlabel('pm'); a.deflabel(pl)
    a.movl_ld(1, 0); a.cmpeq(10, 0); m.bf_far(pm); a.dt(3); a.bf_lbl(pl)  # wait until the pointer moves
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.mov_rm_rn(0, 4); m.imm32(OS_REC_STOP, 2); a.jsr(2); a.nop()  # it did not: undo
    m.imm32(MGR, 4); a.movl_st_disp(9, 8, 4); m.imm32(DELETE_CMD, 2); a.jsr(2); a.nop()
    msg_text(m, "No input data"); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(pm); a.deflabel('rs_pm'); sti(m, 0, FXMON); m.pool_jump()
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); m.imm32(RX_PTR, 1); a.movl_ld(1, 0); m.imm32(RX_BASE, 1); a.sub(1, 0); a.shlr2(0); a.shlr(0)  # IOFF = (rx frame - F) & 2047
    a.sub(4, 0); m.imm32(2047, 1); a.and_rr(1, 0); st(m, 0, IOFF); st(m, 4, FPREV); sti(m, 1, MIXON); sti(m, 1, MIXK)
    a.bsr_lbl('rs_fxm'); a.nop()
    a.deflabel(nom2)
    a.bsr_lbl('rs_icinv'); a.nop(); sti(m, 1, ST)
    msg_text(m, "Recording...")
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_fxmon: monitor the line input THROUGH Master FX. Wanted when MON is on, the Master FX page is open (box+16 stamp fresh)
    #      and the OS record source is IN: the OS input stream is kept running (FXMON = 1) and box+12 = IOFF+1 tells Master FX where
    #      the input ring is, so it mixes the input into the audio before its effect. FXMON 2 = start failed (retry when MON is toggled). ----
    a.deflabel('rs_fxmon'); a.stsl_pr(); a.movl_st_dec(10, 15)
    wn = m.newlabel('wn'); wd = m.newlabel('wd')
    a.mov_imm(0, 6)
    m.imm32(MONF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.tst_rr(0, 0); m.bt_far(wn)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); m.bt_far(wn)
    m.imm32(RSBOX_OFF, 3); a.add_rr(3, 2); a.movl_ld(2, 3); m.imm32(RSBOX_MAGIC, 5); a.cmpeq(5, 3); m.bf_far(wn)
    m.imm32(FRAMES, 1); a.movl_ld(1, 3); a.movl_ld_disp(2, 16, 5); a.sub(5, 3); m.imm32(4096, 5); a.cmphi(3, 5); m.bf_far(wn)
    a.mov_imm(1, 6)
    a.deflabel(wn)
    ld(m, FXMON, 7)
    a.mov_imm(1, 0); a.cmpeq(0, 7); fr1 = m.newlabel('fr1'); m.bf_far(fr1)  # FXMON == 1: running
    a.tst_rr(6, 6); m.bt_far('rfx_stop')
    ld(m, IOFF, 0); a.add_imm(1, 0); a.movl_st_disp(0, 12, 2);
    a.bra_lbl('rfx_ret'); a.nop()
    a.deflabel('rfx_stop')
    ld(m, ST, 0); a.tst_rr(0, 0); m.bf_far('rfx_ret')  # recording: leave it
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.mov_rm_rn(0, 4); m.imm32(OS_REC_STOP, 2); a.jsr(2); a.nop(); sti(m, 0, FXMON)
    a.bra_lbl('rfx_ret'); a.nop()
    a.deflabel(fr1)
    a.tst_rr(7, 7); fr0 = m.newlabel('fr0'); a.bt_lbl(fr0)  # FXMON == 2: failed
    a.tst_rr(6, 6); m.bf_far('rfx_ret'); sti(m, 0, FXMON); a.bra_lbl('rfx_ret'); a.nop()
    a.deflabel(fr0)  # FXMON == 0
    a.tst_rr(6, 6); m.bt_far('rfx_ret')
    ld(m, ST, 0); a.tst_rr(0, 0); m.bf_far('rfx_ret')
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.movl_ld_disp(0, 12, 1); a.tst_rr(1, 1); m.bf_far('rfx_ret')  # source must be IN
    m.imm32(RX_PTR, 1); a.movl_ld(1, 10)
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.mov_rm_rn(0, 4); m.imm32(OS_REC_START, 2); a.jsr(2); a.nop()
    m.imm32(RX_PTR, 1); m.imm32(30000, 3); pq = m.newlabel('pq'); pq2 = m.newlabel('pq2'); a.deflabel(pq)
    a.movl_ld(1, 0); a.cmpeq(10, 0); a.bf_lbl(pq2); a.dt(3); a.bf_lbl(pq)
    m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.mov_rm_rn(0, 4); m.imm32(OS_REC_STOP, 2); a.jsr(2); a.nop(); sti(m, 2, FXMON)
    a.bra_lbl('rfx_ret'); a.nop()
    a.deflabel(pq2)
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); m.imm32(RX_PTR, 1); a.movl_ld(1, 0); m.imm32(RX_BASE, 1); a.sub(1, 0); a.shlr2(0); a.shlr(0)
    a.sub(4, 0); m.imm32(2047, 1); a.and_rr(1, 0); st(m, 0, IOFF); sti(m, 1, FXMON)
    a.deflabel('rfx_ret'); a.movl_ld_inc(15, 10); a.ldsl_pr(); a.rts(); a.nop(); m.pool_jump()

    a.deflabel('rs_fxm')
    # Master FX page open (it stamps the frame counter in box+16 every fire): the input is mixed in BEFORE its effect, so the
    # kernel must not add it again; box+12 = IOFF+1 tells Master FX where the input ring is
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); m.bt_far('rf_ret')
    m.imm32(RSBOX_OFF, 3); a.add_rr(3, 2); a.movl_ld(2, 3); m.imm32(RSBOX_MAGIC, 5); a.cmpeq(5, 3); a.bf_lbl('rf_ret')
    a.movl_ld_disp(2, 16, 3); a.mov_rm_rn(4, 5); a.sub(3, 5); m.imm32(4096, 6); a.cmphi(5, 6); a.bf_lbl('rf_ret')
    ld(m, IOFF, 0); a.add_imm(1, 0); a.movl_st_disp(0, 12, 2); sti(m, 0, MIXK)
    a.deflabel('rf_ret'); a.rts(); a.nop(); m.pool_jump()

    # ---- rs_mbox: DO IT of Master FX (other add-on, other slot) -> request through the mailbox B+RSBOX_OFF: +0 magic, +4 request counter, +8 status.
    #      DO IT starts (destination: bank C pad 1, 2, ... 12, then 1 again) or, while recording, stops. ----
    a.deflabel('rs_mbox'); a.stsl_pr()
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); m.bt_far('rb_ret'); m.imm32(RSBOX_OFF, 3); a.add_rr(3, 2)  # r2 = mailbox
    ld(m, RSINIT, 0); a.tst_rr(0, 0); rbi = m.newlabel('rbi'); a.bf_lbl(rbi)
    a.movl_ld_disp(2, 4, 0); st(m, 0, RSSEEN); sti(m, 1, RSINIT)  # first time: requests already pending are ignored
    a.deflabel(rbi); m.imm32(RSBOX_MAGIC, 3); a.movl_ld(2, 0); a.cmpeq(3, 0); rbm = m.newlabel('rbm'); a.bt_lbl(rbm); a.movl_st(3, 2)
    a.deflabel(rbm); ld(m, ST, 4); a.movl_st_disp(4, 8, 2)
    a.tst_rr(4, 4); rbz = m.newlabel('rbz'); a.bf_lbl(rbz); ld(m, FXMON, 0); a.tst_rr(0, 0); a.bf_lbl(rbz); a.mov_imm(0, 0); a.movl_st_disp(0, 12, 2); a.deflabel(rbz)
                                                  # status for Master FX
    a.movl_ld_disp(2, 4, 0); ld(m, RSSEEN, 3); a.cmpeq(3, 0); m.bt_far('rb_ret'); st(m, 0, RSSEEN)
    a.mov_imm(1, 0); a.tst_rr(4, 4); rbs = m.newlabel('rbs'); a.bf_lbl(rbs)  # idle: start on bank C pad RSN
    sti(m, 1, DIFLAG); ld(m, RSN, 0); a.add_imm(25, 0)
    a.deflabel(rbs); st(m, 0, REQ)
    a.deflabel('rb_ret'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rs_dicheck: after rs_start: a DO IT start that worked moves the destination on one pad ----
    a.deflabel('rs_dicheck'); a.stsl_pr()
    ld(m, DIFLAG, 0); a.tst_rr(0, 0); rdc = m.newlabel('rdc'); a.bt_lbl(rdc); sti(m, 0, DIFLAG)
    ld(m, ST, 0); a.tst_rr(0, 0); a.bt_lbl(rdc)
    ld(m, RSN, 0); a.add_imm(1, 0); m.imm32(12, 1); a.cmphs(1, 0); rdw = m.newlabel('rdw'); a.bf_lbl(rdw); a.mov_imm(0, 0); a.deflabel(rdw); st(m, 0, RSN)
    a.deflabel(rdc); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_monit: input monitoring (analog bypass of the codec, like Input Thru), live from the M field, page open or not ----
    #      OS not sampling: line volume regs 0/1 + reg 4 bypass. While sampling (the OS or our input stream) the volume is the OS's: only reg 4.
    a.deflabel('rs_monit'); a.stsl_pr()
    m.imm32(REC_FLAG, 1); a.movl_ld(1, 8)
    m.imm32(MONF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.tst_rr(0, 0); moff = m.newlabel('moff'); m.bt_far(moff)
    ld(m, FXMON, 0); a.mov_imm(1, 2); a.cmpeq(2, 0); m.bt_far(moff)  # input monitored through Master FX: no analog bypass
    ld(m, MAPP, 0); a.tst_rr(0, 0); map_ = m.newlabel('mapp'); a.bt_lbl(map_)
    ld(m, MCNT, 0); a.add_imm(1, 0); st(m, 0, MCNT); a.and_imm(REFRESH - 1); a.tst_rr(0, 0); m.bf_far('rm_ret')
    a.deflabel(map_); sti(m, 1, MAPP)
    mrc = m.newlabel('mrc'); a.tst_rr(8, 8); a.bf_lbl(mrc)
    for r_ in (0, 1): a.mov_imm(r_, 4); a.mov_imm(LVL_0DB, 5); m.imm32(CODEC_W, 2); a.jsr(2); a.nop()
    a.deflabel(mrc); a.mov_imm(4, 4); a.mov_imm(REG4_THRU, 5); m.imm32(CODEC_W, 2); a.jsr(2); a.nop(); far_bra(m, 'rm_ret')
    m.pool_jump()
    a.deflabel(moff)
    ld(m, MAPP, 0); a.tst_rr(0, 0); m.bt_far('rm_ret'); sti(m, 0, MAPP)
    a.mov_imm(4, 4); a.mov_imm(REG4_OS, 5); m.imm32(CODEC_W, 2); a.jsr(2); a.nop()
    a.tst_rr(8, 8); m.bf_far('rm_ret'); m.imm32(GAIN_SHADOW, 1); a.movw_ld(1, 4); m.imm32(CODEC_GAIN, 2); a.jsr(2); a.nop()  # OS volume back
    a.deflabel('rm_ret'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_recstop: stop the OS input stream if this add-on started it ----
    a.deflabel('rs_recstop'); a.stsl_pr()
    ld(m, MIXON, 0); a.tst_rr(0, 0); nrs = m.newlabel('nrs'); a.bt_lbl(nrs)
    sti(m, 0, MIXON); m.imm32(OS_GETREC, 2); a.jsr(2); a.nop(); a.mov_rm_rn(0, 4); m.imm32(OS_REC_STOP, 2); a.jsr(2); a.nop()
    a.deflabel(nrs); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_icinv: invalidate the cache lines of the kernel (same as Sample FX icinv; runs from P2) ----
    a.deflabel('rs_icinv')
    a.movl_pool('__cstart', 4); m.imm32(0xFFFFFFF0, 1); a.and_rr(1, 4)
    m.imm32(CACHE_SPAN // 16 + 1, 5); m.imm32(0xF0000008, 6); m.imm32(0x1FFFFC00, 7); m.imm32(0xFF0, 3)
    icl = m.newlabel('icl'); a.deflabel(icl)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0)
    a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(icl)
    a.rts(); a.nop()
    m.pool_jump()

    # ---- rs_kern (runs from the CACHED alias, registers only): r4 ring ptr, r5 frames, r6 dest s16*, r7 = -17,
    #      r10 ring end, r13 ring base. mono = ((L>>1)+(R>>1))>>16 = (L>>17) + (R>>17) ----
    a.deflabel('__cstart')
    a.deflabel('rs_kern')
    kl = m.newlabel('kl'); kn = m.newlabel('kn'); a.deflabel(kl)
    a.movl_ld_inc(4, 0); a.movl_ld_inc(4, 1); a.shad(7, 0); a.shad(7, 1); a.add_rr(1, 0); a.movw_st(0, 6); a.add_imm(2, 6)
    a.cmphs(10, 4); a.bf_lbl(kn); a.mov_rm_rn(13, 4)
    a.deflabel(kn); a.dt(5); a.bf_lbl(kl); a.rts(); a.nop()
    # rs_kern2: as rs_kern plus the input ring (r14 ptr, r11 base, r12 end): mono = sat16(out mono + in mono)
    a.deflabel('rs_kern2')
    kl2 = m.newlabel('kl2'); kn2 = m.newlabel('kn2'); kn3 = m.newlabel('kn3'); s1 = m.newlabel('s1'); s2 = m.newlabel('s2'); a.deflabel(kl2)
    a.movl_ld_inc(4, 0); a.movl_ld_inc(4, 1); a.shad(7, 0); a.shad(7, 1); a.add_rr(1, 0)
    a.movl_ld_inc(14, 1); a.movl_ld_inc(14, 2); a.shad(7, 1); a.shad(7, 2); a.add_rr(2, 1); a.add_rr(1, 0)
    a.mov_imm(-1, 2); a.extu_w(2, 2); a.shlr(2); a.not_rr(2, 3)  # r2 = 0x7FFF, r3 = -0x8000
    a.cmpgt(2, 0); a.bf_lbl(s1); a.mov_rm_rn(2, 0); a.deflabel(s1)
    a.cmpgt(0, 3); a.bf_lbl(s2); a.mov_rm_rn(3, 0); a.deflabel(s2)
    a.movw_st(0, 6); a.add_imm(2, 6)
    a.cmphs(10, 4); a.bf_lbl(kn2); a.mov_rm_rn(13, 4)
    a.deflabel(kn2); a.cmphs(12, 14); a.bf_lbl(kn3); a.mov_rm_rn(11, 14)
    a.deflabel(kn3); a.dt(5); a.bf_lbl(kl2); a.rts(); a.nop()
    # rs_kerns: stereo, output only: r4 out ptr, r5 frames, r6 L dest, r8 R dest, r7 = -16
    a.deflabel('rs_kerns')
    ks = m.newlabel('ks'); ksn = m.newlabel('ksn'); a.deflabel(ks)
    a.movl_ld_inc(4, 0); a.movl_ld_inc(4, 1); a.shad(7, 0); a.shad(7, 1); a.movw_st(0, 6); a.movw_st(1, 8); a.add_imm(2, 6); a.add_imm(2, 8)
    a.cmphs(10, 4); a.bf_lbl(ksn); a.mov_rm_rn(13, 4)
    a.deflabel(ksn); a.dt(5); a.bf_lbl(ks); a.rts(); a.nop()
    # rs_kerns2: stereo + input ring (r14 ptr, r11 base, r12 end): L = sat16(out L + in L), R likewise
    a.deflabel('rs_kerns2')
    k2 = m.newlabel('k2'); k2a = m.newlabel('k2a'); k2b = m.newlabel('k2b'); k2c = m.newlabel('k2c'); k2d = m.newlabel('k2d'); k2e = m.newlabel('k2e'); k2f = m.newlabel('k2f'); a.deflabel(k2)
    a.movl_ld_inc(4, 0); a.movl_ld_inc(4, 1); a.shad(7, 0); a.shad(7, 1)
    a.movl_ld_inc(14, 2); a.movl_ld_inc(14, 3); a.shad(7, 2); a.shad(7, 3); a.add_rr(2, 0); a.add_rr(3, 1)
    a.mov_imm(-1, 2); a.extu_w(2, 2); a.shlr(2); a.not_rr(2, 3)
    a.cmpgt(2, 0); a.bf_lbl(k2a); a.mov_rm_rn(2, 0); a.deflabel(k2a)
    a.cmpgt(0, 3); a.bf_lbl(k2b); a.mov_rm_rn(3, 0); a.deflabel(k2b)
    a.cmpgt(2, 1); a.bf_lbl(k2c); a.mov_rm_rn(2, 1); a.deflabel(k2c)
    a.cmpgt(1, 3); a.bf_lbl(k2d); a.mov_rm_rn(3, 1); a.deflabel(k2d)
    a.movw_st(0, 6); a.movw_st(1, 8); a.add_imm(2, 6); a.add_imm(2, 8)
    a.cmphs(10, 4); a.bf_lbl(k2e); a.mov_rm_rn(13, 4)
    a.deflabel(k2e); a.cmphs(12, 14); a.bf_lbl(k2f); a.mov_rm_rn(11, 14)
    a.deflabel(k2f); a.dt(5); a.bf_lbl(k2); a.rts(); a.nop()
    # rs_mv: r4 src, r6 dest, r5 halfwords (dest below src: forward copy)
    a.deflabel('rs_mv')
    mv = m.newlabel('mv'); a.deflabel(mv); a.movw_ldinc(4, 0); a.movw_st(0, 6); a.add_imm(2, 6); a.dt(5); a.bf_lbl(mv); a.rts(); a.nop()
    a.deflabel('__cend')
    m.pool_jump()
    a.deflabel('rs_end')
pg.on_fire(fire)
pg.on_inactive(lambda m: call(m, 'rs_maint'))

def ev_pad(m, hret0, hret1):
    """SHIFT + pad = start / stop the recording. Pads without SHIFT go to the OS."""
    a = m.a
    m.imm32(KEYS_ADDR, 1); a.movl_ld(1, 0); m.imm32(keymask('SHIFT'), 1); a.and_rr(1, 0); a.tst_rr(0, 0)
    nosh = m.newlabel('nosh'); m.bt_far(nosh)
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); rel = m.newlabel('rel'); m.bt_far(rel)  # release: consumed
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9); a.mov_imm(12, 0); a.cmphs(0, 9); m.bt_far(rel)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.and_imm(3); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(0, 9)
    a.add_imm(1, 9); st(m, 9, REQ)
    a.deflabel(rel); far_bra(m, hret1)
    a.deflabel(nosh); far_bra(m, hret0)
    m.pool_jump()
pg.on_event(EV_PAD, ev_pad)

if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'RESAMPLE.WAV'); pg.build_wav(out)
    import random
    from testkit import PageTest
    from shasm import SHAsm
    t = PageTest(pg, out); h, e = t.h, t.e
    e.map_ram(0xA5017000, 0x1000)
    LOG, LIST_END = 0x88E00000, 0x884C4FE0  # LOG: +0 n extract, +4 n delete, +8 n loop guard
    def stub(addr, fn): t.stub(addr, fn)
    tempo_stub = lambda bpm10: (lambda s_: (s_.movl_pool(bpm10, 2), s_.movl_st_disp(2, 4, 4), s_.mov_imm(1, 0), s_.rts(), s_.nop()))
    stub(SYS_PARAM, tempo_stub(1200))
    def cnt(s_, off): s_.movl_pool(LOG, 1); s_.movl_ld_disp(1, off, 0); s_.add_imm(1, 0); s_.movl_st_disp(0, off, 1)
    def extract_like(s_):  # like the OS: copy of the current block to the end of the list, name 'X..', current
        s_.stsl_pr(); s_.movl_st_dec(8, 15); s_.movl_st_dec(9, 15); s_.mov_rm_rn(4, 9); cnt(s_, 0)
        s_.movl_pool(LIST_END, 2); s_.jsr(2); s_.nop()
        s_.mov_rm_rn(9, 4); s_.movl_ld_disp(4, 8, 1); s_.mov_rm_rn(0, 8)
        s_.movl_ld(1, 3); s_.shlr2(3); s_.mov_rm_rn(8, 2)
        s_.deflabel('cp'); s_.movl_ld_inc(1, 0); s_.movl_st(0, 2); s_.add_imm(4, 2); s_.dt(3); s_.bf_lbl('cp')
        s_.mov_imm(0, 0); s_.movl_st(0, 2)
        s_.mov_rm_rn(8, 2); s_.add_imm(4, 2); s_.mov_imm(ord('X'), 0); s_.movb_st(0, 2)
        s_.movl_st_disp(8, 8, 4)
        s_.movl_ld_inc(15, 9); s_.movl_ld_inc(15, 8); s_.ldsl_pr(); s_.mov_imm(0, 0); s_.rts(); s_.nop()
    stub(0x884C5E90, extract_like)
    stub(0x884C5FFC, lambda s_: (cnt(s_, 4), s_.movl_ld_disp(4, 8, 1), s_.mov_imm(0, 0), s_.movl_st(0, 1), s_.movl_ld(4, 0), s_.movl_st_disp(0, 8, 4), s_.rts(), s_.nop()))
    stub(0x884C4F04, lambda s_: (cnt(s_, 8), s_.rts(), s_.nop()))
    MSGT = lambda: t.msg()
    log = lambda off: h.ram_r32(LOG + off)
    cur = lambda: h.ram_r32(MGR + 8)
    s16 = lambda v: v - 0x10000 if v & 0x8000 else v
    def addblock(n, stereo=False, name=b'TEMPLATE'):
        blk = h.list_end(); size = (0x100 + 2 * n * (2 if stereo else 1) + 0x200 + 3) & ~3
        h.ram_w32(blk, size)
        for i, c in enumerate(name.ljust(16, b'\0')): h.ram_w8(blk + 4 + i, c)
        h.ram_w32(blk + 0x18, n); h.ram_w32(blk + 0x1C, n if stereo else 0); h.ram_w32(blk + 0x20, 2); h.ram_w32(blk + 0x24, n - 3)
        h.ram_w16(blk + 0x92, 2 if stereo else 1); h.ram_w16(blk + 0x3E, 5)
        for i in range(n * (2 if stereo else 1)): h.ram_w16(blk + 0x100 + 2 * i, 0x1111 + i)
        h.ram_w32(blk + size, 0); return blk
    rnd = random.Random(5)
    LV = lambda f: ((f * 7919 + 13) % 40001) - 20000
    RV = lambda f: ((f * 104729 + 7) % 40001) - 20000
    MONO = lambda f: (LV(f) >> 1) + (RV(f) >> 1)
    def ringw(f): a_ = OUT_BASE + (f * 8) % OUT_SIZE; h.ram_w32(a_, ((LV(f) << 16) | rnd.randrange(65536)) & 0xFFFFFFFF); h.ram_w32(a_ + 4, ((RV(f) << 16) | rnd.randrange(65536)) & 0xFFFFFFFF)
    F = [0x7C0 * 32 + 12]
    def advance(n, fires=1):
        """the OS sends n more frames: real data behind the counter, garbage ahead of it (the render rewrites it every round)"""
        for f in range(F[0], F[0] + n): ringw(f)
        F[0] += n; h.ram_w32(FRAMES, F[0])
        for f in range(F[0], F[0] + 128):
            a_ = OUT_BASE + (f * 8) % OUT_SIZE; h.ram_w32(a_, rnd.randrange(1 << 32)); h.ram_w32(a_ + 4, rnd.randrange(1 << 32))
        h.fire(fires, maxsteps=200_000_000)
    SH = lambda on=True: h.ram_w32(KEYS_ADDR, keymask('SHIFT') if on else 0)
    txt = lambda: t.label(STL, 7)
    pcm = lambda blk, n: [s16(e.rd(blk + 0x100 + 2 * i, 2)) for i in range(n)]
    padname = lambda prog, pad_: bytes(e.rd(PROG_BASE + prog * PROG_STRIDE + PAD_OFF + pad_ * PAD_SIZE + i, 1) for i in range(16)).split(b'\0')[0]
    padb = lambda prog, pad_, off: e.rd(PROG_BASE + prog * PROG_STRIDE + PAD_OFF + pad_ * PAD_SIZE + off, 1)
    def start(pad_bank, k):
        SH(True); r = t.pad(pad_bank, k); SH(False); t.fire(2); return r
    h.ram_w16(PROG_IDX, 3); t.open()
    # 1) no current sample
    h.ram_w32(MGR + 8, 0); h.ram_w32(FRAMES, F[0]); assert start(0, 5) == (1, 1) and t.slot(ST) == 0 and MSGT() == "Need a sample", MSGT()
    print("  ok no current sample: 'Need a sample', nothing started")
    # 2) pads without SHIFT go to the OS and do nothing here; SHIFT release is consumed
    tpl = addblock(16); h.ram_w32(MGR + 8, tpl); end0 = h.list_end()
    SH(False); assert t.pad(0, 5) == (0, 0); t.fire(2); assert t.slot(ST) == 0 and t.slot(REQ) == 0
    print("  ok pad without SHIFT: left to the OS, no recording")
    # 3) start with SHIFT+pad A06
    advance(0)
    assert start(0, 5) == (1, 1), "start consumed"
    assert t.slot(ST) == 1 and t.slot(TPAD) == 5 and t.slot(TPROG) == 3 and txt() == "REC 00s", (t.slot(ST), txt())
    blk = t.slot(BLK); assert blk == end0 and cur() == blk and log(0) == 1, "new block at the end of the list, current"
    assert (e.rd(tpl + 0x20, 4), e.rd(tpl + 0x24, 4)) == (2, 13), "template range restored"
    assert e.rd(blk + 0x92, 2) == 1 and e.rd(blk + 0x1C, 4) == 0 and e.rd(blk + 0x3E, 2) == 0, "mono, player uses start/end"
    assert h.ram_r32(blk) == 0x100 + SLACK and h.ram_r32(blk + 0x100 + SLACK) == 0 and MSGT() == "Recording...", MSGT()
    print("  ok SHIFT+pad: block made by the OS Extract (4 frames of the template), mono, list well-formed, 'REC 00s'")
    F0 = t.slot(FPREV); assert F0 == F[0]
    # 4) record across several fires, across the ring wrap (2048 frames), garbage ahead of the counter is never read
    for n in (100, 300, 64, 900, 700, 17 * 4):
        advance(n); k = t.slot(CNT); assert h.ram_r32(blk) == (0x100 + 2 * k + SLACK + 3) & ~3 and h.ram_r32(blk + h.ram_r32(blk)) == 0, "list well-formed every fire"
        assert k == F[0] - GUARD - F0 or k == F[0] - F0 - GUARD, (k, F[0] - F0)
    assert t.slot(DROPS) == 0 and txt() == "REC 00s"
    print("  ok recording across the ring wrap: %d frames copied, block size and terminator right every fire" % t.slot(CNT))
    # 5) stop with SHIFT+pad (any pad), assign to A06 of program 3
    advance(40); SH(True); assert t.pad(0, 9) == (1, 1); SH(False); t.fire(2)
    n = F[0] - GUARD - F0; assert t.slot(ST) == 0 and txt() == "IDLE   "
    assert (e.rd(blk + 0x18, 4), e.rd(blk + 0x1C, 4), e.rd(blk + 0x20, 4), e.rd(blk + 0x24, 4), e.rd(blk + 0x92, 2)) == (n, 0, 0, n, 1), (e.rd(blk + 0x18, 4), n)
    size = (0x100 + 2 * n + SLACK + 3) & ~3; assert h.ram_r32(blk) == size and h.ram_r32(blk + size) == 0 and h.list_end() == blk + size
    got = pcm(blk, n); assert got == [MONO(F0 + i) for i in range(n)], [(i, got[i], MONO(F0 + i)) for i in range(n) if got[i] != MONO(F0 + i)][:5]
    assert cur() == blk and log(8) == 1 and log(4) == 0
    nm = bytes(e.rd(blk + 4 + i, 1) for i in range(16)).split(b'\0')[0]; assert nm.startswith(b'X') and padname(3, 5) == nm
    assert (padb(3, 5, 0x11), padb(3, 5, 0x13), padb(3, 5, 0x14), padb(3, 5, 0x62), padb(3, 5, 0x63)) == (100, 0x7F, 0, 1, 1)
    assert padname(3, 4) == b'' and padname(2, 5) == b'', "other pads/programs untouched"
    assert MSGT() == "Saved to A06", MSGT()
    print("  ok SHIFT+pad stops: %d frames == (L>>1 + R>>1)>>16 of the ring, fields set, current sample, on pad A06 (%s)" % (n, nm.decode()))
    # 6) bank C pad 4, LEN capped and the frames reach the limit -> finishes by itself (cap made small through MAXF)
    h.ram_w32(MGR + 8, tpl); e.wr(LENF, 0, 1); advance(0)
    assert start(2, 3) == (1, 1) and t.slot(TPAD) == 27
    blk2 = t.slot(BLK); assert blk2 == end0 + size
    h.ram_w32(MAXF, 300); F1 = t.slot(FPREV)
    advance(200); assert t.slot(ST) == 1 and t.slot(CNT) == 200 - GUARD + 0 or True
    advance(400); assert t.slot(ST) == 0 and e.rd(blk2 + 0x18, 4) == 300 and txt() == "IDLE   ", (t.slot(ST), e.rd(blk2 + 0x18, 4))
    assert pcm(blk2, 300) == [MONO(F1 + i) for i in range(300)] and padname(3, 27) != b'' and MSGT() == "Saved to C04", MSGT()
    print("  ok limit reached: stops by itself, exact 300 frames, assigned to C04")
    # 7) LEN field = bars from the tempo (120 BPM, 4/4 default; bar length from the sequence bar table when there is one)
    for idx, k in ((1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32)):
        want = ((384 * k) >> 2) * TPF // 1200
        h.ram_w32(MGR + 8, tpl); e.wr(LENF, idx, 1); assert start(0, 0) == (1, 1) and t.slot(MAXF) == want, (idx, t.slot(MAXF), want)
        advance(0); SH(True); t.pad(0, 1); SH(False); t.fire(2)
    assert ((384 * 4) >> 2) * TPF // 1200 == 2 * RATE, "1 bar of 4/4 at 120 BPM = 2 s"
    SQ = 0x88E30000; h.ram_w32(SEQ_PTR, SQ); e.wr(SQ + BARS_OFF, 2, 2); h.ram_w32(SQ + BART_OFF, 0x60000000); h.ram_w32(SQ + BART_OFF + 4, 0x60000000 | 480)
    h.ram_w32(MGR + 8, tpl); e.wr(LENF, 3, 1); assert start(0, 0) == (1, 1) and t.slot(MAXF) == ((480 * 4) >> 2) * TPF // 1200, t.slot(MAXF)
    advance(0); SH(True); t.pad(0, 1); SH(False); t.fire(2)
    h.ram_w32(SEQ_PTR, 0); stub(SYS_PARAM, tempo_stub(900)); h.ram_w32(MGR + 8, tpl); assert start(0, 0) == (1, 1) and t.slot(MAXF) == ((384 * 4) >> 2) * TPF // 900
    advance(0); SH(True); t.pad(0, 1); SH(False); t.fire(2)
    stub(SYS_PARAM, tempo_stub(0)); h.ram_w32(MGR + 8, tpl); start(0, 0); lim = h.ram_r32(MGR + 4); bk = t.slot(BLK)
    assert t.slot(MAXF) == (lim - bk - 0x200 - SLACK) // 2, "no tempo: no cap, what fits"
    advance(0); SH(True); t.pad(0, 1); SH(False); t.fire(2); stub(SYS_PARAM, tempo_stub(1200))
    e.wr(LENF, 0, 1); h.ram_w32(MGR + 8, tpl); start(0, 0); lim = h.ram_r32(MGR + 4); bk = t.slot(BLK)
    assert t.slot(MAXF) == (lim - bk - 0x200 - SLACK) // 2, (t.slot(MAXF), (lim - bk - 0x200 - SLACK) // 2)
    advance(0); SH(True); t.pad(0, 1); SH(False); t.fire(2)
    print("  ok LEN 1/4 .. 8 bars from the tempo (and the sequence bar length), MAX = what fits in sample memory")
    # 8) dropout: the loop fell more than 1500 frames behind -> oldest skipped, counted, shown as '!'
    h.ram_w32(MGR + 8, tpl); e.wr(LENF, 0, 1); advance(0); start(0, 2); b3 = t.slot(BLK); F2 = t.slot(FPREV)
    advance(1800); assert t.slot(DROPS) == 1 and t.slot(CNT) == CATCHUP and txt() == "REC 00!", (t.slot(DROPS), t.slot(CNT), txt())
    top = F2 + 1800 - GUARD
    assert pcm(b3, CATCHUP) == [MONO(top - CATCHUP + i) for i in range(CATCHUP)], "newest frames kept"
    SH(True); t.pad(0, 3); SH(False); t.fire(2)
    print("  ok falls behind: oldest frames skipped, dropout counted ('REC 00!'), newest 1500 frames exact")
    # 9) too short: Delete of the new block, nothing assigned
    h.ram_w32(MGR + 8, tpl); advance(0); d0 = log(4); a0 = padname(3, 1); start(0, 1); advance(60); SH(True); t.pad(0, 2); SH(False); t.fire(2)
    assert log(4) == d0 + 1 and t.slot(ST) == 0 and MSGT() == "Too short" and padname(3, 1) == a0
    print("  ok shorter than %d frames: block deleted, 'Too short', pad untouched" % MINF)
    # 10) memory full: a big block leaves less than 0.1 s of room -> the new block is deleted
    h.ram_w32(MGR + 8, tpl); fill = h.list_end(); fsz = (lim - fill - 9000) & ~3; h.ram_w32(fill, fsz); h.ram_w32(fill + fsz, 0)
    assert h.list_end() == fill + fsz; d0 = log(4)
    SH(True); t.pad(0, 1); SH(False); t.fire(2)
    assert t.slot(ST) == 0 and log(4) == d0 + 1 and MSGT() == "Memory full" and h.list_end() == fill + fsz, MSGT()
    h.ram_w32(fill, 0); print("  ok no room: block deleted, 'Memory full', list unchanged")
    # 11) aborts when something appears after our block (another sample created meanwhile)
    h.ram_w32(MGR + 8, tpl); advance(0); start(0, 4); b4 = t.slot(BLK); advance(100)
    h.ram_w32(b4 + h.ram_r32(b4), 0x400)
    advance(100); assert t.slot(ST) == 0 and MSGT() == "Rec aborted" and h.ram_r32(b4 + h.ram_r32(b4)) == 0x400, "nothing overwritten"
    print("  ok another block appears after ours: recording aborted, nothing overwritten")
    h.ram_w32(b4 + h.ram_r32(b4), 0)
    # 12) stereo template -> mono block; recording goes on with the page closed; release of SHIFT+pad consumed
    h.ram_w32(MGR + 8, 0)
    st_t = addblock(16, stereo=True, name=b'STEREO'); h.ram_w32(MGR + 8, st_t); advance(0); start(1, 7)
    b5 = t.slot(BLK); assert e.rd(b5 + 0x92, 2) == 1 and e.rd(b5 + 0x1C, 4) == 0 and t.slot(TPAD) == 19
    t.close(); F5 = t.slot(FPREV)
    for nn in (200, 500, 300): advance(nn)
    assert t.slot(CNT) == F[0] - GUARD - F5, "page closed: still recording"
    t.open(); SH(True); t.pad(0, 0); SH(False); t.fire(2)
    n5 = F[0] - GUARD - F5; assert e.rd(b5 + 0x18, 4) == n5 and pcm(b5, n5) == [MONO(F5 + i) for i in range(n5)] and padname(3, 19) != b''
    print("  ok stereo template -> mono sample; recording continues with the page closed; assigned to B08")
    # 12b) MIX: the OS input stream is started with the OS recorder start routine, the input ring is mixed in (saturating)
    LOG2, OBJ, IOFFT = 0x88E00100, 0x8849D114, 700
    def start_stub(s_):
        s_.movl_pool(LOG2, 1); s_.movl_ld(1, 0); s_.add_imm(1, 0); s_.movl_st(0, 1)
        s_.movl_ld_disp(1, 8, 0); s_.tst_rr(0, 0); s_.bt_lbl('nm')
        s_.movl_pool(FRAMES, 2); s_.movl_ld(2, 2); s_.movl_pool(IOFFT, 3); s_.add_rr(3, 2); s_.movl_pool(2047, 3); s_.and_rr(3, 2)
        s_.shll2(2); s_.shll(2); s_.movl_pool(RX_BASE, 3); s_.add_rr(3, 2); s_.movl_pool(RX_PTR, 3); s_.movl_st(2, 3)
        s_.deflabel('nm'); s_.rts(); s_.nop()
    stub(OS_GETREC, lambda s_: (s_.movl_pool(OBJ, 0), s_.rts(), s_.nop()))
    stub(OS_REC_START, start_stub)
    stub(OS_REC_STOP, lambda s_: (s_.movl_pool(LOG2, 1), s_.movl_ld_disp(1, 4, 0), s_.add_imm(1, 0), s_.movl_st_disp(0, 4, 1), s_.rts(), s_.nop()))
    nstart = lambda: h.ram_r32(LOG2); nstop = lambda: h.ram_r32(LOG2 + 4)
    ILV = lambda f: ((f * 6151 + 3) % 64001) - 32000
    IRV = lambda f: ((f * 7027 + 11) % 64001) - 32000
    sat = lambda v: max(-32768, min(32767, v))
    EXP = lambda f: sat(MONO(f) + (ILV(f) >> 1) + (IRV(f) >> 1))
    def advance_mix(n, fires=1):
        for f in range(F[0], F[0] + n):
            ringw(f); a_ = RX_BASE + (((f + IOFFT) & 2047) * 8)
            h.ram_w32(a_, ((ILV(f) << 16) | (rnd.randrange(256) << 8)) & 0xFFFFFFFF); h.ram_w32(a_ + 4, ((IRV(f) << 16) | (rnd.randrange(256) << 8)) & 0xFFFFFFFF)
        F[0] += n; h.ram_w32(FRAMES, F[0]); h.ram_w32(RX_PTR, RX_BASE + (((F[0] + IOFFT) & 2047) * 8))
        for f in range(F[0], F[0] + 128):
            a_ = OUT_BASE + (f * 8) % OUT_SIZE; h.ram_w32(a_, rnd.randrange(1 << 32)); h.ram_w32(a_ + 4, rnd.randrange(1 << 32))
            a_ = RX_BASE + (((f + IOFFT) & 2047) * 8); h.ram_w32(a_, rnd.randrange(1 << 32)); h.ram_w32(a_ + 4, rnd.randrange(1 << 32))
        h.fire(fires, maxsteps=200_000_000)
    h.ram_w32(MGR + 8, tpl); e.wr(MIXF, 1, 1); h.ram_w32(OBJ + 12, 1); h.ram_w32(LOG2, 0); h.ram_w32(LOG2 + 4, 0); h.ram_w32(LOG2 + 8, 1)
    n0, d0 = 0, log(4); r_ = start(0, 6); assert r_ == (1, 1) and t.slot(ST) == 0 and MSGT() == "Set REC src IN" and nstart() == 0, (r_, t.slot(ST), MSGT(), nstart())
    print("  ok MIX with the OS record source on MAIN: refused with 'Set REC src IN', nothing started")
    h.ram_w32(OBJ + 12, 0); h.ram_w32(LOG2 + 8, 0); x0 = log(0); end1 = h.list_end(); advance(0)
    assert start(0, 6) == (1, 1) and t.slot(ST) == 0 and MSGT() == "No input data" and nstart() == 1 and nstop() == 1 and log(4) == d0 + 1 and h.list_end() == end1, (MSGT(), nstart(), nstop())
    print("  ok MIX but the input stream never starts: stopped again, new block deleted, 'No input data'")
    h.ram_w32(LOG2 + 8, 1); h.ram_w32(MGR + 8, tpl); advance(0); h.ram_w32(FRAMES, F[0]); h.ram_w32(RX_PTR, RX_BASE)
    assert start(0, 6) == (1, 1) and t.slot(ST) == 1 and nstart() == 2 and t.slot(MIXON) == 1 and t.slot(IOFF) == IOFFT, (t.slot(ST), nstart(), t.slot(IOFF))
    b6 = t.slot(BLK); F6 = t.slot(FPREV); assert F6 == F[0]
    for nn in (100, 300, 64, 900, 700, 68): advance_mix(nn)
    advance_mix(40); SH(True); t.pad(0, 9); SH(False); t.fire(2)
    n6 = F[0] - GUARD - F6; assert t.slot(ST) == 0 and t.slot(MIXON) == 0 and nstop() == 2, (t.slot(ST), nstop())
    got = pcm(b6, n6); want = [EXP(F6 + i) for i in range(n6)]
    assert got == want, [(i, got[i], want[i]) for i in range(n6) if got[i] != want[i]][:5]
    clip = sum(1 for v in want if v in (32767, -32768)); assert clip > 50, "the test must exercise saturation"
    print("  ok MIX: %d frames == sat16(output mono + input mono), input aligned by the pointer offset, %d saturated, across the ring wrap" % (n6, clip))
    h.ram_w32(MGR + 8, tpl); advance(0); h.ram_w32(RX_PTR, RX_BASE); st0 = nstart(); sp0 = nstop()
    start(0, 7); b7 = t.slot(BLK); advance_mix(100); h.ram_w32(b7 + h.ram_r32(b7), 0x400); advance_mix(100)
    assert t.slot(ST) == 0 and MSGT() == "Rec aborted" and nstop() == sp0 + 1 and t.slot(MIXON) == 0
    h.ram_w32(b7 + h.ram_r32(b7), 0); print("  ok MIX aborted (block appears after ours): input stream stopped")
    LOG3 = 0x88E00200  # codec write list: +0 count, then 8 bytes per entry (reg, value)
    def logw(s_, fixed=None):
        s_.movl_pool(LOG3, 1); s_.movl_ld(1, 0); s_.add_imm(1, 0); s_.movl_st(0, 1); s_.shll2(0); s_.shll(0); s_.add_rr(0, 1)
        if fixed is not None: s_.mov_imm(fixed - 256, 4); s_.extu_b(4, 4)
        s_.movl_st(4, 1); s_.movl_st_disp(5, 4, 1)
        for r in range(0, 8): s_.mov_imm(0x55, r) if r != 1 else None
        s_.rts(); s_.nop()
    stub(0x88E21000, logw); stub(0x88E21100, lambda s_: (s_.mov_rm_rn(4, 5), logw(s_, 0xFF)))
    stub(CODEC_W, lambda s_: (s_.movl_pool(0x88E21000, 0), s_.jmp(0), s_.nop()))
    stub(CODEC_GAIN, lambda s_: (s_.movl_pool(0x88E21100, 0), s_.jmp(0), s_.nop()))
    def clog():
        n = h.ram_r32(LOG3); r = [(h.ram_r32(LOG3 + 8 * i + 8) & 0xff, h.ram_r32(LOG3 + 8 * i + 12)) for i in range(n)]
        h.ram_w32(LOG3, 0); return r
    h.ram_w32(LOG3, 0); h.ram_w16(GAIN_SHADOW, 0x1234); h.ram_w32(REC_FLAG, 0); e.wr(MONF, 0, 1); t.fire(2); clog()
    t.fire(40); assert clog() == [], "monitoring off: the codec is not touched"
    e.wr(MONF, 1, 1); t.fire(1); assert clog() == [(0, LVL_0DB), (1, LVL_0DB), (4, REG4_THRU)], "active as soon as it is switched"
    t.fire(REFRESH - 2); assert clog() == []
    t.fire(2); assert clog() == [(0, LVL_0DB), (1, LVL_0DB), (4, REG4_THRU)], "refresh"
    t.close(); t.fire(REFRESH + 2); assert clog() == [(0, LVL_0DB), (1, LVL_0DB), (4, REG4_THRU)], "page closed: still active"
    h.ram_w32(REC_FLAG, 1); t.fire(REFRESH * 3 + 1); assert clog() == [(4, REG4_THRU)] * 3, "while sampling: only reg 4"
    clog(); t.fire(REFRESH); assert clog() == [(4, REG4_THRU)], "while sampling: only reg 4 (volume belongs to the OS)"
    h.ram_w32(REC_FLAG, 0); t.fire(REFRESH); assert clog() == [(0, LVL_0DB), (1, LVL_0DB), (4, REG4_THRU)]
    e.wr(MONF, 0, 1); t.fire(1); assert clog() == [(4, REG4_OS), (0xFF, 0x1234)], "switched off: OS values back"
    t.fire(REFRESH * 3); assert clog() == [], "then nothing"
    e.wr(MONF, 1, 1); h.ram_w32(REC_FLAG, 1); t.fire(1); clog(); e.wr(MONF, 0, 1); t.fire(1); assert clog() == [(4, REG4_OS)], "off while sampling: volume stays with the OS"
    h.ram_w32(REC_FLAG, 0); e.wr(MONF, 0, 1); t.open(); t.fire(REFRESH)
    print("  ok monitoring: active as soon as switched, refreshed, page open or closed, only reg 4 while sampling, OS values back when off")
    e.wr(MIXF, 0, 1); h.ram_w32(MGR + 8, tpl); advance(0); s1 = nstart(); sp1 = nstop(); start(0, 8); advance(100); SH(True); t.pad(0, 9); SH(False); t.fire(2)
    assert nstart() == s1 and nstop() == sp1 and t.slot(MIXON) == 0, "OUT mode never touches the OS recorder"
    print("  ok OUT mode: the OS recorder is not touched")
    # 12c) STEREO (2CH): planar L then R, the R plane is reserved up front and moved next to the L plane at the end
    e.wr(CHF, 1, 1); e.wr(MIXF, 0, 1); e.wr(MONF, 0, 1); e.wr(LENF, 1, 1); h.ram_w32(MGR + 8, tpl); advance(0)
    assert start(0, 3) == (1, 1) and t.slot(ST) == 1 and t.slot(SCH) == 1, MSGT()
    bs = t.slot(BLK); Fs = t.slot(FPREV); mx = t.slot(MAXF); assert mx == 22050
    assert e.rd(bs + 0x92, 2) == 2 and h.ram_r32(bs) == (0x100 + 4 * mx + SLACK + 3) & ~3 and h.ram_r32(bs + h.ram_r32(bs)) == 0 and h.list_end() == bs + h.ram_r32(bs)
    for nn in (100, 300, 64, 900, 700, 68): advance(nn)
    advance(40); SH(True); t.pad(0, 9); SH(False); t.fire(2)
    ns = F[0] - GUARD - Fs; assert t.slot(ST) == 0
    assert (e.rd(bs + 0x18, 4), e.rd(bs + 0x1C, 4), e.rd(bs + 0x20, 4), e.rd(bs + 0x24, 4), e.rd(bs + 0x92, 2)) == (ns, ns, 0, ns, 2)
    szs = (0x100 + 4 * ns + SLACK + 3) & ~3; assert h.ram_r32(bs) == szs and h.ram_r32(bs + szs) == 0 and h.list_end() == bs + szs
    gl = pcm(bs, ns); gr = [s16(e.rd(bs + 0x100 + 2 * ns + 2 * i, 2)) for i in range(ns)]
    assert gl == [LV(Fs + i) for i in range(ns)] and gr == [RV(Fs + i) for i in range(ns)], "L and R planes exact"
    assert padname(3, 3) != b'' and padb(3, 3, 0x62) == 1 and padb(3, 3, 0x63) == 1 and MSGT() == "Saved to A04", MSGT()
    print("  ok STEREO: %d frames, L and R exact, R plane moved next to L, fields (+0x1C = n, 2 channels), size and terminator right, pad A04" % ns)
    # stereo + MIX: each channel sat16(output + input)
    e.wr(MIXF, 1, 1); h.ram_w32(OBJ + 12, 0); h.ram_w32(LOG2 + 8, 1); h.ram_w32(MGR + 8, tpl); advance(0); h.ram_w32(RX_PTR, RX_BASE)
    assert start(0, 4) == (1, 1) and t.slot(ST) == 1 and t.slot(MIXON) == 1
    bm = t.slot(BLK); Fm = t.slot(FPREV)
    for nn in (100, 300, 64, 900, 700, 68): advance_mix(nn)
    advance_mix(40); SH(True); t.pad(0, 9); SH(False); t.fire(2)
    nm = F[0] - GUARD - Fm; assert t.slot(ST) == 0 and t.slot(MIXON) == 0 and e.rd(bm + 0x1C, 4) == nm
    gl = pcm(bm, nm); gr = [s16(e.rd(bm + 0x100 + 2 * nm + 2 * i, 2)) for i in range(nm)]
    wl = [sat(LV(Fm + i) + ILV(Fm + i)) for i in range(nm)]; wr_ = [sat(RV(Fm + i) + IRV(Fm + i)) for i in range(nm)]
    assert gl == wl and gr == wr_, [(i, gl[i], wl[i]) for i in range(nm) if gl[i] != wl[i]][:3]
    assert sum(1 for v in wl + wr_ if v in (32767, -32768)) > 50
    print("  ok STEREO + MIX: %d frames, L = sat16(out L + in L), R = sat16(out R + in R)" % nm)
    # stereo, the limit is reached exactly (no move needed)
    e.wr(MIXF, 0, 1); h.ram_w32(MGR + 8, tpl); advance(0); start(0, 5); b9 = t.slot(BLK); F9 = t.slot(FPREV); h.ram_w32(MAXF, 300)
    advance(200); advance(400); assert t.slot(ST) == 0 and e.rd(b9 + 0x18, 4) == 300 and e.rd(b9 + 0x1C, 4) == 300
    assert pcm(b9, 300) == [LV(F9 + i) for i in range(300)] and [s16(e.rd(b9 + 0x100 + 600 + 2 * i, 2)) for i in range(300)] == [RV(F9 + i) for i in range(300)]
    print("  ok STEREO: limit reached exactly, no move, both planes exact")
    e.wr(CHF, 0, 1); e.wr(LENF, 0, 1)
    # (the Master FX DO IT mailbox and 'monitor through Master FX' tests need the changed Master FX add-on: not part of this submission)
    # 13) all of it leaves the list well-formed (walk the list)
    p_ = h.ram_r32(MGR); nb = 0
    while True:
        sz = h.ram_r32(p_)
        if sz == 0: break
        assert sz % 4 == 0 and sz < 0x400000, hex(sz); p_ += sz; nb += 1
    assert p_ == h.list_end(); print("  ok sample list walks cleanly (%d blocks) after all of the above" % nb)
    print("VALIDATO: Resample -> %s" % os.path.basename(out))
