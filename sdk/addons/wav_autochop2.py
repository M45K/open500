#!/usr/bin/env python3
# AUTO CHOP (pagina nativa) scritto con l'API pages.PageAddon -> CHOPAGE.WAV
#   riga 0: selettore sample (rotella)     riga 1: "B120.0 N  8 P  1"     DO IT = taglia, CANCEL = indietro
# v25 (2026-09-25): ANTEPRIMA (il pad k suona la fetta k: start/end del sample spostati solo per il play), DO IT =
#   "Hit first pad" -> il pad colpito (banco*12 + pad) e' il primo pad, oppure 2o DO IT = dal campo P; CANCEL annulla;
#   fette in Mono (+0x62 = 1) e mute group 32 (+0x63); messaggi che non spostano il cursore.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import page_or_addon, ld, st, sti

MGR, EXTRACT_CMD = 0x8848ED30, 0x884C5E90
PROG_BASE, PROG_IDX, PROG_STRIDE, MAXPAD = 0x8837B5AC, 0x8847C0E2, 0x1F98, 48

PLAYER, AUD_PLAY, AUD_STOP = 0x8848CD78, 0x884C2636, 0x884C29D2
BANKOBJ = 0x8849C98C                # byte 0 = banco pad corrente (0 = A)
CHOP_MUTE, CHOP_MONO = 32, 1
EV_PAD, LEAVE_FIRES = 0x38, 16

from pages import INPUT_OBJ, ENABLE_DOIT
from mpcmod import LOAD_SCRATCH, EVQ as _EVQ
pg = page_or_addon("chop26", open_press=None)   # SHIFT+STOP ora apre PAD TOOLS (2026-09-26); in Chop Shop = modo BPM
SHOWMSG = pg.SHOWMSG                # timer del messaggio della pagina (in Chop Shop ogni pagina ha il suo)
pg.other_menu("Auto Chop")          # voce nel menu OTHER dell'OS
pg.msg_fires = 0xC00
pg.keep_focus = True
pg.far_hend = True
pg.selector()
BPM10 = pg.value("B", 0, 1, fmt="  0.0", lo=300, hi=3000, default=1200)
NSL   = pg.value(" N", 6, 1, fmt="  0", lo=1, hi=MAXPAD, default=8)
PAD1  = pg.value(" P", 11, 1, fmt="  0", lo=1, hi=MAXPAD, default=1)
pg.message()
ERR, IDX, SRC, S0, E0, SLEN, LEN, PROG, BEAT, NB = [pg.alloc_slot() for _ in range(10)]
ACTIVE, OUTCNT, CONF, SV20, SV24, SV2C, SV3E = [pg.alloc_slot() for _ in range(7)]

def far_call(m, label):
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def fire(m):
    a = m.a
    sti(m, 0, OUTCNT); sti(m, 1, ACTIVE)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()   # l'OS spegne DO IT dopo ogni uso
pg.on_fire(fire)

def inactive(m):
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    sti(m, 0, ACTIVE); sti(m, 0, OUTCNT); sti(m, 0, CONF)
    a.deflabel(ix)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """domanda aperta: il pad colpito e' il primo pad -> taglia; altrimenti il pad k suona la fetta k"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on)
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop(); far_bra(m, hret1)
    a.deflabel(on)
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)                     # r9 = pad fisico 0..11
    ld(m, CONF, 0); a.tst_rr(0, 0); npk = m.newlabel('npk'); m.bt_far(npk)
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop()
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(9, 0)
    a.add_imm(1, 0); st(m, 0, PAD1); sti(m, 0, CONF)
    far_call(m, 'chopmsg'); far_bra(m, hret1)
    a.deflabel(npk)
    st(m, 9, IDX); far_call(m, 'geom')
    ld(m, ERR, 0); a.tst_rr(0, 0); hz = m.newlabel('hz'); a.bt_lbl(hz); far_bra(m, hret1); a.deflabel(hz)
    whole = m.newlabel('whole')
    ld(m, IDX, 9); ld(m, NSL, 0); a.cmphs(0, 9); m.bt_far(whole)                          # k >= N
    far_call(m, 'bounds'); a.tst_rr(0, 0); m.bt_far(whole)
    ld(m, SRC, 5)
    a.movl_ld_disp(5, 0x20, 0); st(m, 0, SV20); a.movl_ld_disp(5, 0x24, 0); st(m, 0, SV24); a.movl_ld_disp(5, 0x2C, 0); st(m, 0, SV2C)
    a.mov_rm_rn(5, 2); a.add_imm(0x3E, 2); a.movw_ld(2, 0); st(m, 0, SV3E); a.mov_imm(0, 0); a.movw_st(0, 2)   # +0x3E = 0: se no il lettore suona tutto il sample
    ld(m, SRC, 5); a.movl_st_disp(11, 0x20, 5); a.movl_st_disp(12, 0x24, 5); a.mov_imm(0, 0); a.movl_st_disp(0, 0x2C, 5)
    m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
    ld(m, SRC, 5); ld(m, SV20, 0); a.movl_st_disp(0, 0x20, 5); ld(m, SV24, 0); a.movl_st_disp(0, 0x24, 5); ld(m, SV2C, 0); a.movl_st_disp(0, 0x2C, 5)
    a.mov_rm_rn(5, 2); a.add_imm(0x3E, 2); ld(m, SV3E, 0); a.movw_st(0, 2)
    far_bra(m, hret1)
    a.deflabel(whole)
    ld(m, SRC, 5); m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
    far_bra(m, hret1)
pg.on_event(EV_PAD, ev_pad)

def on_cancel(m, hret1):
    a = m.a
    ld(m, CONF, 0); a.tst_rr(0, 0); cx = m.newlabel('cx'); a.bt_lbl(cx)
    sti(m, 0, CONF); sti(m, 1, SHOWMSG); far_bra(m, hret1)
    a.deflabel(cx)
pg.cancel_fn = on_cancel

def doit(m):
    a = m.a
    # ---- DO IT: 1a pressione -> "Hit first pad" (resta); 2a -> dal campo P ----
    a.bsr_lbl('geom'); a.nop()
    ld(m, ERR, 0); a.tst_rr(0, 0); dok = m.newlabel('dok'); m.bt_far(dok)
    pg.emit_message_dec(m, "Error ", ERR, ""); far_bra(m, 'hret1_')
    a.deflabel(dok)
    ld(m, CONF, 0); a.tst_rr(0, 0); cyes = m.newlabel('cyes'); m.bf_far(cyes)
    sti(m, 1, CONF); pg.emit_message(m, "Hit first pad"); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)
    far_bra(m, 'hret1_')
    a.deflabel(cyes); sti(m, 0, CONF)
    a.bsr_lbl('chopmsg'); a.nop(); far_bra(m, 'hret1_')
    m.pool_jump()
    # ---- chopmsg: taglia e mostra l'esito ----
    a.deflabel('chopmsg'); a.stsl_pr()
    a.bsr_lbl('chop'); a.nop()
    ld(m, ERR, 8); a.tst_rr(8, 8); iserr = m.newlabel('iserr'); m.bf_far(iserr)
    pg.emit_message_dec(m, "Done: ", IDX, " slices"); a.bra_lbl('__dd'); a.nop()
    a.deflabel(iserr); pg.emit_message_dec(m, "Error ", ERR, ""); a.deflabel('__dd')
    m.imm32(_EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- geom: sorgente = sample corrente; LEN, BEAT = rate*600/BPM10, NB = round(LEN/BEAT), SLEN = NB*BEAT/N ----
    a.deflabel('geom'); a.stsl_pr()
    sti(m, 0, ERR)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8); st(m, 8, SRC)
    a.tst_rr(8, 8); cno = m.newlabel('cno'); a.bf_lbl(cno); sti(m, 0x21, ERR); a.bra_lbl('__gx'); a.nop(); a.deflabel(cno)
    a.movl_ld_disp(8, 0x18, 2); st(m, 2, LEN)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 2); a.extu_w(2, 2); m.imm32(PROG_STRIDE, 3); a.mul_l(3, 2); a.sts_macl(2); m.imm32(PROG_BASE, 3); a.add_rr(3, 2); st(m, 2, PROG)
    ld(m, SRC, 8); m.imm32(0x94, 2); a.add_rr(8, 2); a.movl_ld(2, 4); m.imm32(600, 3); a.mul_l(3, 4); a.sts_macl(4); ld(m, BPM10, 5); pg._hcall(m, '__udiv'); st(m, 0, BEAT)
    ld(m, BEAT, 2); a.mov_rm_rn(2, 5); a.shlr(2); ld(m, LEN, 4); a.add_rr(2, 4); pg._hcall(m, '__udiv')
    a.tst_rr(0, 0); nbok = m.newlabel('nbok'); a.bf_lbl(nbok); a.mov_imm(1, 0); a.deflabel(nbok); st(m, 0, NB)
    ld(m, BEAT, 4); ld(m, NB, 2); a.mul_l(2, 4); a.sts_macl(4); ld(m, NSL, 5); pg._hcall(m, '__udiv'); st(m, 0, SLEN)
    a.deflabel('__gx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- bounds: r9 = i -> r11 = inizio, r12 = fine (inclusa); r0 = 0 se oltre la fine del sample ----
    a.deflabel('bounds')
    ld(m, SLEN, 10); a.mov_rm_rn(9, 11); a.mul_l(10, 11); a.sts_macl(11)
    ld(m, LEN, 2); a.mov_imm(0, 0); a.cmphs(2, 11); bx = m.newlabel('bx'); a.bt_lbl(bx)
    a.mov_rm_rn(11, 12); a.add_rr(10, 12); a.add_imm(-1, 12)
    a.cmphs(2, 12); eok = m.newlabel('eok'); a.bf_lbl(eok); a.mov_rm_rn(2, 12); a.add_imm(-1, 12); a.deflabel(eok)
    a.mov_imm(1, 0)
    a.deflabel(bx); a.rts(); a.nop()
    m.pool_jump()
    # ---- chop: fette -> Extract, pad da PAD1 (Mono, mute group) ----
    a.deflabel('chop'); a.stsl_pr()
    a.bsr_lbl('geom'); a.nop(); ld(m, ERR, 0); a.tst_rr(0, 0); m.bf_far('__cret')
    sti(m, 0, IDX)
    ld(m, SRC, 8); a.movl_ld_disp(8, 0x20, 2); st(m, 2, S0); a.movl_ld_disp(8, 0x24, 2); st(m, 2, E0)
    cl = m.newlabel('cl'); cend = m.newlabel('cend')
    a.deflabel(cl)
    ld(m, IDX, 9); ld(m, NSL, 2); a.cmpge(2, 9); m.bt_far(cend)
    ld(m, PAD1, 2); a.add_imm(-1, 2); a.add_rr(9, 2); a.mov_imm(MAXPAD, 0); a.cmpge(0, 2); m.bt_far(cend)
    a.bsr_lbl('bounds'); a.nop(); a.tst_rr(0, 0); m.bt_far(cend)
    ld(m, SRC, 8); a.movl_st_disp(11, 0x20, 8); a.movl_st_disp(12, 0x24, 8)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4)
    m.os_extract(8)
    a.tst_rr(0, 0); xok = m.newlabel('xok'); a.bt_lbl(xok); st(m, 0, ERR); far_bra(m, cend); a.deflabel(xok)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8); a.add_imm(4, 8)
    ld(m, IDX, 9); ld(m, PAD1, 2); a.add_imm(-1, 2); a.add_rr(9, 2); m.imm32(0xA4, 3); a.mul_l(3, 2); a.sts_macl(2); ld(m, PROG, 3); a.add_rr(3, 2); a.add_imm(0x18, 2)
    a.mov_rm_rn(2, 12); a.mov_imm(16, 3)
    cp = m.newlabel('cp'); a.deflabel(cp); a.movb_ldinc(8, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.dt(3); a.bf_lbl(cp)
    a.mov_rm_rn(2, 12); a.add_imm(0x10, 12); a.mov_imm(0, 0); a.movb_st_disp(0, 12)
    a.mov_imm(100, 0); a.movb_st_disp(1, 12); a.mov_imm(0, 0); a.movb_st_disp(2, 12); a.mov_imm(0x7F, 0); a.movb_st_disp(3, 12)
    a.mov_imm(0, 0); a.movb_st_disp(4, 12); a.movb_st_disp(5, 12); a.movb_st_disp(6, 12); a.movb_st_disp(7, 12)
    a.mov_rm_rn(2, 12); a.add_imm(0x60, 12)
    a.mov_imm(CHOP_MONO, 0); a.movb_st_disp(2, 12); a.mov_imm(CHOP_MUTE, 0); a.movb_st_disp(3, 12)   # +0x62 Mono, +0x63 gruppo
    ld(m, IDX, 9); a.add_imm(1, 9); st(m, 9, IDX)
    far_bra(m, cl)
    m.pool_jump()
    a.deflabel(cend)
    ld(m, SRC, 8); ld(m, S0, 2); a.movl_st_disp(2, 0x20, 8); ld(m, E0, 2); a.movl_st_disp(2, 0x24, 8)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4)
    a.deflabel('__cret'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('hret1_')
pg.on_doit(doit)

def selftest(out):
    """test in emulatore sul WAV 'out' (CHOPAGE.WAV, o CHOPSHOP.WAV con questa pagina ospitata)"""
    # ---- validazione emulatore (stessa del vecchio wav_chopage) ----
    from validate import Harness
    from mpcmod import keymask, KEYS_ADDR, EVQ, OS_PUSH_PAGE, LOAD_SCRATCH
    from pages import SET_FOCUS, ENABLE_DOIT, GROUP, VT_VALUE, VT_LABEL, SEL_CB
    from shasm import SHAsm
    LOG = 0x88710000; SRCB = 0x88680000; NEWB = 0x88700000; CNT = 0x88720000
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch()
    def stub(addr, asmfn):
        s = SHAsm(addr); asmfn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    stub(OS_PUSH_PAGE, lambda s: (s.movl_pool(LOG, 1), s.movl_st(4, 1), s.movl_st_disp(5, 4, 1), s.movl_st_disp(6, 8, 1), s.rts(), s.nop()))
    stub(ENABLE_DOIT, lambda s: (s.movl_pool(LOG+0x10, 1), s.movl_st(5, 1), s.rts(), s.nop()))
    stub(SET_FOCUS, lambda s: (s.movl_pool(LOG+0x20, 1), s.movl_st(4, 1), s.movl_st_disp(5, 4, 1), s.rts(), s.nop()))
    def xstub(s):
        s.movl_ld_disp(4, 8, 1); s.movl_pool(CNT, 2); s.movl_ld(2, 3); s.add_imm(1, 3); s.movl_st(3, 2)
        s.mov_rm_rn(3, 6); s.shll8(6); s.movl_pool(NEWB, 2); s.add_rr(2, 6); s.movl_pool(0x100, 0); s.movl_st(0, 6)
        s.movl_pool(0x43494C53, 0); s.movl_st_disp(0, 4, 6); s.mov_rm_rn(3, 0); s.add_imm(0x30, 0); s.shll16(0); s.movl_pool(0x005F45, 2); s.or_rr(2, 0); s.movl_st_disp(0, 8, 6)
        s.mov_imm(0, 0); s.movl_st_disp(0, 12, 6); s.movl_st_disp(0, 16, 6)
        s.mov_rm_rn(3, 7); s.shll2(7); s.add_rr(7, 7); s.movl_pool(LOG+0x100, 0); s.add_rr(0, 7)
        s.movl_ld_disp(1, 0x20, 0); s.movl_st(0, 7); s.movl_ld_disp(1, 0x24, 0); s.movl_st_disp(0, 4, 7)
        s.movl_st_disp(6, 8, 4); s.mov_imm(0, 0); s.rts(); s.nop()
    stub(EXTRACT_CMD, xstub)
    stub(AUD_PLAY, lambda s: (s.movl_pool(LOG + 0x40, 1), s.movl_ld(1, 0), s.add_imm(1, 0), s.movl_st(0, 1),
                              s.movl_ld_disp(5, 0x20, 0), s.movl_st_disp(0, 4, 1), s.movl_ld_disp(5, 0x24, 0), s.movl_st_disp(0, 8, 1),
                              s.movl_ld_disp(5, 0x2C, 0), s.movl_st_disp(0, 12, 1),
                              s.mov_rm_rn(5, 2), s.add_imm(0x3E, 2), s.movw_ld(2, 0), s.extu_w(0, 0), s.movl_st_disp(0, 16, 1), s.rts(), s.nop()))
    stub(AUD_STOP, lambda s: (s.rts(), s.nop()))
    LEN0 = 176400
    h.ram_w32(SRCB, 0x1000); h.ram_w32(SRCB+0x18, LEN0); h.ram_w32(SRCB+0x20, 0); h.ram_w32(SRCB+0x24, LEN0-1); h.ram_w32(SRCB+0x94, 44100)
    h.ram_w32(MGR+8, SRCB); h.ram_w16(PROG_IDX, 1); h.ram_w32(CNT, 0)
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato dall'HUB (slot 0)"
    h.fire(300); h.lcd_text()
    assert h.ram_r32(BPM10) == 1200 and h.ram_r32(NSL) == 8 and h.ram_r32(PAD1) == 1, "default"
    c = h.ram_r32(pg.page + 0xC); names = []
    while c:
        vt = h.ram_r32(c + 0x1C); names.append('L' if vt == VT_LABEL else 'V' if vt == VT_VALUE else '?'); c = h.ram_r32(c + 8)
    assert names == ['V', 'L', 'V', 'L', 'V', 'L', 'V', 'L'], names
    h.ram_w32(LOG+4, 0); h.ram_w32(KEYS_ADDR, keymask('SHIFT', 'STOP')); h.fire(2); h.ram_w32(KEYS_ADDR, 0); h.fire(2)
    assert h.ram_r32(LOG+4) != pg.page, "SHIFT+STOP non deve piu' aprire Auto Chop (ora e' di PAD TOOLS)"
    MSG = 0x88730000
    def call(obj, type_, code=0, val=0, ident=0, pc=None):
        e = h.emu
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, ident)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pc or pg.handler; e.maxsteps = 5_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    P = pg.page; W1 = pg.values[1]
    assert call(P, 0x23) == 0 and h.ram_r32(LOG+0x10) == 1 and h.ram_r32(LOG+0x24) == pg.first, "enter: DO IT on + focus selettore"
    assert call(W1, 0x10, ident=(GROUP << 16) | 1) == 1 and h.ram_r32(MSG+4) == 1200, "GET bpm"
    assert call(W1, 0x10, ident=(5 << 16) | 8) == 0
    assert call(W1, 0x11, val=1205, ident=(GROUP << 16) | 1) == 1 and h.ram_r32(BPM10) == 1205
    assert call(W1, 0x11, val=6, ident=(GROUP << 16) | 2) == 1 and h.ram_r32(NSL) == 6
    assert call(W1, 0x11, val=13, ident=(GROUP << 16) | 3) == 1 and h.ram_r32(PAD1) == 13
    assert call(W1, 0x12, ident=(GROUP << 16) | 1) == 1 and call(W1, 0x11, val=1200, ident=(GROUP << 16) | 1) == 1
    assert call(W1, 6) == 0 and call(P, 6) == 0 and call(P, 0x24) == 0, "rotella/redraw non gestiti dal nostro codice"
    assert call(P, 2, code=0x1D) == 0
    assert call(P, 2, code=0x2D) == 1 and h.ram_r32(LOG+4) == 0 and h.ram_r32(LOG+8) == 2, "CANCEL -> indietro"
    beat = 44100*600//1200; nb = (LEN0 + beat//2)//beat; slen = nb*beat//6
    # anteprima: pad k = fetta k (start/end/loop solo per il play), ripristino
    h.ram_w32(SRCB + 0x2C, 1); h.ram_w16(SRCB + 0x3E, 4)               # +0x3E != 0 (WAV da pack): il lettore suonerebbe tutto
    for k in (0, 3, 5):
        assert call(P, 0x38, code=k, val=100) == 1
        assert (h.ram_r32(LOG+0x44), h.ram_r32(LOG+0x48), h.ram_r32(LOG+0x4C), h.ram_r32(LOG+0x50)) == (k*slen, min(k*slen + slen - 1, LEN0 - 1), 0, 0), k
    assert (h.ram_r32(SRCB+0x20), h.ram_r32(SRCB+0x24), h.ram_r32(SRCB+0x2C), h.emu.rd(SRCB+0x3E, 2)) == (0, LEN0 - 1, 1, 4), "sample non ripristinato"
    h.ram_w16(SRCB + 0x3E, 0)
    assert call(P, 0x38, code=9, val=100) == 1 and (h.ram_r32(LOG+0x44), h.ram_r32(LOG+0x48)) == (0, LEN0 - 1), "pad oltre N = sample intero"
    print("  ok anteprima: pad k = fetta k, sample ripristinato")
    # DO IT: domanda; CANCEL annulla; DO IT + pad colpito (banco B pad 2) = primo pad 14
    assert call(P, 2, code=0x2E) == 1 and bytes(h.emu.rd(pg.msgt + i, 1) for i in range(13)) == b'Hit first pad' and h.ram_r32(CNT) == 0
    assert call(P, 2, code=0x2D) == 1 and h.ram_r32(CONF) == 0 and h.ram_r32(LOG+8) == 2
    assert call(P, 2, code=0x2E) == 1 and h.ram_r32(CONF) == 1
    h.emu.wr(BANKOBJ, 1, 1); np0 = h.ram_r32(LOG+0x40)
    assert call(P, 0x38, code=1, val=90) == 1 and h.ram_r32(LOG+0x40) == np0 and h.ram_r32(PAD1) == 14
    assert h.ram_r32(ERR) == 0 and h.ram_r32(IDX) == 6
    for k in range(6):
        st_, en_ = h.ram_r32(LOG+0x100 + (k+1)*8), h.ram_r32(LOG+0x100 + (k+1)*8 + 4)
        assert st_ == k*slen and en_ == min(k*slen + slen - 1, LEN0 - 1), (k, st_, en_)
    prog = PROG_BASE + 1*PROG_STRIDE
    for k in range(6):
        slot = prog + 0x18 + (13+k)*0xA4
        assert bytes(h.emu.rd(slot+i, 1) for i in range(7)) == b'SLICE_%d' % (k+1) and h.emu.rd(slot+0x11, 1) == 100
        assert h.emu.rd(slot+0x62, 1) == CHOP_MONO and h.emu.rd(slot+0x63, 1) == CHOP_MUTE
    assert h.ram_r32(MGR+8) == SRCB and h.ram_r32(SRCB+0x20) == 0
    txt = bytes(h.emu.rd(pg.msgt + i, 1) for i in range(16)); assert txt == b'Done: 06 slices ', txt
    assert h.emu.rd(pg.msgw + 0x17, 1) & 0x80 and not (h.emu.rd(pg.first + 0x17, 1) & 0x80)
    h.fire(pg.msg_fires + 2); assert (h.emu.rd(pg.first + 0x17, 1) & 0x80) and not (h.emu.rd(pg.msgw + 0x17, 1) & 0x80)
    print("  ok DO IT: 'Hit first pad', CANCEL, pad colpito banco B pad 2 -> fette dal pad 14, Mono + mute group %d" % CHOP_MUTE)
    print("VALIDATO Auto Chop v26 (solo da OTHER): pagina, GET/SET, anteprima, Hit first pad, 6 fette Mono/mute, CANCEL, messaggio")


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'CHOPAGE.WAV'); pg.build_wav(out); selftest(out)
