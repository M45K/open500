#!/usr/bin/env python3
# NOTE REPEAT (MODE -> OTHER -> "Note Repeat") -> NOTERPT.WAV   (2026-11-06)
#   Hold a pad with this page open: the pad is hit again and again at the chosen rate until you let go.
#   row 0: "RPT*1/16 - V:OFF"   RATE 1/1 (one bar) 1/2 1/4 1/8 1/16 1/32 1/64, TYPE  - plain  T triplet  . dotted,  V: velocity varies the rate
#   row 1: "EUC N16 H 5 R 0"    PAT OFF / EUC / RND, N = steps (1-32), H = hits, R = rotate (EUC) or seed (RND)
#   '*' after RPT while a pad is being repeated.
#   RATE: note value in a 4/4 bar, 96 ticks per quarter (the OS's resolution): 1/1 = 384 ticks ... 1/64 = 6 ticks; T = x 2/3, . = x 3/2.
#   V: OFF  the rate is as set.   PRS  (per press) a hard hit (velocity >= 85) goes one rate faster, a soft hit (< 43) one rate slower, in between as set
#          (clamped 1/1 .. 1/64), decided when the pad goes down.   LIV  (live, 2026-11-06) the same, but it follows the PRESSURE of the held pad: before each
#          step the newest 0xA0 message for the pad in the OS pad ring is read (ring 0x8848C2E0, 64 x u32, write index = (*0x881C0628 >> 2) & 63; message =
#          type 0xA0 << 24 | pad << 16 | pressure; found in the OS pad reader 0x884E7544, confirmed with Ring Peek on the MPC) and sets the rate of the next steps
#          and the VELOCITY of the hits (pressure, 1..127; the first hit uses the press velocity).
#   PAT   OFF  every step hits.   EUC  Euclidean pattern: N steps, H hits spread as evenly as possible (step i hits when ((i + R) mod N) * H mod N < H),
#                                  R rotates it.   RND  random pattern: each step hits with probability H / N, a new pattern every loop of N steps
#                                  (R = 0); R = 1..31 is a seed: the same random pattern every loop.   The pattern restarts at step 1 on every pad hit.
#   Timing: with a sequence running the first hit waits for the next grid line of the rate (position estimate as Seq Launch / Grid Seq: tick of the
#   last played event + audio frames * tempo), then the steps follow the audio frame counter (44100 / s) at the tempo read when the pad went down.
#   Stopped: the first hit is at once and the steps run free at the tempo.   Gate = half a step.
#   How the hits sound: the add-on posts pad events {type 0x38, code = pad 0..11, data = velocity (press) / 0 (release)} into the OS event queue
#   (os post_event 0x884E6DF2, queue 0x88483EE4: the same events the pads make; code + bank as the real pad: the bank is the OS's own current bank).  The page
#   lets those through to the OS (it recognises them: pad + velocity of the last event it posted) and swallows the real pad events (as Seq Launch).
#   UNVERIFIED on hardware: that the posted pad events play the pad and are recorded like a real hit (this is what the add-on tests first).
#   Addresses: pad event 0x38 / bank byte 0x8849C98C & 3 (Pad Mute, Seq Launch), post_event (mpcmod), audio frame counter 0x881C05F0, play pointer 0x8847CBD0,
#   current sequence block 0x8847CBC8 (events 16 B from +0x1C10, end tick 0x7FFFFFFF), system parameter call 0x884E5CD0 id 0x2000C = tempo (BPM x 10).
import sys, os
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti
from mpcmod import EVQ, OS_POST_EVENT

CURSEQ, PLAYPTR, FRAMES, TPF = 0x8847CBC8, 0x8847CBD0, 0x881C05F0, 275625
SYS_PARAM, ID_TEMPO = 0x884E5CD0, 0x2000C
EV_OFF, BARS_OFF, BART_OFF = 0x1C10, 0x1C, 0x30
BANKOBJ, EV_PAD = 0x8849C98C, 0x38

pg = PageAddon("noterpt1", open_press=None)
pg.other_menu("Note Repeat")
pg.label("RPT", 0, 0)
IND = pg.dlabel(3, 0, 1)
RATE = pg.choice("", 4, 0, ["1/1 ", "1/2 ", "1/4 ", "1/8 ", "1/16", "1/32", "1/64"], default=4)
TYPEF = pg.choice("", 9, 0, ["-", "T", "."], default=0)
pg.label("V:", 11, 0)
VELF = pg.choice("", 13, 0, ["OFF", "PRS", "LIV"], default=0)
PAT = pg.choice("", 0, 1, ["OFF", "EUC", "RND"], default=0)
NF = pg.value("N", 4, 1, fmt=" 0", lo=1, hi=32, default=16)
HF = pg.value("H", 8, 1, fmt=" 0", lo=0, hi=32, default=5)
RF = pg.value("R", 12, 1, fmt=" 0", lo=0, hi=31, default=0)
# position estimate (as Seq Launch)
PPREV, PTICK, PNEXT, PFR, TEMPO, PON, POS = [pg.alloc_slot() for _ in range(7)]
MSGB = [pg.alloc_slot() for _ in range(4)]
# repeater
HELD, HVEL, NEWP, RTEMPO, STEPF, NEXT, RELAT, RELPEND, STEPI, MASK, RNG = [pg.alloc_slot() for _ in range(11)]
INJP, INJR, INJPAD, INJVEL, INJT = [pg.alloc_slot() for _ in range(5)]
CURV, PW = [pg.alloc_slot() for _ in range(2)]
RING, WIDX = 0x8848C2E0, 0x881C0628

def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()

def fire(m):
    a = m.a
    call(m, 'lq_track'); call(m, 'rp_run'); call(m, 'rp_ind'); a.movl_pool('rp_end', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # ---- lq_off: not running (no usable position) ----
    a.deflabel('lq_off'); sti(m, 0, PON); a.mov_imm(-1, 0); st(m, 0, POS); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- lq_track: follow the playback position (Seq Launch / Grid Seq method) -> POS = tick in the current sequence, -1 = not running ----
    a.deflabel('lq_track'); a.stsl_pr()
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 4); ld(m, PPREV, 0); a.cmpeq(4, 0); same = m.newlabel('same'); m.bt_far(same)
    st(m, 4, PPREV)
    m.imm32(CURSEQ, 1); a.movl_ld(1, 5); a.tst_rr(5, 5); hs = m.newlabel('hs'); a.bf_lbl(hs); a.bra_lbl('lq_off'); a.nop()
    a.deflabel(hs); m.imm32(EV_OFF, 0); a.add_rr(0, 5); a.cmphi(5, 4); pst = m.newlabel('pst'); a.bt_lbl(pst)  # pointer at / before the first event: tick 0
    a.mov_imm(0, 0); st(m, 0, PTICK); nxt = m.newlabel('nxt'); a.bra_lbl(nxt); a.nop()
    a.deflabel(pst); a.mov_rm_rn(4, 0); a.add_imm(-16, 0); a.movl_ld(0, 0); st(m, 0, PTICK)  # event that just played
    a.deflabel(nxt); a.movl_ld(4, 0); st(m, 0, PNEXT)  # next event (END = 0x7FFFFFFF)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, PFR)
    call(m, 'rp_tempo'); ld(m, RTEMPO, 0); st(m, 0, TEMPO); sti(m, 1, PON)
    a.deflabel(same)
    ld(m, PON, 0); a.tst_rr(0, 0); m.bt_far('lq_off')
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); ld(m, PFR, 0); a.sub(0, 4)  # r4 = frames since
    m.imm32(1000000, 0); a.cmphs(0, 4); m.bt_far('lq_off')  # > 22 s without events: stopped
    ld(m, TEMPO, 0); a.mul_l(0, 4); a.sts_macl(4); m.imm32(TPF, 5); pg._hcall(m, '__udiv')
    ld(m, PTICK, 4); a.add_rr(4, 0); a.mov_rm_rn(0, 6)  # r6 = position estimate
    ld(m, PNEXT, 7)
    m.imm32(CURSEQ, 2); a.movl_ld(2, 1); a.mov_rm_rn(1, 2); a.add_imm(BARS_OFF, 2); a.movw_ld(2, 0); a.extu_w(0, 0)
    a.shll2(0); a.add_rr(0, 1); m.imm32(BART_OFF, 0); a.add_rr(0, 1); a.movl_ld(1, 0); m.imm32(0xFFFFFF, 1); a.and_rr(1, 0)  # r0 = end of sequence
    pe = m.newlabel('pe'); a.cmphi(0, 7); a.bf_lbl(pe); a.mov_rm_rn(0, 7); a.deflabel(pe)  # r7 = min(next event, end)
    a.mov_rm_rn(7, 0); a.add_imm(48, 0); a.cmphi(0, 6); m.bt_far('lq_off')  # more than half a beat late: stopped
    pc_ = m.newlabel('pc'); a.cmphi(7, 6); a.bf_lbl(pc_); a.mov_rm_rn(7, 6); a.deflabel(pc_)  # never beyond the next event
    st(m, 6, POS); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_tempo: RTEMPO = tempo x 10 (system parameter), 1200 if unknown ----
    a.deflabel('rp_tempo'); a.stsl_pr()
    sti(m, 0, MSGB[1]); sti(m, 0x10, MSGB[0]); sti(m, ID_TEMPO, MSGB[3]); sti(m, 0, MSGB[2])
    m.imm32(MSGB[0], 4); m.imm32(SYS_PARAM, 2); a.jsr(2); a.nop()
    ld(m, MSGB[1], 0); a.tst_rr(0, 0); tk = m.newlabel('tk'); a.bf_lbl(tk); m.imm32(1200, 0); a.deflabel(tk); st(m, 0, RTEMPO)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_inj: post a pad event: r4 = velocity (0 = release), r5 = pad 0..11 ----
    a.deflabel('rp_inj'); a.stsl_pr()
    a.mov_rm_rn(4, 10); a.mov_rm_rn(5, 11)
    st(m, 11, INJPAD); st(m, 10, INJVEL)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, INJT)
    a.tst_rr(10, 10); ir = m.newlabel('ir'); a.bt_lbl(ir); sti(m, 1, INJP); ij = m.newlabel('ij'); a.bra_lbl(ij); a.nop()
    a.deflabel(ir); sti(m, 1, INJR)
    a.deflabel(ij)
    a.add_imm(-8, 15)
    a.mov_rm_rn(11, 0); a.shll16(0); a.or_imm(EV_PAD); a.movl_st(0, 15)
    a.mov_rm_rn(10, 0); a.movl_st_disp(0, 4, 15)
    m.imm32(EVQ, 4); a.mov_rm_rn(15, 5); m.imm32(OS_POST_EVENT, 2); a.jsr(2); a.nop()
    a.add_imm(8, 15)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_mask: r4 = 1 fresh start, 0 = next loop.  MASK = bit i set = step i hits ----
    a.deflabel('rp_mask'); a.stsl_pr()
    m.imm32(PAT, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.tst_rr(0, 0); pm1 = m.newlabel('pm1'); a.bf_lbl(pm1)
    a.mov_imm(-1, 0); st(m, 0, MASK); a.ldsl_pr(); a.rts(); a.nop()  # OFF: every step hits
    a.deflabel(pm1)
    a.mov_rm_rn(0, 10); a.mov_rm_rn(4, 9)  # r10 = PAT, r9 = fresh
    ld(m, NF, 12); a.mov_imm(1, 0); a.cmpge(0, 12); pn_ = m.newlabel('pn'); a.bt_lbl(pn_); a.mov_imm(1, 12); a.deflabel(pn_)  # N >= 1
    ld(m, HF, 13); a.cmphi(12, 13); ph_ = m.newlabel('ph'); a.bf_lbl(ph_); a.mov_rm_rn(12, 13); a.deflabel(ph_)  # H <= N
    ld(m, RF, 14)
    a.mov_imm(0, 11); a.mov_imm(1, 3)  # r11 = mask, r3 = bit
    a.mov_imm(2, 0); a.cmpeq(0, 10); m.bt_far('rp_rnd')
    # EUC: R mod N
    a.mov_rm_rn(14, 4); a.mov_rm_rn(12, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 14)
    a.mov_imm(0, 9)  # r9 = i
    eu = m.newlabel('eu'); a.deflabel(eu)
    a.mov_rm_rn(9, 4); a.add_rr(14, 4); a.mov_rm_rn(12, 5); a.mov_rm_rn(3, 8); pg._hcall(m, '__udiv'); a.mov_rm_rn(8, 3)  # r1 = (i + R) mod N (r3 kept in r8)
    a.mov_rm_rn(1, 4); a.mul_l(13, 4); a.sts_macl(4); a.mov_rm_rn(12, 5); pg._hcall(m, '__udiv')  # r1 = (j * H) mod N
    a.cmphi(1, 13); en = m.newlabel('en'); a.bf_lbl(en); a.or_rr(3, 11); a.deflabel(en)
    a.shll(3); a.add_imm(1, 9); a.cmphs(12, 9); a.bf_lbl(eu)
    st(m, 11, MASK); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('rp_rnd')
    # RND: seed
    a.tst_rr(14, 14); rs = m.newlabel('rs'); a.bt_lbl(rs)
    m.imm32(0x9E3779B1, 0); a.mul_l(0, 14); a.sts_macl(9); a.mov_imm(1, 0); a.or_rr(0, 9); a.bra_lbl('rnd_go'); a.nop()  # fixed seed from R
    a.deflabel(rs); ld(m, RNG, 9)
    a.tst_rr(9, 9); rz = m.newlabel('rz'); a.bf_lbl(rz); m.imm32(0x2545F491, 9); a.deflabel(rz)
    a.deflabel('rnd_go')
    m.pool_jump()
    a.mov_imm(0, 10)  # r10 = i
    rl = m.newlabel('rl'); a.deflabel(rl)
    a.mov_rm_rn(9, 2); a.shll8(2); a.shll2(2); a.shll2(2); a.shll(2); a.xor_rr(2, 9)
    a.mov_rm_rn(9, 2); a.shlr16(2); a.shlr(2); a.xor_rr(2, 9)
    a.mov_rm_rn(9, 2); a.shll2(2); a.shll2(2); a.shll(2); a.xor_rr(2, 9)
    a.mov_rm_rn(9, 4); a.mov_rm_rn(12, 5); a.mov_rm_rn(3, 8); pg._hcall(m, '__udiv'); a.mov_rm_rn(8, 3)  # r1 = random mod N
    a.cmphi(1, 13); rn_ = m.newlabel('rn'); a.bf_lbl(rn_); a.or_rr(3, 11); a.deflabel(rn_)
    a.shll(3); a.add_imm(1, 10); a.cmphs(12, 10); a.bf_lbl(rl)
    st(m, 9, RNG); st(m, 11, MASK); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_len: STEPF = step length in frames from RATE / TYPE / the velocity or pressure CURV (V: PRS / LIV); r10 = length in ticks on return ----
    a.deflabel('rp_len'); a.stsl_pr()
    m.imm32(RATE, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_rm_rn(0, 10)  # r10 = rate 0..6
    m.imm32(VELF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.tst_rr(0, 0); nv = m.newlabel('nv'); a.bt_lbl(nv)
    ld(m, CURV, 0); a.mov_imm(43, 1); a.cmphs(1, 0); g43 = m.newlabel('g43'); a.bt_lbl(g43); a.add_imm(-1, 10); a.bra_lbl(nv); a.nop()  # vel < 43: slower
    a.deflabel(g43); a.mov_imm(85, 1); a.cmphs(1, 0); a.bf_lbl(nv); a.add_imm(1, 10)  # vel >= 85: faster
    a.deflabel(nv)
    a.mov_imm(0, 0); a.cmpge(0, 10); c1 = m.newlabel('c1'); a.bt_lbl(c1); a.mov_imm(0, 10); a.deflabel(c1)  # clamp 0..6
    a.mov_imm(6, 0); a.cmpgt(0, 10); c2 = m.newlabel('c2'); a.bf_lbl(c2); a.mov_imm(6, 10); a.deflabel(c2)
    m.imm32(384, 0); a.mov_rm_rn(10, 11); lp_ = m.newlabel('lp'); ld_ = m.newlabel('ld'); a.deflabel(lp_); a.tst_rr(11, 11); a.bt_lbl(ld_)
    a.shlr(0); a.add_imm(-1, 11); a.bra_lbl(lp_); a.nop()
    a.deflabel(ld_)  # r0 = ticks of the plain note
    a.mov_rm_rn(0, 10)
    m.imm32(TYPEF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.tst_rr(0, 0); pl_ = m.newlabel('pl'); a.bt_lbl(pl_)
    a.mov_imm(1, 1); a.cmpeq(1, 0); dt_ = m.newlabel('dt'); a.bf_lbl(dt_)
    a.mov_rm_rn(10, 4); a.shll(4); a.mov_imm(3, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(0, 10); a.bra_lbl(pl_); a.nop()  # triplet: x 2/3
    a.deflabel(dt_); a.mov_rm_rn(10, 1); a.shlr(1); a.add_rr(1, 10)  # dotted: x 3/2
    a.deflabel(pl_)  # r10 = L ticks
    a.mov_rm_rn(10, 4); m.imm32(TPF, 0); a.mul_l(0, 4); a.sts_macl(4); ld(m, RTEMPO, 5); pg._hcall(m, '__udiv'); st(m, 0, STEPF)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_press: newest 0xA0 message of the held pad in the OS pad ring since the last look -> CURV (the pad's pressure) ----
    a.deflabel('rp_press'); a.stsl_pr()
    m.imm32(WIDX, 1); a.movl_ld(1, 0); a.shlr2(0); a.and_imm(63); a.mov_rm_rn(0, 8)  # r8 = write index
    ld(m, PW, 11); a.cmpeq(8, 11); m.bt_far('pp_done')
    m.imm32(RING, 12); ld(m, HELD, 13); a.add_imm(-1, 13); a.mov_rm_rn(8, 9)
    ppl = m.newlabel('ppl'); ppn = m.newlabel('ppn'); a.deflabel(ppl)
    a.add_imm(-1, 9); a.mov_imm(63, 0); a.and_rr(0, 9)
    a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(12, 0); a.movl_ld(0, 4)  # r4 = entry
    a.mov_rm_rn(4, 2); a.shlr16(2); a.shlr8(2); a.extu_b(2, 2); a.mov_imm(-96, 0); a.extu_b(0, 0); a.cmpeq(0, 2); a.bf_lbl(ppn)
    a.mov_rm_rn(4, 2); a.shlr16(2); a.mov_imm(15, 0); a.and_rr(0, 2); a.cmpeq(13, 2); a.bf_lbl(ppn)
    a.extu_b(4, 4); st(m, 4, CURV); a.bra_lbl('pp_done'); a.nop()
    a.deflabel(ppn); a.cmpeq(9, 11); m.bf_far(ppl)
    a.deflabel('pp_done'); st(m, 8, PW); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_start: a pad went down: tempo, step length, first hit time, pattern ----
    a.deflabel('rp_start'); a.stsl_pr()
    call(m, 'rp_tempo')
    ld(m, HVEL, 0); st(m, 0, CURV); m.imm32(WIDX, 1); a.movl_ld(1, 0); a.shlr2(0); a.and_imm(63); st(m, 0, PW)
    call(m, 'rp_len')
    m.imm32(FRAMES, 1); a.movl_ld(1, 6)  # r6 = now
    ld(m, POS, 7); a.mov_imm(-1, 0); a.cmpeq(0, 7); fr = m.newlabel('fr'); m.bt_far(fr)
    a.mov_rm_rn(7, 4); a.mov_rm_rn(10, 5); pg._hcall(m, '__udiv')  # r1 = POS mod L
    a.tst_rr(1, 1); m.bt_far(fr)
    a.mov_rm_rn(10, 4); a.sub(1, 4); m.imm32(TPF, 0); a.mul_l(0, 4); a.sts_macl(4); ld(m, RTEMPO, 5); pg._hcall(m, '__udiv')  # frames to the next grid line
    a.add_rr(0, 6)
    a.deflabel(fr)
    st(m, 6, NEXT); sti(m, 0, STEPI)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); a.or_imm(1); st(m, 0, RNG)  # RND seed (used when R = 0)
    a.mov_imm(1, 4); call(m, 'rp_mask')
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_run: every fire ----
    a.deflabel('rp_run'); a.stsl_pr()
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); ld(m, INJT, 0); a.sub(0, 4); m.imm32(22050, 0); a.cmphi(0, 4); ok_ = m.newlabel('ok'); a.bf_lbl(ok_)
    sti(m, 0, INJP); sti(m, 0, INJR)  # an injected event that never came back: forget it
    a.deflabel(ok_)
    ld(m, RELPEND, 0); a.tst_rr(0, 0); nr = m.newlabel('nr'); m.bt_far(nr)
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); ld(m, RELAT, 5); a.sub(5, 4); a.cmppz(4); m.bf_far(nr)
    sti(m, 0, RELPEND); a.mov_imm(0, 4); ld(m, INJPAD, 5); call(m, 'rp_inj')  # gate end: release
    a.deflabel(nr)
    ld(m, HELD, 0); a.tst_rr(0, 0); hv = m.newlabel('hv'); a.bf_lbl(hv); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(hv)
    ld(m, NEWP, 0); a.tst_rr(0, 0); nn = m.newlabel('nn'); m.bt_far(nn)
    sti(m, 0, NEWP); call(m, 'rp_start')
    a.deflabel(nn)
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); ld(m, NEXT, 5); a.sub(5, 4); a.cmppz(4); m.bt_far('rp_due')
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('rp_due')
    m.imm32(VELF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(2, 1); a.cmpeq(1, 0); nol = m.newlabel('nol'); m.bf_far(nol)
    call(m, 'rp_press'); call(m, 'rp_len')  # LIV: pressure now -> step length of the next steps
    a.deflabel(nol)
    ld(m, STEPI, 7); ld(m, MASK, 0); a.mov_imm(1, 3); a.mov_rm_rn(7, 6)
    bl = m.newlabel('bl'); bd = m.newlabel('bd'); a.deflabel(bl); a.tst_rr(6, 6); a.bt_lbl(bd); a.shll(3); a.add_imm(-1, 6); a.bra_lbl(bl); a.nop()
    a.deflabel(bd); a.tst_rr(3, 0); nh = m.newlabel('nh'); m.bt_far(nh)  # T = no hit on this step
    ld(m, HVEL, 4); m.imm32(VELF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(2, 1); a.cmpeq(1, 0); hv_ = m.newlabel('hv'); a.bf_lbl(hv_)
    ld(m, CURV, 4); a.tst_rr(4, 4); a.bf_lbl(hv_); a.mov_imm(1, 4)  # LIV: the hit velocity follows the pressure (1..127)
    a.deflabel(hv_); a.mov_imm(127, 0); a.cmphi(0, 4); hc_ = m.newlabel('hc'); a.bf_lbl(hc_); a.mov_rm_rn(0, 4); a.deflabel(hc_)
    ld(m, HELD, 5); a.add_imm(-1, 5); call(m, 'rp_inj')
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); ld(m, STEPF, 5); a.shlr(5); a.add_rr(5, 4); st(m, 4, RELAT); sti(m, 1, RELPEND)
    a.deflabel(nh)
    ld(m, STEPI, 7); a.add_imm(1, 7); ld(m, NF, 6); a.cmphs(6, 7); sw = m.newlabel('sw'); a.bf_lbl(sw)
    a.mov_imm(0, 7); st(m, 7, STEPI)
    m.imm32(PAT, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(2, 1); a.cmpeq(0, 1); wr = m.newlabel('wr'); a.bf_lbl(wr)
    a.mov_imm(0, 4); call(m, 'rp_mask')
    a.deflabel(wr); sw2 = m.newlabel('sw2'); a.bra_lbl(sw2); a.nop()
    a.deflabel(sw); st(m, 7, STEPI)
    a.deflabel(sw2)
    ld(m, NEXT, 4); ld(m, STEPF, 5); a.add_rr(5, 4); st(m, 4, NEXT)
    m.imm32(FRAMES, 1); a.movl_ld(1, 6); a.sub(4, 6); a.cmpge(5, 6); ok2 = m.newlabel('ok2'); a.bf_lbl(ok2)  # more than a step behind: restart the clock
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); a.add_rr(5, 4); st(m, 4, NEXT)
    a.deflabel(ok2)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- rp_ind: '*' after RPT while a pad repeats ----
    a.deflabel('rp_ind'); a.stsl_pr()
    a.mov_imm(0x20, 4); ld(m, HELD, 0); a.tst_rr(0, 0); li = m.newlabel('li'); a.bt_lbl(li); a.mov_imm(ord('*'), 4)
    a.deflabel(li); m.imm32(pg.addr['dlabels'][IND], 1); a.movb_ld(1, 0); a.cmpeq(4, 0); lr = m.newlabel('lr'); a.bt_lbl(lr)
    a.movb_st(4, 1); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel(lr); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('rp_end')
pg.on_fire(fire)

def inactive(m):
    a = m.a
    sti(m, 0, HELD); sti(m, 0, RELPEND); sti(m, 0, INJP); sti(m, 0, INJR)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """page open: our own posted events go through to the OS; real pad events start / stop the repeat and are swallowed"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.mov_rm_rn(0, 10)  # r10 = velocity
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9); a.mov_imm(12, 0); a.cmphs(0, 9); rel = m.newlabel('rel'); m.bt_far(rel)  # pad >= 12: swallow
    pas = m.newlabel('pas'); a.tst_rr(10, 10); isrel = m.newlabel('isrel'); m.bt_far(isrel)
    # press: ours?
    ld(m, INJP, 0); a.tst_rr(0, 0); m.bt_far('ev_real')
    ld(m, INJPAD, 0); a.cmpeq(9, 0); m.bf_far('ev_real'); ld(m, INJVEL, 0); a.cmpeq(10, 0); m.bf_far('ev_real')
    sti(m, 0, INJP); a.bra_lbl(pas); a.nop()
    a.deflabel('ev_real')
    a.mov_rm_rn(9, 0); a.add_imm(1, 0); st(m, 0, HELD); st(m, 10, HVEL); sti(m, 1, NEWP); a.bra_lbl(rel); a.nop()
    a.deflabel(isrel)
    ld(m, INJR, 0); a.tst_rr(0, 0); nj = m.newlabel('nj'); m.bt_far(nj)
    ld(m, INJPAD, 0); a.cmpeq(9, 0); m.bf_far(nj)
    sti(m, 0, INJR); a.bra_lbl(pas); a.nop()
    a.deflabel(nj)
    ld(m, HELD, 0); a.add_imm(-1, 0); a.cmpeq(9, 0); m.bf_far(rel)  # real release of the held pad
    sti(m, 0, HELD)
    ld(m, RELPEND, 0); a.tst_rr(0, 0); m.bt_far(rel)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, RELAT)  # gate still open: release at the next fire
    a.deflabel(rel); a.movl_pool(hret1, 2); a.jmp(2); a.nop()
    a.deflabel(pas); a.movl_pool(hret0, 2); a.jmp(2); a.nop()
    m.pool_jump()
pg.on_event(EV_PAD, ev_pad)

if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'NOTERPT.WAV'); pg.build_wav(out)
    import struct
    from testkit import PageTest
    import testseq
    t = PageTest(pg, out); h = t.h; e = t.e; t.open()
    LOG = 0x88E20000
    def logger(s_):  # fake post_event: appends {word0, word1} to LOG+8+8n
        s_.movl_pool(LOG, 1); s_.movl_ld(1, 2); s_.mov_rm_rn(2, 3); s_.shll2(3); s_.shll(3)
        s_.movl_pool(LOG + 8, 6); s_.add_rr(3, 6); s_.movl_ld(5, 0); s_.movl_st(0, 6); s_.movl_ld_disp(5, 4, 0); s_.movl_st_disp(0, 4, 6)
        s_.add_imm(1, 2); s_.movl_st(2, 1); s_.rts(); s_.nop()
    t.stub(OS_POST_EVENT, logger)
    t.stub(SYS_PARAM, lambda s_: (s_.movl_pool(1200, 2), s_.movl_st_disp(2, 4, 4), s_.mov_imm(1, 0), s_.rts(), s_.nop()))  # 120.0 BPM
    def evs():
        n = h.ram_r32(LOG); r = []
        for i in range(n):
            w0 = h.ram_r32(LOG + 8 + 8 * i); w1 = h.ram_r32(LOG + 12 + 8 * i)
            assert w0 & 0xFFFF == 0x38; r.append(((w0 >> 16), w1))
        return r
    def clr(): h.ram_w32(LOG, 0)
    FR = 0x500000
    def setfr(v): h.ram_w32(FRAMES, v)
    def sel(addr, v): e.wr(addr, v, 1)
    def press(pad, vel=100, bank=0): e.wr(BANKOBJ, bank, 1); return t.event(0x38, pad, vel)
    def release(pad): return t.event(0x38, pad, 0)
    def reset():
        t.set_slot(HELD, 0); t.set_slot(RELPEND, 0); t.set_slot(INJP, 0); t.set_slot(INJR, 0); clr(); setfr(FR); t.fire(2)
    def hits(nfr, step=100):  # fire every 'step' frames for 'nfr' frames, return frame offsets of the injected presses
        out = []; seen = 0
        for f in range(0, nfr, step):
            setfr(FR + f); t.fire(1)
            ev = evs()
            while seen < len(ev):
                if ev[seen][1] != 0: out.append(f)
                seen += 1
        return out
    # --- stopped: first hit at once, then every step; 1/16 at 120 BPM = 24 ticks = 5512 frames; gate = half a step ---
    reset(); assert press(3, 100) == 1, "real pad press swallowed"
    setfr(FR); t.fire(1)
    assert t.slot(STEPF) == 24 * TPF // 1200 and t.slot(HELD) == 4 and evs() == [(3, 100)], (t.slot(STEPF), evs())
    assert e.rd(pg.dlabels[IND], 1) == ord('*')
    setfr(FR + 2000); t.fire(1); assert evs() == [(3, 100)], "no second hit before the step"
    setfr(FR + 2800); t.fire(1); assert evs() == [(3, 100), (3, 0)], "gate end = release at half a step: %s" % evs()
    setfr(FR + 5600); t.fire(1); assert evs()[-1] == (3, 100) and len(evs()) == 3, evs()
    # --- our own posted events are let through, the real ones are not ---
    assert t.event(0x38, 3, 0) == 0 and t.slot(INJR) == 0, "our release passes"  # (release was posted: INJR set at posting)
    assert t.event(0x38, 3, 100) == 0 and t.slot(INJP) == 0, "our press passes"
    # --- real release: stops, a still-open gate is released ---
    assert release(3) == 1 and t.slot(HELD) == 0
    setfr(FR + 5700); t.fire(1); n_before = len(evs()); setfr(FR + 5800); t.fire(1)
    assert evs()[-1] == (3, 0), "gate closed when the pad is let go: %s" % evs()
    setfr(FR + 20000); t.fire(2); assert len(evs()) == n_before and e.rd(pg.dlabels[IND], 1) == 0x20, "no more hits after the release"
    print("  ok hold: first hit at once, steps of 24 ticks, gate release, our events pass through, real events swallowed, stop on release")
    # --- rates / types: step length in ticks ---
    base = [384, 192, 96, 48, 24, 12, 6]
    def stepf(rate, typ, vel=100, v_on=0):
        reset(); sel(RATE, rate); sel(TYPEF, typ); sel(VELF, v_on); press(0, vel); setfr(FR); t.fire(1); r = t.slot(STEPF); release(0); t.fire(1); return r
    for r_ in range(7):
        for ty, mul in ((0, (1, 1)), (1, (2, 3)), (2, (3, 2))):
            ticks = base[r_] * mul[0] // mul[1]
            assert stepf(r_, ty) == ticks * TPF // 1200, (r_, ty, stepf(r_, ty), ticks)
    print("  ok rates: 1/1 ... 1/64, plain / triplet / dotted")
    # --- velocity: soft = one rate slower (longer step), hard = one faster, clamped ---
    assert stepf(4, 0, 20, 1) == 48 * TPF // 1200 and stepf(4, 0, 64, 1) == 24 * TPF // 1200 and stepf(4, 0, 120, 1) == 12 * TPF // 1200
    assert stepf(4, 0, 20, 0) == 24 * TPF // 1200 and stepf(4, 0, 120, 0) == 24 * TPF // 1200, "V:OFF ignores velocity"
    assert stepf(6, 0, 120, 1) == 6 * TPF // 1200 and stepf(0, 0, 10, 1) == 384 * TPF // 1200, "clamped at both ends"
    assert stepf(4, 1, 120, 1) == 8 * TPF // 1200, "faster + triplet"
    print("  ok velocity: soft slower, hard faster, V:OFF off, clamped")
    # --- patterns ---
    sel(VELF, 0); sel(RATE, 4); sel(TYPEF, 0)
    def run_loop(pat, n, hh, rr, loops=1):
        """returns list per loop of the steps (0..n-1) that hit"""
        reset(); sel(PAT, pat); t.set_slot(NF, n); t.set_slot(HF, hh); t.set_slot(RF, rr); press(5, 100)
        sf = 24 * TPF // 1200; res = []; cur = []; seen = 0
        for k in range(n * loops):
            setfr(FR + k * sf + 10); t.fire(1)
            ev = evs()
            hit = any(x[1] != 0 for x in ev[seen:]); seen = len(ev); cur.append(1 if hit else 0)
            if len(cur) == n: res.append(cur); cur = []
        release(5); t.fire(1); return res
    def euc(n, hh, rr): return [1 if (((i + rr) % n) * hh) % n < hh else 0 for i in range(n)]
    assert run_loop(0, 8, 3, 0) == [[1] * 8], "PAT OFF: every step"
    for n, hh, rr in ((8, 3, 0), (8, 3, 2), (16, 5, 0), (16, 5, 7), (7, 4, 1), (5, 5, 0), (12, 0, 0), (32, 11, 3), (1, 1, 0), (6, 9, 0)):
        got = run_loop(1, n, hh, rr)[0]; want = euc(n, min(hh, n), rr % n)
        assert got == want, ("EUC", n, hh, rr, got, want)
    assert sum(euc(8, 3, 0)) == 3 and sum(euc(16, 5, 0)) == 5
    print("  ok EUC: pattern as computed, H hits in N steps, rotate, H > N clamped, H = 0 silent")
    a1 = run_loop(2, 16, 6, 5, loops=3); assert a1[0] == a1[1] == a1[2], "RND with a seed: the same pattern every loop"
    a2 = run_loop(2, 16, 6, 9, loops=1); assert a2[0] != a1[0], "another seed, another pattern"
    b = run_loop(2, 16, 6, 0, loops=6); assert len({tuple(x) for x in b}) > 1, "RND with R = 0: a new pattern every loop"
    assert run_loop(2, 16, 16, 0, loops=2) == [[1] * 16] * 2 and run_loop(2, 16, 0, 0, loops=2) == [[0] * 16] * 2, "H = N always, H = 0 never"
    tot = sum(sum(x) for x in run_loop(2, 32, 8, 0, loops=40)); assert 0.15 * 32 * 40 < tot < 0.35 * 32 * 40, tot
    print("  ok RND: seeded patterns repeat, R = 0 re-rolls, density follows H / N")
    sel(PAT, 0)
    # --- a running sequence: the first hit waits for the next grid line ---
    seq = testseq.make_seq(); P = 0x881DB59C
    for i in range(0, len(seq), 4): h.ram_w32(P + i, struct.unpack('<I', seq[i:i + 4])[0])
    ts = [x[0] for x in testseq.events(8, 1)]
    h.ram_w32(CURSEQ, P)
    k2 = max(i for i, x in enumerate(ts) if 384 <= x < 768); reset()
    h.ram_w32(PLAYPTR, P + EV_OFF + 16 * (k2 + 1)); setfr(FR); t.fire(2); pos = t.slot(POS); assert pos == ts[k2], (pos, ts[k2])
    sel(RATE, 3); press(2, 100)  # 1/8 = 48 ticks
    setfr(FR); t.fire(1); rem = pos % 48; want_fr = (48 - rem) * TPF // 1200 if rem else 0
    sf = 48 * TPF // 1200
    assert t.slot(NEXT) - FR == want_fr and evs() == ([] if rem else [(2, 100)]), (t.slot(NEXT) - FR, want_fr, rem, evs())
    first = hits(want_fr + 2 * sf + 500)
    assert first and abs(first[0] - want_fr) <= 100 and abs(first[1] - first[0] - sf) <= 100, (first, want_fr, sf)
    release(2); t.fire(2); clr()
    # not running: at once
    reset(); setfr(FR + 5000000); t.fire(2); assert t.slot(POS) == 0xFFFFFFFF; press(2, 100); setfr(FR + 5000000); t.fire(1); assert evs() == [(2, 100)], evs()
    release(2); t.fire(2)
    print("  ok sync: running sequence = first hit on the grid line, then every step; stopped = at once")
    # --- another pad takes over; release of an old pad ignored; closing the page stops it; bad pads ---
    reset(); sel(RATE, 4); press(1, 90); setfr(FR); t.fire(1); press(7, 80); setfr(FR + 6000); t.fire(1)
    assert evs()[-1] == (7, 80) and t.slot(HELD) == 8, evs()
    assert t.event(0x38, 7, 80) == 0 and t.event(0x38, 7, 0) == 0, "the OS delivers our posted events"
    assert release(1) == 1 and t.slot(HELD) == 8, "release of the old pad does not stop the new one"
    assert release(7) == 1 and t.slot(HELD) == 0
    clr(); press(4, 100); setfr(FR + 20000); t.fire(1); assert t.slot(HELD) == 5; t.close(); t.fire(2); assert t.slot(HELD) == 0, "page closed: stops"
    t.open(); assert t.event(0x38, 12, 100) == 1 and t.slot(HELD) == 0, "pad code >= 12 ignored"
    print("  ok pads: last pad wins, old release ignored, page closed stops, bad pad ignored")
    # --- LIV: the pressure of the held pad (0xA0 messages of the OS pad ring) changes the rate and the hit velocity while held ---
    def ring_put(i, v): h.ram_w32(RING + 4 * (i & 63), v)
    reset(); sel(RATE, 4); sel(TYPEF, 0); sel(VELF, 2); sel(PAT, 0)
    w0 = 10; ring_put(w0 - 1, 0xA0030077); h.ram_w32(WIDX, w0 * 4)  # an old pressure message before the press: ignored
    press(3, 100); setfr(FR); t.fire(1)
    assert evs() == [(3, 100)] and t.slot(STEPF) == 12 * TPF // 1200, (evs(), t.slot(STEPF))  # velocity 100 = hard: one rate faster
    sf = 12 * TPF // 1200
    ring_put(w0, 0xA0050060); ring_put(w0 + 1, 0x90030055); ring_put(w0 + 2, 0xA0030014); h.ram_w32(WIDX, (w0 + 3) * 4)  # other pad / a hit message / pad 3 pressure 20
    setfr(FR + sf + 10); t.fire(1)
    assert evs()[-1] == (3, 20) or evs()[-2] == (3, 20), evs()
    assert t.slot(STEPF) == 48 * TPF // 1200, "soft pressure: one rate slower: %d" % t.slot(STEPF)
    n = len(evs()); setfr(FR + sf + 10 + 5000); t.fire(1); assert len(evs()) == n, "next step is 48 ticks away (slow)"
    ring_put(w0 + 3, 0xA0030078); h.ram_w32(WIDX, (w0 + 4) * 4); setfr(FR + sf + 10 + 11024 + 10); t.fire(1)
    ev = [x for x in evs() if x[1] != 0]; assert ev[-1] == (3, 120) and t.slot(STEPF) == 12 * TPF // 1200, (ev, t.slot(STEPF))
    ring_put(w0 + 4, 0xA0030000); h.ram_w32(WIDX, (w0 + 5) * 4); setfr(FR + 100000); t.fire(2); ev = [x for x in evs() if x[1] != 0]; assert ev[-1][1] == 1, "pressure 0 -> velocity 1: %s" % ev[-2:]
    release(3); t.fire(2); sel(VELF, 1)
    print("  ok LIV: live pressure of the held pad changes rate and hit velocity, other pads / hits / old messages ignored")
    # --- stale injected-event flags are forgotten ---
    reset(); sel(RATE, 4); press(0, 100); setfr(FR); t.fire(1); assert t.slot(INJP) == 1
    release(0); setfr(FR + 50000); t.fire(1); assert t.slot(INJP) == 0, "never came back: forgotten"
    setfr(FR + 100000); t.fire(1); assert t.slot(INJR) == 0
    print("OK: NOTERPT.WAV built and tested (rates 1/1..1/64 plain / T / dotted, velocity, EUC, RND, sync, pass-through)")
