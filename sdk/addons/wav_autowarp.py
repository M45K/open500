#!/usr/bin/env python3
# AUTO WARP (pagina nativa) -> AUTOWARP.WAV
#   riga 0: selettore sample (rotella)      riga 1: "B 2 T120.0 RTHM"
#     B = battute del loop (1..32; proposte al cambio sample: 1/2/4/8/16, quella col BPM piu' vicino a T)
#     T = tempo di destinazione (BPM x10; all'ingresso = tempo della sequenza corrente, parametro OS 0x2000C)
#     preset dello stretch dell'OS: RTHM (MF RTHM A) / PERC (PERC A) / MUSC (MUSIC 1A) / VOX (VOCAL A)
#   ANTEPRIMA (v2): a ogni cambio di sample/B/T/preset (dopo DEBOUNCE fire) lo stretch dell'OS crea un sample
#   temporaneo: BPM originale = B*4 battiti nella regione start..end; Orig/New come la pagina Time Stretch (SET dei
#   parametri di sistema 0x50008/0x50009: l'OS ricalcola il rapporto float in MGR+0x14), preset in MGR+0x18, comando
#   Stretch dell'OS (TRIM Edit "Stretch", FUN_a00215e8). TUTTI I PAD suonano l'anteprima (rilascio = stop).
#   DO IT = "Save? DO IT=yes" (resta); 2o DO IT = l'anteprima diventa il sample nuovo (corrente); CANCEL annulla la
#   domanda; uscita dalla pagina (altra pagina attiva per LEAVE_FIRES fire) = anteprima cancellata. Originale intatto.
# RE 2026-09-25: MGR+0x0C orig x10, +0x10 new x10, +0x14 float nuova/vecchia lunghezza, +0x18 preset (0..53), +0x19 adjust;
# gestore parametri di sistema FUN_a0041376(msg) (msg {u16 tipo 0x10 GET / 0x11 SET, +4 valore, +12 id}).
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import page_or_addon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ

MGR, STRETCH_CMD, DELETE_CMD, SYS_PARAM, LOOP_GUARD = 0x8848ED30, 0x884C5F44, 0x884C5FFC, 0x884E5CD0, 0x884C4F04   # SYS_PARAM: ingresso vero 0xA0041374 (Ghidra FUN_a0041376 salta il push di r14!)
ID_TEMPO, ID_ORIG, ID_NEW = 0x2000C, 0x50008, 0x50009
PRESETS = [("RTHM", 15), ("PERC", 21), ("MUSC", 33), ("VOX", 9)]     # nome campo, indice preset OS
BAR_GUESS = (1, 2, 4, 8, 16)
MAGIC_ADDON, MAGIC_HUB = 0x4143504D, 0x4843504D
LEAVE_FIRES = 16
DEBOUNCE = 48
PLAYER, AUD_PLAY, AUD_STOP = 0x8848CD78, 0x884C2636, 0x884C29D2
EV_PAD = 0x38

pg = page_or_addon("awarp2", open_press=None)     # in Chop Shop = modo Warp
pg.other_menu("Auto Warp")
pg.msg_fires = 0xC00
pg.keep_focus = True
pg.far_hend = True
pg.selector()
BARS = pg.value("B", 0, 1, fmt=" 0", lo=1, hi=32, default=2)
TGT = pg.value(" T", 3, 1, fmt="  0.0", lo=300, hi=3000, default=1200)
PRE = pg.choice(" ", 10, 1, [p for p, _ in PRESETS], default=0)
pg.message()
ACTIVE, OUTCNT, LSRC, ORG, ERR = [pg.alloc_slot() for _ in range(5)]
PREV, SRC, PEND, LBARS, LTGT, LPRE, CONF = [pg.alloc_slot() for _ in range(7)]
from mpcmod import LOAD_SCRATCH
SHOWMSG = pg.SHOWMSG                # timer del messaggio della pagina (in Chop Shop ogni pagina ha il suo)
MSGB = [pg.alloc_slot() for _ in range(4)]          # messaggio per il gestore parametri di sistema (16 byte contigui)

def sysmsg(m, typ, ident, val_reg=None):
    """asm: messaggio {typ, valore, 0, id} al gestore parametri di sistema dell'OS; GET -> valore in MSGB+4"""
    a = m.a
    if val_reg is not None: st(m, val_reg, MSGB[1])      # prima il valore: sti usa r0 come appoggio
    else: sti(m, 0, MSGB[1])
    sti(m, typ, MSGB[0]); sti(m, ident, MSGB[3]); sti(m, 0, MSGB[2])
    m.imm32(MSGB[0], 4); m.imm32(SYS_PARAM, 2); a.jsr(2); a.nop()

def far_call(m, label):
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def fire(m):
    a = m.a
    sti(m, 0, OUTCNT)
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); act = m.newlabel('act'); m.bf_far(act)
    # ingresso: T = tempo della sequenza corrente (se plausibile), forza stima battute e anteprima
    sti(m, 1, ACTIVE); sti(m, 0, LSRC); sti(m, 0, PEND); sti(m, 0, CONF)
    sysmsg(m, 0x10, ID_TEMPO)
    ld(m, MSGB[1], 0); m.imm32(300, 3); a.cmpge(3, 0); tl = m.newlabel('tl'); m.bf_far(tl)
    m.imm32(3000, 3); a.cmpgt(3, 0); m.bt_far(tl); st(m, 0, TGT)
    a.deflabel(tl)
    a.deflabel(act)
    # sample corrente (l'anteprima conta come l'originale)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8)
    ld(m, PREV, 2); a.cmpeq(2, 8); c1 = m.newlabel('c1'); a.bf_lbl(c1); ld(m, LSRC, 8); a.deflabel(c1)
    ch = m.newlabel('ch'); same = m.newlabel('same')
    ld(m, LSRC, 2); a.cmpeq(2, 8); ns = m.newlabel('ns'); m.bt_far(ns)
    st(m, 8, LSRC); far_call(m, 'guess')                                          # nuovo sample: stima battute
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.bra_lbl(ch); a.nop()
    a.deflabel(ns)
    ld(m, BARS, 0); ld(m, LBARS, 2); a.cmpeq(2, 0); a.bf_lbl(ch)
    ld(m, TGT, 0); ld(m, LTGT, 2); a.cmpeq(2, 0); a.bf_lbl(ch)
    m.imm32(PRE, 1); a.movb_ld(1, 0); a.extu_b(0, 0); ld(m, LPRE, 2); a.cmpeq(2, 0); m.bt_far(same)
    a.deflabel(ch)
    ld(m, BARS, 0); st(m, 0, LBARS); ld(m, TGT, 0); st(m, 0, LTGT); m.imm32(PRE, 1); a.movb_ld(1, 0); a.extu_b(0, 0); st(m, 0, LPRE)
    sti(m, DEBOUNCE, PEND)
    ld(m, CONF, 0); a.tst_rr(0, 0); cfz = m.newlabel('cfz'); a.bt_lbl(cfz); sti(m, 0, CONF); sti(m, 1, SHOWMSG); a.deflabel(cfz)
    a.deflabel(same)
    ld(m, PEND, 0); a.tst_rr(0, 0); fd = m.newlabel('fd'); m.bt_far(fd)
    a.add_imm(-1, 0); st(m, 0, PEND); a.tst_rr(0, 0); m.bf_far(fd)
    far_call(m, 'rebuild')
    a.deflabel(fd)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()   # l'OS spegne DO IT dopo ogni uso
pg.on_fire(fire)

def ev_pad(m, hret0, hret1):
    """tutti i pad = ascolto dell'anteprima (o del sample corrente), come in TRIM"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on)
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop(); far_bra(m, hret1)
    a.deflabel(on)
    ld(m, PREV, 5); a.tst_rr(5, 5); hv = m.newlabel('hv'); a.bf_lbl(hv); m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); a.deflabel(hv)
    a.tst_rr(5, 5); hz = m.newlabel('hz'); a.bf_lbl(hz); far_bra(m, hret1); a.deflabel(hz)
    m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
    far_bra(m, hret1)
pg.on_event(EV_PAD, ev_pad)

def on_cancel(m, hret1):
    a = m.a
    ld(m, CONF, 0); a.tst_rr(0, 0); cx = m.newlabel('cx'); a.bt_lbl(cx)
    sti(m, 0, CONF); sti(m, 1, SHOWMSG); far_bra(m, hret1)
    a.deflabel(cx)
pg.cancel_fn = on_cancel

def inactive(m):
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    far_call(m, 'delprev'); sti(m, 0, ACTIVE); sti(m, 0, OUTCNT); sti(m, 0, CONF)
    a.deflabel(ix)
pg.on_inactive(inactive)

def doit(m):
    a = m.a
    MT = pg.addr['msgt']
    def is_audio(r_blk, bad):                        # blocco valido e non un add-on, altrimenti bra bad
        a.tst_rr(r_blk, r_blk); m.bt_far(bad)
        m.imm32(0x100, 2); a.add_rr(r_blk, 2); a.movl_ld(2, 0)
        m.imm32(MAGIC_ADDON, 3); a.cmpeq(3, 0); m.bt_far(bad)
        m.imm32(MAGIC_HUB, 3); a.cmpeq(3, 0); m.bt_far(bad)

    ld(m, PEND, 0); a.tst_rr(0, 0); np_ = m.newlabel('np'); a.bt_lbl(np_); sti(m, 0, PEND); a.bsr_lbl('rebuild'); a.nop(); a.deflabel(np_)
    ld(m, PREV, 0); a.tst_rr(0, 0); hp = m.newlabel('hp'); m.bf_far(hp)
    ld(m, ERR, 8); a.tst_rr(8, 8); m.bf_far('__iserr'); sti(m, 0x21, ERR); a.bra_lbl('__iserr'); a.nop()
    a.deflabel(hp)
    ld(m, CONF, 0); a.tst_rr(0, 0); cyes = m.newlabel('cyes'); m.bf_far(cyes)
    sti(m, 1, CONF); pg.emit_message(m, "Save? DO IT=yes"); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)
    a.bra_lbl('__wx'); a.nop()
    a.deflabel(cyes); sti(m, 0, CONF)
    ld(m, PREV, 8); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1); st(m, 8, LSRC); st(m, 8, SRC); sti(m, 0, PREV)   # tenuta
    # "Warp  93.5>120.0"
    m.imm32(MT, 12)
    for ch in "Warp ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    ld(m, ORG, 4); a.bsr_lbl('bpm5'); a.nop()
    a.mov_imm(ord('>'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    ld(m, TGT, 4); a.bsr_lbl('bpm5'); a.nop()
    a.mov_imm(0, 0); a.movb_st(0, 12)
    pg._hcall(m, '__showmsg'); a.bra_lbl('__wx'); a.nop()
    a.deflabel('__iserr'); ld(m, ERR, 8)
    a.mov_imm(0x21, 3); a.cmpeq(3, 8); e1 = m.newlabel('e1'); a.bf_lbl(e1); pg.emit_message(m, "No sample"); a.bra_lbl('__wx'); a.nop()
    a.deflabel(e1); a.mov_imm(3, 3); a.cmpeq(3, 8); e2 = m.newlabel('e2'); a.bf_lbl(e2); pg.emit_message(m, "Memory full"); a.bra_lbl('__wx'); a.nop()
    a.deflabel(e2); a.mov_imm(0x40, 3); a.cmpeq(3, 8); e3 = m.newlabel('e3'); a.bf_lbl(e3); pg.emit_message(m, "Bad loop length"); a.bra_lbl('__wx'); a.nop()
    a.deflabel(e3); pg.emit_message_dec(m, "Error ", ERR, "")
    a.deflabel('__wx'); a.bra_lbl('awret'); a.nop()
    m.pool_jump()

    # ---- orgbpm: r8 = blocco -> r0 = BARS*4 battiti su end-start campioni, in BPM x10 (0 se regione vuota) ----
    a.deflabel('orgbpm'); a.stsl_pr()
    a.movl_ld_disp(8, 0x24, 5); a.movl_ld_disp(8, 0x20, 2); a.sub(2, 5)            # r5 = n
    a.mov_imm(0, 0); a.cmppl(5); ob = m.newlabel('ob'); m.bf_far(ob)
    m.imm32(0x94, 2); a.add_rr(8, 2); a.movl_ld(2, 4)                               # r4 = rate
    m.imm32(2400, 2); a.mul_l(2, 4); a.sts_macl(4); a.mov_rm_rn(9, 2); a.mul_l(2, 4); a.sts_macl(4)   # r9 = battute
    a.mov_rm_rn(5, 2); a.shlr(2); a.add_rr(2, 4)                                     # arrotondamento (+n/2)
    pg._hcall(m, '__udiv')
    a.deflabel(ob); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- guess: r8 = blocco -> BARS = 1/2/4/8/16 col BPM piu' vicino a T ----
    a.deflabel('guess'); a.stsl_pr()
    bad = m.newlabel('gbad'); is_audio(8, bad)
    m.imm32(0x7FFFFFFF, 13)                                                          # miglior distanza
    for b in BAR_GUESS:
        a.mov_imm(b, 9); a.bsr_lbl('orgbpm'); a.nop()
        a.tst_rr(0, 0); nx = m.newlabel('gn'); m.bt_far(nx)
        ld(m, TGT, 2); a.sub(2, 0); a.cmppz(0); p = m.newlabel('gp'); a.bt_lbl(p); a.neg(0, 0); a.deflabel(p)
        a.cmpgt(0, 13); m.bf_far(nx); a.mov_rm_rn(0, 13); a.mov_imm(b, 0); st(m, 0, BARS)
        a.deflabel(nx); m.pool_jump()
    a.deflabel(bad); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- delprev: ferma l'ascolto, cancella l'anteprima (ultimo blocco della lista), corrente = SRC ----
    a.deflabel('delprev'); a.stsl_pr()
    ld(m, PREV, 9); a.tst_rr(9, 9); dpx = m.newlabel('dpx'); m.bt_far(dpx)
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop()
    ld(m, PREV, 9); m.imm32(MGR, 4); a.movl_st_disp(9, 8, 4); m.imm32(DELETE_CMD, 2); a.jsr(2); a.nop()
    sti(m, 0, PREV)
    ld(m, SRC, 8); a.tst_rr(8, 8); m.bt_far(dpx); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1)
    a.deflabel(dpx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- rebuild: anteprima = stretch di LSRC da ORG a TGT (sample temporaneo PREV; corrente resta LSRC) ----
    a.deflabel('rebuild'); a.stsl_pr()
    sti(m, 0, ERR)
    ld(m, LSRC, 8); st(m, 8, SRC)                                                  # prima la nuova sorgente:
    a.bsr_lbl('delprev'); a.nop()                                                   # delprev rimette corrente = SRC
    ld(m, SRC, 8); nos = m.newlabel('nos'); is_audio(8, nos)
    m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1)                                        # sorgente dello stretch
    ld(m, BARS, 9); a.bsr_lbl('orgbpm'); a.nop(); st(m, 0, ORG)
    m.imm32(100, 3); a.cmpge(3, 0); oor = m.newlabel('oor'); m.bf_far(oor)             # BPM x10 plausibile: 10..999.9
    m.imm32(9999, 3); a.cmpgt(3, 0); m.bt_far(oor)
    ld(m, ORG, 0); sysmsg(m, 0x11, ID_ORIG, 0)
    ld(m, TGT, 0); sysmsg(m, 0x11, ID_NEW, 0)
    # preset: indice OS dalla scelta
    m.imm32(PRE, 1); a.movb_ld(1, 0); a.extu_b(0, 0)
    pe = m.newlabel('pe')
    for k, (_, idx) in enumerate(PRESETS):
        nx = m.newlabel('pn'); a.mov_imm(k, 3); a.cmpeq(3, 0); a.bf_lbl(nx); a.mov_imm(idx, 2); a.bra_lbl(pe); a.nop(); a.deflabel(nx)
    a.mov_imm(PRESETS[0][1], 2)
    a.deflabel(pe); m.imm32(MGR + 0x18, 1); a.movb_st(2, 1)
    m.imm32(MGR, 4); m.imm32(STRETCH_CMD, 2); a.jsr(2); a.nop()
    st(m, 0, ERR); a.tst_rr(0, 0); sok = m.newlabel('sok'); a.bf_lbl(sok)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 9); st(m, 9, PREV)                          # nuovo blocco = anteprima
    a.deflabel(sok)
    ld(m, SRC, 8); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1)                          # corrente = originale
    a.bra_lbl('__wr'); a.nop()
    a.deflabel(nos); sti(m, 0x21, ERR); a.bra_lbl('__wr'); a.nop()
    a.deflabel(oor); sti(m, 0x40, ERR)
    a.deflabel('__wr'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- bpm5: r4 = BPM x10 -> 5 caratteri "123.4" / " 93.5" a r12 (r12 avanza) ----
    a.deflabel('bpm5'); a.stsl_pr()
    a.mov_imm(10, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 11)                       # r11 = decimi
    a.mov_rm_rn(0, 4); a.mov_imm(100, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 10)   # r0 = centinaia, r10 = resto
    a.tst_rr(0, 0); h1 = m.newlabel('h1'); a.bf_lbl(h1); a.mov_imm(ord(' ') - 0x30, 0); a.deflabel(h1)
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(10, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(0x30, 1); a.movb_st(1, 12); a.add_imm(1, 12)
    a.mov_imm(ord('.'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.add_imm(0x30, 11); a.movb_st(11, 12); a.add_imm(1, 12)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('awret')
pg.on_doit(doit)


def ref_org(bars, rate, n): return (bars * 2400 * rate + n // 2) // n if n > 0 else 0
def ref_guess(rate, n, tgt):
    best, bb = None, None
    for b in BAR_GUESS:
        v = ref_org(b, rate, n)
        if v and (best is None or abs(v - tgt) < best): best, bb = abs(v - tgt), b
    return bb

def selftest(out):
    """test in emulatore sul WAV 'out' (AUTOWARP.WAV, o CHOPSHOP.WAV con questa pagina ospitata)"""
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE
    from pages import SET_FOCUS
    from shasm import SHAsm
    LOG, MSG = 0x88E00000, 0x88E10000       # LOG: +0 orig, +4 new, +8 preset, +0xC n stretch, +0x10 n delete, +0x14 n play, +0x18 blk play, +0x20 tempo seq
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    def cnt(s, off): s.movl_pool(LOG, 1); s.movl_ld_disp(1, off, 0); s.add_imm(1, 0); s.movl_st_disp(0, off, 1)
    stub(OS_PUSH_PAGE, lambda s: (s.rts(), s.nop())); stub(ENABLE_DOIT, lambda s: (s.rts(), s.nop())); stub(SET_FOCUS, lambda s: (s.rts(), s.nop()))
    def sysstub(s):                          # come l'OS: GET 0x2000C = tempo; SET 0x50008/9 -> MGR+0xC/+0x10
        s.movw_ld(4, 0); s.extu_w(0, 0); s.movl_ld_disp(4, 12, 1)
        s.mov_imm(0x10, 2); s.cmpeq(2, 0); s.bf_lbl('set')
        s.movl_pool(LOG + 0x20, 2); s.movl_ld(2, 2); s.movl_st_disp(2, 4, 4); s.bra_lbl('x'); s.nop()
        s.deflabel('set'); s.movl_ld_disp(4, 4, 2)
        s.movl_pool(ID_ORIG, 3); s.cmpeq(3, 1); s.bf_lbl('n2'); s.movl_pool(MGR, 3); s.movl_st_disp(2, 0xC, 3); s.bra_lbl('x'); s.nop()
        s.deflabel('n2'); s.movl_pool(ID_NEW, 3); s.cmpeq(3, 1); s.bf_lbl('x'); s.movl_pool(MGR, 3); s.movl_st_disp(2, 0x10, 3)
        s.deflabel('x'); s.mov_imm(1, 0); s.rts(); s.nop()
    stub(SYS_PARAM, sysstub)
    LIST_END = 0x884C4FE0
    def stretch(s):                          # copia il corrente in fondo alla lista (come un nuovo sample), registra i parametri
        s.stsl_pr(); s.movl_st_dec(8, 15); s.movl_st_dec(9, 15); s.mov_rm_rn(4, 9)
        s.movl_pool(LOG, 1); s.movl_ld_disp(4, 0xC, 0); s.movl_st(0, 1); s.movl_ld_disp(4, 0x10, 0); s.movl_st_disp(0, 4, 1)
        s.mov_rm_rn(4, 2); s.add_imm(0x18, 2); s.movb_ld(2, 0); s.extu_b(0, 0); s.movl_st_disp(0, 8, 1)
        cnt(s, 0xC)
        s.movl_pool(LIST_END, 2); s.jsr(2); s.nop(); s.mov_rm_rn(9, 4)
        s.movl_ld_disp(4, 8, 1); s.mov_rm_rn(0, 8); s.movl_ld(1, 3); s.shlr2(3); s.mov_rm_rn(8, 2)
        s.deflabel('cp'); s.movl_ld_inc(1, 0); s.movl_st(0, 2); s.add_imm(4, 2); s.dt(3); s.bf_lbl('cp')
        s.mov_imm(0, 0); s.movl_st(0, 2); s.movl_st_disp(8, 8, 4)
        s.movl_ld_inc(15, 9); s.movl_ld_inc(15, 8); s.ldsl_pr(); s.mov_imm(0, 0); s.rts(); s.nop()
    stub(STRETCH_CMD, stretch)
    stub(DELETE_CMD, lambda s: (cnt(s, 0x10), s.movl_ld_disp(4, 8, 1), s.mov_imm(0, 0), s.movl_st(0, 1), s.movl_ld(4, 0), s.movl_st_disp(0, 8, 4), s.rts(), s.nop()))
    stub(AUD_PLAY, lambda s: (cnt(s, 0x14), s.movl_st_disp(5, 0x18, 1), s.rts(), s.nop()))
    stub(AUD_STOP, lambda s: (s.rts(), s.nop()))
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato"
    def call(obj, type_, code=0, val=0, ident=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, ident)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 20_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    def mk(n, rate=44100, s=0, en=None):
        blk = h.list_end(); size = (0x100 + 2 * n + 0x200 + 3) & ~3
        h.ram_w32(blk, size); [h.ram_w8(blk + 4 + i, c) for i, c in enumerate(b'LOOP'.ljust(16, b'\0'))]
        h.ram_w32(blk + 0x18, n); h.ram_w32(blk + 0x20, s); h.ram_w32(blk + 0x24, n if en is None else en)
        h.ram_w32(blk + 0x94, rate); h.ram_w16(blk + 0x92, 1); h.ram_w32(blk + size, 0); return blk
    msg = lambda: bytes(e.rd(pg.msgt + i, 1) for i in range(16))
    log = lambda off: h.ram_r32(LOG + off)
    cur = lambda: h.ram_r32(MGR + 8)
    for k in range(0, 0x24, 4): h.ram_w32(LOG + k, 0)
    h.ram_w32(LOG + 0x20, 1000)                                  # tempo della sequenza: 100.0
    n = int(round(8 * 60 / 93.5 * 44100)); blk = mk(n); h.ram_w32(MGR + 8, blk)
    h.ram_w32(EVQ + 0x68, pg.page); h.fire(3)
    assert h.ram_r32(TGT) == 1000 and h.ram_r32(BARS) == 2, (h.ram_r32(TGT), h.ram_r32(BARS))
    assert log(0xC) == 0, "anteprima prima del debounce"
    h.fire(DEBOUNCE + 2); P = h.ram_r32(PREV)
    assert log(0xC) == 1 and P and P > blk and cur() == blk, (log(0xC), hex(P), hex(cur()))
    assert (log(0), log(4), log(8)) == (ref_org(2, 44100, n), 1000, 15)
    print("  ok ingresso: T = tempo sequenza, 2 battute, anteprima (stretch %d->1000, RTHM) dopo il debounce; corrente = originale" % ref_org(2, 44100, n))
    assert call(pg.page, EV_PAD, code=2, val=90) == 1 and log(0x14) == 1 and log(0x18) == P
    print("  ok pad -> play(anteprima)")
    # cambio preset -> nuova anteprima (la vecchia cancellata)
    e.wr(PRE, 1, 1); h.fire(DEBOUNCE + 2)
    assert log(0xC) == 2 and log(0x10) == 1 and log(8) == 21 and h.ram_r32(PREV) == P and cur() == blk
    print("  ok cambio preset -> anteprima rifatta (PERC), la vecchia cancellata")
    # DO IT: domanda, CANCEL annulla, DO IT DO IT salva
    assert call(pg.page, 2, code=0x2E) == 1 and msg() == b'Save? DO IT=yes ' and h.ram_r32(PREV) == P
    h.fire(0x2000); assert h.ram_r32(CONF) == 1, "la domanda deve restare"
    assert call(pg.page, 2, code=0x2D) == 1 and h.ram_r32(CONF) == 0 and h.ram_r32(PREV) == P
    assert call(pg.page, 2, code=0x2E) == 1 and call(pg.page, 2, code=0x2E) == 1
    assert cur() == P and h.ram_r32(PREV) == 0 and msg() == (b'Warp %5.1f>100.0' % (ref_org(2, 44100, n) / 10)), (hex(cur()), msg())
    h.fire(DEBOUNCE + 4); assert log(0xC) == 2, "dopo il salvataggio nessuna anteprima finche' non cambi"
    print("  ok DO IT: domanda persistente, CANCEL annulla, 2o DO IT salva: %r" % msg().decode())
    # nuovo cambio -> anteprima; uscita dalla pagina -> cancellata
    h.ram_w32(TGT, 1200); h.fire(DEBOUNCE + 2); P2 = h.ram_r32(PREV); assert P2 and log(0xC) == 3
    nd = log(0x10); h.ram_w32(EVQ + 0x68, 0x8849A4EC); h.fire(LEAVE_FIRES + 2)
    assert log(0x10) == nd + 1 and h.ram_r32(PREV) == 0 and cur() == P
    print("  ok uscita dalla pagina: anteprima cancellata, corrente = sample salvato")
    # stima battute su altri loop (pagina attiva)
    h.ram_w32(EVQ + 0x68, pg.page); h.fire(3)
    for bpm, bars, tgt in ((170.0, 4, 1700), (85.0, 1, 900), (128.0, 8, 1280), (60.0, 2, 1200)):
        n2 = int(round(bars * 4 * 60 / bpm * 44100)); b2 = mk(n2); h.ram_w32(TGT, tgt); h.ram_w32(MGR + 8, b2); h.fire(2)
        assert h.ram_r32(BARS) == ref_guess(44100, n2, tgt), (bpm, bars, h.ram_r32(BARS))
    print("  ok stima battute coerente col modello (170/85/128/60 BPM)")
    h.fire(DEBOUNCE + 2)
    # errori
    h.ram_w32(MGR + 8, 0); h.fire(DEBOUNCE + 2); assert call(pg.page, 2, code=0x2E) == 1 and msg().startswith(b'No sample'), msg()
    b3 = mk(1000, en=0); h.ram_w32(MGR + 8, b3); h.fire(DEBOUNCE + 2)
    assert call(pg.page, 2, code=0x2E) == 1 and msg().startswith(b'Bad loop length'), msg()
    print("  ok errori: 'No sample', regione non valida")
    print("VALIDATO: Auto Warp v2 (anteprima sui pad, conferma) -> %s" % os.path.basename(out))


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'AUTOWARP.WAV'); pg.build_wav(out); selftest(out)
