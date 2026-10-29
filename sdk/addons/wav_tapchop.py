#!/usr/bin/env python3
# TAP CHOP (lazy chop) = modo "Tap" di Chop Shop (wav_chopshop.py); da solo -> TAPCHOP.WAV, solo per i test
#   riga 0: selettore sample (rotella)      riga 1: "SNAP On    N  0"
#     SNAP = ogni tocco va sull'attacco piu' vicino (-50..+30 ms) se ce n'e' uno chiaro, altrimenti resta dov'e'
#     N = fette della presa (solo lettura)
#   DO IT = "Tap pads to chop": il primo pad fa partire il sample intero (start..end, loop spento) e prende la fetta 1
#   (dall'inizio); ogni pad colpito mentre suona e' un taglio e la fetta da li' va SUL PAD COLPITO (banco*12 + pad
#   fisico, come l'OS); lo stesso pad due volte: vale l'ultima fetta. Tocchi a meno di 30 ms dal precedente = ignorati.
#   Fine del sample o DO IT = fine della presa -> "Keep? DO IT=yes"; i pad suonano le loro fette; DO IT = Extract delle
#   fette sui pad del program corrente (Mono, mute group 32), l'originale resta; CANCEL annulla la presa.
#   L'Extract va UNA FETTA PER FIRE con "Slicing kk/NN" (HW 2026-10-26/27: con 12 fette tutte nello stesso DO IT l'MPC
#   restava fermo a lungo e sembrava bloccato); tra una fetta e l'altra il sample originale e' gia' rimesso com'era;
#   CANCEL ferma ("Stopped, NN kept": le fette fatte restano), DO IT ignorato. Una fetta con fine < inizio si salta
#   (con Tune molto basso l'aggancio poteva passare il taglio dopo: l'Extract dell'OS copierebbe ~4 G campioni).
#   Orologio: contatore frame audio dell'OS (44100/s) letto all'arrivo dell'evento pad (un fire, ~3 ms): niente calcoli
#   durante la presa; l'aggancio agli attacchi gira a fine presa, dalla cache (P1). Il lettore dell'ascolto dell'OS
#   suona il sample 1:1 a 44,1 kHz (AUD_PLAY non legge il rate +0x94): posizione = start + frame passati. (HW 2026-10-24:
#   con un WAV da CF la v1 scalava col rate del sample e chiudeva la presa prima della fine.) Il lettore applica il Tune
#   del sample (+0x30, s16 in centesimi; la voce fa (tune + 3600) / 1200 ottave, 0x884C2B3C): velocita' = 2^(Tune/1200)
#   (Tune -1200 = meta'). Presa "con intonazione" (scelta utente 2026-10-26, dopo "angels" a -12): posizione = start +
#   frame passati * velocita' (Q16, tabella a passi di 25 cent + interpolazione); finestre dell'aggancio e antirimbalzo
#   in campioni scalate allo stesso modo. Durante AUD_PLAY: start/end espliciti, loop spento, +0x3E = 0 (se != 0 il
#   lettore ignora start/end e suona tutto il sample); poi tutto rimesso.
#   SCHERMO REMOTO: tagli (tipo 1) e ascolto nella cassetta B+MBOX_OFF, come Trans-Chop.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import page_or_addon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ, SCRATCH_MAGIC, MBOX_OFF, MBOX_MAGIC, MBOX_MAXN, MBOX_PLAY
SIG2 = 0x8B2A3D01                   # seconda firma della scratch del loader (loader.py)

MGR, EXTRACT_CMD = 0x8848ED30, 0x884C5E90
PLAYER, AUD_PLAY, AUD_STOP = 0x8848CD78, 0x884C2636, 0x884C29D2
PROG_BASE, PROG_IDX, PROG_STRIDE, MAXPAD = 0x8837B5AC, 0x8847C0E2, 0x1F98, 48
FRAMES = 0x881C05F0                 # contatore frame audio dell'OS (44100/s)
EV_PAD, LEAVE_FIRES = 0x38, 16
MAGIC_ADDON, MAGIC_HUB = 0x4143504D, 0x4843504D
CHOP_MUTE, CHOP_MONO = 32, 1        # come Trans-Chop
BANKOBJ = 0x8849C98C                # byte 0 = banco pad corrente (0 = A)
MAXSL = 48
MIN_N = 1024                        # regione minima (campioni)
HOP = 32                            # blocco dell'inviluppo per l'aggancio (campioni)
MS_TAP, MS_BACK, MS_FWD, MS_PRE, MS_GAP = 30, 50, 30, 5, 10   # tocchi minimi, finestra, "un filo prima", fetta minima
MINRISE = 600                       # salita minima di un attacco (|x| su 16 bit)
SNAP_SPAN = 0x200                   # byte di codice di tp_snap (righe di cache invalidate prima di chiamarlo da P1)
S_IDLE, S_ARM, S_TAKE, S_DONE, S_COMMIT = 0, 1, 2, 3, 4
ERR_SHORT, ERR_NOSMP = 0x41, 0x21

pg = page_or_addon("tapchop", open_press=None)     # in Chop Shop = modo Tap
SHOWMSG = pg.SHOWMSG
pg.other_menu("Tap Chop")
pg.msg_fires = 0xC00
pg.keep_focus = True
pg.far_hend = True
pg.selector()
SNAP = pg.choice("SNAP ", 0, 1, ["On", "Off"], default=0)
pg.label(" N", 10, 1)
NLBL = pg.dlabel(12, 1, 3)
pg.message()
ACTIVE, OUTCNT, ST, T0, SRC, S0, E0, NT, K, ERR, CNT, PROG = [pg.alloc_slot() for _ in range(12)]
ENGINE = 44100                      # frequenza del motore audio (il lettore suona 1:1 a questa)
SV20, SV24, SV2C, SV3E, AUDB, SPEED, MINTAP, WBK, WFK, PREK, GAPK = [pg.alloc_slot() for _ in range(11)]
PTAB = [round(65536 * 2 ** (25 * i / 1200)) for i in range(49)]   # 2^(c/1200) in Q16 per c = 0, 25, .. 1200
CUT = pg.alloc_buf(4 * MAXSL)       # inizio di ogni fetta (campioni, assoluti nel sample), CUT[0] = start
PADS = pg.alloc_buf(4 * MAXSL)      # pad di ogni fetta (0..47)


def far_call(m, label):
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def cached_call(m, label):
    """chiama label tramite l'alias P1 (cacheato): il codice addon da P2 gira a ~1M istr/s sull'HW"""
    m.a.movl_pool(label, 2); m.imm32(0xDFFFFFFF, 3); m.a.and_rr(3, 2); m.a.jsr(2); m.a.nop()

def absr0(m):
    a = m.a; l = m.newlabel('ab'); a.cmppz(0); a.bt_lbl(l); a.neg(0, 0); a.deflabel(l)

def stop(m):
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); m.a.jsr(2); m.a.nop()


def fire(m):
    a = m.a
    sti(m, 0, OUTCNT)
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); act = m.newlabel('act'); a.bf_lbl(act)
    sti(m, 1, ACTIVE); far_call(m, 'tp_reset')                                           # appena entrati: niente presa
    a.deflabel(act)
    ld(m, ST, 9); a.mov_imm(S_COMMIT, 0); a.cmpeq(0, 9); ncm = m.newlabel('ncm'); m.bf_far(ncm)
    far_call(m, 'tp_cstep'); fz = m.newlabel('fz'); far_bra(m, fz)                      # Extract: una fetta per fire
    a.deflabel(ncm)
    # presa in corso o finita: il sample deve restare quello; a fine sample la presa finisce
    a.mov_imm(S_TAKE, 0); a.cmphs(0, 9); m.bf_far(fz)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 0); ld(m, SRC, 2); a.cmpeq(2, 0); lost = m.newlabel('lost'); m.bf_far(lost)
    a.mov_imm(S_TAKE, 0); a.cmpeq(0, 9); m.bf_far(fz)
    far_call(m, 'tp_pos'); ld(m, E0, 2); a.cmpgt(2, 0); m.bf_far(fz)                    # pos > end: fine del sample
    far_call(m, 'tp_endtake'); far_bra(m, fz)
    a.deflabel(lost)
    stop(m); far_call(m, 'tp_reset'); sti(m, 1, SHOWMSG)
    a.deflabel(fz)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
    far_bra(m, 'tp_fx')
    m.pool_jump()
    # ---- tp_mb: r5 = cassetta B+MBOX_OFF (0 senza loader). r0-r3 ----
    a.deflabel('tp_mb'); mbn = m.newlabel('mbn')
    a.mov_imm(0, 5); m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); a.bt_lbl(mbn)
    a.mov_rm_rn(2, 1); a.add_imm(0x40, 1)                                                # firme del loader (B+0x40, +0x44)
    a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 3); a.cmpeq(3, 0); a.bf_lbl(mbn)
    a.movl_ld_disp(1, 4, 0); m.imm32(SIG2, 3); a.cmpeq(3, 0); a.bf_lbl(mbn)
    m.imm32(MBOX_OFF, 5); a.add_rr(2, 5)
    a.deflabel(mbn); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_pub: r4 = 1 pagina aperta (n = NT) / 0 chiusa (n = 0) -> cassetta tipo 1, seq per ultimo. r0-r7 ----
    a.deflabel('tp_pub'); px = m.newlabel('px'); a.stsl_pr()
    a.bsr_lbl('tp_mb'); a.nop(); a.tst_rr(5, 5); m.bt_far(px)
    m.imm32(MBOX_MAGIC, 0); a.movl_st(0, 5); a.mov_imm(1, 0); a.movl_st_disp(0, 8, 5)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 0); a.movl_st_disp(0, 0xC, 5); a.movl_st_disp(4, 0x14, 5)
    a.mov_imm(0, 6); pn = m.newlabel('pn'); a.tst_rr(4, 4); a.bt_lbl(pn); ld(m, NT, 6); a.deflabel(pn)
    a.mov_imm(MBOX_MAXN, 0); pm = m.newlabel('pm'); a.cmphs(0, 6); a.bf_lbl(pm); a.mov_rm_rn(0, 6); a.deflabel(pm)
    a.movl_st_disp(6, 0x10, 5)
    m.imm32(CUT, 7); a.mov_rm_rn(5, 3); a.add_imm(0x20, 3)
    pc = m.newlabel('pc'); pe = m.newlabel('pe'); a.tst_rr(6, 6); a.bt_lbl(pe)
    a.deflabel(pc); a.movl_ld(7, 0); a.add_imm(4, 7); a.movl_st(0, 3); a.add_imm(4, 3); a.dt(6); a.bf_lbl(pc)
    a.deflabel(pe); a.movl_ld_disp(5, 4, 0); a.add_imm(1, 0); a.movl_st_disp(0, 4, 5)
    a.deflabel(px); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_play: r4 = fetta (-1 sample intero), r6 = start, r7 = end: ascolto acceso nella cassetta; r4 = -2: spento ----
    a.deflabel('tp_play'); a.stsl_pr(); a.bsr_lbl('tp_mb'); a.nop(); a.tst_rr(5, 5); ppx = m.newlabel('ppx'); m.bt_far(ppx)
    m.imm32(MBOX_PLAY, 3); a.add_rr(5, 3)
    a.mov_imm(-2, 0); a.cmpeq(0, 4); pof = m.newlabel('pof'); a.bt_lbl(pof)
    a.movl_st_disp(4, 4, 3); a.movl_st_disp(6, 8, 3); a.movl_st_disp(7, 12, 3)
    a.mov_imm(1, 0); a.movl_st(0, 3); pse = m.newlabel('pse'); a.bra_lbl(pse); a.nop()
    a.deflabel(pof); a.mov_imm(0, 0); a.movl_st(0, 3)
    a.deflabel(pse); a.movl_ld_disp(3, 16, 0); a.add_imm(1, 0); a.movl_st_disp(0, 16, 3)
    a.deflabel(ppx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_aud: r5 = blocco, r6 = start, r7 = end -> AUD_PLAY su start..end (+0x3E 0, loop spento; il Tune resta),
    #      poi il blocco torna com'era (la voce si copia i puntatori al play). r0-r7 ----
    a.deflabel('tp_aud'); a.stsl_pr()
    st(m, 5, AUDB)
    a.movl_ld_disp(5, 0x20, 0); st(m, 0, SV20); a.movl_ld_disp(5, 0x24, 0); st(m, 0, SV24); a.movl_ld_disp(5, 0x2C, 0); st(m, 0, SV2C)
    a.mov_rm_rn(5, 2); a.add_imm(0x3E, 2); a.movw_ld(2, 0); st(m, 0, SV3E); a.mov_imm(0, 0); a.movw_st(0, 2)        # +0x3E
    a.movl_st_disp(6, 0x20, 5); a.movl_st_disp(7, 0x24, 5); a.mov_imm(0, 0); a.movl_st_disp(0, 0x2C, 5)
    m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
    ld(m, AUDB, 5)
    ld(m, SV20, 0); a.movl_st_disp(0, 0x20, 5); ld(m, SV24, 0); a.movl_st_disp(0, 0x24, 5); ld(m, SV2C, 0); a.movl_st_disp(0, 0x2C, 5)
    a.mov_rm_rn(5, 2); a.add_imm(0x3E, 2); ld(m, SV3E, 0); a.movw_st(0, 2)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_nlbl: etichetta N = NT (3 cifre, spazi a sinistra) + ridisegno ----
    a.deflabel('tp_nlbl'); a.stsl_pr()
    for r in (10, 11, 12, 13, 14): a.movl_st_dec(r, 15)
    ld(m, NT, 4); a.mov_imm(100, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 10); a.mov_rm_rn(0, 13)   # r13 centinaia
    a.mov_rm_rn(10, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 11); a.mov_rm_rn(0, 14)  # r14 decine, r11 unita'
    m.imm32(pg.addr['dlabels'][NLBL], 12)
    a.mov_rm_rn(13, 0); a.tst_rr(0, 0); c1 = m.newlabel('c1'); a.bf_lbl(c1); a.mov_imm(ord(' ') - 0x30, 0); a.deflabel(c1)
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(14, 0); ld(m, NT, 2); a.mov_imm(10, 3); a.cmpge(3, 2); c2 = m.newlabel('c2'); a.bt_lbl(c2); a.mov_imm(ord(' ') - 0x30, 0); a.deflabel(c2)
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_rm_rn(11, 0); a.add_imm(0x30, 0); a.movb_st(0, 12)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    for r in (14, 13, 12, 11, 10): a.movl_ld_inc(15, r)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_pos: r0 = posizione della presa nel sample = S0 + (frame passati * SPEED) >> 16. r0-r4 ----
    a.deflabel('tp_pos')
    m.imm32(FRAMES, 2); a.movl_ld(2, 4); ld(m, T0, 0); a.sub(0, 4)                      # r4 = frame passati
    ld(m, SPEED, 0); a.dmulu_l(0, 4); a.sts_mach(1); a.sts_macl(0)
    a.shlr16(0); a.shll16(1); a.or_rr(1, 0)
    ld(m, S0, 4); a.add_rr(4, 0)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_consts: durate in campioni del sample = frame del motore * SPEED ----
    a.deflabel('tp_consts')
    for slot, ms in ((MINTAP, MS_TAP), (WBK, MS_BACK), (WFK, MS_FWD), (PREK, MS_PRE), (GAPK, MS_GAP)):
        m.imm32(ENGINE * ms // 1000, 0); ld(m, SPEED, 2); a.mul_l(2, 0); a.sts_macl(0); a.shlr16(0); st(m, 0, slot)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_speed: SPEED (Q16) = 2^(Tune/1200) del sample SRC, Tune limitato a +-3600 come la voce ----
    a.deflabel('tp_speed'); a.stsl_pr()
    ld(m, SRC, 2); a.add_imm(0x30, 2); a.movw_ld(2, 0)                                     # Tune (s16)
    m.imm32(3600, 1); a.add_rr(1, 0)
    c0 = m.newlabel('c0'); a.cmppz(0); a.bt_lbl(c0); a.mov_imm(0, 0); a.deflabel(c0)
    m.imm32(7200, 1); c1 = m.newlabel('c1'); a.cmpgt(1, 0); a.bf_lbl(c1); a.mov_rm_rn(1, 0); a.deflabel(c1)
    a.mov_rm_rn(0, 4); m.imm32(1200, 5); pg._hcall(m, '__udiv')                           # r0 = ottava 0..6, r1 = resto
    a.mov_rm_rn(1, 4); st(m, 0, SPEED)                                                     # (SPEED = ottava, per ora)
    a.mov_imm(25, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 6)                            # r0 = indice, r6 = frazione
    a.movl_pool('tp_ptab', 2); a.shll2(0); a.add_rr(0, 2); a.movl_ld(2, 7); a.movl_ld_disp(2, 4, 4); a.sub(7, 4)
    a.mul_l(6, 4); a.sts_macl(4); a.mov_imm(25, 5); pg._hcall(m, '__udiv'); a.add_rr(7, 0)   # v = T[i] + d*fr/25
    ld(m, SPEED, 3); a.add_imm(-3, 3); a.shad(3, 0); st(m, 0, SPEED)                       # v * 2^(ottava-3)
    a.ldsl_pr(); a.rts(); a.nop()
    a.pool_here()
    while (len(a.words) * 2 + a.base) & 3: a.nop()
    a.deflabel('tp_ptab')
    for v in PTAB: a._emit(v & 0xFFFF); a._emit(v >> 16)
    # ---- tp_chk: r5 = blocco -> r0 = 0 ok / ERR_NOSMP / ERR_SHORT. r0-r3 ----
    a.deflabel('tp_chk'); nos = m.newlabel('nos'); cok = m.newlabel('cok')
    a.tst_rr(5, 5); a.bt_lbl(nos)
    m.imm32(0x100, 2); a.add_rr(5, 2); a.movl_ld(2, 0)
    m.imm32(MAGIC_ADDON, 3); a.cmpeq(3, 0); a.bt_lbl(nos); m.imm32(MAGIC_HUB, 3); a.cmpeq(3, 0); a.bt_lbl(nos)
    a.movl_ld_disp(5, 0x20, 2); a.movl_ld_disp(5, 0x24, 3); a.sub(2, 3); a.add_imm(1, 3)  # n = end - start + 1
    m.imm32(MIN_N, 2); a.cmpge(2, 3); a.bt_lbl(cok)
    a.mov_imm(ERR_SHORT, 0); a.rts(); a.nop()
    a.deflabel(nos); a.mov_imm(ERR_NOSMP, 0); a.rts(); a.nop()
    a.deflabel(cok); a.mov_imm(0, 0); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_reset: niente presa (tagli e ascolto nella cassetta) ----
    a.deflabel('tp_reset'); a.stsl_pr()
    sti(m, S_IDLE, ST); sti(m, 0, NT)
    a.bsr_lbl('tp_nlbl'); a.nop(); a.mov_imm(1, 4); a.bsr_lbl('tp_pub'); a.nop(); a.mov_imm(-2, 4); a.bsr_lbl('tp_play'); a.nop()
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_endtake: stop, aggancio agli attacchi (SNAP On), "Keep? DO IT=yes" ----
    a.deflabel('tp_endtake'); a.stsl_pr()
    stop(m)
    ld(m, SNAP, 0); a.extu_b(0, 0); a.tst_rr(0, 0); ns = m.newlabel('ns'); m.bf_far(ns)  # 0 = On
    ld(m, NT, 0); a.mov_imm(2, 2); a.cmpge(2, 0); m.bf_far(ns)
    a.bsr_lbl('tp_icinv'); a.nop(); cached_call(m, 'tp_snap')
    a.deflabel(ns)
    sti(m, S_DONE, ST)
    a.mov_imm(1, 4); a.bsr_lbl('tp_pub'); a.nop(); a.mov_imm(-2, 4); a.bsr_lbl('tp_play'); a.nop()
    pg.emit_message(m, "Keep? DO IT=yes"); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_icinv: invalida le righe di cache di tp_snap (il codice e' arrivato via P2; vedi Trans-Chop) ----
    a.deflabel('tp_icinv')
    a.movl_pool('tp_snap', 4); m.imm32(0xFFFFFFF0, 1); a.and_rr(1, 4)
    m.imm32(SNAP_SPAN // 16 + 1, 5); m.imm32(0xF0000008, 6); m.imm32(0x1FFFFC00, 7); m.imm32(0xFF0, 3)
    icl = m.newlabel('icl'); a.deflabel(icl)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0); a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(icl)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_snap (da P1): per ogni taglio k >= 1, finestra [tocco - BACK, tocco + FWD] (dopo la fetta precedente + GAP,
    #      entro end): blocchi di HOP campioni (canale L), picco P, salita P - min(dei 2 precedenti); la salita migliore,
    #      se >= MINRISE e >= meta' del picco massimo, sposta il taglio sul primo |x| >= picco/4 da un blocco prima, meno
    #      PRE (se resta dopo la fetta precedente + GAP). Registri r8-r14 salvati. ----
    a.deflabel('tp_snap')
    for r in (8, 9, 10, 11, 12, 13, 14): a.movl_st_dec(r, 15)
    ld(m, SRC, 8); m.imm32(0x100, 0); a.add_rr(0, 8)                                      # r8 = &L[0]
    sti(m, 1, K)
    kl = m.newlabel('kl'); kx = m.newlabel('kx'); kn = m.newlabel('kn'); a.deflabel(kl)
    ld(m, K, 0); ld(m, NT, 2); a.cmpge(2, 0); m.bt_far(kx)                               # k >= NT -> fine
    m.imm32(CUT, 7); a.shll2(0); a.add_rr(0, 7); a.movl_ld(7, 4)                         # r4 = tocco
    a.mov_rm_rn(7, 0); a.add_imm(-4, 0); a.movl_ld(0, 5); ld(m, GAPK, 0); a.add_rr(0, 5)  # r5 = fetta prec. + GAP
    ld(m, WBK, 0); a.mov_rm_rn(4, 9); a.sub(0, 9)                                         # r9 = da
    c1 = m.newlabel('c1'); a.cmpgt(9, 5); a.bf_lbl(c1); a.mov_rm_rn(5, 9); a.deflabel(c1)
    ld(m, WFK, 0); a.mov_rm_rn(4, 10); a.add_rr(0, 10)                                    # r10 = a
    ld(m, E0, 0); c2 = m.newlabel('c2'); a.cmpgt(0, 10); a.bf_lbl(c2); a.mov_rm_rn(0, 10); a.deflabel(c2)
    a.mov_rm_rn(10, 0); a.sub(9, 0); a.mov_imm(2 * HOP, 1); a.cmpge(1, 0); m.bf_far(kn)
    a.mov_imm(0, 11); a.mov_rm_rn(9, 12); a.mov_imm(0, 13); a.mov_imm(0, 14)             # migliore, suo blocco, suo picco, max
    m.imm32(0x10000, 6); a.mov_rm_rn(6, 3)                                                # p1 = p2 = "nessuno"
    bl = m.newlabel('bl'); bx = m.newlabel('bx'); a.deflabel(bl)
    a.mov_rm_rn(9, 0); a.add_imm(HOP, 0); a.cmpgt(10, 0); a.bt_lbl(bx)
    a.mov_rm_rn(9, 1); a.shll(1); a.add_rr(8, 1); a.mov_imm(HOP, 2); a.mov_imm(0, 7)       # r7 = picco del blocco
    il = m.newlabel('il'); a.deflabel(il)
    a.movw_ld(1, 0); a.add_imm(2, 1); absr0(m); a.cmpgt(7, 0); p_ = m.newlabel('p'); a.bf_lbl(p_); a.mov_rm_rn(0, 7); a.deflabel(p_)
    a.dt(2); a.bf_lbl(il)
    a.mov_rm_rn(6, 0); q = m.newlabel('q'); a.cmpgt(3, 0); a.bf_lbl(q); a.mov_rm_rn(3, 0); a.deflabel(q)   # r0 = min(p1, p2)
    a.neg(0, 0); a.add_rr(7, 0)                                                           # salita
    w = m.newlabel('w'); a.cmpgt(11, 0); a.bf_lbl(w); a.mov_rm_rn(0, 11); a.mov_rm_rn(9, 12); a.mov_rm_rn(7, 13); a.deflabel(w)
    w2 = m.newlabel('w2'); a.cmpgt(14, 7); a.bf_lbl(w2); a.mov_rm_rn(7, 14); a.deflabel(w2)
    a.mov_rm_rn(6, 3); a.mov_rm_rn(7, 6); a.add_imm(HOP, 9); a.bra_lbl(bl); a.nop()
    a.deflabel(bx)
    m.imm32(MINRISE, 0); a.cmpge(0, 11); m.bf_far(kn)                                     # attacco chiaro?
    a.mov_rm_rn(14, 0); a.shlr(0); a.cmpge(0, 11); m.bf_far(kn)
    a.shlr2(13)                                                                           # r13 = soglia (picco/4)
    a.mov_rm_rn(12, 9); a.add_imm(-HOP, 9); c3 = m.newlabel('c3'); a.cmpgt(9, 5); a.bf_lbl(c3); a.mov_rm_rn(5, 9); a.deflabel(c3)
    a.mov_rm_rn(12, 11); a.mov_imm(2 * HOP, 2); a.mov_rm_rn(9, 1); a.shll(1); a.add_rr(8, 1)
    sl = m.newlabel('sl'); sf = m.newlabel('sf'); sx = m.newlabel('sx'); a.deflabel(sl)
    a.movw_ld(1, 0); a.add_imm(2, 1); absr0(m); a.cmpge(13, 0); a.bt_lbl(sf)
    a.add_imm(1, 9); a.dt(2); a.bf_lbl(sl); a.bra_lbl(sx); a.nop()
    a.deflabel(sf); a.mov_rm_rn(9, 11)
    a.deflabel(sx); ld(m, PREK, 0); a.sub(0, 11)                                           # un filo prima
    a.cmpgt(5, 11); m.bf_far(kn)
    ld(m, K, 0); a.shll2(0); m.imm32(CUT, 7); a.add_rr(0, 7); a.movl_st(11, 7)
    a.deflabel(kn); ld(m, K, 0); a.add_imm(1, 0); st(m, 0, K); a.bra_lbl(kl); a.nop()
    a.deflabel(kx)
    for r in (14, 13, 12, 11, 10, 9, 8): a.movl_ld_inc(15, r)
    a.rts(); a.nop()
    a.pool_here()
    a.deflabel('tp_snapend')
    a.deflabel('tp_fx')
pg.on_fire(fire)

def inactive(m):
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    sti(m, 0, ACTIVE); sti(m, 0, OUTCNT)
    ld(m, ST, 0); a.tst_rr(0, 0); ni = m.newlabel('ni'); a.bt_lbl(ni); stop(m); a.deflabel(ni)  # uscita durante una presa
    sti(m, S_IDLE, ST); sti(m, 0, NT)
    a.mov_imm(0, 4); far_call(m, 'tp_pub')                                               # schermo remoto: niente tagli
    a.deflabel(ix)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    a = m.a
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(0, 9)   # r9 = pad
    a.mov_imm(MAXPAD, 0); a.cmphs(0, 9); pok = m.newlabel('pok'); a.bf_lbl(pok); far_bra(m, hret1); a.deflabel(pok)
    ld(m, ST, 10)
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); m.bf_far(on)
    # rilascio: durante la presa (e in attesa) niente, il sample continua; altrimenti fine dell'ascolto
    a.mov_imm(S_ARM, 0); a.cmpeq(0, 10); m.bt_far(hret1); a.mov_imm(S_TAKE, 0); a.cmpeq(0, 10); m.bt_far(hret1)
    stop(m); a.mov_imm(-2, 4); far_call(m, 'tp_play'); far_bra(m, hret1)
    a.deflabel(on)
    a.mov_imm(S_ARM, 0); a.cmpeq(0, 10); first = m.newlabel('first'); m.bt_far(first)
    a.mov_imm(S_TAKE, 0); a.cmpeq(0, 10); tap = m.newlabel('tap'); m.bt_far(tap)
    # ---- fuori dalla presa: il pad suona la sua fetta (l'ultima con quel pad); senza presa il sample intero ----
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); ld(m, SRC, 0); a.cmpeq(0, 5); whole = m.newlabel('whole'); m.bf_far(whole)
    ld(m, NT, 3); m.imm32(PADS, 6); a.mov_rm_rn(3, 2); a.shll2(2); a.add_rr(2, 6)
    fl = m.newlabel('fl'); fnd = m.newlabel('fnd'); a.deflabel(fl)
    a.tst_rr(3, 3); m.bt_far(whole); a.add_imm(-1, 3); a.add_imm(-4, 6); a.movl_ld(6, 0); a.cmpeq(9, 0); a.bt_lbl(fnd); a.bra_lbl(fl); a.nop()
    a.deflabel(fnd)                                                                       # r3 = fetta
    m.imm32(CUT, 2); a.mov_rm_rn(3, 0); a.shll2(0); a.add_rr(0, 2); a.movl_ld(2, 6)       # r6 = start = CUT[k]
    a.mov_rm_rn(3, 0); a.add_imm(1, 0); ld(m, NT, 1); a.cmpge(1, 0); lst = m.newlabel('lst'); ld(m, E0, 7); a.bt_lbl(lst)
    a.movl_ld_disp(2, 4, 7); a.add_imm(-1, 7)                                              # r7 = end = CUT[k+1]-1
    a.deflabel(lst)
    a.mov_rm_rn(3, 4); far_call(m, 'tp_play')                                             # schermo remoto: fetta (r6, r7 restano)
    ld(m, SRC, 5); far_call(m, 'tp_aud')
    far_bra(m, hret1)
    m.pool_jump()
    a.deflabel(whole)
    ld(m, NT, 0); a.tst_rr(0, 0); m.bf_far(hret1)                                         # presa su un altro sample: niente
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); a.tst_rr(5, 5); m.bt_far(hret1)
    a.movl_ld_disp(5, 0x20, 6); a.movl_ld_disp(5, 0x24, 7); a.mov_imm(-1, 4); far_call(m, 'tp_play')
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); a.movl_ld_disp(5, 0x20, 6); a.movl_ld_disp(5, 0x24, 7); far_call(m, 'tp_aud')
    far_bra(m, hret1)
    m.pool_jump()
    # ---- primo pad: parte il sample intero, fetta 1 dall'inizio su questo pad ----
    a.deflabel(first)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); far_call(m, 'tp_chk'); a.tst_rr(0, 0); fok = m.newlabel('fok'); m.bt_far(fok)
    sti(m, S_IDLE, ST); sti(m, 1, SHOWMSG); far_bra(m, hret1)                             # sample cambiato e non valido
    a.deflabel(fok)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); st(m, 5, SRC)
    a.movl_ld_disp(5, 0x20, 6); st(m, 6, S0); a.movl_ld_disp(5, 0x24, 7); st(m, 7, E0)
    far_call(m, 'tp_aud')                                                                 # start..end, loop spento
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, T0)
    far_call(m, 'tp_speed')                                                               # velocita' dal Tune (dopo il play)
    ld(m, S0, 0); m.imm32(CUT, 1); a.movl_st(0, 1); m.imm32(PADS, 1); a.movl_st(9, 1)
    sti(m, 1, NT); sti(m, S_TAKE, ST)
    far_call(m, 'tp_consts'); far_call(m, 'tp_nlbl'); a.mov_imm(1, 4); far_call(m, 'tp_pub')
    a.mov_imm(-1, 4); ld(m, S0, 6); ld(m, E0, 7); far_call(m, 'tp_play')                # schermo remoto: sample intero
    far_bra(m, hret1)
    m.pool_jump()
    # ---- presa: taglio qui, fetta da qui sul pad colpito ----
    a.deflabel(tap)
    far_call(m, 'tp_pos')                                                                 # r0 = posizione
    ld(m, E0, 2); a.cmpgt(2, 0); m.bt_far(hret1)                                          # oltre la fine: la chiude il fire
    ld(m, NT, 3); a.mov_imm(MAXSL, 2); a.cmpge(2, 3); m.bt_far(hret1)
    m.imm32(CUT, 6); a.mov_rm_rn(3, 2); a.shll2(2); a.add_rr(2, 6)                       # r6 = &CUT[NT]
    a.mov_rm_rn(6, 2); a.add_imm(-4, 2); a.movl_ld(2, 2); ld(m, MINTAP, 7); a.add_rr(7, 2)
    a.cmpgt(2, 0); m.bf_far(hret1)                                                        # troppo vicino al tocco prima
    a.movl_st(0, 6)
    m.imm32(PADS, 6); a.mov_rm_rn(3, 2); a.shll2(2); a.add_rr(2, 6); a.movl_st(9, 6)
    a.add_imm(1, 3); st(m, 3, NT)
    far_call(m, 'tp_nlbl'); a.mov_imm(1, 4); far_call(m, 'tp_pub')
    far_bra(m, hret1)
    m.pool_jump()
pg.on_event(EV_PAD, ev_pad)

def on_cancel(m, hret1):
    a = m.a
    ld(m, ST, 0); a.tst_rr(0, 0); cx = m.newlabel('cx'); m.bt_far(cx)
    a.mov_imm(S_COMMIT, 3); a.cmpeq(3, 0); ncm = m.newlabel('ncm'); m.bf_far(ncm)
    sti(m, S_IDLE, ST); pg.emit_message_dec(m, "Stopped, ", CNT, " kept"); far_bra(m, hret1)   # le fette fatte restano
    a.deflabel(ncm)
    stop(m); far_call(m, 'tp_reset'); sti(m, 1, SHOWMSG); far_bra(m, hret1)
    a.deflabel(cx)
pg.cancel_fn = on_cancel


def doit(m):
    a = m.a
    ld(m, ST, 9)
    a.mov_imm(S_TAKE, 0); a.cmpeq(0, 9); ntk = m.newlabel('ntk'); m.bf_far(ntk)
    far_call(m, 'tp_endtake'); far_bra(m, 'tp_dret')                                    # DO IT durante la presa: fine
    a.deflabel(ntk)
    a.mov_imm(S_ARM, 0); a.cmpeq(0, 9); m.bt_far('tp_dret')                              # in attesa del primo pad
    a.mov_imm(S_COMMIT, 0); a.cmpeq(0, 9); m.bt_far('tp_dret')                           # Extract in corso
    a.mov_imm(S_DONE, 0); a.cmpeq(0, 9); ndn = m.newlabel('ndn'); m.bf_far(ndn)
    far_call(m, 'tp_cstart'); far_bra(m, 'tp_dret')
    m.pool_jump()
    a.deflabel(ndn)                                                                       # fermo -> in attesa del primo pad
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); far_call(m, 'tp_chk'); a.mov_rm_rn(0, 8); a.tst_rr(8, 8); dok = m.newlabel('dok'); m.bt_far(dok)
    a.mov_imm(ERR_NOSMP, 3); a.cmpeq(3, 8); dsh = m.newlabel('dsh'); m.bf_far(dsh)
    pg.emit_message(m, "No sample"); far_bra(m, 'tp_dret')
    a.deflabel(dsh); pg.emit_message(m, "Sample too short"); far_bra(m, 'tp_dret')
    m.pool_jump()
    a.deflabel(dok)
    sti(m, S_ARM, ST); sti(m, 0, NT); far_call(m, 'tp_nlbl'); a.mov_imm(1, 4); far_call(m, 'tp_pub')
    pg.emit_message(m, "Tap pads to chop"); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)
    far_bra(m, 'tp_dret')
    m.pool_jump()
    # ---- tp_cstart: DO IT su "Keep?" -> S_COMMIT; le Extract le fa tp_cstep, una per fire ----
    a.deflabel('tp_cstart'); a.stsl_pr()
    sti(m, 0, ERR); sti(m, 0, CNT); sti(m, 0, K)
    ld(m, SRC, 8); a.movl_ld_disp(8, 0x20, 0); st(m, 0, SV20); a.movl_ld_disp(8, 0x24, 0); st(m, 0, SV24)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 2); a.extu_w(2, 2); m.imm32(PROG_STRIDE, 3); a.mul_l(3, 2); a.sts_macl(2); m.imm32(PROG_BASE, 3); a.add_rr(3, 2); st(m, 2, PROG)
    sti(m, S_COMMIT, ST); a.bsr_lbl('tp_prog'); a.nop()
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_prog: "Slicing kk/NN" (kk = K+1) fisso fino alla fine ----
    a.deflabel('tp_prog'); a.stsl_pr()
    m.imm32(pg.addr['msgt'], 12)
    def text(t):
        for ch in t: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    def dec2():
        a.mov_imm(10, 5); pg._hcall(m, '__udiv')
        a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    text("Slicing "); ld(m, K, 4); a.add_imm(1, 4); dec2(); text("/"); ld(m, NT, 4); dec2(); text("   ")
    a.mov_imm(0, 0); a.movb_st(0, 12)
    pg._hcall(m, '__showmsg'); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- tp_cstep (fire, S_COMMIT): la prossima fetta da estrarre (salta quelle il cui pad torna dopo e quelle con
    #      fine < inizio), Extract sul suo pad, originale subito rimesso; alla fine "Done: NN slices" ----
    a.deflabel('tp_cstep'); a.stsl_pr()
    cl = m.newlabel('cl'); cend = m.newlabel('cend'); cnx = m.newlabel('cnx'); cret = m.newlabel('cret'); a.deflabel(cl)
    ld(m, K, 9); ld(m, NT, 2); a.cmpge(2, 9); m.bt_far(cend)
    m.imm32(PADS, 3); a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(0, 3); a.movl_ld(3, 10)    # r10 = pad
    a.mov_rm_rn(9, 11); dl = m.newlabel('dl'); dx = m.newlabel('dx'); a.deflabel(dl)
    a.add_imm(1, 11); a.cmpge(2, 11); a.bt_lbl(dx); a.add_imm(4, 3); a.movl_ld(3, 0); a.cmpeq(10, 0); m.bt_far(cnx); a.bra_lbl(dl); a.nop()
    a.deflabel(dx)
    m.imm32(CUT, 3); a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(0, 3); a.movl_ld(3, 11)      # start = CUT[k]
    a.mov_rm_rn(9, 0); a.add_imm(1, 0); a.cmpge(2, 0); ce = m.newlabel('ce'); ld(m, E0, 12); a.bt_lbl(ce)
    a.movl_ld_disp(3, 4, 12); a.add_imm(-1, 12)                                           # end = CUT[k+1]-1
    a.deflabel(ce)
    a.cmpgt(12, 11); m.bt_far(cnx)                                                        # fine < inizio: si salta
    ld(m, SRC, 8); a.movl_st_disp(11, 0x20, 8); a.movl_st_disp(12, 0x24, 8)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4)
    m.os_extract(8)
    a.mov_rm_rn(0, 13); m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 9)                          # r13 = esito, r9 = sample nuovo
    ld(m, SRC, 8); ld(m, SV20, 2); a.movl_st_disp(2, 0x20, 8); ld(m, SV24, 2); a.movl_st_disp(2, 0x24, 8)
    m.imm32(MGR, 4); a.movl_st_disp(8, 8, 4)                                              # originale rimesso
    a.tst_rr(13, 13); xok = m.newlabel('xok'); a.bt_lbl(xok); st(m, 13, ERR); far_bra(m, cend); a.deflabel(xok)
    a.mov_rm_rn(9, 8); a.add_imm(4, 8)                                                    # nome del sample nuovo
    a.mov_rm_rn(10, 2); m.imm32(0xA4, 3); a.mul_l(3, 2); a.sts_macl(2); ld(m, PROG, 3); a.add_rr(3, 2); a.add_imm(0x18, 2)
    a.mov_rm_rn(2, 12); a.mov_imm(16, 3)
    cp = m.newlabel('cp'); a.deflabel(cp); a.movb_ldinc(8, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.dt(3); a.bf_lbl(cp)
    a.mov_rm_rn(2, 12); a.add_imm(0x10, 12); a.mov_imm(0, 0); a.movb_st_disp(0, 12)
    a.mov_imm(100, 0); a.movb_st_disp(1, 12); a.mov_imm(0, 0); a.movb_st_disp(2, 12); a.mov_imm(0x7F, 0); a.movb_st_disp(3, 12)
    a.mov_imm(0, 0); a.movb_st_disp(4, 12); a.movb_st_disp(5, 12); a.movb_st_disp(6, 12); a.movb_st_disp(7, 12)
    a.mov_rm_rn(2, 12); a.add_imm(0x60, 12)
    a.mov_imm(CHOP_MONO, 0); a.movb_st_disp(2, 12); a.mov_imm(CHOP_MUTE, 0); a.movb_st_disp(3, 12)   # +0x62 Mono, +0x63 gruppo
    ld(m, CNT, 0); a.add_imm(1, 0); st(m, 0, CNT)
    ld(m, K, 9); a.add_imm(1, 9); st(m, 9, K); ld(m, NT, 2); a.cmpge(2, 9); m.bt_far(cend)
    a.bsr_lbl('tp_prog'); a.nop(); far_bra(m, cret)                                       # una sola Extract per fire
    m.pool_jump()
    a.deflabel(cnx); ld(m, K, 0); a.add_imm(1, 0); st(m, 0, K); far_bra(m, cl)
    m.pool_jump()
    a.deflabel(cend); sti(m, S_IDLE, ST)
    ld(m, ERR, 8); a.tst_rr(8, 8); cme = m.newlabel('cme'); m.bf_far(cme)
    pg.emit_message_dec(m, "Done: ", CNT, " slices"); far_bra(m, cret)
    a.deflabel(cme); a.mov_imm(3, 3); a.cmpeq(3, 8); cmf = m.newlabel('cmf'); m.bf_far(cmf)
    pg.emit_message(m, "Memory full"); far_bra(m, cret)
    a.deflabel(cmf); pg.emit_message_dec(m, "Error ", ERR, "")
    a.deflabel(cret); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('tp_dret')
pg.on_doit(doit)


# ---------------- modello Python di riferimento ----------------
def ref_speed(tune):
    c = max(0, min(7200, tune + 3600)); oc, rem = divmod(c, 1200); i, fr = divmod(rem, 25)
    return ((PTAB[i] + (PTAB[i + 1] - PTAB[i]) * fr // 25) << oc) >> 3

def ref_pos(s0, frames, sp): return s0 + ((frames * sp) >> 16)

def ref_snap(L, cuts, e0, sp=65536):
    ms = lambda v: ((ENGINE * v // 1000) * sp & 0xFFFFFFFF) >> 16
    WB, WF, PRE, GAP = ms(MS_BACK), ms(MS_FWD), ms(MS_PRE), ms(MS_GAP)
    out = list(cuts)
    for k in range(1, len(out)):
        t = out[k]; prevg = out[k - 1] + GAP
        lo = max(t - WB, prevg); hi = min(t + WF, e0)
        if hi - lo < 2 * HOP: continue
        best, bestb, bestp, mx, p1, p2, b = 0, lo, 0, 0, 0x10000, 0x10000, lo
        while b + HOP <= hi:
            P = max(abs(L[i]) for i in range(b, b + HOP)); r = P - min(p1, p2)
            if r > best: best, bestb, bestp = r, b, P
            mx = max(mx, P); p2, p1 = p1, P; b += HOP
        if best < MINRISE or best < mx // 2: continue
        s = max(bestb - HOP, prevg); o = bestb
        for i in range(s, s + 2 * HOP):
            if abs(L[i]) >= bestp >> 2: o = i; break
        if o - PRE > prevg: out[k] = o - PRE
    return out

def ref_commit(cuts, pads, e0):
    """[(pad, start, end)] nell'ordine delle Extract: non si estraggono le fette il cui pad torna dopo e quelle con fine < inizio"""
    n = len(cuts)
    sl = [(pads[k], cuts[k], cuts[k + 1] - 1 if k + 1 < n else e0) for k in range(n) if pads[k] not in pads[k + 1:]]
    return [x for x in sl if x[2] >= x[1]]                                                # fine < inizio: saltata


def selftest(out):
    """test in emulatore sul WAV 'out' (TAPCHOP.WAV, o CHOPSHOP.WAV con questa pagina ospitata)"""
    import math, random
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE
    from pages import SET_FOCUS
    from shasm import SHAsm
    LOG, MSG = 0x88E00000, 0x88E10000     # LOG: +0 n extract, +4.. (start,end); +0x100 n play, +0x104 start, +0x108 end, +0x10C loop, +0x110 n stop,
                                          #      +0x114 parola +0x30 (Tune), +0x118 +0x3E: come li vede il lettore
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
                              s.movl_ld_disp(5, 0x2C, 0), s.movl_st_disp(0, 12, 1), s.movl_ld_disp(5, 0x30, 0), s.movl_st_disp(0, 0x14, 1),
                              s.mov_rm_rn(5, 2), s.add_imm(0x3E, 2), s.movw_ld(2, 0), s.extu_w(0, 0), s.movl_st_disp(0, 0x18, 1), s.rts(), s.nop()))
    stub(AUD_STOP, lambda s: (s.movl_pool(LOG + 0x110, 1), s.movl_ld(1, 0), s.add_imm(1, 0), s.movl_st(0, 1), s.rts(), s.nop()))
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato"
    def call(obj, type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 50_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    def fire(n=1): return h.fire(n, maxsteps=50_000_000)
    DOIT, CANCEL = (2, 0x2E), (2, 0x2D)
    def key(k): return call(pg.page, k[0], code=k[1])
    def pad(code, bank=0, down=True): e.wr(BANKOBJ, bank, 1); return call(pg.page, EV_PAD, code=code, val=100 if down else 0)
    # sample: 2 s a 44100, colpi (rumore che decade) a posti noti + coda bassa (niente attacchi)
    rate = 44100; n = 2 * rate; rnd = random.Random(5); L = [0] * n
    hits = [11025, 22050, 33075, 44100, 55125, 66150]
    for at in hits:
        for i in range(min(4000, n - at)): L[at + i] = max(-32768, min(32767, L[at + i] + int(20000 * math.exp(-i / 600.0) * rnd.uniform(-1, 1))))
    for i in range(70000, n): L[i] = int(300 * math.sin(i / 30.0))
    s0, e0 = 0, n - 1
    blk = h.list_end(); size = (0x100 + 2 * n + 0x200 + 3) & ~3
    h.ram_w32(blk, size); [h.ram_w8(blk + 4 + i, c) for i, c in enumerate(b'TAPLOOP'.ljust(16, b'\0'))]
    h.ram_w32(blk + 0x18, n); h.ram_w32(blk + 0x1C, n); h.ram_w32(blk + 0x20, s0); h.ram_w32(blk + 0x24, e0); h.ram_w32(blk + 0x2C, 1); h.ram_w32(blk + 0x94, rate); h.ram_w16(blk + 0x92, 1)
    for i, v in enumerate(L): h.ram_w16(blk + 0x100 + 2 * i, v & 0xffff)
    h.ram_w32(blk + 0x30, 0x12340000); h.ram_w16(blk + 0x3E, 4)      # Tune 0 (+0x32 = altro campo) e +0x3E != 0 (WAV da pack)
    h.ram_w32(blk + size, 0); h.ram_w32(MGR + 8, blk)
    for k in range(0, 0x120, 4): h.ram_w32(LOG + k, 0)
    def plain(): return h.ram_r32(LOG + 0x114) == 0x12340000 and h.ram_r32(LOG + 0x118) == 0      # al play: Tune com'e', +0x3E 0
    def restored(): return h.ram_r32(blk + 0x30) == 0x12340000 and e.rd(blk + 0x3E, 2) == 4 and (h.ram_r32(blk + 0x20), h.ram_r32(blk + 0x24), h.ram_r32(blk + 0x2C)) == (s0, e0, 1)
    lbl = lambda: bytes(e.rd(pg.dlabels[NLBL] + i, 1) for i in range(3)).decode()
    msg = lambda: bytes(e.rd(pg.msgt + i, 1) for i in range(16))
    cuts = lambda: [h.ram_r32(CUT + 4 * i) for i in range(h.ram_r32(NT))]
    pads = lambda: [h.ram_r32(PADS + 4 * i) for i in range(h.ram_r32(NT))]
    MB = h.ram_r32(MGR + 4) + MBOX_OFF; MP = MB + MBOX_PLAY
    h.ram_w32(EVQ + 0x68, pg.page); fire(3)
    assert h.ram_r32(ST) == S_IDLE and lbl() == '  0' and h.ram_r32(MB + 0x14) == 1 and h.ram_r32(MB + 0x10) == 0
    # pad senza presa: sample intero; rilascio = stop
    assert pad(5) == 1 and h.ram_r32(LOG + 0x100) == 1 and (h.ram_r32(LOG + 0x104), h.ram_r32(LOG + 0x108)) == (s0, e0) and plain() and restored()
    assert pad(5, down=False) == 1 and h.ram_r32(LOG + 0x110) == 1 and h.ram_r32(MP) == 0
    print("  ok pagina: N = 0, pad = sample intero (+0x3E 4 a 0 solo durante il play), rilascio = stop")
    # DO IT -> in attesa; primo pad -> parte il sample (loop spento solo per il play)
    F0 = 0x7FFFF000; h.ram_w32(FRAMES, F0)                       # vicino al giro del contatore a 32 bit (passa durante la presa)
    assert key(DOIT) == 1 and h.ram_r32(ST) == S_ARM and msg() == b'Tap pads to chop'
    np0 = h.ram_r32(LOG + 0x100)
    assert pad(0) == 1 and h.ram_r32(ST) == S_TAKE and h.ram_r32(LOG + 0x100) == np0 + 1
    assert (h.ram_r32(LOG + 0x104), h.ram_r32(LOG + 0x108), h.ram_r32(LOG + 0x10C)) == (s0, e0, 0) and plain() and restored()
    assert cuts() == [s0] and pads() == [0] and lbl() == '  1' and h.ram_r32(MP) == 1 and h.ram_r32(MP + 4) == 0xFFFFFFFF
    assert pad(0, down=False) == 1 and h.ram_r32(LOG + 0x110) == 1, "il rilascio non deve fermare la presa"
    # tocchi: un po' in ritardo / in anticipo rispetto ai colpi, banchi diversi, uno troppo vicino, lo stesso pad due volte
    taps = [(hits[0] + 700, 3, 0), (hits[0] + 1000, 4, 0), (hits[1] - 1200, 2, 1), (hits[2] + 300, 3, 0), (hits[3] - 50, 11, 3), (hits[5] + 400, 7, 2), (80000, 8, 0)]   # niente tocco su hits[4]
    for at, code, bank in taps:
        h.ram_w32(FRAMES, (F0 + at) & 0xFFFFFFFF); fire(1); assert pad(code, bank) == 1 and h.ram_r32(ST) == S_TAKE
    want_taps = [s0, hits[0] + 700, hits[1] - 1200, hits[2] + 300, hits[3] - 50, hits[5] + 400, 80000]
    want_pads = [0, 3, 14, 3, 47, 31, 8]
    assert cuts() == want_taps and pads() == want_pads and lbl() == '  7', (cuts(), pads(), lbl())
    MBc = lambda: [h.ram_r32(MB + 0x20 + 4 * i) for i in range(h.ram_r32(MB + 0x10))]
    assert MBc() == want_taps and h.ram_r32(MB + 0xC) == blk
    print("  ok presa: 7 tocchi (uno a 7 ms dal precedente ignorato), pad = banco*12 + pad, cassetta aggiornata a ogni tocco")
    # fine del sample -> fine presa, aggancio agli attacchi
    h.ram_w32(FRAMES, (F0 + n + 10) & 0xFFFFFFFF); fire(1)
    snapped = ref_snap(L, want_taps, e0)
    assert h.ram_r32(ST) == S_DONE and msg() == b'Keep? DO IT=yes ' and cuts() == snapped, (cuts(), snapped)
    pre = ENGINE * MS_PRE // 1000
    for k, hit_ in ((1, hits[0]), (2, hits[1]), (3, hits[2]), (4, hits[3]), (5, hits[5])): assert abs(snapped[k] - (hit_ - pre)) <= HOP, (k, snapped[k], hit_)
    assert snapped[6] == want_taps[6], "senza un attacco chiaro il tocco resta"
    assert MBc() == snapped and h.ram_r32(MP) == 0
    print("  ok fine sample -> 'Keep? DO IT=yes', tagli agganciati ai colpi (%s), coda senza attacchi intatta"
          % ', '.join('%+d' % (c - (hh - pre)) for c, hh in zip(snapped[1:6], hits[:4] + hits[5:])))
    # ascolto: pad 3 (banco A) = la sua ULTIMA fetta (k=3); pad 47 (banco D, 12) = fetta 4
    assert pad(3) == 1 and (h.ram_r32(LOG + 0x104), h.ram_r32(LOG + 0x108), h.ram_r32(LOG + 0x10C)) == (snapped[3], snapped[4] - 1, 0)
    assert [h.ram_r32(MP + 4 * i) for i in range(4)] == [1, 3, snapped[3], snapped[4] - 1]
    assert plain() and restored(), "fetta: +0x3E a 0 solo durante il play, poi start/end/loop/+0x3E ripristinati"
    assert pad(3, down=False) == 1 and h.ram_r32(MP) == 0
    assert pad(11, bank=3) == 1 and (h.ram_r32(LOG + 0x104), h.ram_r32(LOG + 0x108)) == (snapped[4], snapped[5] - 1)
    np1 = h.ram_r32(LOG + 0x100); assert pad(9) == 1 and h.ram_r32(LOG + 0x100) == np1, "pad senza fetta: niente"
    print("  ok ascolto: ogni pad la sua fetta (pad doppio = l'ultima), pad senza fetta muto, originale ripristinato")
    # DO IT = Extract sui pad colpiti, una fetta per fire con "Slicing kk/NN"; tra una fetta e l'altra l'originale e' com'era
    want = ref_commit(snapped, want_pads, e0); nt = len(snapped)
    assert key(DOIT) == 1 and h.ram_r32(ST) == S_COMMIT and h.ram_r32(LOG) == 0 and msg() == b'Slicing 01/%02d   ' % nt, msg()
    steps = 0
    while h.ram_r32(ST) == S_COMMIT:
        n0 = h.ram_r32(LOG); fire(1); steps += 1
        assert h.ram_r32(LOG) - n0 <= 1 and steps <= nt, "una sola Extract per fire"
        assert (h.ram_r32(blk + 0x20), h.ram_r32(blk + 0x24), h.ram_r32(MGR + 8)) == (s0, e0, blk), "originale non rimesso tra le fette"
        if h.ram_r32(ST) == S_COMMIT:
            assert msg() == b'Slicing %02d/%02d   ' % (h.ram_r32(K) + 1, nt), msg()
            if steps == 1: assert key(DOIT) == 1 and h.ram_r32(ST) == S_COMMIT and h.ram_r32(LOG) == 1, "DO IT durante l'Extract: ignorato"
    assert steps == len(want), (steps, len(want))
    ne = h.ram_r32(LOG); got = [(h.ram_r32(LOG + 4 + 8 * i), h.ram_r32(LOG + 8 + 8 * i)) for i in range(ne)]
    assert got == [(s, en) for _, s, en in want], (got, want)
    pidx = e.rd(PROG_IDX, 2); pbase = PROG_BASE + pidx * PROG_STRIDE + 0x18
    for i, (p, _, _) in enumerate(want):
        blkp = pbase + p * 0xA4
        assert bytes(e.rd(blkp + j, 1) for j in range(3)) == b'SL' + bytes([0x41 + i]), (i, p)
        assert e.rd(blkp + 0x11, 1) == 100 and e.rd(blkp + 0x62, 1) == CHOP_MONO and e.rd(blkp + 0x63, 1) == CHOP_MUTE
    assert (h.ram_r32(blk + 0x20), h.ram_r32(blk + 0x24), h.ram_r32(MGR + 8)) == (s0, e0, blk), "originale non ripristinato"
    assert msg().startswith(b'Done: %02d slices' % ne), msg()
    print("  ok DO IT: %d Extract, una per fire con 'Slicing kk/%02d' (la fetta 2 sul pad 4 scartata: il pad 4 torna dopo) sui pad %s, %r"
          % (ne, nt, [p + 1 for p, _, _ in want], msg().decode()))
    # CANCEL durante la presa, DO IT che la chiude prima della fine, SNAP Off
    for k in range(0, 0x120, 4): h.ram_w32(LOG + k, 0)
    assert key(DOIT) == 1 and h.ram_r32(ST) == S_ARM and lbl() == '  0'
    assert key(CANCEL) == 1 and h.ram_r32(ST) == S_IDLE
    fire(2); assert not (e.rd(pg.msgw + 0x17, 1) & 0x80), "il messaggio deve sparire"
    e.wr(SNAP, 1, 1); h.ram_w32(FRAMES, F0)
    assert key(DOIT) == 1 and pad(1) == 1 and h.ram_r32(ST) == S_TAKE
    h.ram_w32(FRAMES, (F0 + hits[0] + 700) & 0xFFFFFFFF); assert pad(2) == 1
    assert key(CANCEL) == 1 and h.ram_r32(ST) == S_IDLE and h.ram_r32(LOG + 0x110) >= 1 and h.ram_r32(NT) == 0
    h.ram_w32(FRAMES, F0); assert key(DOIT) == 1 and pad(1) == 1
    h.ram_w32(FRAMES, (F0 + hits[0] + 700) & 0xFFFFFFFF); assert pad(2) == 1
    assert key(DOIT) == 1 and h.ram_r32(ST) == S_DONE and cuts() == [s0, hits[0] + 700], cuts()
    print("  ok CANCEL annulla la presa, DO IT la chiude prima della fine, SNAP Off = tagli dove hai toccato")
    # CANCEL durante l'Extract: le fette fatte restano, il resto no; una fetta con fine < inizio si salta
    assert key(CANCEL) == 1 and h.ram_r32(ST) == S_IDLE
    h.ram_w32(FRAMES, F0); assert key(DOIT) == 1 and pad(1) == 1
    for at, code in ((hits[0] + 700, 2), (hits[2], 3), (hits[3], 4)):
        h.ram_w32(FRAMES, (F0 + at) & 0xFFFFFFFF); assert pad(code) == 1
    assert key(DOIT) == 1 and h.ram_r32(ST) == S_DONE and h.ram_r32(NT) == 4
    c = cuts(); h.ram_w32(CUT + 8, c[1] - 10)                    # CUT[2] < CUT[1]: fetta 1 con fine < inizio
    for k in range(0, 0x120, 4): h.ram_w32(LOG + k, 0)
    lg = lambda i: (h.ram_r32(LOG + 4 + 8 * i), h.ram_r32(LOG + 8 + 8 * i))
    assert key(DOIT) == 1 and h.ram_r32(ST) == S_COMMIT
    fire(1); assert h.ram_r32(LOG) == 1 and lg(0) == (c[0], c[1] - 1)
    fire(1); assert h.ram_r32(LOG) == 2 and lg(1) == (c[1] - 10, c[3] - 1), [lg(i) for i in range(h.ram_r32(LOG))]
    assert key(CANCEL) == 1 and h.ram_r32(ST) == S_IDLE and msg() == b'Stopped, 02 kept', msg()
    fire(3); assert h.ram_r32(LOG) == 2 and (h.ram_r32(blk + 0x20), h.ram_r32(blk + 0x24), h.ram_r32(MGR + 8)) == (s0, e0, blk)
    assert ref_commit([c[0], c[1], c[1] - 10, c[3]], [0, 1, 2, 3], e0)[:2] == [(0, c[0], c[1] - 1), (2, c[1] - 10, c[3] - 1)]
    print("  ok CANCEL durante l'Extract: 'Stopped, 02 kept', niente altre Extract; fetta con fine < inizio saltata")
    # il sample cambia durante la presa -> annullata; sample non valido
    assert key(CANCEL) == 1 and key(DOIT) == 1 and pad(1) == 1 and h.ram_r32(ST) == S_TAKE
    b2 = h.list_end(); h.ram_w32(b2, 0x300); h.ram_w32(b2 + 0x20, 0); h.ram_w32(b2 + 0x24, 100); h.ram_w32(b2 + 0x94, rate); h.ram_w32(b2 + 0x300, 0)
    h.ram_w32(MGR + 8, b2); fire(1); assert h.ram_r32(ST) == S_IDLE and h.ram_r32(NT) == 0
    assert key(DOIT) == 1 and msg().startswith(b'Sample too short') and h.ram_r32(ST) == S_IDLE
    h.ram_w32(MGR + 8, 0); assert key(DOIT) == 1 and msg().startswith(b'No sample')
    print("  ok sample cambiato durante la presa = annullata; 'Sample too short', 'No sample'")
    # WAV a 48000 Hz: il lettore lo suona 1:1, la posizione resta start + frame (SNAP Off: qui contano solo le posizioni)
    b3 = h.list_end(); n3 = 96000; sz3 = (0x100 + 2 * n3 + 0x200 + 3) & ~3
    h.ram_w32(b3, sz3); h.ram_w32(b3 + 0x18, n3); h.ram_w32(b3 + 0x20, 1000); h.ram_w32(b3 + 0x24, n3 - 1); h.ram_w32(b3 + 0x94, 48000)
    for i in range(0, n3, 64): h.ram_w16(b3 + 0x100 + 2 * i, 0)
    h.ram_w32(b3 + sz3, 0); h.ram_w32(MGR + 8, b3); fire(1)
    F1 = 0x12345; h.ram_w32(FRAMES, F1)
    assert key(DOIT) == 1 and pad(0) == 1
    for fr in (50000, 61111):
        h.ram_w32(FRAMES, F1 + fr); assert pad(1) == 1
    assert cuts() == [1000, 1000 + 50000, 1000 + 61111], cuts()
    h.ram_w32(FRAMES, F1 + (n3 - 1 - 1000)); fire(1); assert h.ram_r32(ST) == S_TAKE, "presa chiusa prima della fine del sample"
    h.ram_w32(FRAMES, F1 + (n3 - 1000)); fire(1); assert h.ram_r32(ST) == S_DONE, "la presa finisce quando il lettore arriva a end"
    print("  ok WAV a 48 kHz: tagli %s (1:1 come il lettore), la presa finisce esattamente a end" % cuts()[1:])
    # Tune del sample: il lettore lo applica (velocita' 2^(Tune/1200)) e Tap ne tiene conto (SNAP Off: contano le posizioni)
    for tune in (-1200, 350, -3700):
        b4 = h.list_end(); n4 = 88200; sz4 = (0x100 + 2 * n4 + 0x200 + 3) & ~3
        h.ram_w32(b4, sz4); h.ram_w32(b4 + 0x18, n4); h.ram_w32(b4 + 0x20, 0); h.ram_w32(b4 + 0x24, n4 - 1); h.ram_w32(b4 + 0x94, 44100)
        h.ram_w32(b4 + 0x30, tune & 0xFFFF); h.ram_w32(b4 + 0x100, 0); h.ram_w32(b4 + sz4, 0)
        assert key(CANCEL) == 1; h.ram_w32(MGR + 8, b4); fire(1)
        sp = ref_speed(tune); F2 = 0x200000; h.ram_w32(FRAMES, F2)
        assert key(DOIT) == 1 and pad(0) == 1 and h.ram_r32(SPEED) == sp and h.ram_r32(LOG + 0x114) & 0xFFFF == tune & 0xFFFF, (tune, h.ram_r32(SPEED), sp)
        for fr in (30000, 50000):
            h.ram_w32(FRAMES, F2 + fr); assert pad(1) == 1
        assert cuts() == [0, ref_pos(0, 30000, sp), ref_pos(0, 50000, sp)], (tune, cuts())
        f0 = ((n4 - 1) << 16) // sp; endf = next(f for f in range(f0 - 2, f0 + (1 << 16) // sp + 4) if ref_pos(0, f, sp) > n4 - 1)
        h.ram_w32(FRAMES, F2 + endf - 1); fire(1); assert h.ram_r32(ST) == S_TAKE, (tune, endf)
        h.ram_w32(FRAMES, F2 + endf); fire(1); assert h.ram_r32(ST) == S_DONE, (tune, endf)
        assert h.ram_r32(b4 + 0x30) == tune & 0xFFFF
    assert ref_speed(-1200) == 32768 and ref_speed(1200) == 131072 and ref_speed(-3700) == ref_speed(-3600) == 8192
    print("  ok Tune -1200 (velocita' 1/2: la presa dura il doppio, tagli a meta'), +350, -3700 (limitato a -3600 come la voce)")
    # uscita dalla pagina: presa annullata, cassetta chiusa
    assert key(CANCEL) == 1 and key(DOIT) == 1 and pad(0) == 1
    h.ram_w32(EVQ + 0x68, 0x88123456); fire(LEAVE_FIRES + 2)
    assert h.ram_r32(ST) == S_IDLE and h.ram_r32(MB + 0x14) == 0 and h.ram_r32(MB + 0x10) == 0
    print("  ok uscita dalla pagina: presa annullata, cassetta 'nessun tool aperto'")
    lab = pg.host.a.labels; span = lab[pg.ns + 'tp_snapend'] - lab[pg.ns + 'tp_snap']
    assert span <= SNAP_SPAN, "tp_snap (%d byte) oltre SNAP_SPAN" % span
    print("VALIDATO: Tap Chop (lazy chop: fetta sul pad colpito, aggancio agli attacchi %d byte da P1) -> %s" % (span, os.path.basename(out)))


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'TAPCHOP.WAV'); pg.build_wav(out); selftest(out)
