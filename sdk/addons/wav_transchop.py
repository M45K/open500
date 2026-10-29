#!/usr/bin/env python3
# TRANS-CHOP (pagina nativa) -> TRANSCHP.WAV: taglia il sample sui TRANSIENTI e mette le fette sui pad
#   riga 0: selettore sample (rotella)      riga 1: "SENS50 P 1 N 12"
#     SENS = sensibilita' 1..99 (piu' alta = piu' fette)   P = primo pad (1..48)   N = fette trovate (solo lettura)
#   Rilevamento (canale L, regione start..end): inviluppo = picco |x| per blocco di 2^HOPSH campioni (256, di piu' su
#   sample lunghissimi); transiente al blocco k se E[k] - min(E[k-1], E[k-2]) > max(E)*(100-SENS)/200, almeno 60 ms dal
#   precedente; punto = primo campione con |x| >= E[k]/4 nei blocchi k-1..k, meno 5 ms ("un filo prima"). La prima fetta
#   parte sempre da start. Ricalcolo dopo DEBOUNCE fire dal cambio di sample/SENS (i cicli girano dalla cache).
#   ANTEPRIMA: il pad k (1..12) suona la fetta k: start/end del sample spostati sulla fetta per il solo play (la voce si
#   copia i puntatori), loop spento durante il play, tutto ripristinato; rilascio = stop.
#   DO IT = "Hit first pad" (resta): colpendo un pad (banco corrente compreso: pad = banco*12 + pad fisico, come l'OS)
#   le fette (Extract, sample nuovi) vanno sui pad del program corrente da quel pad in poi; oppure 2o DO IT = da P.
#   CANCEL annulla. L'originale resta.
#   SCHERMO REMOTO (v4, 2026-10-02): a ogni analisi i tagli (POS) vanno nella "cassetta" B+MBOX_OFF (tipo 1, sample,
#   n, attivo); uscendo dalla pagina attivo = 0. Se c'e' MIDI Screen li manda al browser, altrimenti non costa nulla.
#   v5: anche l'ASCOLTO (pad premuto: fetta, start, end; rilascio = spento) in MBOX_PLAY, per evidenziare la fetta e
#   far scorrere il cursore nel browser.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import page_or_addon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ, LOAD_SCRATCH, SCRATCH_MAGIC, MBOX_OFF, MBOX_MAGIC, MBOX_MAXN, MBOX_PLAY, SLOT_CODE_MAX
SIG2 = 0x8B2A3D01                   # seconda firma della scratch del loader (loader.py)

MGR, EXTRACT_CMD = 0x8848ED30, 0x884C5E90
PLAYER, AUD_PLAY, AUD_STOP = 0x8848CD78, 0x884C2636, 0x884C29D2
PROG_BASE, PROG_IDX, PROG_STRIDE, MAXPAD = 0x8837B5AC, 0x8847C0E2, 0x1F98, 48
EV_PAD, DEBOUNCE, LEAVE_FIRES = 0x38, 48, 16
MAGIC_ADDON, MAGIC_HUB = 0x4143504D, 0x4843504D
CHOP_MUTE, CHOP_MONO = 32, 1        # fette: mute group 32 (il 1 si usa di solito per gli hat), voice overlap Mono
BANKOBJ = 0x8849C98C                # oggetto pad dell'OS: byte 0 = banco corrente (0 = A); FUN_a0040dde
MAXSL = 48                          # fette massime (pad di un program)
import pages
HOSTED = pages.HOST is not None     # pagina di Chop Shop: il codice delle 4 pagine arriva a ~0x4B00 -> buffer piu' corto
MAXBLK = 4096 if HOSTED else 5632   # blocchi di inviluppo nel buffer di lavoro (Chop Shop: hop 512 gia' da 24 s invece di 33 s)
WORK_OFF = 0x20C0 if HOSTED else 0x3000   # buffer di lavoro = LOAD_SCRATCH - WORK_OFF (da solo slot+0x4000): ENV u16, POS u32
POS_OFF = 2 * MAXBLK                # POS[48] subito dopo ENV (Chop Shop: finisce esattamente alla scratch)
CACHE_SPAN = 0x2000
MIN_N = 1024                        # regione minima (campioni)
ERR_SHORT, ERR_NOSMP, ERR_NOTR = 0x41, 0x21, 0x42

pg = page_or_addon("tchop3", open_press=None)     # in Chop Shop = modo Transient
SHOWMSG = pg.SHOWMSG                # timer del messaggio della pagina (in Chop Shop ogni pagina ha il suo)
pg.reserve = SLOT_CODE_MAX                # HUB v2: buffer di lavoro sotto la scratch (WORK_OFF) -> tutte le 7 pagine
pg.other_menu("Trans-Chop")
pg.msg_fires = 0xC00
pg.keep_focus = True
pg.far_hend = True
pg.selector()
SENS = pg.value("SENS", 0, 1, fmt=" 0", lo=1, hi=99, default=50)
PADP = pg.value(" P", 6, 1, fmt=" 0", lo=1, hi=MAXPAD, default=1)
pg.label(" N", 10, 1)
NLBL = pg.dlabel(12, 1, 3)
pg.message()
ACTIVE, OUTCNT, LSRC, LSENS, PEND, CONF, ERR = [pg.alloc_slot() for _ in range(7)]
SRC, S0, E0, NN, HOPSH, NB, MAXE, NSL, ENVSRC = [pg.alloc_slot() for _ in range(9)]
DTH, MINGAP, PRE, LASTK, KK, IDX, PROG, CNT, SV20, SV24, SV2C, SV3E = [pg.alloc_slot() for _ in range(12)]


def far_call(m, label):
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def cached_call(m, label):
    """chiama label tramite l'alias P1 (cacheato): il codice addon da P2 gira a ~1M istr/s sull'HW"""
    m.a.movl_pool(label, 2); m.imm32(0xDFFFFFFF, 3); m.a.and_rr(3, 2); m.a.jsr(2); m.a.nop()

def work(m, r, off=0):
    """r = indirizzo del buffer di lavoro (+off): LOAD_SCRATCH rilocata - WORK_OFF"""
    m.imm32(LOAD_SCRATCH, r); m.imm32(WORK_OFF - off, 3); m.a.sub(3, r)


def fire(m):
    a = m.a
    sti(m, 0, OUTCNT)
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); act = m.newlabel('act'); a.bf_lbl(act)
    sti(m, 1, ACTIVE); sti(m, 0, LSRC); sti(m, 0, ENVSRC); sti(m, 0, CONF); sti(m, 0, NSL)
    a.deflabel(act)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8)
    ch = m.newlabel('ch'); same = m.newlabel('same')
    ld(m, LSRC, 2); a.cmpeq(2, 8); a.bf_lbl(ch)
    ld(m, SENS, 0); ld(m, LSENS, 2); a.cmpeq(2, 0); m.bt_far(same)
    a.deflabel(ch)
    st(m, 8, LSRC); ld(m, SENS, 0); st(m, 0, LSENS); sti(m, DEBOUNCE, PEND)
    ld(m, CONF, 0); a.tst_rr(0, 0); cfz = m.newlabel('cfz'); a.bt_lbl(cfz); sti(m, 0, CONF); sti(m, 1, SHOWMSG); a.deflabel(cfz)
    a.deflabel(same)
    ld(m, PEND, 0); a.tst_rr(0, 0); fd = m.newlabel('fd'); m.bt_far(fd)
    a.add_imm(-1, 0); st(m, 0, PEND); a.tst_rr(0, 0); m.bf_far(fd)
    far_call(m, 'analyze'); a.mov_imm(1, 4); far_call(m, 'tc_pub')                       # tagli -> schermo remoto
    a.deflabel(fd)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
    far_bra(m, 'tc_fx')
    m.pool_jump()
    # ---- tc_pub: r4 = 1 pagina aperta (n = NSL) / 0 chiusa (n = 0) -> cassetta B+MBOX_OFF, seq per ultimo. r0-r7 ----
    a.deflabel('tc_mb'); mbn = m.newlabel('mbn')                                         # r5 = cassetta (0 senza loader). r0-r3
    a.mov_imm(0, 5); m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); a.bt_lbl(mbn)
    a.mov_rm_rn(2, 1); a.add_imm(0x40, 1)                                                # firme del loader (B+0x40, +0x44)
    a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 3); a.cmpeq(3, 0); a.bf_lbl(mbn)
    a.movl_ld_disp(1, 4, 0); m.imm32(SIG2, 3); a.cmpeq(3, 0); a.bf_lbl(mbn)
    m.imm32(MBOX_OFF, 5); a.add_rr(2, 5)
    a.deflabel(mbn); a.rts(); a.nop()
    m.pool_jump()
    # ---- tc_play: r4 = fetta (-1 sample intero), r6 = blocco: ascolto acceso con start/end del blocco; r4 = -2: spento ----
    a.deflabel('tc_play'); a.stsl_pr(); a.bsr_lbl('tc_mb'); a.nop(); a.tst_rr(5, 5); ppx = m.newlabel('ppx'); m.bt_far(ppx)
    m.imm32(MBOX_PLAY, 7); a.add_rr(5, 7)
    a.mov_imm(-2, 0); a.cmpeq(0, 4); pof = m.newlabel('pof'); a.bt_lbl(pof)
    a.movl_st_disp(4, 4, 7); a.movl_ld_disp(6, 0x20, 0); a.movl_st_disp(0, 8, 7); a.movl_ld_disp(6, 0x24, 0); a.movl_st_disp(0, 12, 7)
    a.mov_imm(1, 0); a.movl_st(0, 7); pse = m.newlabel('pse'); a.bra_lbl(pse); a.nop()
    a.deflabel(pof); a.mov_imm(0, 0); a.movl_st(0, 7)
    a.deflabel(pse); a.movl_ld_disp(7, 16, 0); a.add_imm(1, 0); a.movl_st_disp(0, 16, 7)
    a.deflabel(ppx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('tc_pub'); px = m.newlabel('px'); a.stsl_pr()
    a.bsr_lbl('tc_mb'); a.nop(); a.tst_rr(5, 5); m.bt_far(px)
    m.imm32(MBOX_MAGIC, 0); a.movl_st(0, 5); a.mov_imm(1, 0); a.movl_st_disp(0, 8, 5)
    ld(m, LSRC, 0); a.movl_st_disp(0, 0xC, 5); a.movl_st_disp(4, 0x14, 5)
    a.mov_imm(0, 6); pn = m.newlabel('pn'); a.tst_rr(4, 4); a.bt_lbl(pn); ld(m, NSL, 6); a.deflabel(pn)
    a.mov_imm(MBOX_MAXN, 0); pm = m.newlabel('pm'); a.cmphs(0, 6); a.bf_lbl(pm); a.mov_rm_rn(0, 6); a.deflabel(pm)
    a.movl_st_disp(6, 0x10, 5)
    work(m, 7, POS_OFF); a.mov_rm_rn(5, 3); a.add_imm(0x20, 3)
    pc = m.newlabel('pc'); pe = m.newlabel('pe'); a.tst_rr(6, 6); a.bt_lbl(pe)
    a.deflabel(pc); a.movl_ld(7, 0); a.add_imm(4, 7); a.movl_st(0, 3); a.add_imm(4, 3); a.dt(6); a.bf_lbl(pc)
    a.deflabel(pe); a.movl_ld_disp(5, 4, 0); a.add_imm(1, 0); a.movl_st_disp(0, 4, 5)
    a.deflabel(px); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('tc_fx')
pg.on_fire(fire)

def inactive(m):
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    sti(m, 0, ACTIVE); sti(m, 0, OUTCNT); sti(m, 0, CONF)
    a.mov_imm(0, 4); far_call(m, 'tc_pub')                                               # schermo remoto: niente tagli
    a.deflabel(ix)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """pad k (1..12) = ascolto della fetta k (o del sample intero se non ci sono fette)"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on)
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop()
    a.mov_imm(-2, 4); far_call(m, 'tc_play'); far_bra(m, hret1)                          # schermo remoto: ascolto spento
    a.deflabel(on)
    ld(m, CONF, 0); a.tst_rr(0, 0); npk = m.newlabel('npk'); m.bt_far(npk)
    # domanda aperta: primo pad = banco*12 + pad fisico -> taglia e distribuisci
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop()
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(9, 0)
    a.add_imm(1, 0); st(m, 0, PADP); sti(m, 0, CONF)
    far_call(m, 'chopmsg')
    far_bra(m, hret1)
    a.deflabel(npk)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); a.tst_rr(5, 5); hz = m.newlabel('hz'); a.bf_lbl(hz); far_bra(m, hret1); a.deflabel(hz)
    # fetta = codice pad (0..11); fuori range o analisi non valida per questo sample -> sample intero
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    whole = m.newlabel('whole')
    ld(m, ENVSRC, 0); a.cmpeq(5, 0); m.bf_far(whole)
    ld(m, NSL, 0); a.cmphs(0, 9); m.bt_far(whole)                                       # k >= NSL
    a.movl_ld_disp(5, 0x20, 0); st(m, 0, SV20); a.movl_ld_disp(5, 0x24, 0); st(m, 0, SV24); a.movl_ld_disp(5, 0x2C, 0); st(m, 0, SV2C)
    a.mov_rm_rn(5, 2); a.add_imm(0x3E, 2); a.movw_ld(2, 0); st(m, 0, SV3E); a.mov_imm(0, 0); a.movw_st(0, 2)   # +0x3E = 0: se no il lettore suona tutto il sample
    work(m, 10, POS_OFF); a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(0, 10)
    a.movl_ld(10, 0); a.movl_st_disp(0, 0x20, 5)                                       # start = POS[k]
    a.add_imm(1, 9); ld(m, NSL, 0); a.cmphs(0, 9); lst = m.newlabel('lst')
    ld(m, E0, 2); a.bt_lbl(lst)
    a.movl_ld_disp(10, 4, 2); a.add_imm(-1, 2)                                          # end = POS[k+1]-1
    a.deflabel(lst); a.movl_st_disp(2, 0x24, 5); a.mov_imm(0, 0); a.movl_st_disp(0, 0x2C, 5)
    a.mov_rm_rn(9, 4); a.add_imm(-1, 4); a.mov_rm_rn(5, 6); far_call(m, 'tc_play')        # schermo remoto: fetta k
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5)
    m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5)
    ld(m, SV20, 0); a.movl_st_disp(0, 0x20, 5); ld(m, SV24, 0); a.movl_st_disp(0, 0x24, 5); ld(m, SV2C, 0); a.movl_st_disp(0, 0x2C, 5)
    a.mov_rm_rn(5, 2); a.add_imm(0x3E, 2); ld(m, SV3E, 0); a.movw_st(0, 2)
    far_bra(m, hret1)
    a.deflabel(whole)
    a.mov_imm(-1, 4); a.mov_rm_rn(5, 6); far_call(m, 'tc_play')                         # schermo remoto: sample intero
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5)
    m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
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
    def absr0():
        l = m.newlabel('ab'); a.cmppz(0); a.bt_lbl(l); a.neg(0, 0); a.deflabel(l)
    a.deflabel('__cstart')
    # ---- DO IT: analisi aggiornata; 1a pressione -> domanda; 2a -> taglia ----
    ld(m, PEND, 0); a.tst_rr(0, 0); np_ = m.newlabel('np'); a.bt_lbl(np_); sti(m, 0, PEND); a.bsr_lbl('analyze'); a.nop(); a.deflabel(np_)
    ld(m, ERR, 8); a.tst_rr(8, 8); m.bf_far('__err')
    ld(m, NSL, 0); a.mov_imm(2, 3); a.cmpge(3, 0); m.bt_far('__okn'); sti(m, ERR_NOTR, ERR); far_bra(m, '__err')
    a.deflabel('__okn')
    ld(m, CONF, 0); a.tst_rr(0, 0); cyes = m.newlabel('cyes'); m.bf_far(cyes)
    sti(m, 1, CONF); pg.emit_message(m, "Hit first pad"); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)
    far_bra(m, 'tcret')
    a.deflabel(cyes); sti(m, 0, CONF)                                                   # 2o DO IT: dal pad P
    a.bsr_lbl('chopmsg'); a.nop(); far_bra(m, 'tcret')
    # ---- chopmsg: taglia (commit) e mostra l'esito ----
    a.deflabel('chopmsg'); a.stsl_pr()
    a.bsr_lbl('commit'); a.nop()
    ld(m, ERR, 8); a.tst_rr(8, 8); cme = m.newlabel('cme'); m.bf_far(cme)
    pg.emit_message_dec(m, "Done: ", CNT, " slices"); a.bra_lbl('__cmx'); a.nop()
    a.deflabel(cme); pg.emit_message_dec(m, "Error ", ERR, "")
    a.deflabel('__cmx'); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('__err'); ld(m, ERR, 8)
    for code, text in ((ERR_NOSMP, "No sample"), (ERR_SHORT, "Sample too short"), (ERR_NOTR, "No transients"), (3, "Memory full")):
        nx = m.newlabel('er'); a.mov_imm(code, 3); a.cmpeq(3, 8); m.bf_far(nx); pg.emit_message(m, text); far_bra(m, 'tcret'); a.deflabel(nx); m.pool_jump()
    pg.emit_message_dec(m, "Error ", ERR, ""); far_bra(m, 'tcret')
    m.pool_jump()

    # ---- icinv: invalida le righe di cache del nostro codice (vedi Sample FX) ----
    a.deflabel('icinv')
    a.movl_pool('__cstart', 4); m.imm32(0xFFFFFFF0, 1); a.and_rr(1, 4)
    m.imm32(CACHE_SPAN // 16 + 1, 5); m.imm32(0xF0000008, 6); m.imm32(0x1FFFFC00, 7); m.imm32(0xFF0, 3)
    icl = m.newlabel('icl'); a.deflabel(icl)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0); a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(icl)
    a.rts(); a.nop()
    m.pool_jump()

    # ---- analyze: (inviluppo se il sample e' cambiato) + scelta dei transienti -> POS[], NSL, etichetta N ----
    a.deflabel('analyze'); a.stsl_pr()
    sti(m, 0, ERR); sti(m, 0, NSL)
    ld(m, LSRC, 8); st(m, 8, SRC)
    nos = m.newlabel('nos'); a.tst_rr(8, 8); m.bt_far(nos)
    m.imm32(0x100, 2); a.add_rr(8, 2); a.movl_ld(2, 0)
    m.imm32(MAGIC_ADDON, 3); a.cmpeq(3, 0); m.bt_far(nos); m.imm32(MAGIC_HUB, 3); a.cmpeq(3, 0); m.bt_far(nos)
    a.movl_ld_disp(8, 0x20, 2); st(m, 2, S0); a.movl_ld_disp(8, 0x24, 3); st(m, 3, E0)
    a.sub(2, 3); a.add_imm(1, 3); st(m, 3, NN)                                           # n = end - start + 1
    m.imm32(MIN_N, 2); a.cmpge(2, 3); short = m.newlabel('short'); m.bf_far(short)
    # HOPSH: 8, di piu' finche' n >> HOPSH >= MAXBLK
    a.mov_imm(8, 4); m.imm32(MAXBLK, 5)
    hl = m.newlabel('hl'); hx = m.newlabel('hx'); a.deflabel(hl)
    a.mov_rm_rn(3, 0); a.mov_rm_rn(4, 1); a.neg(1, 1); a.shad(1, 0); a.cmphs(5, 0); a.bf_lbl(hx); a.add_imm(1, 4); a.bra_lbl(hl); a.nop()
    a.deflabel(hx)
    st(m, 4, HOPSH)
    a.mov_imm(1, 0); a.shad(4, 0); a.add_imm(-1, 0); a.add_rr(0, 3); a.neg(4, 1); a.shad(1, 3); st(m, 3, NB)   # NB = ceil(n / hop)
    # inviluppo solo se il sample e' cambiato (o mai calcolato)
    ld(m, ENVSRC, 0); ld(m, SRC, 8); a.cmpeq(8, 0); hv = m.newlabel('hv'); m.bt_far(hv)
    a.bsr_lbl('icinv'); a.nop(); cached_call(m, 'env')
    ld(m, SRC, 8); st(m, 8, ENVSRC)
    a.deflabel(hv)
    a.bsr_lbl('icinv'); a.nop(); cached_call(m, 'pick')
    a.bra_lbl('__anl'); a.nop()
    a.deflabel(nos); sti(m, ERR_NOSMP, ERR); a.bra_lbl('__anl'); a.nop()
    a.deflabel(short); sti(m, ERR_SHORT, ERR)
    a.deflabel('__anl')
    # etichetta N: 3 cifre, spazi a sinistra
    ld(m, NSL, 4); a.mov_imm(100, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 10)           # r0 = centinaia, r10 = resto
    a.mov_rm_rn(0, 13)
    a.mov_rm_rn(10, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 11); a.mov_rm_rn(0, 14)   # r14 decine, r11 unita'
    m.imm32(pg.addr['dlabels'][NLBL], 12)
    a.mov_rm_rn(13, 0); a.tst_rr(0, 0); c1 = m.newlabel('c1'); a.bf_lbl(c1); a.mov_imm(ord(' ') - 0x30, 0); a.deflabel(c1)
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(14, 0); ld(m, NSL, 2); a.mov_imm(10, 3); a.cmpge(3, 2); c2 = m.newlabel('c2'); a.bt_lbl(c2); a.mov_imm(ord(' ') - 0x30, 0); a.deflabel(c2)
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(11, 0); a.add_imm(0x30, 0); a.movb_st(0, 12)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- env (dalla cache): ENV[b] = max |x| nel blocco b (canale L), MAXE = max(ENV) ----
    a.deflabel('env'); a.stsl_pr()
    ld(m, SRC, 8); ld(m, S0, 10); a.shll(10); a.add_rr(8, 10); m.imm32(0x100, 1); a.add_rr(1, 10)   # r10 = &L[start]
    ld(m, NN, 11); ld(m, HOPSH, 4); a.mov_imm(1, 12); a.shad(4, 12)                     # r11 = rimasti, r12 = hop
    work(m, 13); a.mov_imm(0, 14)                                                        # r13 = ENV, r14 = MAXE
    bl = m.newlabel('bl'); bx = m.newlabel('bx'); a.deflabel(bl)
    a.cmppl(11); m.bf_far(bx)
    a.mov_rm_rn(12, 9); a.cmpgt(11, 9); c = m.newlabel('c'); a.bf_lbl(c); a.mov_rm_rn(11, 9); a.deflabel(c)   # r9 = min(hop, rimasti)
    a.sub(9, 11); a.mov_imm(0, 7)                                                        # r7 = picco del blocco
    il = m.newlabel('il'); a.deflabel(il)
    a.movw_ld(10, 0); a.add_imm(2, 10); absr0(); a.cmpgt(7, 0); p = m.newlabel('p'); a.bf_lbl(p); a.mov_rm_rn(0, 7); a.deflabel(p)
    a.dt(9); a.bf_lbl(il)
    a.movw_st(7, 13); a.add_imm(2, 13)
    a.cmpgt(14, 7); q = m.newlabel('q'); a.bf_lbl(q); a.mov_rm_rn(7, 14); a.deflabel(q)
    a.bra_lbl(bl); a.nop()
    a.deflabel(bx); st(m, 14, MAXE)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- pick (dalla cache): transienti -> POS[0..NSL) ----
    a.deflabel('pick'); a.stsl_pr()
    ld(m, MAXE, 4); m.imm32(100, 2); ld(m, SENS, 3); a.sub(3, 2); a.mul_l(2, 4); a.sts_macl(4); m.imm32(200, 5); pg._hcall(m, '__udiv'); st(m, 0, DTH)
    m.imm32(0x94, 2); ld(m, SRC, 8); a.add_rr(8, 2); a.movl_ld(2, 9)                    # r9 = rate
    a.mov_rm_rn(9, 4); m.imm32(60, 2); a.mul_l(2, 4); a.sts_macl(4); m.imm32(1000, 5); pg._hcall(m, '__udiv')
    ld(m, HOPSH, 1); a.neg(1, 1); a.shad(1, 0); a.tst_rr(0, 0); g1 = m.newlabel('g1'); a.bf_lbl(g1); a.mov_imm(1, 0); a.deflabel(g1); st(m, 0, MINGAP)
    ld(m, SRC, 8); m.imm32(0x94, 2); a.add_rr(8, 2); a.movl_ld(2, 4); a.mov_imm(5, 2); a.mul_l(2, 4); a.sts_macl(4); m.imm32(1000, 5); pg._hcall(m, '__udiv'); st(m, 0, PRE)
    work(m, 10, POS_OFF); ld(m, S0, 0); a.movl_st(0, 10); sti(m, 1, NSL); sti(m, 0, LASTK)
    sti(m, 1, KK)
    kl = m.newlabel('kl'); kx = m.newlabel('kx'); kn = m.newlabel('kn'); a.deflabel(kl)
    ld(m, KK, 9); ld(m, NB, 0); a.cmpge(0, 9); m.bt_far(kx)                              # k >= NB -> fine
    ld(m, NSL, 0); a.mov_imm(MAXSL, 1); a.cmpge(1, 0); m.bt_far(kx)
    work(m, 13); a.mov_rm_rn(9, 0); a.shll(0); a.add_rr(0, 13)                          # r13 = &ENV[k]
    a.movw_ld(13, 4); a.extu_w(4, 4)                                                     # r4 = E[k]
    a.mov_rm_rn(13, 1); a.add_imm(-2, 1); a.movw_ld(1, 5); a.extu_w(5, 5)                 # r5 = E[k-1]
    a.mov_imm(2, 0); a.cmpge(0, 9); k2 = m.newlabel('k2'); a.bf_lbl(k2); a.add_imm(-2, 1); a.deflabel(k2)
    a.movw_ld(1, 6); a.extu_w(6, 6)                                                      # r6 = E[max(k-2,0)]
    a.cmpgt(5, 6); mm_ = m.newlabel('mm'); a.bt_lbl(mm_); a.mov_rm_rn(6, 5); a.deflabel(mm_)   # r5 = min
    a.mov_rm_rn(4, 7); a.sub(5, 7)                                                       # r7 = salita
    ld(m, DTH, 0); a.cmpgt(0, 7); m.bf_far(kn)
    ld(m, LASTK, 0); a.mov_rm_rn(9, 2); a.sub(0, 2); ld(m, MINGAP, 0); a.cmpge(0, 2); m.bf_far(kn)   # (ld usa r1)
    # punto: primo |x| >= E[k]/4 in [S0+(k-1)*hop, min(S0+(k+1)*hop, S0+n)); se nessuno S0+k*hop
    a.shlr2(4)                                                                           # r4 = soglia
    ld(m, HOPSH, 12); ld(m, S0, 2); a.mov_rm_rn(9, 3); a.add_imm(-1, 3); a.shad(12, 3); a.add_rr(2, 3)   # r3 = inizio scansione
    a.mov_rm_rn(9, 6); a.add_imm(1, 6); a.shad(12, 6); a.add_rr(2, 6)                   # r6 = fine (S0+(k+1)*hop)
    ld(m, NN, 1); a.add_rr(2, 1); a.cmpgt(1, 6); fe = m.newlabel('fe'); a.bf_lbl(fe); a.mov_rm_rn(1, 6); a.deflabel(fe)
    a.mov_rm_rn(9, 11); a.shad(12, 11); a.add_rr(2, 11)                                  # r11 = default S0+k*hop
    ld(m, SRC, 8); a.mov_rm_rn(3, 10); a.shll(10); a.add_rr(8, 10); m.imm32(0x100, 1); a.add_rr(1, 10)
    sl = m.newlabel('sl'); sf = m.newlabel('sf'); sx = m.newlabel('sx'); a.deflabel(sl)
    a.cmpge(6, 3); a.bt_lbl(sx)
    a.movw_ld(10, 0); a.add_imm(2, 10); absr0(); a.cmpge(4, 0); a.bt_lbl(sf)
    a.add_imm(1, 3); a.bra_lbl(sl); a.nop()
    a.deflabel(sf); a.mov_rm_rn(3, 11)
    a.deflabel(sx)
    ld(m, PRE, 0); a.sub(0, 11)                                                          # un filo prima
    work(m, 10, POS_OFF); ld(m, NSL, 1); a.shll2(1); a.add_rr(1, 10)                     # r10 = &POS[NSL]
    a.mov_rm_rn(10, 1); a.add_imm(-4, 1); a.movl_ld(1, 0)                                # r0 = POS[NSL-1]
    a.mov_imm(1, 1); a.shad(12, 1); a.add_rr(1, 0); a.cmpgt(0, 11); m.bf_far(kn)        # p > prec + hop
    a.movl_st(11, 10); ld(m, NSL, 0); a.add_imm(1, 0); st(m, 0, NSL); st(m, 9, LASTK)
    a.deflabel(kn); ld(m, KK, 9); a.add_imm(1, 9); st(m, 9, KK); far_bra(m, kl)
    m.pool_jump()
    a.deflabel(kx)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- commit: Extract di ogni fetta, assegnazione ai pad da P ----
    a.deflabel('commit'); a.stsl_pr()
    sti(m, 0, ERR); sti(m, 0, CNT)
    ld(m, SRC, 8); a.movl_ld_disp(8, 0x20, 0); st(m, 0, SV20); a.movl_ld_disp(8, 0x24, 0); st(m, 0, SV24)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 2); a.extu_w(2, 2); m.imm32(PROG_STRIDE, 3); a.mul_l(3, 2); a.sts_macl(2); m.imm32(PROG_BASE, 3); a.add_rr(3, 2); st(m, 2, PROG)
    cl = m.newlabel('cl'); cend = m.newlabel('cend'); a.deflabel(cl)
    ld(m, CNT, 9); ld(m, NSL, 2); a.cmpge(2, 9); m.bt_far(cend)
    ld(m, PADP, 2); a.add_imm(-1, 2); a.add_rr(9, 2); a.mov_imm(MAXPAD, 0); a.cmpge(0, 2); m.bt_far(cend)
    work(m, 10, POS_OFF); a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(0, 10)
    a.movl_ld(10, 11)                                                                    # start = POS[i]
    a.mov_rm_rn(9, 0); a.add_imm(1, 0); ld(m, NSL, 2); a.cmpge(2, 0); ce = m.newlabel('ce'); ld(m, E0, 12); a.bt_lbl(ce)
    a.movl_ld_disp(10, 4, 12); a.add_imm(-1, 12)
    a.deflabel(ce)
    ld(m, SRC, 8); a.movl_st_disp(11, 0x20, 8); a.movl_st_disp(12, 0x24, 8)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4)
    m.os_extract(8)
    a.tst_rr(0, 0); xok = m.newlabel('xok'); a.bt_lbl(xok); st(m, 0, ERR); far_bra(m, cend); a.deflabel(xok)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8); a.add_imm(4, 8)                            # nome del sample nuovo
    ld(m, CNT, 9); ld(m, PADP, 2); a.add_imm(-1, 2); a.add_rr(9, 2); m.imm32(0xA4, 3); a.mul_l(3, 2); a.sts_macl(2); ld(m, PROG, 3); a.add_rr(3, 2); a.add_imm(0x18, 2)
    a.mov_rm_rn(2, 12); a.mov_imm(16, 3)
    cp = m.newlabel('cp'); a.deflabel(cp); a.movb_ldinc(8, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.dt(3); a.bf_lbl(cp)
    a.mov_rm_rn(2, 12); a.add_imm(0x10, 12); a.mov_imm(0, 0); a.movb_st_disp(0, 12)
    a.mov_imm(100, 0); a.movb_st_disp(1, 12); a.mov_imm(0, 0); a.movb_st_disp(2, 12); a.mov_imm(0x7F, 0); a.movb_st_disp(3, 12)
    a.mov_imm(0, 0); a.movb_st_disp(4, 12); a.movb_st_disp(5, 12); a.movb_st_disp(6, 12); a.movb_st_disp(7, 12)
    a.mov_rm_rn(2, 12); a.add_imm(0x60, 12)                                              # r2 = inizio blocco pad -> +0x60
    a.mov_imm(CHOP_MONO, 0); a.movb_st_disp(2, 12); a.mov_imm(CHOP_MUTE, 0); a.movb_st_disp(3, 12)   # +0x62 Mono, +0x63 gruppo
    ld(m, CNT, 9); a.add_imm(1, 9); st(m, 9, CNT)
    far_bra(m, cl)
    m.pool_jump()
    a.deflabel(cend)
    ld(m, SRC, 8); ld(m, SV20, 2); a.movl_st_disp(2, 0x20, 8); ld(m, SV24, 2); a.movl_st_disp(2, 0x24, 8)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('tcret')
pg.on_doit(doit)


# ---------------- modello Python di riferimento ----------------
def ref_analyze(L, s, e, sens, rate):
    n = e - s + 1; hopsh = 8
    while (n >> hopsh) >= MAXBLK: hopsh += 1
    hop = 1 << hopsh; NB = (n + hop - 1) >> hopsh
    E = [max(abs(x) for x in L[s + b*hop: min(s + (b+1)*hop, s + n)]) for b in range(NB)]
    D = max(E) * (100 - sens) // 200
    mingap = max(1, (rate * 60 // 1000) >> hopsh); pre = rate * 5 // 1000
    pos = [s]; last = 0
    for k in range(1, NB):
        if len(pos) >= MAXSL: break
        rise = E[k] - min(E[k-1], E[max(k-2, 0)])
        if rise > D and k - last >= mingap:
            t = E[k] >> 2; p = s + k*hop
            for i in range(s + (k-1)*hop, min(s + (k+1)*hop, s + n)):
                if abs(L[i]) >= t: p = i; break
            p -= pre
            if p > pos[-1] + hop: pos.append(p); last = k
    return pos

def selftest(out):
    """test in emulatore sul WAV 'out' (TRANSCHP.WAV, o CHOPSHOP.WAV con questa pagina ospitata)"""
    import math, random
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE
    from pages import SET_FOCUS
    from shasm import SHAsm
    LOG, MSG = 0x88E00000, 0x88E10000     # LOG: +0 n extract, +4.. (start,end) delle extract, +0x100 n play, +0x104 start, +0x108 end, +0x10C loop
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    stub(OS_PUSH_PAGE, lambda s: (s.rts(), s.nop())); stub(ENABLE_DOIT, lambda s: (s.rts(), s.nop())); stub(SET_FOCUS, lambda s: (s.rts(), s.nop()))
    NEWB = 0x88D00000       # blocchi "fetta" finti: nome SL<n>
    def extract(s):         # registra start/end, crea un blocco-nome in NEWB + 0x20*i, lo rende corrente
        s.movl_ld_disp(4, 8, 2); s.movl_pool(LOG, 1); s.movl_ld(1, 0); s.shll2(0); s.shll(0); s.add_rr(1, 0); s.add_imm(4, 0)
        s.movl_ld_disp(2, 0x20, 3); s.movl_st(3, 0); s.movl_ld_disp(2, 0x24, 3); s.movl_st_disp(3, 4, 0)
        s.movl_ld(1, 0); s.mov_rm_rn(0, 3); s.shll2(3); s.shll2(3); s.shll(3); s.movl_pool(NEWB, 2); s.add_rr(3, 2)
        s.mov_rm_rn(2, 3); s.add_imm(4, 3); s.mov_imm(ord('S'), 5); s.movb_st(5, 3); s.add_imm(1, 3); s.mov_imm(ord('L'), 5); s.movb_st(5, 3); s.add_imm(1, 3)
        s.mov_rm_rn(0, 5); s.add_imm(0x41, 5); s.movb_st(5, 3)
        s.movl_st_disp(2, 8, 4); s.add_imm(1, 0); s.movl_st(0, 1); s.mov_imm(0, 0); s.rts(); s.nop()
    stub(EXTRACT_CMD, extract)
    stub(AUD_PLAY, lambda s: (s.movl_pool(LOG + 0x100, 1), s.movl_ld(1, 0), s.add_imm(1, 0), s.movl_st(0, 1),
                              s.movl_ld_disp(5, 0x20, 0), s.movl_st_disp(0, 4, 1), s.movl_ld_disp(5, 0x24, 0), s.movl_st_disp(0, 8, 1),
                              s.movl_ld_disp(5, 0x2C, 0), s.movl_st_disp(0, 12, 1),
                              s.mov_rm_rn(5, 2), s.add_imm(0x3E, 2), s.movw_ld(2, 0), s.extu_w(0, 0), s.movl_st_disp(0, 16, 1), s.rts(), s.nop()))
    stub(AUD_STOP, lambda s: (s.rts(), s.nop()))
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato"
    def call(obj, type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 200_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    def fire(n): return h.fire(n, maxsteps=200_000_000)
    # loop sintetico: 2 battute a 95 BPM, cassa/rullante sui quarti, hat sugli ottavi (stereo: R = L/2)
    rate = 44100; beat = int(rate * 60 / 95); n = beat * 8; rnd = random.Random(3); L = [0] * n
    def hit(at, amp, dec, freq, noise):
        for i in range(min(int(rate * 0.25), n - at)):
            x = amp * math.exp(-i / (rate * dec)) * ((1 - noise) * math.sin(2 * math.pi * freq * i / rate) + noise * rnd.uniform(-1, 1))
            L[at + i] = max(-32768, min(32767, L[at + i] + int(x)))
    for b in range(8):
        t0 = b * beat + 1000
        hit(t0, 28000, 0.08, 60, 0.1) if b % 2 == 0 else hit(t0, 22000, 0.05, 200, 0.7)
        hit(t0 + beat // 2, 6000, 0.02, 8000, 0.95)
    s0, e0 = 0, n - 1
    blk = h.list_end(); size = (0x100 + 4 * n + 0x200 + 3) & ~3
    h.ram_w32(blk, size); [h.ram_w8(blk + 4 + i, c) for i, c in enumerate(b'BREAK01'.ljust(16, b'\0'))]
    h.ram_w32(blk + 0x18, n); h.ram_w32(blk + 0x1C, n); h.ram_w32(blk + 0x20, s0); h.ram_w32(blk + 0x24, e0); h.ram_w32(blk + 0x2C, 1); h.ram_w32(blk + 0x94, rate); h.ram_w16(blk + 0x92, 2)
    for i, v in enumerate(L + [x // 2 for x in L]): h.ram_w16(blk + 0x100 + 2 * i, v & 0xffff)
    h.ram_w32(blk + size, 0); h.ram_w32(MGR + 8, blk)
    for k in range(0, 0x110, 4): h.ram_w32(LOG + k, 0)
    work_base = pg.handler & ~0x7FFF
    lbl = lambda: bytes(e.rd(pg.dlabels[NLBL] + i, 1) for i in range(3)).decode()
    msg = lambda: bytes(e.rd(pg.msgt + i, 1) for i in range(16))
    h.ram_w32(EVQ + 0x68, pg.page); fire(DEBOUNCE + 4)
    def posv(): return [h.ram_r32(LOAD_SCRATCH - WORK_OFF + POS_OFF + 4 * i) for i in range(h.ram_r32(NSL))]
    exp = ref_analyze(L, s0, e0, 50, rate)
    assert posv() == exp, (posv()[:6], exp[:6])
    assert lbl() == '%3d' % len(exp), lbl()
    print("  ok SENS 50: %d fette, bit-exact col modello, etichetta N=%r" % (len(exp), lbl()))
    for sens in (90, 20, 50):
        h.ram_w32(SENS, sens); fire(DEBOUNCE + 4); exp = ref_analyze(L, s0, e0, sens, rate)
        assert posv() == exp and lbl() == '%3d' % len(exp), (sens, posv()[:5], exp[:5], lbl())
        print("  ok SENS %d: %d fette" % (sens, len(exp)))
    # pad k = fetta k; start/end/loop ripristinati
    exp = ref_analyze(L, s0, e0, 50, rate)
    h.ram_w16(blk + 0x3E, 4)                                            # +0x3E != 0 (WAV da pack): il lettore suonerebbe tutto
    assert call(pg.page, EV_PAD, code=2, val=100) == 1 and h.ram_r32(LOG + 0x110) == 0 and e.rd(blk + 0x3E, 2) == 4, "+0x3E a 0 solo durante il play"
    h.ram_w16(blk + 0x3E, 0)
    assert (h.ram_r32(LOG + 0x104), h.ram_r32(LOG + 0x108), h.ram_r32(LOG + 0x10C)) == (exp[2], exp[3] - 1, 0), (h.ram_r32(LOG + 0x104), h.ram_r32(LOG + 0x108))
    assert (h.ram_r32(blk + 0x20), h.ram_r32(blk + 0x24), h.ram_r32(blk + 0x2C)) == (s0, e0, 1), "start/end/loop non ripristinati"
    assert call(pg.page, EV_PAD, code=len(exp) - 1, val=100) == 1 and h.ram_r32(LOG + 0x108) == e0
    assert call(pg.page, EV_PAD, code=11, val=100) == 1 if len(exp) <= 11 else True
    print("  ok pad = fetta (start/end giusti, loop spento durante il play, tutto ripristinato)")
    MP = h.ram_r32(MGR + 4) + MBOX_OFF + MBOX_PLAY; c0 = h.ram_r32(MP + 16)
    assert call(pg.page, EV_PAD, code=3, val=100) == 1
    assert [h.ram_r32(MP + 4 * i) for i in range(4)] == [1, 3, exp[3], exp[4] - 1] and h.ram_r32(MP + 16) == c0 + 1, [h.ram_r32(MP + 4 * i) for i in range(5)]
    assert call(pg.page, EV_PAD, code=3, val=0) == 1 and h.ram_r32(MP) == 0 and h.ram_r32(MP + 16) == c0 + 2
    print("  ok schermo remoto: pad 4 -> ascolto fetta 4 (start/end) nella cassetta, rilascio -> spento")
    # schermo remoto: tagli nella cassetta (B = MGR[1] del loader)
    MB = h.ram_r32(MGR + 4) + MBOX_OFF; sq0 = h.ram_r32(MB + 4)
    assert h.ram_r32(MB) == MBOX_MAGIC and h.ram_r32(MB + 8) == 1 and h.ram_r32(MB + 0xC) == blk and h.ram_r32(MB + 0x14) == 1
    assert [h.ram_r32(MB + 0x20 + 4 * i) for i in range(h.ram_r32(MB + 0x10))] == exp and sq0 > 0
    h.ram_w32(SENS, 90); fire(DEBOUNCE + 4); assert h.ram_r32(MB + 4) > sq0 and h.ram_r32(MB + 0x10) == len(ref_analyze(L, s0, e0, 90, rate))
    h.ram_w32(SENS, 50); fire(DEBOUNCE + 4); assert [h.ram_r32(MB + 0x20 + 4 * i) for i in range(h.ram_r32(MB + 0x10))] == exp
    print("  ok schermo remoto: tagli nella cassetta (%d), aggiornati al cambio di SENS" % len(exp))
    # DO IT: domanda, CANCEL, DO IT x2 -> extract + pad
    h.ram_w32(PADP, 1)
    assert call(pg.page, 2, code=0x2E) == 1 and msg().startswith(b'Hit first pad') and h.ram_r32(LOG) == 0
    assert call(pg.page, 2, code=0x2D) == 1 and h.ram_r32(CONF) == 0
    np0 = h.ram_r32(LOG + 0x100)
    assert call(pg.page, 2, code=0x2E) == 1 and h.ram_r32(CONF) == 1
    e.wr(BANKOBJ, 0, 1); assert call(pg.page, EV_PAD, code=4, val=110) == 1              # banco A, pad 5
    assert h.ram_r32(LOG + 0x100) == np0, "con la domanda aperta il pad non deve suonare"
    assert h.ram_r32(PADP) == 5 and h.ram_r32(CONF) == 0
    ne = h.ram_r32(LOG); assert ne == len(exp), (ne, len(exp))
    got = [(h.ram_r32(LOG + 4 + 8 * i), h.ram_r32(LOG + 8 + 8 * i)) for i in range(ne)]
    want = [(exp[i], (exp[i + 1] - 1) if i + 1 < len(exp) else e0) for i in range(len(exp))]
    assert got == want, (got[:3], want[:3])
    pidx = e.rd(PROG_IDX, 2); pbase = PROG_BASE + pidx * PROG_STRIDE + 0x18
    for i in range(ne):
        blkp = pbase + (4 + i) * 0xA4
        nm = bytes(e.rd(blkp + j, 1) for j in range(3)); assert nm == b'SL' + bytes([0x41 + i]), (i, nm)
        assert e.rd(blkp + 0x11, 1) == 100 and e.rd(blkp + 0x13, 1) == 0x7F
        assert e.rd(blkp + 0x62, 1) == CHOP_MONO and e.rd(blkp + 0x63, 1) == CHOP_MUTE, (i, e.rd(blkp + 0x62, 1), e.rd(blkp + 0x63, 1))
    assert (h.ram_r32(blk + 0x20), h.ram_r32(blk + 0x24), h.ram_r32(MGR + 8)) == (s0, e0, blk), "originale non ripristinato"
    assert msg().startswith(b'Done: %02d slices' % ne), msg()
    # banco C, pad 3 -> primo pad 27; 2o DO IT -> dal campo P
    for k in range(0, 0x110, 4): h.ram_w32(LOG + k, 0)
    assert call(pg.page, 2, code=0x2E) == 1; e.wr(BANKOBJ, 2, 1); assert call(pg.page, EV_PAD, code=2, val=90) == 1
    assert h.ram_r32(PADP) == 27 and h.ram_r32(LOG) == min(ne, 48 - 26), (h.ram_r32(PADP), h.ram_r32(LOG))
    nmp = bytes(e.rd(pbase + 26 * 0xA4 + j, 1) for j in range(2)); assert nmp == b'SL', nmp
    for k in range(0, 0x110, 4): h.ram_w32(LOG + k, 0)
    h.ram_w32(PADP, 45); assert call(pg.page, 2, code=0x2E) == 1 and call(pg.page, 2, code=0x2E) == 1
    assert h.ram_r32(LOG) == 4 and msg().startswith(b'Done: 04 slices'), (h.ram_r32(LOG), msg())
    print("  ok pad colpito (banco C pad 3 -> dal pad 27), 2o DO IT -> dal campo P (45: 4 fette fino al pad 48)")
    print("  ok DO IT: %d extract con i confini giusti, assegnate ai pad 5..%d, originale ripristinato: %r" % (ne, 4 + ne, msg().decode()))
    # troppo corto / nessun sample
    b2 = h.list_end(); h.ram_w32(b2, 0x300); h.ram_w32(b2 + 0x20, 0); h.ram_w32(b2 + 0x24, 100); h.ram_w32(b2 + 0x94, rate); h.ram_w32(b2 + 0x300, 0)
    h.ram_w32(MGR + 8, b2); fire(DEBOUNCE + 4); assert call(pg.page, 2, code=0x2E) == 1 and msg().startswith(b'Sample too short'), msg()
    print("  ok 'Sample too short'")
    MB = h.ram_r32(MGR + 4) + MBOX_OFF; assert h.ram_r32(MB + 0x10) == 0                  # troppo corto: nessun taglio
    h.ram_w32(EVQ + 0x68, 0x88123456); fire(LEAVE_FIRES + 2); assert h.ram_r32(MB + 0x14) == 0 and h.ram_r32(MB + 0x10) == 0
    print("  ok uscita dalla pagina: cassetta 'nessun tool aperto'")
    # sample lungo: HOPSH cresce, lavoro per fire
    print("VALIDATO: Trans-Chop v3 (primo pad colpendolo, Mono + mute group 32) -> %s" % os.path.basename(out))


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'TRANSCHP.WAV'); pg.build_wav(out); selftest(out)
