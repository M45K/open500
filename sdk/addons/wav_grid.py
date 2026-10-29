#!/usr/bin/env python3
# GRID SEQ (MODE -> OTHER -> "Grid Seq") -> GRID.WAV - step sequencer a griglia sulla sequenza VERA dell'OS (2026-09-28).
#   riga 1: 16 step della battuta per il pad scelto: 0xFF = colpo, ':' = inizio quarto, '.' = vuoto; il cursore lampeggia
#   riga 2: "A01 B 1 S05 V100" = pad scelto (premi un pad: lo scegli e suona), battuta, step (cursore), velocity
#   DO IT: accende/spegne il colpo sotto il cursore. Un colpo "fuori griglia" conta se sta entro mezzo step.
# v6 (2026-10-26, richiesta utente): il PAD premuto mette/toglie il SUO colpo sotto il cursore (il cursore resta: piu'
#   pad sullo stesso step), con la velocity del colpo (campo V = ultima velocity); l'OS lo fa suonare comunque; la corsia
#   mostrata e' quella dell'ultimo pad. QLINK e DO IT restano. TESTINA in play: '>' (o blocco pieno sul colpo) sullo
#   step che suona, solo se suona la battuta mostrata. Posizione = tick dell'evento appena suonato (PLAYPTR - 16, quando
#   PLAYPTR avanza) + frame audio passati * tempo (parametro 0x2000C, BPM x10) / 275625 (96 tick per quarto); se il
#   prossimo evento (o la fine) non arriva entro mezzo battito = fermo, niente testina.
# Sequenza (HW 2026-09-28, SEQ PROBE): blocco corrente = *(u32*)0x8847CBC8, formato MPC1000 SEQ: +0x1C u16 battute,
#   +0x30 tabella battute (tick & 0xFFFFFF), eventi da 16 B a +0x1C10 ordinati per tick, fine tick 0x7FFFFFFF.
#   Inserimento/rimozione: spostamento dell'arena sequenze (0x881DB59C..+0x1A0000, blocchi [size], 0 = fine) di 16 byte,
#   dimensione del blocco +-16, puntatore di riproduzione 0x8847CBD0 +-16, a interrupt bloccati (provato su HW).
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti
from mpcmod import EVQ
SYS_PARAM, ID_TEMPO, FRAMES, TPF = 0x884E5CD0, 0x2000C, 0x881C05F0, 275625   # tick = frame * BPMx10 / TPF
from pages import SET_FOCUS
from view import View, VIEW_GRID

CURSEQ, PLAYPTR = 0x8847CBC8, 0x8847CBD0
EV_OFF, BARS_OFF, BART_OFF = 0x1C10, 0x1C, 0x30
ARENA_LO, ARENA_HI = 0x881DB59C, 0x881DB59C + 0x1A0000
PROG_BASE, PROG_IDX, PROG_STRIDE, NOTE_A = 0x8837B5AC, 0x8847C0E2, 0x1F98, 0x1ED8
BANKOBJ = 0x8849C98C
EV_PAD = 0x38
# v5: caratteri custom HD44780 (CGRAM 6 e 7; il flush dell'OS mappa i codici 0x16 -> 6 e 0x0E -> 7, FUN_a0025df0).
# Salvati leggendo la CGRAM all'apertura della pagina e rimessi all'uscita: le altre pagine dell'OS non cambiano.
LCD_CMD, LCD_DATA = 0xB6000000, 0xB6000004
CG_SLOTS = (6, 7)
C_BOX, C_HIT = 0x16, 0x0E
GLYPHS = {6: [0x1F, 0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1F],     # cursore su step vuoto: rettangolo solo contorno
          7: [0x00, 0x0E, 0x0E, 0x0E, 0x0E, 0x0E, 0x0E, 0x00]}     # colpo: blocco pieno piu' piccolo della casella
# cursore su un colpo: blocco pieno a tutta casella (0xFF del ROM)
BLINK_ON = 32          # cursore: cambia ogni 32 fire (v4: ogni 8 lampeggiava troppo)
K_DOWN, K_LEFT, K_RIGHT, K_UP, K_QLINK = 0x01, 0x02, 0x03, 0x04, 0x20   # codici evento tasti (KEYS.md)
END = 0x7FFFFFFF

pg = PageAddon("grid1", open_press=None)
pg.other_menu("Grid Seq")
pg.far_hend = True
GRIDL = pg.dlabel(0, 0, 16)
PADL = pg.dlabel(0, 1, 3)
BARV = pg.value(" B", 3, 1, fmt=" 0", lo=1, hi=99, default=1)
STV = pg.value(" S", 7, 1, fmt=" 0", lo=1, hi=16, default=1)
VELV = pg.value(" V", 11, 1, fmt="  0", lo=1, hi=127, default=100)
pg.message()
FID = {slot: fid for fid, slot in pg.slots.items()}
SELPAD, BLINK, MASK, NOTE, TRACK, BSTART, SLEN, PSEQ, GMODE, RES = [pg.alloc_slot() for _ in range(10)]
CGON = pg.alloc_slot()                               # 1 = caratteri CGRAM nostri caricati (quelli dell'OS salvati)
CGSAVE = [pg.alloc_slot() for _ in range(4)][0]      # 16 byte: CGRAM 6 e 7 dell'OS
VBUF = [pg.alloc_slot() for _ in range(8)][0]       # vista 2 GRID (32 byte)
VCNT = pg.alloc_slot()
for _s in list(range(VBUF, VBUF + 32, 4)) + [VCNT]: del pg.defaults[_s]
view = View(VIEW_GRID, ver=1)
NOTE_B = 0x1F08                                      # program: nota -> pad (128 byte, 0x30 = nessuno)
# vista 2 GRID v1 (32 byte, ogni 8 fire a pagina aperta): u16 battute, battuta, step (cursore), pad scelto, banco, modo
#   (0 griglia, 1 valori), traccia; poi 12 x u16 = colpi nei 16 step della battuta per i 12 pad del banco (stessa finestra
#   di mezzo step della griglia). Il telefono vede tutti i pad insieme, l'MPC uno alla volta.
PPREV, PTICK, PFR, PNEXT, PON, TEMPO = [pg.alloc_slot() for _ in range(6)]   # testina (v6)
MSGB = [pg.alloc_slot() for _ in range(4)]          # messaggio al gestore parametri di sistema (16 byte contigui)
OLDTXT = [pg.alloc_slot() for _ in range(5)][0]      # copia del testo gia' mostrato (16 + 3 char): ridisegna solo se cambia   # GMODE 0 = griglia, 1 = valori

def put(m, ch): m.a.mov_imm(ord(ch) if ord(ch) < 128 else ord(ch) - 256, 0); m.a.movb_st(0, 12); m.a.add_imm(1, 12)
def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()

def fire(m):
    a = m.a
    ld(m, CGON, 0); a.tst_rr(0, 0); cg = m.newlabel('cg'); m.bf_far(cg)
    a.mov_imm(0, 4); call(m, 'gd_cg'); sti(m, 1, CGON); sti(m, 0, OLDTXT)             # ridisegna tutto
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 0); st(m, 0, PPREV); sti(m, 0, PON)              # testina: niente finche' non avanza
    a.deflabel(cg)
    call(m, 'gd_ctx'); a.tst_rr(0, 0); ok = m.newlabel('ok'); m.bf_far(ok)
    m.imm32(pg.addr['dlabels'][GRIDL], 12)
    for ch in "NO SEQUENCE     ": put(m, ch)
    a.bra_lbl('gd_f2'); a.nop()
    m.pool_jump()
    a.deflabel(ok)
    # riga 1: 16 step
    call(m, 'gd_play'); a.mov_rm_rn(0, 13)                                             # r13 = step della testina (-1 = no)
    ld(m, MASK, 8); ld(m, STV, 9); a.add_imm(-1, 9)
    a.mov_imm(1, 10); ld(m, GMODE, 0); a.tst_rr(0, 0); gm = m.newlabel('gm'); a.bt_lbl(gm); a.mov_imm(0, 10); a.deflabel(gm)   # r10 = cursore (fisso; sui valori niente)
    m.imm32(pg.addr['dlabels'][GRIDL], 12); a.mov_imm(0, 11)                            # r11 = step
    m.pool_jump()
    sl = m.newlabel('sl'); a.deflabel(sl)
    a.mov_rm_rn(8, 0); a.mov_rm_rn(11, 1); a.neg(1, 1); a.shad(1, 0); a.and_imm(1); a.mov_rm_rn(0, 7)   # r7 = colpo
    ch_ = m.newlabel('ch'); nc = m.newlabel('nc'); pn = m.newlabel('pn')
    a.cmpeq(13, 11); a.bf_lbl(pn)                                                       # testina: '>' / pieno sul colpo
    a.tst_rr(7, 7); a.mov_imm(ord('>'), 3); a.bt_lbl(ch_); a.mov_imm(-1, 3); a.bra_lbl(ch_); a.nop()
    a.deflabel(pn)
    a.cmpeq(9, 11); a.bf_lbl(nc); a.tst_rr(10, 10); a.bt_lbl(nc)
    a.tst_rr(7, 7); a.mov_imm(C_BOX, 3); a.bt_lbl(ch_); a.mov_imm(-1, 3); a.bra_lbl(ch_); a.nop()   # cursore: contorno / pieno
    a.deflabel(nc); a.tst_rr(7, 7); a.mov_imm(C_HIT, 3); a.bf_lbl(ch_)
    a.mov_rm_rn(11, 0); a.and_imm(3); a.tst_rr(0, 0); a.mov_imm(ord('.'), 3); a.bf_lbl(ch_); a.mov_imm(ord(':'), 3)
    a.deflabel(ch_)
    a.movb_st(3, 12); a.add_imm(1, 12); a.add_imm(1, 11); a.mov_imm(16, 0); a.cmpeq(0, 11); a.bf_lbl(sl)
    a.deflabel('gd_f2')
    # riga 2: nome del pad scelto
    m.imm32(pg.addr['dlabels'][PADL], 12); ld(m, SELPAD, 4); a.mov_imm(12, 5); pg._hcall(m, '__udiv')
    a.add_imm(ord('A'), 0); a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(1, 1); a.mov_rm_rn(1, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
    a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(0x30, 1); a.movb_st(1, 12)
    # in griglia il focus dell'OS resta sul campo S (anche se l'OS lo sposta, es. dopo DO IT)
    ld(m, GMODE, 0); a.tst_rr(0, 0); fk = m.newlabel('fk'); m.bf_far(fk)
    m.imm32(pg.addr['page'] + 0x24, 1); a.movl_ld(1, 0); m.imm32(pg.addr['values'][FID[STV]], 5); a.cmpeq(5, 0); m.bt_far(fk)
    m.imm32(pg.addr['page'], 4); m.imm32(SET_FOCUS, 2); a.jsr(2); a.nop()
    a.deflabel(fk)
    # ridisegno solo se il testo e' cambiato (v4: ridisegnare a ogni fire faceva sfarfallare la griglia)
    m.imm32(OLDTXT, 5); a.mov_imm(0, 3)
    for lbl, w in ((GRIDL, 16), (PADL, 3)):
        m.imm32(pg.addr['dlabels'][lbl], 4); a.mov_imm(w, 6)
        cl = m.newlabel('cl'); cs = m.newlabel('cs'); a.deflabel(cl)
        a.movb_ld(4, 0); a.movb_ld(5, 1); a.cmpeq(0, 1); a.bt_lbl(cs); a.mov_imm(1, 3); a.movb_st(0, 5)
        a.deflabel(cs); a.add_imm(1, 4); a.add_imm(1, 5); a.dt(6); a.bf_lbl(cl)
    a.tst_rr(3, 3); nr = m.newlabel('nr'); m.bt_far(nr)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel(nr)
    call(m, 'gd_vpub')
pg.on_fire(fire)

def inactive(m):
    a = m.a
    ld(m, CGON, 0); a.tst_rr(0, 0); ni = m.newlabel('ni'); m.bt_far(ni)
    a.mov_imm(1, 4); call(m, 'gd_cg'); sti(m, 0, CGON)
    a.deflabel(ni)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """pad premuto: diventa lo strumento della griglia (banco corrente) e mette/toglie il suo colpo sotto il cursore con
    la sua velocity (v6); l'OS lo fa suonare comunque"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); rl = m.newlabel('rl'); m.bt_far(rl)
    a.mov_rm_rn(8, 1); a.add_imm(2, 1); a.movw_ld(1, 9); a.extu_w(9, 9); a.mov_imm(12, 0); a.cmphs(0, 9); m.bt_far(rl)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(0, 9)
    st(m, 9, SELPAD)
    a.movl_ld_disp(8, 4, 0); m.imm32(127, 1); v1 = m.newlabel('v1'); a.cmpgt(1, 0); a.bf_lbl(v1); a.mov_rm_rn(1, 0); a.deflabel(v1)
    v2 = m.newlabel('v2'); a.cmppl(0); a.bt_lbl(v2); a.mov_imm(1, 0); a.deflabel(v2); st(m, 0, VELV)   # V = velocity 1..127
    a.movl_pool('__doit', 2); a.jsr(2); a.nop()                                          # colpo sotto il cursore
    a.deflabel(rl)
    a.movl_pool(hret0, 2); a.jmp(2); a.nop()
    m.pool_jump()
pg.on_event(EV_PAD, ev_pad)

def ev_key(m, hret0, hret1):
    """tasti (v3), due zone come l'OS:
       GRIGLIA (default): SX/DX = step (a fine battuta passa alla dopo/prima, in giro), QLINK = metti/togli, GIU' = valori
       VALORI: SX/DX = campo (OS), rotella = valore, SU = torna in griglia. QLINK funziona in entrambe.
    Gli altri tasti proseguono (CANCEL, DO IT, ...)."""
    a = m.a
    a.mov_rm_rn(8, 1); a.add_imm(2, 1); a.movw_ld(1, 10); a.extu_w(10, 10)
    kl = m.newlabel('kl'); kr = m.newlabel('kr'); kq = m.newlabel('kq'); ku = m.newlabel('ku'); kdn = m.newlabel('kdn'); kx = m.newlabel('kx')
    a.mov_imm(K_QLINK, 0); a.cmpeq(0, 10); m.bt_far(kq)
    a.mov_imm(K_UP, 0); a.cmpeq(0, 10); m.bt_far(ku)
    a.mov_imm(K_DOWN, 0); a.cmpeq(0, 10); m.bt_far(kdn)
    ld(m, GMODE, 0); a.tst_rr(0, 0); m.bf_far(kx)                                         # sui valori: SX/DX all'OS
    a.mov_imm(K_LEFT, 0); a.cmpeq(0, 10); m.bt_far(kl)
    a.mov_imm(K_RIGHT, 0); a.cmpeq(0, 10); m.bt_far(kr)
    a.bra_lbl(kx); a.nop()
    m.pool_jump()
    a.deflabel(kq); a.movl_pool('__doit', 2); a.jsr(2); a.nop(); a.movl_pool(hret1, 2); a.jmp(2); a.nop()
    a.deflabel(ku); sti(m, 0, GMODE); sti(m, BLINK_ON, BLINK); m.imm32(pg.addr['values'][FID[STV]], 5)
    kf = m.newlabel('kf'); a.bra_lbl(kf); a.nop()
    a.deflabel(kdn); sti(m, 1, GMODE); m.imm32(pg.addr['values'][FID[BARV]], 5)
    a.deflabel(kf); m.imm32(pg.addr['page'], 4); m.imm32(SET_FOCUS, 2); a.jsr(2); a.nop()
    a.movl_pool(hret1, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(kl); a.mov_imm(-1, 13); a.bra_lbl('gd_mv'); a.nop()
    a.deflabel(kr); a.mov_imm(1, 13)
    a.deflabel('gd_mv')
    a.movl_st_dec(13, 15); a.movl_pool('gd_ctx', 2); a.jsr(2); a.nop(); a.movl_ld_inc(15, 13)          # gd_ctx usa r13
    a.tst_rr(0, 0); kd = m.newlabel('kd'); m.bt_far(kd)
    ld(m, PSEQ, 8); a.mov_rm_rn(8, 1); a.add_imm(BARS_OFF, 1); a.movw_ld(1, 9); a.extu_w(9, 9)   # r9 = battute
    b1 = m.newlabel('b1'); a.tst_rr(9, 9); a.bf_lbl(b1); a.mov_imm(1, 9); a.deflabel(b1)
    ld(m, BARV, 11); ld(m, STV, 12); a.add_rr(13, 12)
    lo = m.newlabel('lo'); ok = m.newlabel('ok'); a.cmppl(12); a.bf_lbl(lo)
    a.mov_imm(16, 0); a.cmpgt(0, 12); a.bf_lbl(ok)                                        # oltre 16: battuta dopo
    a.mov_imm(1, 12); a.add_imm(1, 11); a.cmpgt(9, 11); a.bf_lbl(ok); a.mov_imm(1, 11); a.bra_lbl(ok); a.nop()
    a.deflabel(lo); a.mov_imm(16, 12); a.add_imm(-1, 11); a.cmppl(11); a.bt_lbl(ok); a.mov_rm_rn(9, 11)   # sotto 1: battuta prima
    a.deflabel(ok); st(m, 11, BARV); st(m, 12, STV); sti(m, BLINK_ON, BLINK)                    # cursore subito visibile
    a.deflabel(kd); a.movl_pool(hret1, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(kx)
pg.on_event(2, ev_key)

def doit(m):
    a = m.a
    call(m, 'gd_ctx'); a.tst_rr(0, 0); ok = m.newlabel('ok'); m.bf_far(ok)
    pg.emit_message(m, "NO SEQUENCE"); a.movl_pool('gd_dx', 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(ok)
    # step s, tick st = BSTART + s*SLEN
    ld(m, STV, 9); a.add_imm(-1, 9); ld(m, SLEN, 10); a.mov_rm_rn(9, 11); a.mul_l(10, 11); a.sts_macl(11); ld(m, BSTART, 0); a.add_rr(0, 11)   # r11 = st
    ld(m, MASK, 0); a.mov_rm_rn(9, 1); a.neg(1, 1); a.shad(1, 0); a.and_imm(1); a.tst_rr(0, 0); ins = m.newlabel('ins'); m.bt_far(ins)
    # togli tutte le note del pad nella finestra [st - SLEN/2, st + SLEN/2)
    a.mov_rm_rn(10, 12); a.shar(12); a.mov_rm_rn(11, 13); a.sub(12, 13)                 # r13 = inizio finestra
    rloop = m.newlabel('rloop'); a.deflabel(rloop)
    ld(m, PSEQ, 8); m.imm32(EV_OFF, 4); a.add_rr(8, 4); m.imm32(END, 3)
    sc = m.newlabel('sc'); nf = m.newlabel('nf'); a.deflabel(sc)
    a.movl_ld(4, 0); a.cmpeq(3, 0); m.bt_far(nf)
    a.sub(13, 0); a.cmphs(10, 0); nx = m.newlabel('nx'); a.bt_lbl(nx)                   # (tick - inizio) < SLEN (senza segno)
    a.mov_rm_rn(4, 1); a.add_imm(4, 1); a.movb_ld(1, 0); a.extu_b(0, 0); ld(m, TRACK, 2); a.cmpeq(2, 0); a.bf_lbl(nx)
    a.mov_rm_rn(4, 1); a.add_imm(5, 1); a.movb_ld(1, 0); a.extu_b(0, 0); m.imm32(0x90, 2); a.cmpeq(2, 0); a.bf_lbl(nx)
    a.mov_rm_rn(4, 1); a.add_imm(6, 1); a.movb_ld(1, 0); a.extu_b(0, 0); m.imm32(NOTE, 2); a.movl_ld(2, 2); a.cmpeq(2, 0); a.bf_lbl(nx)
    call(m, 'gd_remove'); a.bra_lbl(rloop); a.nop()
    a.deflabel(nx); a.add_imm(16, 4); a.bra_lbl(sc); a.nop()
    m.pool_jump()
    a.deflabel(nf)
    sti(m, 2, RES); sti(m, BLINK_ON, BLINK); a.movl_pool('gd_dx', 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(ins)
    a.mov_rm_rn(11, 4); call(m, 'gd_insert'); a.tst_rr(0, 0); full = m.newlabel('full'); m.bt_far(full)
    sti(m, 1, RES); sti(m, BLINK_ON, BLINK); a.movl_pool('gd_dx', 2); a.jmp(2); a.nop()
    a.deflabel(full); pg.emit_message(m, "MEMORY FULL"); a.movl_pool('gd_dx', 2); a.jmp(2); a.nop()
    m.pool_jump()

    # ---- gd_ctx: r0 = 1 se c'e' una sequenza; PSEQ, BSTART, SLEN, NOTE, TRACK, MASK (clampa BARV) ----
    a.deflabel('gd_ctx'); a.stsl_pr()
    m.imm32(CURSEQ, 1); a.movl_ld(1, 8)
    m.imm32(ARENA_LO, 1); bad = m.newlabel('bad'); a.cmphs(1, 8); m.bf_far(bad); m.imm32(ARENA_HI, 1); a.cmphs(1, 8); m.bt_far(bad)
    st(m, 8, PSEQ)
    a.mov_rm_rn(8, 1); a.add_imm(BARS_OFF, 1); a.movw_ld(1, 9); a.extu_w(9, 9)            # r9 = battute
    b1 = m.newlabel('b1'); a.tst_rr(9, 9); a.bf_lbl(b1); a.mov_imm(1, 9); a.deflabel(b1)
    ld(m, BARV, 10); b2 = m.newlabel('b2'); a.cmpgt(9, 10); a.bf_lbl(b2); a.mov_rm_rn(9, 10); st(m, 10, BARV); a.deflabel(b2)
    a.add_imm(-1, 10); a.shll2(10); a.add_rr(8, 10); a.add_imm(BART_OFF, 10)
    m.imm32(0xFFFFFF, 3); a.movl_ld(10, 11); a.and_rr(3, 11); a.movl_ld_disp(10, 4, 12); a.and_rr(3, 12)   # inizio, battuta dopo
    st(m, 11, BSTART); a.sub(11, 12); ln = m.newlabel('ln'); a.cmppl(12); a.bt_lbl(ln); m.imm32(384, 12); a.deflabel(ln)
    a.mov_rm_rn(12, 4); a.shlr2(4); a.shlr2(4); st(m, 4, SLEN)                          # SLEN = durata battuta / 16
    # nota del pad scelto (program corrente)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 0); a.extu_w(0, 0); m.imm32(PROG_STRIDE, 1); a.mul_l(1, 0); a.sts_macl(0)
    m.imm32(PROG_BASE + NOTE_A, 1); a.add_rr(1, 0); ld(m, SELPAD, 1); a.add_rr(1, 0); a.movb_ld(0, 0); a.extu_b(0, 0); st(m, 0, NOTE)
    # traccia: quella della prima nota, altrimenti 1
    m.imm32(EV_OFF, 13); a.add_rr(8, 13); a.mov_imm(1, 0); a.movl_ld(13, 1); m.imm32(END, 2); a.cmpeq(2, 1); t1 = m.newlabel('t1'); a.bt_lbl(t1)
    a.mov_rm_rn(13, 1); a.add_imm(4, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.deflabel(t1); st(m, 0, TRACK)
    # maschera: note del pad/traccia nella battuta (finestra da inizio - SLEN/2)
    ld(m, SLEN, 10); a.mov_rm_rn(10, 11); a.shar(11); ld(m, BSTART, 12); a.sub(11, 12)   # r12 = inizio finestra
    a.mov_rm_rn(10, 11); a.shll2(11); a.shll2(11)                                        # r11 = 16*SLEN
    a.mov_imm(0, 14); ld(m, NOTE, 9); ld(m, TRACK, 7); m.imm32(END, 6)
    m.pool_jump()
    ml = m.newlabel('ml'); mx = m.newlabel('mx'); a.deflabel(ml)
    a.movl_ld(13, 0); a.cmpeq(6, 0); m.bt_far(mx)
    a.mov_rm_rn(0, 5); a.sub(12, 5); a.cmphs(11, 5); mn = m.newlabel('mn'); a.bt_lbl(mn)
    a.mov_rm_rn(13, 1); a.add_imm(4, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.cmpeq(7, 0); a.bf_lbl(mn)
    a.mov_rm_rn(13, 1); a.add_imm(5, 1); a.movb_ld(1, 0); a.extu_b(0, 0); m.imm32(0x90, 2); a.cmpeq(2, 0); a.bf_lbl(mn)
    a.mov_rm_rn(13, 1); a.add_imm(6, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.cmpeq(9, 0); a.bf_lbl(mn)
    a.mov_rm_rn(5, 4); a.mov_rm_rn(10, 5); pg._hcall(m, '__udiv')                         # step = (tick - inizio) / SLEN
    a.mov_imm(1, 1); a.shad(0, 1); a.or_rr(1, 14)
    a.deflabel(mn); a.add_imm(16, 13); a.bra_lbl(ml); a.nop()
    a.deflabel(mx); st(m, 14, MASK)
    a.mov_imm(1, 0); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(bad); a.mov_imm(0, 0); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- gd_end: r10 = fine dell'arena (blocco di dimensione 0); r0 = 0 se non trovata ----
    a.deflabel('gd_end')
    m.imm32(ARENA_LO, 10); m.imm32(ARENA_HI, 3)
    el = m.newlabel('el'); ee = m.newlabel('ee'); a.deflabel(el)
    a.movl_ld(10, 0); a.tst_rr(0, 0); a.bt_lbl(ee); a.add_rr(0, 10); a.cmphs(3, 10); a.bf_lbl(el)
    a.rts(); a.mov_imm(0, 0)
    a.deflabel(ee); a.rts(); a.mov_imm(1, 0)
    m.pool_jump()

    # ---- gd_insert: r4 = tick; nota NOTE, traccia TRACK, velocity VELV, durata SLEN*3/4; r0 = 1 ok, 0 memoria piena ----
    a.deflabel('gd_insert'); a.stsl_pr()
    for r in (8, 9, 10, 11, 12, 13, 14): a.movl_st_dec(r, 15)
    a.mov_rm_rn(4, 11)                                                                   # r11 = tick
    a.bsr_lbl('gd_end'); a.nop(); a.tst_rr(0, 0); ifu = m.newlabel('ifu'); m.bt_far(ifu)
    a.mov_rm_rn(10, 0); a.add_imm(4 + 16, 0); m.imm32(ARENA_HI, 1); a.cmphs(1, 0); m.bt_far(ifu)
    ld(m, PSEQ, 8); m.imm32(EV_OFF, 12); a.add_rr(8, 12)                                  # posizione: prima nota con tick > r11
    m.pool_jump()
    pl = m.newlabel('pl'); px = m.newlabel('px'); a.deflabel(pl)
    a.movl_ld(12, 0); m.imm32(END, 1); a.cmpeq(1, 0); a.bt_lbl(px); a.cmpgt(11, 0); a.bt_lbl(px); a.add_imm(16, 12); a.bra_lbl(pl); a.nop()
    a.deflabel(px)
    ld(m, TRACK, 13); ld(m, NOTE, 14); ld(m, VELV, 9)
    ld(m, SLEN, 6); a.mov_rm_rn(6, 2); a.shll(6); a.add_rr(2, 6); a.shar(6); a.shar(6)          # r6 = durata = SLEN*3/4
    a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0)
    a.mov_rm_rn(10, 1); a.add_imm(4, 1); a.mov_rm_rn(1, 2); a.add_imm(16, 2)
    ml2 = m.newlabel('ml2'); mx2 = m.newlabel('mx2'); a.deflabel(ml2)
    a.cmphs(1, 12); a.bt_lbl(mx2); a.add_imm(-4, 1); a.add_imm(-4, 2); a.movl_ld(1, 0); a.movl_st(0, 2); a.bra_lbl(ml2); a.nop()
    a.deflabel(mx2)
    a.movl_st(11, 12); a.mov_rm_rn(12, 1); a.add_imm(4, 1)
    a.mov_rm_rn(13, 0); a.movb_st(0, 1); a.add_imm(1, 1); m.imm32(0x90, 0); a.movb_st(0, 1); a.add_imm(1, 1)
    a.mov_rm_rn(14, 0); a.movb_st(0, 1); a.add_imm(1, 1); a.mov_rm_rn(9, 0); a.movb_st(0, 1); a.add_imm(1, 1)
    a.mov_rm_rn(6, 0); a.movw_st(0, 1); a.add_imm(2, 1); a.mov_imm(0, 0); a.movw_st(0, 1); a.add_imm(2, 1); a.movl_st(0, 1)
    a.movl_ld(8, 0); a.add_imm(16, 0); a.movl_st(0, 8)
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 0); a.cmphs(12, 0); pp = m.newlabel('pp'); a.bf_lbl(pp); a.add_imm(16, 0); a.movl_st(0, 1); a.deflabel(pp)
    a.ldc_sr(7); m.imm32(PLAYPTR, 1); a.movl_ld(1, 3); st(m, 3, PPREV)                 # testina: lo spostamento e' nostro
    a.mov_imm(1, 0); ix = m.newlabel('ix'); a.bra_lbl(ix); a.nop()
    a.deflabel(ifu); a.mov_imm(0, 0)
    a.deflabel(ix)
    for r in (14, 13, 12, 11, 10, 9, 8): a.movl_ld_inc(15, r)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- gd_remove: r4 = evento da togliere ----
    a.deflabel('gd_remove'); a.stsl_pr()
    for r in (8, 9, 10, 11): a.movl_st_dec(r, 15)
    a.mov_rm_rn(4, 11); a.bsr_lbl('gd_end'); a.nop()
    ld(m, PSEQ, 8)
    a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0)
    a.mov_rm_rn(11, 2); a.mov_rm_rn(11, 1); a.add_imm(16, 1); a.mov_rm_rn(10, 3); a.add_imm(4, 3)
    rl = m.newlabel('rl'); rx = m.newlabel('rx'); a.deflabel(rl)
    a.cmphs(3, 1); a.bt_lbl(rx); a.movl_ld(1, 0); a.movl_st(0, 2); a.add_imm(4, 1); a.add_imm(4, 2); a.bra_lbl(rl); a.nop()
    a.deflabel(rx)
    a.movl_ld(8, 0); a.add_imm(-16, 0); a.movl_st(0, 8)
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 0); a.cmphi(11, 0); rp = m.newlabel('rp'); a.bf_lbl(rp); a.add_imm(-16, 0); a.movl_st(0, 1); a.deflabel(rp)
    a.ldc_sr(7); m.imm32(PLAYPTR, 1); a.movl_ld(1, 3); st(m, 3, PPREV)
    for r in (11, 10, 9, 8): a.movl_ld_inc(15, r)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- gd_play: r0 = step della testina nella battuta mostrata (0..15), -1 = fermo / altra battuta. r0-r7 ----
    a.deflabel('gd_play'); a.stsl_pr(); poff = m.newlabel('poff'); same = m.newlabel('same')
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 4); ld(m, PPREV, 0); a.cmpeq(4, 0); m.bt_far(same)
    st(m, 4, PPREV)
    ld(m, PSEQ, 5); m.imm32(EV_OFF, 0); a.add_rr(0, 5); a.cmphi(5, 4); m.bf_far(poff)     # prima del 1o evento: niente
    a.mov_rm_rn(4, 0); a.add_imm(-16, 0); a.movl_ld(0, 0); st(m, 0, PTICK)               # evento appena suonato
    a.movl_ld(4, 0); st(m, 0, PNEXT)                                                     # prossimo (END = 0x7FFFFFFF)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, PFR)
    sti(m, 0, MSGB[1]); sti(m, 0x10, MSGB[0]); sti(m, ID_TEMPO, MSGB[3]); sti(m, 0, MSGB[2])
    m.imm32(MSGB[0], 4); m.imm32(SYS_PARAM, 2); a.jsr(2); a.nop()                        # tempo (BPM x10)
    ld(m, MSGB[1], 0); st(m, 0, TEMPO); sti(m, 1, PON)
    a.deflabel(same)
    ld(m, PON, 0); a.tst_rr(0, 0); m.bt_far(poff)
    m.imm32(FRAMES, 1); a.movl_ld(1, 4); ld(m, PFR, 0); a.sub(0, 4)                      # r4 = frame passati
    m.imm32(1000000, 0); a.cmphs(0, 4); m.bt_far('gd_pstop')                             # > 22 s senza eventi: fermo
    ld(m, TEMPO, 0); a.mul_l(0, 4); a.sts_macl(4); m.imm32(TPF, 5); pg._hcall(m, '__udiv')
    ld(m, PTICK, 4); a.add_rr(4, 0); a.mov_rm_rn(0, 6)                                   # r6 = posizione stimata (tick)
    # fermo se il prossimo evento (o la fine della sequenza) non e' arrivato entro mezzo battito
    ld(m, PNEXT, 7); ld(m, PSEQ, 1); a.mov_rm_rn(1, 2); a.add_imm(BARS_OFF, 2); a.movw_ld(2, 0); a.extu_w(0, 0)
    a.shll2(0); a.add_rr(0, 1); m.imm32(BART_OFF, 0); a.add_rr(0, 1); a.movl_ld(1, 0); m.imm32(0xFFFFFF, 1); a.and_rr(1, 0)   # fine
    pe = m.newlabel('pe'); a.cmphi(0, 7); a.bf_lbl(pe); a.mov_rm_rn(0, 7); a.deflabel(pe)   # r7 = min(prossimo, fine)
    a.mov_rm_rn(7, 0); a.add_imm(48, 0); a.cmphi(0, 6); m.bt_far('gd_pstop')             # pos > limite + 48
    pc_ = m.newlabel('pc'); a.cmphi(7, 6); a.bf_lbl(pc_); a.mov_rm_rn(7, 6); a.deflabel(pc_)   # non oltre il prossimo
    ld(m, BSTART, 0); a.sub(0, 6); ld(m, SLEN, 5); a.mov_rm_rn(5, 0); a.shll2(0); a.shll2(0)   # r0 = 16 step
    a.cmphs(0, 6); m.bt_far(poff)                                                        # fuori dalla battuta mostrata
    a.mov_rm_rn(6, 4); pg._hcall(m, '__udiv')                                            # r0 = step
    a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel('gd_pstop'); sti(m, 0, PON)
    a.deflabel(poff); a.mov_imm(-1, 0); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- gd_cg: r4 = 0 salva CGRAM 6/7 dell'OS e carica i nostri caratteri, 1 = rimette quelli dell'OS (usa r0-r7) ----
    a.deflabel('gd_cg'); a.stsl_pr(); m.imm32(CGSAVE, 5)
    def cmd(v): call(m, 'gd_lw'); a.mov_imm(v, 0); m.imm32(LCD_CMD, 1); a.movb_st(0, 1)
    def wr7(): call(m, 'gd_lw'); m.imm32(LCD_DATA, 1); a.movb_st(7, 1)
    a.tst_rr(4, 4); rs = m.newlabel('rs'); m.bf_far(rs)
    for k in CG_SLOTS:
        cmd(0x40 | k << 3)
        for i in range(8): call(m, 'gd_lw'); m.imm32(LCD_DATA, 1); a.movb_ld(1, 7); a.movb_st(7, 5); a.add_imm(1, 5)
        cmd(0x40 | k << 3)
        for v in GLYPHS[k]: a.mov_imm(v, 7); wr7()
        m.pool_jump()
    cx = m.newlabel('cx'); a.bra_lbl(cx); a.nop()
    a.deflabel(rs)
    for k in CG_SLOTS:
        cmd(0x40 | k << 3)
        for i in range(8): a.movb_ld(5, 0); a.and_imm(0x1F); a.mov_rm_rn(0, 7); a.add_imm(1, 5); wr7()
        m.pool_jump()
    a.deflabel(cx); cmd(-128)                                                            # indirizzo DDRAM (0x80)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # gd_lw: attesa LCD libero (busy flag = bit 7 del registro comandi) + margine; usa r0, r1, r3
    a.deflabel('gd_lw'); m.imm32(LCD_CMD, 1)
    wl = m.newlabel('wl'); a.deflabel(wl); a.movb_ld(1, 0); a.tst_imm(0x80); a.bf_lbl(wl)
    a.mov_imm(40, 3); wd = m.newlabel('wd'); a.deflabel(wd); a.dt(3); a.bf_lbl(wd)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- gd_vpub: vista 2, una volta ogni 8 fire (un solo passaggio sugli eventi per tutti i 12 pad) ----
    a.deflabel('gd_vpub'); a.stsl_pr(); vx = m.newlabel('vx')
    ld(m, VCNT, 0); a.add_imm(1, 0); st(m, 0, VCNT); a.and_imm(7); a.tst_rr(0, 0); m.bf_far(vx)
    m.imm32(VBUF, 1); a.mov_imm(0, 0); a.mov_imm(8, 2)
    vz = m.newlabel('vz'); a.deflabel(vz); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(vz)
    call(m, 'gd_ctx'); a.tst_rr(0, 0); vp = m.newlabel('vp'); m.bt_far(vp)
    m.imm32(VBUF, 12)
    ld(m, PSEQ, 8); a.mov_rm_rn(8, 1); a.add_imm(BARS_OFF, 1); a.movw_ld(1, 0); a.movw_st(0, 12)
    for off, slot in ((2, BARV), (3, STV), (4, SELPAD), (6, GMODE), (7, TRACK)):
        ld(m, slot, 0); a.mov_rm_rn(12, 1); a.add_imm(off, 1); a.movb_st(0, 1)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 7); a.mov_rm_rn(7, 0); a.and_imm(3); a.mov_rm_rn(0, 7)
    a.mov_rm_rn(12, 1); a.add_imm(5, 1); a.movb_st(7, 1)
    a.mov_imm(12, 0); a.mul_l(0, 7); a.sts_macl(7)                                         # r7 = primo pad del banco
    m.pool_jump()
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 9); a.extu_w(9, 9); m.imm32(PROG_STRIDE, 1); a.mul_l(1, 9); a.sts_macl(9)
    m.imm32(PROG_BASE + NOTE_B, 1); a.add_rr(1, 9)                                         # r9 = nota -> pad
    ld(m, SLEN, 10); a.mov_rm_rn(10, 13); a.shar(13); ld(m, BSTART, 0); a.sub(13, 0); a.mov_rm_rn(0, 13)   # r13 = inizio finestra
    a.mov_rm_rn(10, 11); a.shll2(11); a.shll2(11)                                          # r11 = 16 step
    m.imm32(EV_OFF, 14); a.add_rr(8, 14)
    m.pool_jump()
    vl = m.newlabel('vl'); vn = m.newlabel('vn'); ve = m.newlabel('ve'); a.deflabel(vl)
    a.movl_ld(14, 0); m.imm32(END, 1); a.cmpeq(1, 0); m.bt_far(ve)
    a.mov_rm_rn(0, 5); a.sub(13, 5); a.cmphs(11, 5); m.bt_far(vn)                           # fuori dalla battuta
    a.mov_rm_rn(14, 1); a.add_imm(5, 1); a.movb_ld(1, 0); a.extu_b(0, 0); m.imm32(0x90, 2); a.cmpeq(2, 0); m.bf_far(vn)
    a.mov_rm_rn(14, 1); a.add_imm(4, 1); a.movb_ld(1, 0); a.extu_b(0, 0); ld(m, TRACK, 2); a.cmpeq(2, 0); m.bf_far(vn)
    a.mov_rm_rn(14, 1); a.add_imm(6, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.add_rr(9, 0); a.movb_ld(0, 3); a.extu_b(3, 3)
    a.sub(7, 3); a.mov_imm(12, 0); a.cmphs(0, 3); m.bt_far(vn)                               # pad fuori dal banco
    a.mov_rm_rn(5, 4); a.mov_rm_rn(10, 5); pg._hcall(m, '__udiv')                           # step (r3 intatto)
    a.mov_imm(1, 1); a.shad(0, 1)
    a.mov_rm_rn(3, 0); a.add_rr(0, 0); a.add_rr(12, 0); a.add_imm(8, 0); a.movw_ld(0, 2); a.or_rr(1, 2); a.movw_st(2, 0)
    a.deflabel(vn); a.add_imm(16, 14); a.movl_pool(vl, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(ve)
    a.deflabel(vp); view.publish(m, VBUF, 32)
    a.deflabel(vx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    view.emit_subs(m)
    a.deflabel('gd_dx')
pg.on_doit(doit)

if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'GRID.WAV'); pg.build_wav(out)
    from validate import Harness
    from shasm import SHAsm
    from pages import ENABLE_DOIT, SET_FOCUS
    from mpcmod import OS_PUSH_PAGE
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s_ = SHAsm(addr); fn(s_); blob = s_.assemble()
        for i in range(0, len(blob), 2): e.wr(addr + i, struct.unpack('<H', blob[i:i+2])[0], 2)
    for ad in (OS_PUSH_PAGE, ENABLE_DOIT): stub(ad, lambda s_: (s_.rts(), s_.nop()))
    stub(SET_FOCUS, lambda s_: (s_.rts(), s_.movl_st_disp(5, 0x24, 4)))     # focus = page+0x24
    from testseq import make_seq
    seq = make_seq()                                          # sequenza di prova generata (8 battute)
    P = ARENA_LO; blk2 = struct.pack('<I', 64) + bytes(range(60))
    arena = seq + blk2 + bytes(4)
    for i in range(0, len(arena), 4): h.ram_w32(P + i, struct.unpack('<I', arena[i:i+4])[0])
    h.ram_w32(CURSEQ, P); PLAY0 = P + EV_OFF + 16 * 100; h.ram_w32(PLAYPTR, PLAY0)
    NOTES = [36, 38, 47, 55] + [60 + i for i in range(44)]
    h.ram_w16(PROG_IDX, 0)
    for i, n in enumerate(NOTES): h.ram_w8(PROG_BASE + NOTE_A + i, n)
    for n in range(128): h.ram_w8(PROG_BASE + NOTE_B + n, 0x30)                    # nota -> pad, come la tiene l'OS
    for i, n in enumerate(NOTES): h.ram_w8(PROG_BASE + NOTE_B + n, i)
    h.ram_w8(BANKOBJ, 0)
    h.load_hub(); h.load_wav(out); h.fire(3)
    h.ram_w32(EVQ + 0x68, pg.page); h.fire(5)
    MSG = 0x88E10000
    def hcall(type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG + 4, val & 0xffffffff); h.ram_w32(MSG + 8, 0); h.ram_w32(MSG + 12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = pg.page; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 20_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d" % i
        return e.r[0]
    doit_ = lambda: (h.ram_w32(RES, 0), hcall(2, 0x2E), {1: 'COLPO AGGIUNTO', 2: 'COLPO TOLTO'}.get(h.ram_r32(RES)))[2]
    txt = lambda k, w: bytes(e.rd(pg.dlabels[k] + i, 1) for i in range(w))
    def events():
        n = []; o = P + EV_OFF
        while True:
            b = bytes(e.rd(o + i, 1) for i in range(16)); t = struct.unpack('<I', b[:4])[0]
            if t == END: return n
            n.append((t, b[4], b[5], b[6], b[7], struct.unpack('<H', b[8:10])[0])); o += 16
    N0 = len(events())                                        # eventi della sequenza di prova
    def ref_mask(evs, note, bar):
        start = (struct.unpack('<I', seq[BART_OFF + 4 * (bar - 1):BART_OFF + 4 * bar])[0]) & 0xFFFFFF; sl = 24; lo = start - sl // 2
        mk = 0
        for t, tr, ty, nt, v, d in evs:
            if ty == 0x90 and nt == note and tr == 1 and 0 <= t - lo < 16 * sl: mk |= 1 << ((t - lo) // sl)
        return mk
    def row():
        gm = h.ram_r32(GMODE); h.ram_w32(GMODE, 1); h.fire(1); r = txt(GRIDL, 16); h.ram_w32(GMODE, gm)   # senza cursore
        return ''.join('X' if c in (0xFF, C_HIT) else chr(c) for c in r)
    # pad 3 (nota 47, hat) battuta 1
    h.ram_w32(SELPAD, 2)                                              # corsia del pad A03 (v6: premere il pad mette un colpo)
    r = row(); mk = ref_mask(events(), 47, 1)
    exp = ''.join('X' if mk >> s & 1 else (':' if s % 4 == 0 else '.') for s in range(16))
    assert r == exp, (r, exp)
    assert txt(PADL, 3) == b"A03", txt(PADL, 3)
    print("  ok battuta 1, pad A03 (nota 47): %r (come il file)" % r)
    # aggiungi al passo vuoto, poi togli
    empty = [s for s in range(16) if not mk >> s & 1][0]
    h.ram_w32(STV, empty + 1); h.ram_w32(VELV, 90); before = bytes(e.rd(P + i, 1) for i in range(len(arena)))
    assert doit_() == "COLPO AGGIUNTO"
    ev = events(); assert (empty * 24, 1, 0x90, 47, 90, 18) in ev and len(ev) == N0 + 1, ev[:5]
    assert [t for t, *_ in ev] == sorted(t for t, *_ in ev), "ordine per tick"
    r2 = row(); assert r2[empty] == 'X', r2
    assert h.ram_r32(PLAYPTR) == PLAY0 + 16 and bytes(e.rd(P + len(seq) + 16 + i, 1) for i in range(64)) == blk2
    assert doit_() == "COLPO TOLTO"
    assert bytes(e.rd(P + i, 1) for i in range(len(arena))) == before and h.ram_r32(PLAYPTR) == PLAY0
    print("  ok DO IT su step %d: colpo aggiunto (tick %d, vel 90, dur 18) poi tolto -> sequenza identica" % (empty + 1, empty * 24))
    # togli un colpo registrato (fuori griglia) e rimettilo in griglia
    hit = [s for s in range(16) if mk >> s & 1][0]
    h.ram_w32(STV, hit + 1); assert doit_() == "COLPO TOLTO"
    assert not row()[hit] == 'X' and len(events()) < N0
    assert doit_() == "COLPO AGGIUNTO" and row()[hit] == 'X'
    print("  ok step %d: colpo registrato tolto, poi rimesso in griglia" % (hit + 1))
    # battuta oltre la fine -> limitata; seconda battuta
    h.ram_w32(BARV, 50); h.fire(1); assert h.ram_r32(BARV) == 8
    h.ram_w32(BARV, 2); r3 = row(); mk2 = ref_mask(events(), 47, 2)
    assert r3 == ''.join('X' if mk2 >> s & 1 else (':' if s % 4 == 0 else '.') for s in range(16)), r3
    print("  ok battuta limitata a 8; battuta 2: %r" % r3)
    se = [i for i in range(16) if not mk2 >> i & 1][0]                                         # uno step vuoto
    h.ram_w32(STV, se + 1); h.fire(1); assert txt(GRIDL, 16)[se] == C_BOX, txt(GRIDL, 16)     # vuoto: contorno
    h.ram_w32(STV, 1); h.fire(1); assert txt(GRIDL, 16)[0] == 0xFF and txt(GRIDL, 16)[2] == C_HIT  # colpo: pieno; altri colpi piccoli
    h.fire(3); assert txt(GRIDL, 16)[0] == 0xFF                                                  # fisso
    nb = len(events()); h.ram_w8(BANKOBJ, 1); assert hcall(EV_PAD, 0, 100) == 0 and hcall(EV_PAD, 5, 0) == 0 and h.ram_r32(SELPAD) == 12
    assert len(events()) == nb + 1 and hcall(EV_PAD, 0, 100) == 0 and len(events()) == nb       # (v6: colpo di B01 messo e tolto)
    h.fire(1); assert txt(PADL, 3) == b"B01", txt(PADL, 3)
    print("  ok cursore lampeggiante; banco B: pad B01 (il rilascio non cambia pad)")
    # v2: frecce e QLINK
    h.ram_w32(BARV, 1); h.ram_w32(STV, 16)
    assert hcall(2, K_RIGHT) == 1 and (h.ram_r32(BARV), h.ram_r32(STV)) == (2, 1)
    assert hcall(2, K_LEFT) == 1 and (h.ram_r32(BARV), h.ram_r32(STV)) == (1, 16)
    h.ram_w32(STV, 1); assert hcall(2, K_LEFT) == 1 and (h.ram_r32(BARV), h.ram_r32(STV)) == (8, 16)
    assert hcall(2, K_RIGHT) == 1 and (h.ram_r32(BARV), h.ram_r32(STV)) == (1, 1)
    assert hcall(2, K_RIGHT) == 1 and (h.ram_r32(BARV), h.ram_r32(STV)) == (1, 2)
    print("  ok frecce: step +-1, a fine battuta passa alla battuta dopo/prima, in giro (8 -> 1)")
    h.ram_w8(BANKOBJ, 0); h.ram_w32(SELPAD, 2); h.ram_w32(VELV, 90); before = bytes(e.rd(P + i, 1) for i in range(len(arena)))
    n0 = len(events()); r0_ = row(); assert r0_[1] != 'X'
    assert hcall(2, K_QLINK) == 1 and row()[1] == 'X' and len(events()) == n0 + 1
    assert hcall(2, K_QLINK) == 1 and row()[1] != 'X' and bytes(e.rd(P + i, 1) for i in range(len(arena))) == before
    print("  ok QLINK: mette e toglie il colpo sotto il cursore")
    foc = lambda: h.ram_r32(pg.page + 0x24); VW = pg.values
    h.ram_w32(pg.page + 0x24, VW[FID[BARV]]); h.fire(1); assert foc() == VW[FID[STV]]      # in griglia il focus torna su S
    assert hcall(2, K_DOWN) == 1 and foc() == VW[FID[BARV]] and h.ram_r32(GMODE) == 1
    h.fire(1); assert foc() == VW[FID[BARV]]                                              # sui valori l'OS decide
    h.fire(1); assert C_BOX not in txt(GRIDL, 16) and 0xFF not in txt(GRIDL, 16)                 # niente cursore in griglia
    st0 = (h.ram_r32(BARV), h.ram_r32(STV))
    assert hcall(2, K_RIGHT) == 0 and hcall(2, K_LEFT) == 0 and (h.ram_r32(BARV), h.ram_r32(STV)) == st0   # SX/DX all'OS
    assert hcall(2, K_QLINK) == 1 and row()[1] == 'X'; assert hcall(2, K_QLINK) == 1 and row()[1] != 'X'
    assert hcall(2, K_UP) == 1 and foc() == VW[FID[STV]] and h.ram_r32(GMODE) == 0
    assert hcall(2, K_RIGHT) == 1 and h.ram_r32(STV) == 3 and foc() == VW[FID[STV]]
    assert doit_() == "COLPO TOLTO" and h.ram_r32(STV) == 3 and h.ram_r32(BARV) == 1 and foc() == VW[FID[STV]]
    assert hcall(2, 0x2D) == 1 and hcall(2, 0x0F) == 0             # CANCEL esce, MODE all'OS
    print("  ok GIU' = valori (SX/DX e rotella dell'OS), SU = griglia (focus su S, cursore); colpo: il cursore resta")
    h.ram_w32(GMODE, 0); h.fire(1); h.ram_w32(EVQ + 0x70, 0); h.fire(5); assert h.ram_r32(EVQ + 0x70) == 0
    h.ram_w32(STV, 5); h.fire(1); assert h.ram_r32(EVQ + 0x70) == 1
    print("  ok cursore fisso (contorno / pieno sul colpo); ridisegno solo quando il testo cambia")
    # v6: il pad mette/toglie il SUO colpo sotto il cursore con la sua velocity; il cursore resta (piu' pad sullo step)
    h.ram_w32(GMODE, 0); h.ram_w8(BANKOBJ, 0); h.ram_w32(BARV, 1)
    evs = events(); free = [s_ for s_ in range(16) if not (ref_mask(evs, 38, 1) >> s_ & 1 or ref_mask(evs, 36, 1) >> s_ & 1)][0]
    h.ram_w32(STV, free + 1); n0 = len(evs); before = bytes(e.rd(P + i, 1) for i in range(len(arena)))
    bar1 = struct.unpack('<I', seq[BART_OFF:BART_OFF + 4])[0] & 0xFFFFFF
    assert hcall(EV_PAD, 1, 77) == 0 and h.ram_r32(SELPAD) == 1 and h.ram_r32(VELV) == 77 and h.ram_r32(STV) == free + 1
    ev = events(); assert (bar1 + free * 24, 1, 0x90, 38, 77, 18) in ev and len(ev) == n0 + 1, ev[:4]
    assert hcall(EV_PAD, 0, 127) == 0 and h.ram_r32(SELPAD) == 0 and (bar1 + free * 24, 1, 0x90, 36, 127, 18) in events() and len(events()) == n0 + 2
    assert row()[free] == 'X' and txt(PADL, 3) == b"A01"
    assert hcall(EV_PAD, 1, 0) == 0 and len(events()) == n0 + 2                                   # rilascio: niente
    assert hcall(EV_PAD, 1, 300) == 0 and h.ram_r32(VELV) == 127 and len(events()) == n0 + 1     # pad A02 di nuovo: tolto
    assert hcall(EV_PAD, 0, 50) == 0 and bytes(e.rd(P + i, 1) for i in range(len(arena))) == before
    print("  ok pad = colpo sotto il cursore (A02 vel 77, A01 vel 127 sullo stesso step %d), di nuovo = tolto, rilascio niente" % (free + 1))
    # v6: testina in play (tempo dal gestore parametri: 120.0 BPM)
    stub(SYS_PARAM, lambda s_: (s_.movl_pool(1200, 2), s_.movl_st_disp(2, 4, 4), s_.mov_imm(1, 0), s_.rts(), s_.nop()))
    lane = [k for k in range(12) if ref_mask(events(), NOTES[k], 1) == 0][0]                   # corsia vuota: testina = '>'
    h.ram_w32(SELPAD, lane); h.ram_w32(STV, 16); h.ram_w32(BARV, 1)
    ts = [t for t, *_ in events()]; endt = struct.unpack('<I', seq[BART_OFF + 32:BART_OFF + 36])[0] & 0xFFFFFF
    def ref_play(k, dfr, bar=1):
        if k == 0: return -1
        lim = min(ts[k] if k < len(ts) else END, endt); pos = ts[k - 1] + dfr * 1200 // TPF
        if dfr >= 1000000 or pos > lim + 48: return -1
        bs = struct.unpack('<I', seq[BART_OFF + 4 * (bar - 1):BART_OFF + 4 * bar])[0] & 0xFFFFFF
        pos = min(pos, lim); return (pos - bs) // 24 if 0 <= pos - bs < 16 * 24 else -1
    def head(): g = txt(GRIDL, 16); return g.index(ord('>')) if ord('>') in g else -1
    FR = 0x500000; h.ram_w32(FRAMES, FR); h.fire(1); assert head() == -1, "fermo: niente testina"
    k = next(i for i, t in enumerate(ts) if 100 <= t < 300 and ts[i + 1] - t >= 24)
    h.ram_w32(PLAYPTR, P + EV_OFF + 16 * (k + 1)); h.fire(1)
    assert head() == ref_play(k + 1, 0) >= 0, (head(), ref_play(k + 1, 0))
    for dfr in (2000, 5512, 9000):
        h.ram_w32(FRAMES, FR + dfr); h.fire(1); assert head() == ref_play(k + 1, dfr), (dfr, head(), ref_play(k + 1, dfr))
    h.ram_w32(FRAMES, FR + 400000); h.fire(1); assert head() == -1 and h.ram_r32(PON) == 0, "il prossimo evento non arriva: fermo"
    h.ram_w32(FRAMES, FR); h.ram_w32(BARV, 2); h.ram_w32(PLAYPTR, P + EV_OFF + 16 * k); h.fire(1)
    assert h.ram_r32(PON) == 1 and head() == -1, "suona la battuta 1, mostro la 2: niente testina"
    h.ram_w32(BARV, 1); h.fire(1); assert head() == ref_play(k, 0) >= 0
    h.ram_w32(FRAMES, FR + 400000); h.fire(1); assert head() == -1
    pp = h.ram_r32(PLAYPTR); h.ram_w32(STV, 1); assert hcall(EV_PAD, lane, 90) == 0 and h.ram_r32(PLAYPTR) == pp + 16
    h.fire(1); assert head() == -1 and h.ram_r32(PON) == 0, "lo spostamento di PLAYPTR per il nostro colpo non e' play"
    assert hcall(EV_PAD, lane, 90) == 0 and h.ram_r32(PLAYPTR) == pp
    h.ram_w32(EVQ + 0x68, 0x88123456); h.fire(2); h.ram_w32(PLAYPTR, P + EV_OFF + 16 * (k + 1)); h.ram_w32(EVQ + 0x68, pg.page); h.fire(2)
    assert head() == -1, "all'apertura della pagina la testina riparte da ferma"
    print("  ok testina: step dell'evento suonato + tempo (120 BPM), avanza fra due eventi, sparisce da fermo, solo sulla battuta che suona")
    # CGRAM: all'uscita rimette i caratteri dell'OS, al rientro salva e ricarica i nostri
    OSG = {6: [0x0A + i for i in range(8)], 7: [0x13 - i for i in range(8)]}
    cg = {'addr': 0}
    def lcd_w(a_, v):
        if a_ == LCD_CMD and v & 0xC0 == 0x40: cg['addr'] = v & 0x3F
    def lcd_r(a_):
        k, i = divmod(cg['addr'], 8); cg['addr'] += 1; return OSG.get(k, [0] * 8)[i] | 0xE0
    e.mmio_r[LCD_DATA] = lcd_r; e.mmio_w[LCD_CMD] = lcd_w
    def cgram_log():
        out, cur = {}, None
        for pc, a_, v, sz in e.wlog:
            if a_ == LCD_CMD and v & 0xC0 == 0x40: cur = (v & 0x3F) // 8; out[cur] = []
            elif a_ == LCD_CMD and v & 0x80: cur = None
            elif a_ == LCD_DATA and cur is not None: out[cur].append(v & 0xFF)
        e.wlog.clear(); return out
    e.wlog.clear(); h.ram_w32(EVQ + 0x68, 0x88123456); h.fire(2)
    cgram_log()
    h.ram_w32(EVQ + 0x68, pg.page); h.fire(1); lg = cgram_log()
    assert lg == GLYPHS, lg
    h.ram_w32(EVQ + 0x68, 0x88123456); h.fire(1); lg = cgram_log()
    assert lg == OSG, lg
    h.fire(3); assert cgram_log() == {}
    print("  ok CGRAM 6/7: caratteri dell'OS letti e salvati all'apertura, rimessi uguali all'uscita")
    h.ram_w32(EVQ + 0x68, pg.page); h.ram_w32(CURSEQ, 0); h.fire(1); assert txt(GRIDL, 16) == b"NO SEQUENCE     "
    # vista 2 GRID: tutti i 12 pad del banco A nella battuta 1, come la maschera calcolata dal file
    from mpcmod import VBOX_OFF, VSLOT, V_LEN, V_VER, V_PAY
    h.ram_w32(CURSEQ, P); h.ram_w8(BANKOBJ, 0); h.ram_w32(BARV, 1); h.ram_w32(STV, 5); h.ram_w32(GMODE, 0); h.fire(16)
    VS = h.ram_r32(0x8848ED34) + VBOX_OFF + VIEW_GRID * VSLOT; v = bytes(e.rd(VS + V_PAY + i, 1) for i in range(32))
    assert h.ram_r32(VS + V_LEN) == 32 and struct.unpack('<H', v[:2])[0] == 8 and v[2:8] == bytes([1, 5, h.ram_r32(SELPAD), 0, 0, 1]), list(v[:8])
    evs = events()
    for k in range(12):
        exp = ref_mask(evs, NOTES[k], 1); got = struct.unpack('<H', v[8 + 2 * k:10 + 2 * k])[0]
        assert got == exp, (k, NOTES[k], bin(got), bin(exp))
    print("  ok vista GRID: battute, cursore, banco, 12 pad x 16 step della battuta = maschere calcolate dal file")
    print("VALIDATO: Grid Seq -> GRID.WAV")
