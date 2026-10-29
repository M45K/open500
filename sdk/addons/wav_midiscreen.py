#!/usr/bin/env python3
# MIDI SCREEN v4 (MODE -> OTHER -> "MIDI Screen") -> MIDISCR.WAV - schermo remoto via MIDI OUT (2026-10-02).
#   Su TUTTE le pagine manda dalla MIDI OUT (SysEx, manufacturer 0x7D "non commerciale"):
#     01 DISPLAY  F0 7D 01 <seq> <cursore> <32 caratteri 7 bit> F7             (38 B) quando cambia (>= 46 ms), ogni 0,5 s
#                 cursore = posizione+1 del cursore dell'OS (0..31 = riga 1 poi riga 2), 0 = nessuno (v6)
#     02 WAVE     F0 7D 02 <sig> <chunk 0..7> <32 picchi 0..127> F7             (38 B) forma d'onda del sample corrente:
#                 256 colonne = picco |L| / 256 (32 letture per colonna); di nuovo ogni 3 s
#     03 TRIM     F0 7D 03 <sig> <len 5x7> <start 5x7> <end 5x7> F7             (20 B) quando cambia, ogni 0,5 s
#     04 CHOP     F0 7D 04 <sig> <mseq> <parte> <n> <k> <k posizioni 4x7> F7     (9+4k B, k <= 12) tagli pubblicati da un tool
#                 nella "cassetta" B+MBOX_OFF (v5, es. Trans-Chop): n = 0 se nessun tool aperto / altro sample; ogni 3 s
#     05 PLAY     F0 7D 05 <sig> <cnt> <on> <fetta+1, 0 = intero> <start 5x7> <end 5x7> F7   (18 B) ascolto di un tool (v6):
#                 parte appena cambia (senza aspettare il turno): il browser evidenzia la fetta e fa scorrere il cursore
#   sig = numero del sample (cambia quando cambia sample corrente / lunghezza / nome): il ricevitore butta i pezzi vecchi.
#   Un pacchetto al massimo ogni SLOT frame (35 ms), a turno fra i tipi in attesa -> <= ~30% della banda MIDI.
#   riga 1: "MIDI SCREEN ON" (rotella OFF/ON)    riga 2: "SENT 00123 SK 00" = pacchetti mandati / saltati (coda piena)
# MIDI OUT (RE 2026-10-02, os_sdram, HW ok v3): coda TX dell'OS 128 B 0x8848CB78..CBF7, scrittura [0x881C0674], lettura
#   [0x881C0678], svuotata dall'interrupt verso la SCI 0xFFFFFE86. Scriviamo nella coda a interrupt bloccati, solo se c'e'
#   posto per tutto il pacchetto (mai attese: il MIDI clock dell'OS non viene ritardato).
# Sample corrente = *(MGR+8): +4 nome, +0x18 lunghezza (frame), +0x20 start, +0x24 end, PCM L s16 da +0x100.
#   Picchi calcolati 4 colonne per fire (il codice degli addon gira senza cache: pochi accessi per fire).
# TELECOMANDO (v7, MIDI IN): l'interrupt mette i byte ricevuti nella coda RX dell'OS 0x8848CBF8..CC77 (scrittura
#   [0x881C067C]; l'OS legge con [0x881C0680], funzioni 0x884B840A avail / 0x884B8418 get). Noi la LEGGIAMO con un indice
#   nostro (MYRD) senza toccare quello dell'OS. Comandi SysEx dal Mac (id = numero della richiesta, ripetuto nella risposta):
#     10 PING  F0 7D 10 id F7                                  -> F0 7D 11 id ver F7
#     12 PEEK  F0 7D 12 id addr5x7 n(<=32) F7                  -> F0 7D 13 id n <2n nibble> F7
#     14 POKE  F0 7D 14 id addr5x7 n <2n nibble> F7            -> F0 7D 15 id F7
#     16 EVENT F0 7D 16 id type2x7 code2x7 data5x7 F7          -> post_event dell'OS (tasti 2/3, rotella 6, pad 0x38...) -> 17 id
#   Una risposta alla volta (il Mac aspetta prima di mandare la richiesta dopo); parte appena c'e' posto nella coda TX.
# Display: copia dell'OS LCD_OBJ+0x28 (32 byte: codici CGRAM 0..7, ASCII, >= 0x80 -> 0x7F).
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti
from mpcmod import EVQ, LCD_SHADOW, LCD_OBJ, SCRATCH_MAGIC, MBOX_OFF, MBOX_MAGIC, MBOX_MAXN, MBOX_PLAY
from upipe import emit_base as upipe_base, U_TXW, U_TXR, U_RXW, U_RXR, U_TX, U_TXSZ, U_RX, U_RXSZ
from mpcmod import VBOX_OFF, VBOX_MAGIC, VBOX_N, V_SEQ, V_LEN, V_VER, V_ALIVE, V_PAY, V_PAYMAX
SIG2 = 0x8B2A3D01                 # seconda firma della scratch del loader (loader.py)

TXQ, TXQ_END, TXWR, TXRD = 0x8848CB78, 0x8848CBF7, 0x881C0674, 0x881C0678
MGR = 0x8848ED30
FRAMES = 0x881C05F0       # contatore frame audio dell'OS (44100/s): orologio vero (i fire sono > 300/s, variabili)
SLOT = 1536               # >= 35 ms tra due pacchetti qualsiasi (v1/v2 su HW: 84 pacchetti/s = MIDI satura)
DGAP = 2048               # display: >= 46 ms tra due invii
KEEP = 22050              # display e trim: reinvio ogni 0,5 s anche senza cambi (per chi si collega dopo)
WKEEP = 3 * 44100         # forma d'onda: reinvio ogni 3 s
NCOL, CPF, RPC = 256, 4, 32   # colonne, colonne calcolate per fire, letture per colonna
PKT_D, PKT_W, PKT_T, PKT_P = 38, 38, 20, 18
LCD_CUR = LCD_OBJ + 4    # s16: cursore dell'OS = indice 0..31 + 1, 0 = spento (flush FUN_a0025d30 -> FUN_a0025ac4)
CPP = 12                  # posizioni per pacchetto CHOP (57 B: ci sta nella coda da 128 col clock)
assert 9 + 4 * CPP <= 64 and max(PKT_D, PKT_W, PKT_T) <= 64     # BUF = 64 byte

pg = PageAddon("midiscr1", open_press=None)
pg.other_menu("MIDI Screen")
pg.far_hend = True                 # v8: codice > 4 KB (helper e handler con salti assoluti)
ONS = pg.choice("MIDI SCREEN ", 0, 0, ["OFF", "ON"], default=1)
CNTL = pg.dlabel(0, 1, 16)
SENT, SKIP, SEQ, LASTF, RR, DISPF, TRIMF, WAVEF = [pg.alloc_slot() for _ in range(8)]
SIG, CBLK, CLEN, CNAME, READY, WSENT = [pg.alloc_slot() for _ in range(6)]
TL, TS, TE, TSIG = [pg.alloc_slot() for _ in range(4)]
pg.defaults[TSIG] = -1
MBSEQ, CPART, CACT, CN, CHOPF = [pg.alloc_slot() for _ in range(5)]   # tagli: seq mandato, parte, invio in corso, n
pg.defaults[MBSEQ] = -1
LASTCUR, PSEQ = pg.alloc_slot(), pg.alloc_slot(default=-1)   # cursore mandato, contatore ascolto mandato
RXQ, RXQ_END, RXWR = 0x8848CBF8, 0x8848CC77, 0x881C067C
VER = 11
# v8 (2026-10-16) VISTE (docs/PROTOCOL.md §4.2): ogni vista viva della cassetta B+VBOX_OFF (battito < VALIVE) e' mandata
#   quando il suo seq cambia e ogni VKEEP:  F0 7D 30 vista ver seq parte nparti <packed7 di <= VPART_B byte> chk F7
#   packed7 = gruppi di 7 byte: 1 byte coi bit alti (bit j = byte j) + 7 byte coi 7 bit bassi; chk = XOR da 0x30 in poi & 0x7F.
#   Se il seq cambia a meta' invio si riparte dalla parte 0. CAPS: F0 7D 18 id .. F7 -> F0 7D 19 id 1 n <n x (vista, ver)> chk F7
VPART_B, VKEEP, VALIVE = 40, 2 * 44100, 2 * 44100
# v9 (2026-10-16) USB: se l'add-on USB MIDI ha la porta (pipe sdk/upipe.py viva), ogni pacchetto mandato alla MIDI OUT va
#   anche nella coda TX della pipe (stesso ritmo), e i byte della coda RX (dal telefono/computer) passano dallo stesso
#   decodificatore del MIDI IN: telecomando e CAPS funzionano anche via USB. Un solo cavo USB, niente interfaccia MIDI.
VACT, VCUR, VNEXT, VSEQ, VPART, VNPS, VREF = [pg.alloc_slot() for _ in range(7)]
VSENT = [pg.alloc_slot() for _ in range(VBOX_N)][0]   # seq mandato per vista (-1 = da mandare; VREF = 0 -> tutto a -1)
for _s in [VACT, VCUR, VNEXT, VSEQ, VPART, VNPS, VREF] + [VSENT + 4 * i for i in range(VBOX_N)]:
    del pg.defaults[_s]                                       # gia' a 0 (init_scratch): niente codice d'avvio in piu'
MYRD, RXST, RXN, REPLEN = [pg.alloc_slot() for _ in range(4)]
# v10 (2026-10-17, test_session: 22 risposte PEEK perse su 78 con l'app via USB): la risposta va SOLO alla porta da cui e'
#   arrivata la domanda (RXSRC/REPSRC: 0 = MIDI IN DIN, 1 = USB); con la pipe USB viva i pacchetti dello schermo vanno
#   SOLO su USB (prima: prima la DIN da 128 B a 3125 B/s e su USB solo se la DIN li aveva presi). Con una risposta in
#   attesa non si leggono altri comandi (restano in coda: prima venivano letti e buttati -> l'app ripeteva = traffico).
RXSRC, REPSRC = pg.alloc_slot(), pg.alloc_slot()
RXBUF = [pg.alloc_slot() for _ in range(20)][0]        # SysEx ricevuto (80 byte, senza F0/F7)
REPBUF = [pg.alloc_slot() for _ in range(20)][0]       # risposta da mandare (80 byte)
RXMAX = 80
from mpcmod import EVQ as _EVQ, OS_POST_EVENT
# v11 (2026-10-17, PIANO.md 3.1) VISTE DI SISTEMA: MIDI Screen pubblica lui lo stato che l'app leggeva con PEEK (indirizzi
#   dell'OS nell'app, traffico continuo) come viste normali della cassetta (CAPS le elenca, stesso invio):
#   7 PADS v1 = program u16 + 48 x nome16 (layer 0, se vuoto layer 1)             ogni 64 fire (16: le fette "LOOP S1/S2" restano distinte)
#   10 MIX v1 = program u16, banco, 48 x livello (pad +0x8F, 0..100)               ogni 8 fire
#   11 MEM v1 = usati u32, totali u32, n sample u16, frame del corrente u32, nome16 del corrente   ogni 256 fire
#   AZIONE (docs/PROTOCOL.md 4.3) F0 7D 40 id vista azione arg5x7 chk F7 -> F0 7D 41 id esito chk F7:
#   vista 10 azione 0x10 = livello di un pad, arg = livello | pad << 7 | program << 13: eseguita SOLO se il program e'
#   ancora quello (esito 3 se no), pad < 48, livello <= 100. Esito 1 = vista/azione che MIDI Screen non conosce.
from view import View, VIEW_PADS, VIEW_MIX, VIEW_MEM
PROG_BASE, PROG_IDX, PROG_STRIDE, PAD_OFF, PAD_SIZE, LEVEL, BANKOBJ = 0x8837B5AC, 0x8847C0E2, 0x1F98, 0x18, 0xA4, 0x8F, 0x8849C98C
NPAD, NAME8 = 48, 16                                  # (nome intero come sul display)
PADS_LEN, MIX_LEN, MEM_LEN = 2 + NPAD * NAME8, 3 + NPAD, 30
SYSC = pg.alloc_slot()
SUBF = pg.alloc_slot()                                 # frame dell'ultimo CAPS: viste di sistema solo con un'app collegata
SUB_FR = 10 * 44100                                    # (CAPS negli ultimi 10 s; l'app lo rimanda ogni ~4 s)
PADB = [pg.alloc_slot() for _ in range((PADS_LEN + 3) // 4)][0]
MIXB = [pg.alloc_slot() for _ in range((MIX_LEN + 3) // 4)][0]
MEMB = [pg.alloc_slot() for _ in range((MEM_LEN + 3) // 4)][0]
vpads, vmix, vmem = View(VIEW_PADS, 1), View(VIEW_MIX, 1), View(VIEW_MEM, 1)
for _s in [SYSC, SUBF] + list(range(PADB, MEMB + MEM_LEN + 3 & ~3, 4)):
    pg.defaults.pop(_s, None)                                  # gia' a 0 (init_scratch): niente codice d'avvio in piu'
LAST = [pg.alloc_slot() for _ in range(8)][0]          # ultimo display mandato (32 byte)
BUF = [pg.alloc_slot() for _ in range(16)][0]          # pacchetto in costruzione (64 byte: CHOP fino a 57)
PEAKS = [pg.alloc_slot() for _ in range(NCOL // 4)][0]

def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()

def sys_subs(m):
    """v11: ms_sys (viste di sistema) e ms_act (azione 0x40, continua il dispatcher di ms_cmd: r10 = risposta)"""
    a = m.a
    def prog_base(r_prog, r_out):                                       # r_out = primo pad del program r_prog (usa r0, r1)
        m.imm32(PROG_STRIDE, 1); a.mov_rm_rn(r_prog, 0); a.mul_l(1, 0); a.sts_macl(r_out)
        m.imm32(PROG_BASE + PAD_OFF, 1); a.add_rr(1, r_out)
    a.deflabel('ms_sys'); a.stsl_pr(); sx = m.newlabel('sx')
    ld(m, SUBF, 0); a.tst_rr(0, 0); m.bt_far(sx)                                         # nessuna app ha chiesto CAPS
    m.imm32(FRAMES, 1); a.movl_ld(1, 1); a.sub(0, 1); m.imm32(SUB_FR, 2); a.cmphi(2, 1); m.bt_far(sx)
    ld(m, SYSC, 8); a.add_imm(1, 8); st(m, 8, SYSC)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 9); a.extu_w(9, 9); a.mov_imm(127, 0); a.cmphi(0, 9); m.bt_far(sx)   # program assurdo
    # MIX ogni 8 fire
    a.mov_rm_rn(8, 0); a.and_imm(7); a.tst_rr(0, 0); nmx = m.newlabel('nmx'); m.bf_far(nmx)
    m.imm32(MIXB, 4); a.mov_rm_rn(9, 0); a.movb_st(0, 4)
    a.mov_rm_rn(4, 7); a.add_imm(1, 7); a.mov_rm_rn(9, 0); a.shlr8(0); a.movb_st(0, 7); a.add_imm(1, 7)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.and_imm(3); a.movb_st(0, 7); a.add_imm(1, 7)
    m.pool_jump()
    prog_base(9, 5); m.imm32(LEVEL, 0); a.add_rr(0, 5); m.imm32(PAD_SIZE, 6); a.mov_imm(NPAD, 3)
    mxl = m.newlabel('mxl'); a.deflabel(mxl); a.movb_ld(5, 0); a.movb_st(0, 7); a.add_imm(1, 7); a.add_rr(6, 5); a.dt(3); a.bf_lbl(mxl)
    vmix.publish(m, MIXB, MIX_LEN)
    a.deflabel(nmx); m.pool_jump()
    # PADS ogni 64 fire (al passo 1)
    ld(m, SYSC, 0); a.and_imm(63)
    a.mov_imm(1, 1); a.cmpeq(1, 0); npd = m.newlabel('npd'); m.bf_far(npd)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    m.imm32(PADB, 7); a.mov_rm_rn(9, 0); a.movb_st(0, 7); a.add_imm(1, 7); a.shlr8(0); a.movb_st(0, 7); a.add_imm(1, 7)
    prog_base(9, 5); a.mov_imm(NPAD, 3)
    m.pool_jump()
    pdl = m.newlabel('pdl'); a.deflabel(pdl)
    a.mov_rm_rn(5, 4); a.movb_ld(4, 0); a.tst_rr(0, 0); pl0 = m.newlabel('pl0'); a.bf_lbl(pl0); a.add_imm(0x18, 4)   # layer 0 vuoto -> layer 1
    a.deflabel(pl0); a.mov_imm(NAME8, 2); a.mov_imm(0, 6)                                  # r6 = 1 dopo il primo 0
    pcl = m.newlabel('pcl'); pcz = m.newlabel('pcz'); pcw = m.newlabel('pcw'); a.deflabel(pcl)
    a.tst_rr(6, 6); a.bf_lbl(pcz); a.movb_ld(4, 0); a.add_imm(1, 4); a.tst_rr(0, 0); a.bf_lbl(pcw); a.mov_imm(1, 6)
    a.deflabel(pcz); a.mov_imm(0, 0)
    a.deflabel(pcw); a.movb_st(0, 7); a.add_imm(1, 7); a.dt(2); a.bf_lbl(pcl)
    m.imm32(PAD_SIZE, 0); a.add_rr(0, 5); a.dt(3); a.bf_lbl(pdl)
    vpads.publish(m, PADB, PADS_LEN)
    a.deflabel(npd); m.pool_jump()
    # MEM ogni 256 fire (al passo 2): percorre la lista sample (blocchi plausibili, max 4096)
    ld(m, SYSC, 0); a.and_imm(0xFF); a.mov_imm(2, 1); a.cmpeq(1, 0); nme = m.newlabel('nme'); m.bf_far(nme)
    m.imm32(MGR, 1); a.movl_ld(1, 4); a.movl_ld_disp(1, 4, 5); a.mov_rm_rn(4, 6); m.imm32(4096, 3); a.mov_imm(0, 7)
    mwl = m.newlabel('mwl'); mwe = m.newlabel('mwe'); a.deflabel(mwl)
    a.cmphs(5, 6); a.bt_lbl(mwe); a.movl_ld(6, 0); m.imm32(0x100, 1); a.cmphs(1, 0); a.bf_lbl(mwe)
    a.mov_rm_rn(0, 1); a.and_imm(3); a.tst_rr(0, 0); a.bf_lbl(mwe); a.add_rr(1, 6); a.add_imm(1, 7); a.dt(3); a.bf_lbl(mwl)
    a.deflabel(mwe)
    m.pool_jump()
    m.imm32(MEMB, 2); a.mov_rm_rn(6, 0); a.sub(4, 0); a.movl_st(0, 2)                     # usati
    a.mov_rm_rn(5, 0); a.sub(4, 0); a.movl_st_disp(0, 4, 2)                                 # totali
    a.mov_rm_rn(2, 1); a.add_imm(8, 1); a.mov_rm_rn(7, 0); a.movb_st(0, 1); a.add_imm(1, 1); a.shlr8(0); a.movb_st(0, 1)
    m.imm32(MGR + 8, 1); a.movl_ld(1, 4); a.mov_imm(0, 0); a.tst_rr(4, 4); mnc = m.newlabel('mnc'); a.bt_lbl(mnc); a.movl_ld_disp(4, 0x18, 0)
    a.deflabel(mnc); m.pool_jump(); a.mov_rm_rn(2, 1); a.add_imm(10, 1)
    for k in range(4): a.movb_st(0, 1); a.add_imm(1, 1); a.shlr8(0)
    a.mov_imm(16, 3); a.tst_rr(4, 4); mnz = m.newlabel('mnz'); mnl = m.newlabel('mnl'); mnd = m.newlabel('mnd'); a.bt_lbl(mnz)
    a.add_imm(4, 4)
    a.deflabel(mnl); a.movb_ld(4, 0); a.add_imm(1, 4); a.movb_st(0, 1); a.add_imm(1, 1); a.dt(3); a.bf_lbl(mnl); a.bra_lbl(mnd); a.nop()
    a.deflabel(mnz); a.mov_imm(0, 0)
    mzl = m.newlabel('mzl'); a.deflabel(mzl); a.movb_st(0, 1); a.add_imm(1, 1); a.dt(3); a.bf_lbl(mzl)
    a.deflabel(mnd)
    vmem.publish(m, MEMB, MEM_LEN)
    a.deflabel(nme); m.pool_jump()
    a.deflabel(sx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    vmix.emit_subs(m); vpads.emit_subs(m); vmem.emit_subs(m)
    # ---- ms_act: azione 0x40 (r10 = dove scrivere la risposta, dopo F0 7D 41 id). Torna al chk comune di ms_cmd ----
    a.deflabel('ms_act')
    ld(m, RXN, 0); a.mov_imm(10, 1); a.cmpge(1, 0); arj = m.newlabel('arj'); anv = m.newlabel('anv'); aok = m.newlabel('aok'); m.bf_far(arj)
    m.imm32(RXBUF + 3, 1); a.movb_ld(1, 2); a.extu_b(2, 2); a.mov_imm(VIEW_MIX, 0); a.cmpeq(0, 2); m.bf_far(anv)
    m.imm32(RXBUF + 4, 1); a.movb_ld(1, 2); a.extu_b(2, 2); a.mov_imm(0x10, 0); a.cmpeq(0, 2); m.bf_far(anv)
    a.mov_imm(0, 0)
    for i in reversed(range(5)):
        a.shll2(0); a.shll2(0); a.shll2(0); a.shll(0); m.imm32(RXBUF + 5 + i, 1); a.movb_ld(1, 2); a.extu_b(2, 2); a.or_rr(2, 0)
    m.pool_jump()
    a.mov_rm_rn(0, 11)
    a.and_imm(0x7F); a.mov_rm_rn(0, 3)                                                    # r3 = livello
    a.mov_rm_rn(11, 0); a.shlr2(0); a.shlr2(0); a.shlr2(0); a.shlr(0); a.and_imm(0x3F); a.mov_rm_rn(0, 4)   # r4 = pad
    a.mov_rm_rn(11, 0); a.shlr8(0); a.shlr2(0); a.shlr2(0); a.shlr(0); a.mov_rm_rn(0, 5)  # r5 = program
    a.mov_imm(100, 0); a.cmphs(3, 0); m.bf_far(arj)
    a.mov_imm(NPAD, 0); a.cmphs(0, 4); m.bt_far(arj)
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 0); a.extu_w(0, 0); a.cmpeq(5, 0); m.bf_far(arj)
    m.pool_jump()
    m.imm32(PROG_STRIDE, 1); a.mov_rm_rn(5, 0); a.mul_l(1, 0); a.sts_macl(6)
    m.imm32(PAD_SIZE, 1); a.mov_rm_rn(4, 0); a.mul_l(1, 0); a.sts_macl(7); a.add_rr(7, 6)
    m.imm32(PROG_BASE + PAD_OFF + LEVEL, 1); a.add_rr(1, 6); a.movb_st(3, 6)
    a.mov_imm(0, 0); a.bra_lbl(aok); a.nop()
    a.deflabel(arj); a.mov_imm(3, 0); a.bra_lbl(aok); a.nop()
    a.deflabel(anv); a.mov_imm(1, 0)
    a.deflabel(aok); a.movb_st(0, 10); a.add_imm(1, 10)
    a.movl_pool('ms_chk', 2); a.jmp(2); a.nop()
    m.pool_jump()

def fire(m):
    a = m.a
    call(m, 'ms_maint')
    m.imm32(pg.addr['dlabels'][CNTL], 12)
    for ch in "SENT ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    ld(m, SENT, 4); a.mov_imm(5, 13); call(m, 'ms_dec')
    for ch in " SK ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    ld(m, SKIP, 4); a.mov_imm(2, 13); call(m, 'ms_dec')
    a.mov_imm(ord(' '), 0); a.movb_st(0, 12); a.add_imm(1, 12); a.movb_st(0, 12)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
pg.on_fire(fire)

def inactive_and_subs(m):
    """(v8) le subroutine stanno dopo il ramo 'pagina non aperta' del framework: il suo salto corto (bra, 4 KB) non
    deve attraversarle"""
    a = m.a
    call(m, 'ms_maint')
    a.movl_pool('ms_end', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # ---- ms_dec: r4 in decimale, r13 cifre, a r12 (r12 += r13) ----
    a.deflabel('ms_dec'); a.stsl_pr(); a.add_rr(13, 12); a.mov_rm_rn(12, 11)
    dl = m.newlabel('dl'); a.deflabel(dl); a.mov_imm(10, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(0, 4)
    a.add_imm(-1, 11); a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 11); a.dt(13); a.bf_lbl(dl)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- ms_send: r4 = pacchetto, r5 = lunghezza -> pipe USB se viva, altrimenti coda TX dell'OS (MIDI OUT DIN).
    #      r0 = 1 mandato, 0 niente posto (riprovare). r0-r7 ----
    a.deflabel('ms_send'); a.stsl_pr()
    a.movl_st_dec(4, 15); a.movl_st_dec(5, 15)
    a.movl_pool('ms_usbpush', 2); a.jsr(2); a.nop()
    a.movl_ld_inc(15, 5); a.movl_ld_inc(15, 4)
    a.cmppz(0); snx = m.newlabel('snx'); a.bt_lbl(snx)                                    # pipe viva: solo USB
    a.bsr_lbl('ms_dinsend'); a.nop()
    a.deflabel(snx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- ms_usbpush: r4 = pacchetto, r5 = lunghezza -> coda TX della pipe. r0 = 1 messo, 0 niente posto, -1 pipe spenta.
    #      r0-r7 ----
    a.deflabel('ms_usbpush'); a.stsl_pr(); upx = m.newlabel('upx'); upn = m.newlabel('upn'); upf = m.newlabel('upf')
    a.movl_pool('ms_upipe', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); m.bt_far(upn); a.mov_rm_rn(0, 6)
    a.movl_ld_disp(6, U_TXW, 1); a.movl_ld_disp(6, U_TXR, 2); a.sub(1, 2); a.add_imm(-1, 2)
    m.imm32(U_TXSZ - 1, 3); a.and_rr(3, 2); a.cmphs(5, 2); m.bf_far(upf)                  # posto libero >= lunghezza
    a.mov_rm_rn(6, 7); m.imm32(U_TX, 0); a.add_rr(0, 7)
    upl = m.newlabel('upl'); a.deflabel(upl)
    a.movb_ld(4, 0); a.add_imm(1, 4); a.mov_rm_rn(7, 2); a.add_rr(1, 2); a.movb_st(0, 2); a.add_imm(1, 1); a.and_rr(3, 1)
    a.dt(5); a.bf_lbl(upl)
    a.movl_st_disp(1, U_TXW, 6)                                                           # indice per ultimo
    a.bra_lbl(upx); a.mov_imm(1, 0)
    a.deflabel(upf); a.bra_lbl(upx); a.mov_imm(0, 0)
    a.deflabel(upn); a.mov_imm(-1, 0)
    a.deflabel(upx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    upipe_base(m, 'ms_upipe', need_alive=True)
    a.deflabel('ms_dinsend')
    a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0)
    m.imm32(TXWR, 6); a.movl_ld(6, 2); m.imm32(TXRD, 1); a.movl_ld(1, 0); a.sub(2, 0); a.add_imm(-1, 0); a.and_imm(0x7F)
    full = m.newlabel('full'); a.cmpge(5, 0); a.bf_lbl(full)
    m.imm32(TXQ_END, 1); m.imm32(TXQ, 3)
    sl = m.newlabel('sl'); sw = m.newlabel('sw'); sn = m.newlabel('sn'); a.deflabel(sl)
    a.movb_ld(4, 0); a.add_imm(1, 4); a.movb_st(0, 2); a.cmpeq(1, 2); a.bf_lbl(sw); a.mov_rm_rn(3, 2); a.bra_lbl(sn); a.nop()
    a.deflabel(sw); a.add_imm(1, 2); a.deflabel(sn); a.dt(5); a.bf_lbl(sl)
    a.movl_st(2, 6); a.ldc_sr(7); a.rts(); a.mov_imm(1, 0)
    a.deflabel(full); a.ldc_sr(7); a.rts(); a.mov_imm(0, 0)
    m.pool_jump()
    # ---- ms_mbox: r0 = indirizzo della cassetta (loader con firme + 'SCR1'), 0 se non c'e'. r0-r3 ----
    a.deflabel('ms_mbox'); nmb = m.newlabel('nmb')
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); a.bt_lbl(nmb)
    a.mov_rm_rn(2, 1); a.add_imm(0x40, 1)                                                # (mov.l @(disp) arriva solo a 60)
    a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 3); a.cmpeq(3, 0); a.bf_lbl(nmb)
    a.movl_ld_disp(1, 4, 0); m.imm32(SIG2, 3); a.cmpeq(3, 0); a.bf_lbl(nmb)
    m.imm32(MBOX_OFF, 0); a.add_rr(2, 0); a.movl_ld(0, 3); m.imm32(MBOX_MAGIC, 2); a.cmpeq(2, 3); a.bf_lbl(nmb)
    a.rts(); a.nop()
    a.deflabel(nmb); a.rts(); a.mov_imm(0, 0)
    m.pool_jump()
    # ---- ms_vbox: r0 = B+VBOX_OFF se c'e' la cassetta delle viste ('O5V1'), altrimenti 0. r0-r3 ----
    a.deflabel('ms_vbox'); nvb = m.newlabel('nvb')
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 2); a.tst_rr(2, 2); a.bt_lbl(nvb)
    a.mov_rm_rn(2, 1); a.add_imm(0x40, 1)
    a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 3); a.cmpeq(3, 0); a.bf_lbl(nvb)
    a.movl_ld_disp(1, 4, 0); m.imm32(SIG2, 3); a.cmpeq(3, 0); a.bf_lbl(nvb)
    m.imm32(VBOX_OFF, 0); a.add_rr(2, 0); a.movl_ld(0, 3); m.imm32(VBOX_MAGIC, 2); a.cmpeq(2, 3); a.bf_lbl(nvb)
    a.rts(); a.nop()
    a.deflabel(nvb); a.rts(); a.mov_imm(0, 0)
    m.pool_jump()
    # ---- ms_valive: r4 = casella -> T = viva (battito recente). r0-r2 ----
    a.deflabel('ms_valive'); vdead = m.newlabel('vdead')                                 # (v9) morta: battito 0 o lunghezza assurda
    a.movl_ld_disp(4, V_ALIVE, 0); a.tst_rr(0, 0); a.bt_lbl(vdead)                         # casella mai scritta / RAM sporca
    a.movl_ld_disp(4, V_LEN, 1); m.imm32(V_PAYMAX + 1, 2); a.cmphs(2, 1); a.bt_lbl(vdead)
    m.imm32(FRAMES, 1); a.movl_ld(1, 2); a.sub(0, 2)
    m.imm32(VALIVE, 1); a.cmphs(1, 2); a.rts(); a.movt(0)                                   # T = (adesso - battito) >= VALIVE
    a.deflabel(vdead); a.rts(); a.mov_imm(1, 0)
    # ---- ms_vfind: r0 = prima vista viva da mandare (seq != mandato), 0 = nessuna. Ogni VKEEP rimanda tutto. r0-r7 ----
    a.deflabel('ms_vfind'); a.stsl_pr(); vfx = m.newlabel('vfx')
    a.movl_pool('ms_vbox', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); m.bt_far(vfx); a.mov_rm_rn(0, 5)
    m.imm32(FRAMES, 1); a.movl_ld(1, 6); ld(m, VREF, 0); a.mov_rm_rn(6, 2); a.sub(0, 2)
    nrf = m.newlabel('nrf'); rfy = m.newlabel('rfy'); a.tst_rr(0, 0); a.bt_lbl(rfy)          # VREF = 0: primo giro
    m.imm32(VKEEP, 1); a.cmphs(1, 2); a.bf_lbl(nrf)
    a.deflabel(rfy)
    m.imm32(VSENT, 1); a.mov_imm(-1, 0); a.mov_imm(VBOX_N, 2)
    rfl = m.newlabel('rfl'); a.deflabel(rfl); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(rfl)
    a.mov_rm_rn(6, 0); a.or_imm(1); st(m, 0, VREF)                                        # (mai 0)
    a.deflabel(nrf); a.mov_imm(1, 7)
    vl = m.newlabel('vl'); vn = m.newlabel('vn'); a.deflabel(vl)
    a.mov_rm_rn(7, 4); a.shll8(4); a.shll2(4); a.add_rr(5, 4)                                 # r4 = casella N
    a.movl_pool('ms_valive', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); a.bf_lbl(vn)
    a.movl_ld_disp(4, V_SEQ, 0); m.imm32(VSENT, 1); a.mov_rm_rn(7, 2); a.shll2(2); a.add_rr(2, 1); a.movl_ld(1, 3)
    a.cmpeq(3, 0); vf = m.newlabel('vf'); a.bf_lbl(vf)
    a.deflabel(vn); a.add_imm(1, 7); a.mov_imm(VBOX_N, 0); a.cmpeq(0, 7); a.bf_lbl(vl)
    a.deflabel(vfx); a.mov_imm(0, 0); a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(vf); a.mov_rm_rn(7, 0); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- ms_maint: a ogni fire (pagina aperta o no) ----
    def putc(v): a.mov_imm(v if v < 128 else v - 256, 0); a.movb_st(0, 10); a.add_imm(1, 10)
    def v35(r):                                                  # r -> 5 byte da 7 bit (LSB prima); usa r0, r1
        a.mov_rm_rn(r, 1)
        for _ in range(5):
            a.mov_rm_rn(1, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10); a.shlr2(1); a.shlr2(1); a.shlr2(1); a.shlr(1)
    # ---- ms_rx: legge i byte nuovi della coda RX (max 64 per fire), raccoglie i SysEx, esegue i comandi ----
    def rx_v(off, nb):                                           # r0 = numero da nb byte da 7 bit a RXBUF+off (LSB prima)
        a.mov_imm(0, 0)
        for i in reversed(range(nb)):
            a.shll2(0); a.shll2(0); a.shll2(0); a.shll(0); m.imm32(RXBUF + off + i, 1); a.movb_ld(1, 2); a.extu_b(2, 2); a.or_rr(2, 0)
    def rp(v): a.mov_imm(v if v < 128 else v - 256, 0); a.movb_st(0, 10); a.add_imm(1, 10)
    a.deflabel('ms_rx'); a.stsl_pr(); rxx = m.newlabel('rxx')
    ld(m, REPLEN, 0); a.tst_rr(0, 0); m.bf_far(rxx)                                     # risposta in attesa: i comandi aspettano
    sti(m, 0, RXSRC)
    m.imm32(RXWR, 1); a.movl_ld(1, 9); ld(m, MYRD, 8)
    ri = m.newlabel('ri'); a.tst_rr(8, 8); a.bf_lbl(ri); a.mov_rm_rn(9, 8); a.deflabel(ri)         # prima volta: da qui in poi
    a.mov_imm(64, 11)
    rl2 = m.newlabel('rl2'); rdone = m.newlabel('rdone'); a.deflabel(rl2)
    a.cmpeq(9, 8); m.bt_far(rdone)
    a.movb_ld(8, 12); a.extu_b(12, 12)
    m.imm32(RXQ_END, 1); rw = m.newlabel('rw'); rw2 = m.newlabel('rw2'); a.cmpeq(1, 8); a.bf_lbl(rw); m.imm32(RXQ, 8); a.bra_lbl(rw2); a.nop()
    a.deflabel(rw); a.add_imm(1, 8); a.deflabel(rw2)
    a.movl_pool('ms_rxb', 2); a.jsr(2); a.nop()
    ld(m, REPLEN, 0); a.tst_rr(0, 0); m.bf_far(rdone)                                   # comando con risposta: basta per ora
    a.dt(11); m.bf_far(rl2)
    a.deflabel(rdone); st(m, 8, MYRD)
    m.pool_jump()
    # v9: stessi byte anche dalla coda RX della pipe USB
    ux = m.newlabel('ux')
    ld(m, REPLEN, 0); a.tst_rr(0, 0); m.bf_far(ux)
    sti(m, 1, RXSRC)
    a.movl_pool('ms_upipe', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); m.bt_far(ux)
    a.movl_st_dec(0, 15); a.movl_ld_disp(0, U_RXR, 8); a.movl_ld_disp(0, U_RXW, 9); a.mov_imm(64, 11)
    ul = m.newlabel('ul'); ue = m.newlabel('ue'); a.deflabel(ul)
    a.cmpeq(9, 8); m.bt_far(ue)
    a.movl_ld(15, 1); m.imm32(U_RX, 0); a.add_rr(0, 1); a.add_rr(8, 1); a.movb_ld(1, 12); a.extu_b(12, 12)
    a.add_imm(1, 8); m.imm32(U_RXSZ - 1, 0); a.and_rr(0, 8)
    a.movl_pool('ms_rxb', 2); a.jsr(2); a.nop()
    ld(m, REPLEN, 0); a.tst_rr(0, 0); m.bf_far(ue)
    a.dt(11); m.bf_far(ul)
    a.deflabel(ue); a.movl_ld_inc(15, 1); a.movl_st_disp(8, U_RXR, 1)
    a.deflabel(ux); a.deflabel(rxx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- ms_rxb: r12 = un byte ricevuto (MIDI IN o USB): raccoglie i SysEx, a F7 esegue il comando. Preserva r8, r9, r11 ----
    a.deflabel('ms_rxb'); a.stsl_pr(); rnx = m.newlabel('rnx')
    m.imm32(0xF8, 1); a.cmphs(1, 12); m.bt_far(rnx)                                     # tempo reale: ignora
    m.imm32(0xF0, 1); a.cmpeq(1, 12); nf0 = m.newlabel('nf0'); a.bf_lbl(nf0); sti(m, 1, RXST); sti(m, 0, RXN); a.movl_pool(rnx, 2); a.jmp(2); a.nop()
    a.deflabel(nf0)
    m.imm32(0xF7, 1); a.cmpeq(1, 12); nf7 = m.newlabel('nf7'); a.bf_lbl(nf7)
    ld(m, RXST, 0); a.tst_rr(0, 0); sti(m, 0, RXST); m.bt_far(rnx)
    a.movl_st_dec(8, 15); a.movl_st_dec(9, 15); a.movl_st_dec(11, 15)
    a.movl_pool('ms_cmd', 2); a.jsr(2); a.nop()
    a.movl_ld_inc(15, 11); a.movl_ld_inc(15, 9); a.movl_ld_inc(15, 8)
    a.movl_pool(rnx, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(nf7)
    a.mov_rm_rn(12, 0); a.and_imm(0x80); a.tst_rr(0, 0); n80 = m.newlabel('n80'); a.bt_lbl(n80); sti(m, 0, RXST)                  # altro byte di stato: SysEx interrotto
    a.movl_pool(rnx, 2); a.jmp(2); a.nop()
    a.deflabel(n80)
    ld(m, RXST, 0); a.tst_rr(0, 0); m.bt_far(rnx)
    ld(m, RXN, 2); a.mov_imm(RXMAX, 0); a.cmphs(0, 2); m.bt_far(rnx)
    m.imm32(RXBUF, 1); a.add_rr(2, 1); a.movb_st(12, 1); a.add_imm(1, 2); st(m, 2, RXN)
    a.deflabel(rnx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- ms_cmd: RXBUF[0..RXN) = 7D cmd id ...; risposta in REPBUF/REPLEN (una alla volta) ----
    a.deflabel('ms_cmd'); a.stsl_pr(); cx = m.newlabel('cx')
    ld(m, REPLEN, 0); a.tst_rr(0, 0); m.bf_far(cx)                                       # risposta precedente in attesa
    ld(m, RXN, 0); a.mov_imm(3, 1); a.cmpge(1, 0); m.bf_far(cx)
    m.imm32(RXBUF, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(0x7D, 2); a.cmpeq(2, 0); m.bf_far(cx)
    m.imm32(RXBUF + 1, 1); a.movb_ld(1, 13); a.extu_b(13, 13); m.imm32(RXBUF + 2, 1); a.movb_ld(1, 14); a.extu_b(14, 14)   # r13 cmd, r14 id
    m.imm32(REPBUF, 10); rp(0xF0); rp(0x7D); a.mov_rm_rn(13, 0); a.add_imm(1, 0); a.movb_st(0, 10); a.add_imm(1, 10); a.movb_st(14, 10); a.add_imm(1, 10)
    c12 = m.newlabel('c12'); c14 = m.newlabel('c14'); c16 = m.newlabel('c16'); cend = m.newlabel('cend')
    a.mov_imm(0x12, 0); a.cmpeq(0, 13); m.bt_far(c12); a.mov_imm(0x14, 0); a.cmpeq(0, 13); m.bt_far(c14)
    a.mov_imm(0x16, 0); a.cmpeq(0, 13); m.bt_far(c16)
    c18 = m.newlabel('c18'); a.mov_imm(0x18, 0); a.cmpeq(0, 13); m.bt_far(c18)
    a.mov_imm(0x40, 0); a.cmpeq(0, 13); n40 = m.newlabel('n40'); a.bf_lbl(n40); a.movl_pool('ms_act', 2); a.jmp(2); a.nop()   # lontano: salto assoluto
    a.deflabel(n40)
    a.mov_imm(0x10, 0); a.cmpeq(0, 13); m.bf_far(cx)
    rp(VER); a.movl_pool(cend, 2); a.jmp(2); a.nop()
    m.pool_jump()
    # PEEK: n byte da addr -> 2n nibble
    a.deflabel(c12)
    rx_v(3, 5); a.mov_rm_rn(0, 11); m.imm32(RXBUF + 8, 1); a.movb_ld(1, 12); a.extu_b(12, 12)
    a.mov_imm(32, 0); pk1 = m.newlabel('pk1'); a.cmphs(0, 12); a.bf_lbl(pk1); a.mov_rm_rn(0, 12); a.deflabel(pk1)  # n >= 32 -> 32
    a.movb_st(12, 10); a.add_imm(1, 10)
    pkl = m.newlabel('pkl'); pke = m.newlabel('pke'); a.tst_rr(12, 12); a.bt_lbl(pke)
    a.deflabel(pkl); a.movb_ld(11, 2); a.add_imm(1, 11)
    a.mov_rm_rn(2, 0); a.shlr2(0); a.shlr2(0); a.and_imm(15); a.movb_st(0, 10); a.add_imm(1, 10)
    a.mov_rm_rn(2, 0); a.and_imm(15); a.movb_st(0, 10); a.add_imm(1, 10)
    a.dt(12); a.bf_lbl(pkl)
    a.deflabel(pke); a.movl_pool(cend, 2); a.jmp(2); a.nop()
    m.pool_jump()
    # POKE: n byte (2n nibble da RXBUF+9) -> addr
    a.deflabel(c14)
    rx_v(3, 5); a.mov_rm_rn(0, 11); m.imm32(RXBUF + 8, 1); a.movb_ld(1, 12); a.extu_b(12, 12)
    ld(m, RXN, 0); a.add_imm(-9, 0); a.shlr(0); po1 = m.newlabel('po1'); a.cmphs(12, 0); a.bt_lbl(po1); a.mov_rm_rn(0, 12); a.deflabel(po1)   # n <= (RXN-9)/2
    m.imm32(RXBUF + 9, 3)
    pol = m.newlabel('pol'); poe = m.newlabel('poe'); a.tst_rr(12, 12); a.bt_lbl(poe)
    a.deflabel(pol); a.movb_ld(3, 0); a.add_imm(1, 3); a.shll2(0); a.shll2(0); a.movb_ld(3, 2); a.add_imm(1, 3); a.and_imm(0xF0)
    a.mov_rm_rn(2, 1); a.mov_rm_rn(0, 2); a.mov_rm_rn(1, 0); a.and_imm(15); a.or_rr(2, 0); a.movb_st(0, 11); a.add_imm(1, 11)
    a.dt(12); a.bf_lbl(pol)
    a.deflabel(poe); a.movl_pool(cend, 2); a.jmp(2); a.nop()
    m.pool_jump()
    # EVENT: post_event dell'OS
    a.deflabel(c16)
    a.movl_st_dec(10, 15)
    rx_v(3, 2); a.mov_rm_rn(0, 11); rx_v(5, 2); a.shll16(0); a.extu_w(11, 11); a.or_rr(0, 11)   # r11 = type | code<<16
    rx_v(7, 5); a.mov_rm_rn(0, 12)                                                      # r12 = data
    a.add_imm(-8, 15); a.movl_st(11, 15); a.movl_st_disp(12, 4, 15)
    m.imm32(_EVQ, 4); a.mov_rm_rn(15, 5); m.imm32(OS_POST_EVENT, 2); a.jsr(2); a.nop()
    a.add_imm(8, 15); a.movl_ld_inc(15, 10)
    a.movl_pool(cend, 2); a.jmp(2); a.nop()                                              # v11: prima finiva dentro CAPS
    m.pool_jump()
    # CAPS: proto 1, viste vive {vista, ver}, chk
    a.deflabel(c18)
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); a.or_imm(1); st(m, 0, SUBF)                      # v11: app collegata (mai 0)
    rp(1); a.mov_rm_rn(10, 12); a.add_imm(1, 10); a.mov_imm(0, 11)                         # r12 = posto del conteggio
    a.movl_pool('ms_vbox', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); cpe_ = m.newlabel('cpe'); m.bt_far(cpe_)
    a.mov_rm_rn(0, 5); a.mov_imm(1, 7)
    cpl_ = m.newlabel('cpl'); cpn_ = m.newlabel('cpn'); a.deflabel(cpl_)
    a.mov_rm_rn(7, 4); a.shll8(4); a.shll2(4); a.add_rr(5, 4)
    a.movl_pool('ms_valive', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); a.bf_lbl(cpn_)
    a.movb_st(7, 10); a.add_imm(1, 10); a.movl_ld_disp(4, V_VER, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    a.add_imm(1, 11)
    a.deflabel(cpn_); a.add_imm(1, 7); a.mov_imm(VBOX_N, 0); a.cmpeq(0, 7); a.bf_lbl(cpl_)
    m.pool_jump()
    a.deflabel(cpe_); a.movb_st(11, 12)
    a.deflabel('ms_chk')
    m.imm32(REPBUF + 2, 1); a.mov_imm(0, 2)                                                 # chk = XOR da 0x19 (0x41) in poi
    cxl = m.newlabel('cxl'); cxe = m.newlabel('cxe'); a.deflabel(cxl); a.cmpeq(10, 1); a.bt_lbl(cxe)
    a.movb_ld(1, 0); a.xor_rr(0, 2); a.add_imm(1, 1); a.bra_lbl(cxl); a.nop()
    a.deflabel(cxe); a.mov_rm_rn(2, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    a.deflabel(cend); rp(0xF7)
    m.imm32(REPBUF, 0); a.sub(0, 10); st(m, 10, REPLEN)
    ld(m, RXSRC, 0); st(m, 0, REPSRC)
    a.deflabel(cx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('ms_maint'); a.stsl_pr()
    # risposta del telecomando in attesa: appena c'e' posto, alla porta della domanda (non conta come turno)
    ld(m, REPLEN, 5); a.tst_rr(5, 5); nrp = m.newlabel('nrp'); m.bt_far(nrp)
    m.imm32(REPBUF, 4); ld(m, REPSRC, 0); a.tst_rr(0, 0); rpd = m.newlabel('rpd'); rpk = m.newlabel('rpk'); a.bt_lbl(rpd)
    call(m, 'ms_usbpush'); a.tst_rr(0, 0); m.bt_far(nrp); a.bra_lbl(rpk); a.nop()         # -1 = USB spento: risposta scartata
    a.deflabel(rpd); call(m, 'ms_dinsend'); a.tst_rr(0, 0); m.bt_far(nrp)
    a.deflabel(rpk); sti(m, 0, REPLEN)
    a.deflabel(nrp)
    call(m, 'ms_rx')
    ld(m, REPLEN, 5); a.tst_rr(5, 5); nrq = m.newlabel('nrq'); m.bt_far(nrq)              # risposta nuova: subito se c'e' posto
    m.imm32(REPBUF, 4); ld(m, REPSRC, 0); a.tst_rr(0, 0); rqd = m.newlabel('rqd'); rqk = m.newlabel('rqk'); a.bt_lbl(rqd)
    call(m, 'ms_usbpush'); a.tst_rr(0, 0); m.bt_far(nrq); a.bra_lbl(rqk); a.nop()
    a.deflabel(rqd); call(m, 'ms_dinsend'); a.tst_rr(0, 0); m.bt_far(nrq)
    a.deflabel(rqk); sti(m, 0, REPLEN)
    a.deflabel(nrq)
    m.imm32(ONS, 1); a.movb_ld(1, 0); a.tst_imm(0xFF); m.bt_far('ms_mx')
    a.movl_pool('ms_sys', 2); a.jsr(2); a.nop()                                           # v11: viste di sistema
    # 1) sample corrente: r8 = blocco, r9 = lunghezza, r10 = nome (4 byte); cambiato -> nuovo sig, picchi da rifare
    m.imm32(MGR + 8, 1); a.movl_ld(1, 8); a.mov_imm(0, 9); a.mov_imm(0, 10)
    nos = m.newlabel('nos'); a.tst_rr(8, 8); a.bt_lbl(nos); a.movl_ld_disp(8, 0x18, 9); a.movl_ld_disp(8, 4, 10); a.deflabel(nos)
    chg = m.newlabel('chg'); same = m.newlabel('same')
    ld(m, CBLK, 0); a.cmpeq(8, 0); a.bf_lbl(chg); ld(m, CLEN, 0); a.cmpeq(9, 0); a.bf_lbl(chg)
    ld(m, CNAME, 0); a.cmpeq(10, 0); a.bt_lbl(same)
    a.deflabel(chg); st(m, 8, CBLK); st(m, 9, CLEN); st(m, 10, CNAME)
    ld(m, SIG, 0); a.add_imm(1, 0); a.and_imm(0x7F); st(m, 0, SIG); sti(m, 0, READY); sti(m, 0, WSENT)
    a.deflabel(same)
    m.pool_jump()
    # 2) picchi: CPF colonne per fire (r11 = colonna)
    cdone = m.newlabel('cdone')
    a.tst_rr(8, 8); m.bt_far(cdone)
    ld(m, READY, 11); m.imm32(NCOL, 1); a.cmphs(1, 11); m.bt_far(cdone)
    a.mov_imm(CPF, 13)
    cl = m.newlabel('cl'); a.deflabel(cl)
    a.mov_rm_rn(11, 0); a.mul_l(9, 0); a.sts_macl(0); a.shlr8(0)                       # a = c*len >> 8
    a.mov_rm_rn(11, 1); a.add_imm(1, 1); a.mul_l(9, 1); a.sts_macl(1); a.shlr8(1)        # b
    a.sub(0, 1)                                                                          # r1 = n
    a.mov_rm_rn(1, 3); a.shlr2(3); a.shlr2(3); a.shlr(3); s1 = m.newlabel('s1'); a.tst_rr(3, 3); a.bf_lbl(s1); a.mov_imm(1, 3); a.deflabel(s1)
    a.shll(3)                                                                            # r3 = passo in byte
    a.mov_imm(RPC, 5); c32 = m.newlabel('c32'); a.cmphs(5, 1); a.bt_lbl(c32); a.mov_rm_rn(1, 5); a.deflabel(c32)   # r5 = min(n, 32)
    a.shll(0); a.add_rr(8, 0); m.imm32(0x100, 6); a.add_rr(0, 6)                         # r6 = &L[a]
    a.mov_imm(0, 7); pz = m.newlabel('pz'); a.tst_rr(5, 5); a.bt_lbl(pz)
    rl = m.newlabel('rl'); a.deflabel(rl)
    a.movw_ld(6, 0); ab = m.newlabel('ab'); a.cmppz(0); a.bt_lbl(ab); a.neg(0, 0); a.deflabel(ab)
    mx_ = m.newlabel('mx'); a.cmpgt(7, 0); a.bf_lbl(mx_); a.mov_rm_rn(0, 7); a.deflabel(mx_)
    a.add_rr(3, 6); a.dt(5); a.bf_lbl(rl)
    a.deflabel(pz)
    a.shlr8(7); a.mov_imm(127, 0); c7 = m.newlabel('c7'); a.cmpgt(0, 7); a.bf_lbl(c7); a.mov_rm_rn(0, 7); a.deflabel(c7)
    m.imm32(PEAKS, 1); a.add_rr(11, 1); a.movb_st(7, 1)
    a.add_imm(1, 11); a.dt(13); m.bf_far(cl)
    st(m, 11, READY)
    a.deflabel(cdone)
    m.pool_jump()
    # 3) ascolto cambiato: subito (r14 = frame)
    m.imm32(FRAMES, 1); a.movl_ld(1, 14)
    call(m, 'ms_mbox'); a.tst_rr(0, 0); npl = m.newlabel('npl'); m.bt_far(npl)
    m.imm32(MBOX_PLAY, 12); a.add_rr(0, 12); a.movl_ld_disp(12, 16, 13); ld(m, PSEQ, 0); a.cmpeq(13, 0); m.bt_far(npl)
    m.imm32(BUF, 10)
    putc(0xF0); putc(0x7D); putc(0x05); ld(m, SIG, 2); a.movb_st(2, 10); a.add_imm(1, 10)
    a.mov_rm_rn(13, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    a.movl_ld(12, 0); a.and_imm(1); a.movb_st(0, 10); a.add_imm(1, 10)
    a.movl_ld_disp(12, 4, 0); a.add_imm(1, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    a.movl_ld_disp(12, 8, 2); v35(2); a.movl_ld_disp(12, 12, 2); v35(2); putc(0xF7)
    m.imm32(BUF, 4); a.mov_imm(PKT_P, 5); call(m, 'ms_send'); a.tst_rr(0, 0); m.bt_far('ms_full')
    st(m, 13, PSEQ); a.movl_pool('ms_ok', 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(npl)
    # 4) e' ora di un pacchetto?
    ld(m, LASTF, 0); a.mov_rm_rn(14, 2); a.sub(0, 2)
    m.imm32(SLOT, 1); a.cmphs(1, 2); m.bf_far('ms_mx')
    # chi e' in attesa: r11 bit0 trim, bit1 wave, bit2 display.  r12 = start, r13 = end
    a.mov_imm(0, 11); a.mov_imm(0, 12); a.mov_imm(0, 13)
    ns = m.newlabel('ns'); a.tst_rr(8, 8); a.bt_lbl(ns); a.movl_ld_disp(8, 0x20, 12); a.movl_ld_disp(8, 0x24, 13); a.deflabel(ns)
    tp = m.newlabel('tp'); tn = m.newlabel('tn')
    ld(m, TS, 0); a.cmpeq(12, 0); a.bf_lbl(tp); ld(m, TE, 0); a.cmpeq(13, 0); a.bf_lbl(tp); ld(m, TL, 0); a.cmpeq(9, 0); a.bf_lbl(tp)
    ld(m, SIG, 2); ld(m, TSIG, 0); a.cmpeq(2, 0); a.bf_lbl(tp)
    ld(m, TRIMF, 0); a.mov_rm_rn(14, 2); a.sub(0, 2); m.imm32(KEEP, 1); a.cmphs(1, 2); a.bf_lbl(tn)
    a.deflabel(tp); a.mov_imm(1, 11)
    a.deflabel(tn)
    m.pool_jump()
    # forma d'onda: tutta mandata e vecchia di WKEEP -> di nuovo; chunk pronto -> in attesa
    wn = m.newlabel('wn'); wk = m.newlabel('wk')
    ld(m, WSENT, 0); a.mov_imm(8, 1); a.cmpeq(1, 0); a.bf_lbl(wk)
    ld(m, WAVEF, 0); a.mov_rm_rn(14, 2); a.sub(0, 2); m.imm32(WKEEP, 1); a.cmphs(1, 2); a.bf_lbl(wn); sti(m, 0, WSENT)
    a.deflabel(wk)
    ld(m, WSENT, 0); a.shll2(0); a.shll2(0); a.shll(0); a.add_imm(32, 0); ld(m, READY, 2); a.cmphs(0, 2); a.bf_lbl(wn)   # READY >= 32*(k+1)
    a.mov_rm_rn(11, 0); a.or_imm(2); a.mov_rm_rn(0, 11)
    a.deflabel(wn)
    m.pool_jump()
    # display: cambiato (e >= DGAP) o vecchio di KEEP
    dn = m.newlabel('dn'); dp = m.newlabel('dp')
    ld(m, DISPF, 0); a.mov_rm_rn(14, 2); a.sub(0, 2)
    m.imm32(KEEP, 1); a.cmphs(1, 2); a.bt_lbl(dp)
    m.imm32(DGAP, 1); a.cmphs(1, 2); m.bf_far(dn)
    m.imm32(LCD_CUR, 1); a.movw_ld(1, 0); ld(m, LASTCUR, 1); a.cmpeq(1, 0); a.bf_lbl(dp)   # cursore spostato
    m.imm32(LCD_SHADOW, 4); m.imm32(LAST, 5); a.mov_imm(32, 6)
    cc = m.newlabel('cc'); a.deflabel(cc)
    a.movb_ld(4, 0); a.movb_ld(5, 1); a.cmpeq(0, 1); a.bf_lbl(dp); a.add_imm(1, 4); a.add_imm(1, 5); a.dt(6); a.bf_lbl(cc)
    a.bra_lbl(dn); a.nop()
    a.deflabel(dp); a.mov_rm_rn(11, 0); a.or_imm(4); a.mov_rm_rn(0, 11)
    a.deflabel(dn)
    m.pool_jump()
    # tagli: invio in corso, cassetta cambiata (seq) o tagli presenti vecchi di WKEEP
    cpn = m.newlabel('cpn'); cpy = m.newlabel('cpy')
    ld(m, CACT, 0); a.tst_rr(0, 0); a.bf_lbl(cpy)
    call(m, 'ms_mbox'); a.tst_rr(0, 0); sq = m.newlabel('sq'); a.bt_lbl(sq); a.movl_ld_disp(0, 4, 0); a.deflabel(sq)   # r0 = seq (0 senza cassetta)
    ld(m, MBSEQ, 2); a.cmpeq(2, 0); a.bf_lbl(cpy)
    ld(m, CN, 0); a.tst_rr(0, 0); a.bt_lbl(cpn)
    ld(m, CHOPF, 0); a.mov_rm_rn(14, 2); a.sub(0, 2); m.imm32(WKEEP, 1); a.cmphs(1, 2); a.bf_lbl(cpn)
    a.deflabel(cpy); a.mov_rm_rn(11, 0); a.or_imm(8); a.mov_rm_rn(0, 11)
    a.deflabel(cpn)
    # viste: invio in corso o una vista da mandare
    vpn = m.newlabel('vpn'); vpy = m.newlabel('vpy')
    ld(m, VACT, 0); a.tst_rr(0, 0); a.bf_lbl(vpy)
    call(m, 'ms_vfind'); a.tst_rr(0, 0); a.bt_lbl(vpn); st(m, 0, VNEXT)
    a.deflabel(vpy); a.mov_rm_rn(11, 0); a.or_imm(16); a.mov_rm_rn(0, 11)
    a.deflabel(vpn)
    a.tst_rr(11, 11); m.bt_far('ms_mx')
    m.pool_jump()
    # a turno: dopo l'ultimo tipo mandato, il primo in attesa
    ld(m, RR, 0); a.mov_imm(8, 5)
    rr = m.newlabel('rr'); pk = m.newlabel('pk'); a.deflabel(rr)
    a.add_imm(1, 0); a.and_imm(7)
    a.mov_imm(1, 2); a.shad(0, 2); a.tst_rr(2, 11); a.bf_lbl(pk); a.dt(5); a.bf_lbl(rr)
    a.deflabel(pk); st(m, 0, RR)
    m.imm32(BUF, 10)
    kw = m.newlabel('kw'); kd = m.newlabel('kd'); kc = m.newlabel('kc')
    a.mov_imm(1, 1); a.cmpeq(1, 0); m.bt_far(kw); a.mov_imm(2, 1); a.cmpeq(1, 0); m.bt_far(kd); a.mov_imm(3, 1); a.cmpeq(1, 0); m.bt_far(kc)
    kv = m.newlabel('kv'); a.mov_imm(4, 1); a.cmpeq(1, 0); m.bt_far(kv)
    # -- TRIM --
    putc(0xF0); putc(0x7D); putc(0x03); ld(m, SIG, 2); a.movb_st(2, 10); a.add_imm(1, 10)
    v35(9); v35(12); v35(13); putc(0xF7)
    m.imm32(BUF, 4); a.mov_imm(PKT_T, 5); call(m, 'ms_send'); a.tst_rr(0, 0); m.bt_far('ms_full')
    st(m, 9, TL); st(m, 12, TS); st(m, 13, TE); ld(m, SIG, 0); st(m, 0, TSIG); st(m, 14, TRIMF)
    a.movl_pool('ms_ok', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # -- WAVE --
    a.deflabel(kw)
    putc(0xF0); putc(0x7D); putc(0x02); ld(m, SIG, 2); a.movb_st(2, 10); a.add_imm(1, 10)
    ld(m, WSENT, 2); a.movb_st(2, 10); a.add_imm(1, 10)
    a.shll2(2); a.shll2(2); a.shll(2); m.imm32(PEAKS, 1); a.add_rr(1, 2); a.mov_imm(32, 5)
    wl = m.newlabel('wl'); a.deflabel(wl); a.movb_ld(2, 0); a.add_imm(1, 2); a.movb_st(0, 10); a.add_imm(1, 10); a.dt(5); a.bf_lbl(wl)
    putc(0xF7)
    m.imm32(BUF, 4); a.mov_imm(PKT_W, 5); call(m, 'ms_send'); a.tst_rr(0, 0); m.bt_far('ms_full')
    ld(m, WSENT, 0); a.add_imm(1, 0); st(m, 0, WSENT); st(m, 14, WAVEF)
    a.movl_pool('ms_ok', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # -- DISPLAY --
    a.deflabel(kd)
    putc(0xF0); putc(0x7D); putc(0x01); ld(m, SEQ, 2); a.movb_st(2, 10); a.add_imm(1, 10)
    m.imm32(LCD_CUR, 1); a.movw_ld(1, 3); st(m, 3, LASTCUR); a.mov_rm_rn(3, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    m.imm32(LCD_SHADOW, 4); a.mov_imm(32, 5)
    pl = m.newlabel('pl'); a.deflabel(pl)
    a.movb_ld(4, 0); a.mov_rm_rn(0, 3); a.and_imm(0x80); a.tst_rr(0, 0); hi = m.newlabel('hi'); a.bt_lbl(hi); a.mov_imm(0x7F, 3); a.deflabel(hi)
    a.mov_rm_rn(3, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10); a.add_imm(1, 4); a.dt(5); a.bf_lbl(pl)
    putc(0xF7)
    m.imm32(BUF, 4); a.mov_imm(PKT_D, 5); call(m, 'ms_send'); a.tst_rr(0, 0); m.bt_far('ms_full')
    m.imm32(LCD_SHADOW, 4); m.imm32(LAST, 5); a.mov_imm(32, 6)                         # LAST = mandato (stesso fire: identico)
    cp = m.newlabel('cp'); a.deflabel(cp); a.movb_ld(4, 0); a.movb_st(0, 5); a.add_imm(1, 4); a.add_imm(1, 5); a.dt(6); a.bf_lbl(cp)
    ld(m, SEQ, 0); a.add_imm(1, 0); a.and_imm(0x7F); st(m, 0, SEQ); st(m, 14, DISPF)
    a.movl_pool('ms_ok', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # -- CHOP: a inizio invio fotografa seq e n (0 se cassetta assente / tool chiuso / altro sample / altro tipo) --
    a.deflabel(kc)
    call(m, 'ms_mbox'); a.mov_rm_rn(0, 12)                                              # r12 = cassetta (o 0)
    ld(m, CACT, 0); a.tst_rr(0, 0); cs0 = m.newlabel('cs0'); m.bf_far(cs0)
    a.mov_imm(0, 13); a.mov_imm(0, 2); cz = m.newlabel('cz'); a.tst_rr(12, 12); m.bt_far(cz)
    a.movl_ld_disp(12, 4, 2)                                                             # seq
    a.movl_ld_disp(12, 8, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); m.bf_far(cz)                                         # tipo 1 = tagli
    a.movl_ld_disp(12, 0x14, 0); a.tst_rr(0, 0); m.bt_far(cz)                            # tool aperto
    a.movl_ld_disp(12, 0xC, 0); a.cmpeq(8, 0); m.bf_far(cz)                              # stesso sample
    a.movl_ld_disp(12, 0x10, 13); a.mov_imm(MBOX_MAXN, 0); a.cmphs(0, 13); m.bf_far(cz); a.mov_rm_rn(0, 13)   # n (oltre 48 -> 48)
    a.deflabel(cz)
    st(m, 2, MBSEQ); st(m, 13, CN); sti(m, 0, CPART); sti(m, 1, CACT)
    a.deflabel(cs0)
    ld(m, CPART, 9); ld(m, CN, 13)
    a.mov_rm_rn(9, 3); a.shll2(3); a.mov_rm_rn(3, 0); a.add_rr(3, 3); a.add_rr(0, 3)      # r3 = 12*parte
    a.mov_rm_rn(13, 11); a.sub(3, 11); a.mov_imm(CPP, 0); ck = m.newlabel('ck'); a.cmpgt(0, 11); a.bf_lbl(ck); a.mov_rm_rn(0, 11); a.deflabel(ck)   # r11 = k
    ck0 = m.newlabel('ck0'); a.cmppz(11); a.bt_lbl(ck0); a.mov_imm(0, 11); a.deflabel(ck0)
    putc(0xF0); putc(0x7D); putc(0x04); ld(m, SIG, 2); a.movb_st(2, 10); a.add_imm(1, 10)
    ld(m, MBSEQ, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    a.movb_st(9, 10); a.add_imm(1, 10); a.movb_st(13, 10); a.add_imm(1, 10); a.movb_st(11, 10); a.add_imm(1, 10)
    a.shll2(3); a.add_rr(12, 3); a.add_imm(0x20, 3); a.mov_rm_rn(11, 6)                   # r3 = &pos[12*parte]
    cpl = m.newlabel('cpl'); cpe = m.newlabel('cpe'); a.tst_rr(6, 6); a.bt_lbl(cpe)
    a.deflabel(cpl); a.movl_ld(3, 7); a.add_imm(4, 3)
    a.mov_rm_rn(7, 1)
    for _ in range(4):
        a.mov_rm_rn(1, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10); a.shlr2(1); a.shlr2(1); a.shlr2(1); a.shlr(1)
    a.dt(6); a.bf_lbl(cpl)
    a.deflabel(cpe); putc(0xF7)
    m.imm32(BUF, 4); a.mov_rm_rn(10, 5); a.sub(4, 5); call(m, 'ms_send'); a.tst_rr(0, 0); m.bt_far('ms_full')
    a.add_imm(1, 9); st(m, 9, CPART)
    a.mov_rm_rn(9, 3); a.shll2(3); a.mov_rm_rn(3, 0); a.add_rr(3, 3); a.add_rr(0, 3)
    cdn = m.newlabel('cdn'); a.cmphs(13, 3); a.bf_lbl(cdn)                               # 12*parte >= n: finito
    sti(m, 0, CACT); st(m, 14, CHOPF)
    a.deflabel(cdn)
    a.movl_pool('ms_ok', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # -- VIEW: una parte della vista VCUR --
    a.deflabel(kv)
    call(m, 'ms_vbox'); a.mov_rm_rn(0, 12); a.tst_rr(12, 12); vok = m.newlabel('vok'); a.bf_lbl(vok)
    sti(m, 0, VACT); a.movl_pool('ms_mx', 2); a.jmp(2); a.nop()
    a.deflabel(vok)
    ld(m, VACT, 0); a.tst_rr(0, 0); vgo = m.newlabel('vgo'); m.bf_far(vgo)
    ld(m, VNEXT, 0); st(m, 0, VCUR); sti(m, 0, VPART); sti(m, 1, VACT)
    a.shll8(0); a.shll2(0); a.add_rr(12, 0); a.movl_ld_disp(0, V_SEQ, 1); st(m, 1, VSEQ)
    m.pool_jump()
    a.deflabel(vgo)
    ld(m, VCUR, 13); a.mov_rm_rn(13, 11); a.shll8(11); a.shll2(11); a.add_rr(12, 11)        # r11 = casella
    a.movl_ld_disp(11, V_SEQ, 0); ld(m, VSEQ, 1); a.cmpeq(1, 0); vsm = m.newlabel('vsm'); a.bt_lbl(vsm)
    st(m, 0, VSEQ); sti(m, 0, VPART)                                                    # cambiato a meta': da capo
    a.deflabel(vsm)
    a.movl_ld_disp(11, V_LEN, 9); m.imm32(V_PAYMAX, 0); a.cmphs(0, 9); vlc = m.newlabel('vlc'); a.bf_lbl(vlc); a.mov_rm_rn(0, 9); a.deflabel(vlc)
    a.mov_rm_rn(9, 4); a.add_imm(VPART_B - 1, 4); a.mov_imm(VPART_B, 5); pg._hcall(m, '__udiv')
    vn1 = m.newlabel('vn1'); a.tst_rr(0, 0); a.bf_lbl(vn1); a.mov_imm(1, 0); a.deflabel(vn1); st(m, 0, VNPS)   # parti (>= 1)
    m.pool_jump()
    ld(m, VPART, 6); a.mov_rm_rn(6, 3); a.mov_imm(VPART_B, 0); a.mul_l(0, 3); a.sts_macl(3)   # r3 = offset
    a.mov_rm_rn(9, 5); a.sub(3, 5); a.mov_imm(VPART_B, 0); vk1 = m.newlabel('vk1'); a.cmpgt(0, 5); a.bf_lbl(vk1); a.mov_rm_rn(0, 5); a.deflabel(vk1)
    vk2 = m.newlabel('vk2'); a.cmppz(5); a.bt_lbl(vk2); a.mov_imm(0, 5); a.deflabel(vk2)   # r5 = byte di questa parte
    m.imm32(BUF, 10)
    putc(0xF0); putc(0x7D); putc(0x30); a.movb_st(13, 10); a.add_imm(1, 10)
    a.movl_ld_disp(11, V_VER, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    ld(m, VSEQ, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10)
    a.movb_st(6, 10); a.add_imm(1, 10); ld(m, VNPS, 0); a.movb_st(0, 10); a.add_imm(1, 10)
    a.mov_rm_rn(11, 4); a.add_imm(V_PAY, 4); a.add_rr(3, 4)                                # r4 = sorgente
    m.pool_jump()
    # packed7
    pgl = m.newlabel('pgl'); pge = m.newlabel('pge'); a.deflabel(pgl); a.tst_rr(5, 5); m.bt_far(pge)
    a.mov_rm_rn(10, 12); a.add_imm(1, 10); a.mov_imm(0, 3); a.mov_imm(1, 1); a.mov_imm(7, 2)   # r12 = byte alto, r1 = bit
    pbl = m.newlabel('pbl'); pbe = m.newlabel('pbe'); a.deflabel(pbl)
    a.movb_ld(4, 0); a.add_imm(1, 4); a.tst_imm(0x80); hb = m.newlabel('hb'); a.bt_lbl(hb); a.or_rr(1, 3); a.deflabel(hb)
    a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10); a.shll(1)
    a.dt(5); a.bt_lbl(pbe); a.dt(2); a.bf_lbl(pbl)
    a.deflabel(pbe); a.movb_st(3, 12); a.bra_lbl(pgl); a.nop()
    a.deflabel(pge)
    m.imm32(BUF + 2, 1); a.mov_imm(0, 2)                                                    # chk = XOR da 0x30 in poi
    vxl = m.newlabel('vxl'); vxe = m.newlabel('vxe'); a.deflabel(vxl); a.cmpeq(10, 1); a.bt_lbl(vxe)
    a.movb_ld(1, 0); a.xor_rr(0, 2); a.add_imm(1, 1); a.bra_lbl(vxl); a.nop()
    a.deflabel(vxe); a.mov_rm_rn(2, 0); a.and_imm(0x7F); a.movb_st(0, 10); a.add_imm(1, 10); putc(0xF7)
    m.pool_jump()
    m.imm32(BUF, 4); a.mov_rm_rn(10, 5); a.sub(4, 5); call(m, 'ms_send'); a.tst_rr(0, 0); m.bt_far('ms_full')
    ld(m, VPART, 0); a.add_imm(1, 0); st(m, 0, VPART); ld(m, VNPS, 1); a.cmphs(1, 0); vnd = m.newlabel('vnd'); a.bf_lbl(vnd)
    sti(m, 0, VACT); ld(m, VSEQ, 0); m.imm32(VSENT, 1); a.mov_rm_rn(13, 2); a.shll2(2); a.add_rr(2, 1); a.movl_st(0, 1)
    a.deflabel(vnd)
    a.deflabel('ms_ok'); st(m, 14, LASTF); ld(m, SENT, 0); a.add_imm(1, 0); st(m, 0, SENT)
    a.bra_lbl('ms_mx'); a.nop()
    a.deflabel('ms_full'); ld(m, SKIP, 0); a.add_imm(1, 0); st(m, 0, SKIP)
    a.deflabel('ms_mx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    sys_subs(m)
    a.deflabel('ms_end')
pg.on_inactive(inactive_and_subs)

def ref_peaks(L):
    n = len(L); out = []
    for c in range(NCOL):
        a = (c * n) >> 8; b = ((c + 1) * n) >> 8; cnt = b - a; stp = max(cnt >> 5, 1); k = min(cnt, RPC)
        p = max((abs(L[a + i * stp]) for i in range(k)), default=0)
        out.append(min(p >> 8, 127))
    return out

if __name__ == '__main__':
    import math
    out = os.path.join(os.path.dirname(__file__), '..', 'MIDISCR.WAV'); pg.build_wav(out)
    from validate import Harness
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    h.ram_w32(TXWR, TXQ); h.ram_w32(TXRD, TXQ)
    FPF = 128                                             # frame per fire (~344 fire/s come sull'HW)
    got = []                                              # byte ricevuti (come il cavo: la coda si svuota a 3125 B/s)
    ALL = []                                              # tutti i pacchetti (O5_FIXTURE=file.json: li salva per i test JS)
    budget = [0.0]
    def drain(nbytes=None):
        rd = h.ram_r32(TXRD); wr = h.ram_r32(TXWR); k = 0
        while rd != wr and (nbytes is None or k < nbytes):
            got.append(e.rd(rd, 1)); rd = TXQ if rd == TXQ_END else rd + 1; k += 1
        h.ram_w32(TXRD, rd)
    def fire_t(n=1):
        for _ in range(n):
            h.ram_w32(FRAMES, (h.ram_r32(FRAMES) + FPF) & 0xFFFFFFFF); h.fire(1)
            budget[0] += FPF * 3125 / 44100; k = int(budget[0]); budget[0] -= k; drain(k)
    def pkts():
        """pacchetti completi ricevuti (quelli a meta' restano)"""
        res = []; i = 0
        while i < len(got):
            assert got[i] == 0xF0, got[i:i + 8]
            if 0xF7 not in got[i:]: break
            j = got.index(0xF7, i); res.append(got[i + 1:j]); i = j + 1
        del got[:i]; ALL.extend(res); return res
    h.ram_w32(FRAMES, 0xFFFFC000)                         # giro del contatore durante il test
    def v35(b): return sum(b[i] << (7 * i) for i in range(5))
    def mkblock(L, s, en, name=b'KICK'):
        blk = h.list_end(); n = len(L); size = (0x100 + 2 * n + 3) & ~3
        h.ram_w32(blk, size)
        for i, c in enumerate(name.ljust(16, b'\0')): h.ram_w8(blk + 4 + i, c)
        h.ram_w32(blk + 0x18, n); h.ram_w32(blk + 0x20, s); h.ram_w32(blk + 0x24, en); h.ram_w16(blk + 0x92, 1)
        for i, v in enumerate(L): h.ram_w16(blk + 0x100 + 2 * i, v & 0xffff)
        h.ram_w32(blk + size, 0); return blk
    n = 30000
    L = [int(30000 * math.exp(-i / 4000) * math.sin(i / 7)) for i in range(n)]
    blk = mkblock(L, 1000, 25000); h.ram_w32(MGR + 8, blk)
    for i, c in enumerate(b"TRIM st:  1000  end: 25000      "): h.ram_w8(LCD_SHADOW + i, c)
    h.load_hub(); h.load_wav(out); fire_t(400)              # ~1,2 s
    P = pkts()
    assert all(p[0] == 0x7D for p in P)
    T = [p for p in P if p[1] == 3]; W = [p for p in P if p[1] == 2]; D = [p for p in P if p[1] == 1]
    assert T and len(T[0]) == PKT_T - 2 and (v35(T[0][3:8]), v35(T[0][8:13]), v35(T[0][13:18])) == (n, 1000, 25000), T[0]
    sig = T[0][2]; assert all(p[2] == sig for p in T + W)
    wave = [None] * 256
    for p in W:
        assert len(p) == PKT_W - 2; ch = p[3]; wave[32 * ch:32 * ch + 32] = p[4:36]
    assert wave == ref_peaks(L), [i for i in range(256) if wave[i] != ref_peaks(L)[i]][:5]
    assert D and bytes(D[0][4:36]) == b"TRIM st:  1000  end: 25000      " and D[0][3] == 0
    print("  ok all'avvio: trim (len %d, start 1000, end 25000), forma d'onda 256 colonne = modello, display" % n)
    # trim mosso: arriva entro ~1 slot
    h.ram_w32(blk + 0x20, 4321); t0 = 0; T2 = []
    while not T2 and t0 < 60:
        fire_t(1); t0 += 1; T2 = [p for p in pkts() if p[1] == 3 and v35(p[8:13]) == 4321]
    assert T2 and T2[0][2] == sig, t0
    print("  ok start mosso -> pacchetto TRIM ricevuto dopo %d fire (%.0f ms)" % (t0, t0 * FPF * 1000 / 44100))
    # rotella continua sul trim + display che cambia: banda
    nb = 0; sk0 = h.ram_r32(SKIP)
    for k in range(344):
        h.ram_w32(blk + 0x24, 25000 - k * 10); h.ram_w8(LCD_SHADOW + 31, 0x30 + k % 10); fire_t(1)
        nb += sum(len(p) + 2 for p in pkts())
    assert nb <= 0.35 * 3125 and h.ram_r32(SKIP) == sk0, (nb, h.ram_r32(SKIP))
    print("  ok trim e display che cambiano di continuo: %d B in 1 s (%d%% della banda MIDI), niente saltato" % (nb, nb * 100 // 3125))
    # sample cambiato: nuovo sig, nuova forma d'onda
    L2 = [int(20000 * math.sin(i / 3)) if (i // 2000) % 2 else 0 for i in range(9000)]
    b2 = mkblock(L2, 0, 8999, b'SNARE'); h.ram_w32(MGR + 8, b2); fire_t(300)
    P = pkts(); W = [p for p in P if p[1] == 2]; T = [p for p in P if p[1] == 3]
    T = [p for p in T if p[2] == (sig + 1) & 0x7F]; assert T and v35(T[0][3:8]) == 9000
    wave = [None] * 256
    for p in W:
        if p[2] == T[0][2]: wave[32 * p[3]:32 * p[3] + 32] = p[4:36]
    assert wave == ref_peaks(L2)
    print("  ok cambio sample: nuovo numero, forma d'onda nuova = modello")
    # forma d'onda ripetuta ogni 3 s (per chi apre la pagina dopo)
    fire_t(int(WKEEP / FPF) + 60); W = [p for p in pkts() if p[1] == 2]; assert len(W) == 8, len(W)
    # coda piena (clock dell'OS): mai attese, niente scritto
    drain(); got.clear()
    h.ram_w32(TXRD, TXQ + 5); h.ram_w32(TXWR, TXQ + 5 - 10 + 128); before = bytes(e.rd(TXQ + i, 1) for i in range(128))
    sk = h.ram_r32(SKIP); h.ram_w32(b2 + 0x20, 77); h.ram_w32(FRAMES, h.ram_r32(FRAMES) + 4096); h.fire(1)
    assert h.ram_r32(TXWR) == TXQ + 5 - 10 + 128 and bytes(e.rd(TXQ + i, 1) for i in range(128)) == before and h.ram_r32(SKIP) == sk + 1
    h.ram_w32(TXRD, h.ram_r32(TXWR)); got.clear()
    # nessun sample
    h.ram_w32(MGR + 8, 0); fire_t(80); T = [p for p in pkts() if p[1] == 3]; assert T and v35(T[-1][3:8]) == 0
    # v5 tagli: cassetta in B+MBOX_OFF (B = MGR[1] del loader)
    h.ram_w32(MGR + 8, b2); fire_t(300); pkts()
    B = h.ram_r32(MGR + 4); MB = B + MBOX_OFF
    C = [p for p in pkts()]
    def mbox(n, pos, active=1, blk_=None, typ=1):
        h.ram_w32(MB, MBOX_MAGIC); h.ram_w32(MB + 8, typ); h.ram_w32(MB + 0xC, blk_ or b2); h.ram_w32(MB + 0x10, n)
        h.ram_w32(MB + 0x14, active)
        for i, v in enumerate(pos): h.ram_w32(MB + 0x20 + 4 * i, v)
        h.ram_w32(MB + 4, (h.ram_r32(MB + 4) + 1) & 0xFFFFFFFF)
    def chops(nf=120):
        fire_t(nf); res = {}
        for p in pkts():
            if p[1] != 4: continue
            sg, ms, part, n_, k = p[2:7]; assert len(p) == 7 + 4 * k, p
            res.setdefault(ms, {'n': n_, 'sig': sg})
            res[ms][part] = [sum(p[7 + 4 * i + j] << (7 * j) for j in range(4)) for i in range(k)]
        return res
    POS = [i * 300 + (i * i) % 17 for i in range(20)]
    mbox(20, POS); r = chops(); ms = h.ram_r32(MB + 4) & 0x7F
    assert ms in r and r[ms]['n'] == 20 and r[ms][0] + r[ms][1] == POS and r[ms]['sig'] == h.ram_r32(SIG), r
    POS2 = [5, 1000, 2000]; mbox(3, POS2); r = chops(); ms = h.ram_r32(MB + 4) & 0x7F
    assert r[ms]['n'] == 3 and r[ms][0] == POS2
    print("  ok tagli dalla cassetta: 20 posizioni in 2 pacchetti, poi aggiornati (3)")
    mbox(3, POS2, active=0); r = chops(); ms = h.ram_r32(MB + 4) & 0x7F; assert r[ms]['n'] == 0 and r[ms][0] == []
    mbox(3, POS2, blk_=blk); r = chops(); ms = h.ram_r32(MB + 4) & 0x7F; assert r[ms]['n'] == 0
    mbox(3, POS2); r = chops(); assert r[h.ram_r32(MB + 4) & 0x7F]['n'] == 3
    fire_t(int(WKEEP / FPF) + 60); assert any(p[1] == 4 for p in pkts()), "tagli non ripetuti"
    h.ram_w32(MB, 0); r = chops(); assert list(r.values())[-1]['n'] == 0
    print("  ok tool chiuso / altro sample / cassetta assente -> 0 tagli; ripetuti ogni 3 s")
    # v6 cursore del display
    h.ram_w16(LCD_CUR, 21); fire_t(40); D = [p for p in pkts() if p[1] == 1]; assert D and D[-1][3] == 21, D[-1][:5] if D else D
    h.ram_w16(LCD_CUR, 0); fire_t(40); D = [p for p in pkts() if p[1] == 1]; assert D and D[-1][3] == 0
    print("  ok cursore del display: posizione 21 -> pacchetto, spento -> 0")
    # v6 ascolto: parte subito (prima del turno)
    mbox(3, POS2); fire_t(60); pkts(); MP = MB + MBOX_PLAY
    def play(on, k, s_, e_):
        h.ram_w32(MP, on); h.ram_w32(MP + 4, k & 0xFFFFFFFF); h.ram_w32(MP + 8, s_); h.ram_w32(MP + 12, e_); h.ram_w32(MP + 16, h.ram_r32(MP + 16) + 1)
    play(1, 1, 1000, 1999); t = 0; P5 = []
    while not P5 and t < 10: fire_t(1); t += 1; P5 = [p for p in pkts() if p[1] == 5]
    p5 = P5[0]; assert len(p5) == PKT_P - 2 and p5[4:6] == [1, 2] and (v35(p5[6:11]), v35(p5[11:16])) == (1000, 1999), p5
    print("  ok ascolto fetta 2 (1000..1999): pacchetto PLAY ricevuto dopo %d fire (%.0f ms)" % (t, t * FPF * 1000 / 44100))
    play(0, 1, 1000, 1999); fire_t(10); P5 = [p for p in pkts() if p[1] == 5]; assert P5 and P5[0][4] == 0
    play(1, -1, 0, 8999); fire_t(10); P5 = [p for p in pkts() if p[1] == 5]; assert P5 and P5[0][5] == 0
    print("  ok rilascio -> spento; sample intero -> fetta 0")
    # v7 TELECOMANDO (MIDI IN): coda RX dell'OS, risposte nella coda TX
    from shasm import SHAsm
    EVLOG = 0x88E40000; h.ram_w32(EVLOG, 0)
    sa = SHAsm(OS_POST_EVENT)                                     # finto post_event: registra {type|code, data}
    sa.movl_pool(EVLOG, 1); sa.movl_ld(1, 0); sa.add_imm(1, 0); sa.movl_st(0, 1); sa.shll2(0); sa.shll(0); sa.add_rr(0, 1)
    sa.movl_ld(5, 0); sa.movl_st(0, 1); sa.movl_ld_disp(5, 4, 0); sa.movl_st_disp(0, 4, 1); sa.rts(); sa.nop()
    blob = sa.assemble()
    for i in range(0, len(blob), 2): e.wr(OS_POST_EVENT + i, struct.unpack('<H', blob[i:i+2])[0], 2)
    def rx(bs):
        w = h.ram_r32(RXWR)
        for b_ in bs: h.ram_w8(w, b_); w = RXQ if w == RXQ_END else w + 1
        h.ram_w32(RXWR, w)
    def v(x, n): return [(x >> (7 * i)) & 0x7F for i in range(n)]
    def ask(bs, typ, nf=20):
        rx(bs); fire_t(nf); R = [p for p in pkts() if p[1] == typ]; assert R, (typ, bs[:6]); return R[-1]
    h.ram_w32(RXWR, RXQ + 100); h.ram_w32(MYRD, 0); fire_t(2)    # prima lettura: parte da dove scrive l'OS (vicino al giro)
    r = ask([0xF0, 0x7D, 0x10, 5, 0xF7], 0x11); assert r[2:4] == [5, VER], r
    for i, c in enumerate(b"HELLO PEEK TEST!"): h.ram_w8(LCD_SHADOW + i, c)
    r = ask([0xF0, 0x7D, 0x12, 6] + v(LCD_SHADOW, 5) + [16, 0xF7], 0x13)
    assert r[2:4] == [6, 16] and bytes((r[4 + 2 * i] << 4) | r[5 + 2 * i] for i in range(16)) == b"HELLO PEEK TEST!", r
    data = [0xDE, 0xAD, 0x00, 0x7F, 0x80]
    r = ask([0xF0, 0x7D, 0x14, 7] + v(0x88E30000, 5) + [5] + sum(([x >> 4, x & 15] for x in data), []) + [0xF7], 0x15)
    assert r[2] == 7 and [e.rd(0x88E30000 + i, 1) for i in range(5)] == data
    print("  ok telecomando: PING (ver %d), PEEK 16 byte, POKE 5 byte" % VER)
    r = ask([0xF0, 0xF8, 0x7D, 0x16, 8] + v(2, 2) + v(0x1D, 2) + v(0, 5) + [0xF8, 0xF7], 0x17)    # PLAY premuto (clock in mezzo)
    rx([0x90, 0x24, 0x40]); r = ask([0xF0, 0x7D, 0x16, 9] + v(6, 2) + v(0, 2) + v((-3) & 0xFFFFFFFF, 5) + [0xF7], 0x17)  # rotella -3
    n_ = h.ram_r32(EVLOG); ev = [(h.ram_r32(EVLOG + 8 * k + 8), h.ram_r32(EVLOG + 8 * k + 12)) for k in range(n_)]
    assert ev == [(2 | (0x1D << 16), 0), (6, 0xFFFFFFFD)], [(hex(x), hex(y)) for x, y in ev]
    rx([0xF0, 0x7D, 0x10, 1, 0x90, 0xF7]); fire_t(20); assert not [p for p in pkts() if p[1] == 0x11]   # SysEx interrotto: ignorato
    print("  ok EVENT -> post_event dell'OS (tasto PLAY, rotella -3), clock F8 dentro il SysEx ignorato, SysEx interrotto scartato")
    # v8 VISTE: cassetta B+VBOX_OFF, pacchetti 0x30 (packed7 + chk), CAPS
    from mpcmod import VSLOT
    VB = h.ram_r32(MGR + 4) + VBOX_OFF; h.ram_w32(VB, VBOX_MAGIC)
    def unpack7(b):
        out = []
        for i in range(0, len(b), 8):
            hi = b[i]; out += [(x | (0x80 if hi >> j & 1 else 0)) for j, x in enumerate(b[i + 1:i + 8])]
        return bytes(out)
    def vslot(n): return VB + n * VSLOT
    def vput(n, payload, ver=1):
        S_ = vslot(n); h.ram_w32(S_ + V_LEN, len(payload)); h.ram_w32(S_ + V_VER, ver)
        for i, c in enumerate(payload): h.ram_w8(S_ + V_PAY + i, c)
        h.ram_w32(S_ + V_SEQ, (h.ram_r32(S_ + V_SEQ) + 1) & 0xFFFFFFFF)
    alive = set()
    def fire_v(nf):                                        # come un add-on vivo: battito a ogni fire
        res = []
        for _ in range(nf):
            for n_ in alive: h.ram_w32(vslot(n_) + V_ALIVE, h.ram_r32(FRAMES))
            fire_t(1); res += pkts()
        return res
    def views(P):
        got_ = {}
        for p in P:
            if p[1] != 0x30: continue
            assert p[-1] == (eval('^'.join(map(str, p[1:-1]))) & 0x7F), "chk"
            vw, ver, sq, part, npt = p[2:7]
            d = got_.setdefault((vw, sq), {'ver': ver, 'n': npt, 'parts': {}}); d['parts'][part] = unpack7(p[7:-1])
        full = {}
        for (vw, sq), d in got_.items():
            if len(d['parts']) == d['n']: full[(vw, sq)] = (d['ver'], b''.join(d['parts'][i] for i in range(d['n'])))
        return full
    pay = bytes([1]) + bytes((i * 37) & 0xFF for i in range(48))        # vista MUTE di prova (byte > 0x7F inclusi)
    alive.add(4); vput(4, pay); s4 = h.ram_r32(vslot(4) + V_SEQ) & 0x7F
    P = fire_v(200); V = views(P); assert V.get((4, s4)) == (1, pay), V.keys()
    nv = sum(len(p) + 2 for p in P if p[1] == 0x30)
    print("  ok vista MUTE (49 byte, 2 parti packed7 + chk) ricomposta uguale; %d B" % nv)
    big = bytes((i * 101 + 7) & 0xFF for i in range(300)); alive.add(9); vput(9, big, ver=3); s9 = h.ram_r32(vslot(9) + V_SEQ) & 0x7F
    pay2 = bytes([2]) + bytes(48); vput(4, pay2); s4b = h.ram_r32(vslot(4) + V_SEQ) & 0x7F
    P = fire_v(300); V = views(P)
    assert V.get((9, s9)) == (3, big) and V.get((4, s4b)) == (1, pay2), list(V.keys())
    nb = sum(len(p) + 2 for p in P); assert nb <= 0.35 * 3125 * 300 * FPF / 44100, nb
    print("  ok vista da 300 byte (8 parti) + cambio della vista MUTE: entrambe complete, banda %d%%" % (nb * 100 * 44100 // (3125 * 300 * FPF)))
    P = fire_v(int(VKEEP / FPF) + 40); assert any(p[1] == 0x30 and p[2] == 4 for p in P), "vista non ripetuta"
    alive.discard(9); fire_v(int(VALIVE / FPF) + 10); P = fire_v(int(VKEEP / FPF) + 40)
    assert not any(p[1] == 0x30 and p[2] == 9 for p in P) and any(p[1] == 0x30 and p[2] == 4 for p in P)
    print("  ok viste ripetute ogni 2 s; vista senza battito (add-on cancellato) non piu' mandata")
    # seq che cambia a meta' invio: il ricevitore non vede mai un miscuglio
    alive.add(9); vput(9, big); fire_v(3); big2 = bytes(300); vput(9, big2); s9b = h.ram_r32(vslot(9) + V_SEQ) & 0x7F
    P = fire_v(300); V = views(P)
    assert V.get((9, s9b)) == (1, big2) and all(v[1] in (big, big2) for k, v in V.items() if k[0] == 9)
    print("  ok vista cambiata durante l'invio: si riparte, mai parti mescolate")
    for n_ in alive: h.ram_w32(vslot(n_) + V_ALIVE, h.ram_r32(FRAMES))
    r = ask([0xF0, 0x7D, 0x18, 11, 0, 0xF7], 0x19)
    pairs = [tuple(r[5 + 2 * i:7 + 2 * i]) for i in range(r[4])]
    assert r[2:4] == [11, 1] and (4, 1) in pairs and (9, 1) in pairs and {p_[0] for p_ in pairs} <= {4, 9, 7, 10, 11}, r
    assert r[-1] == (eval('^'.join(map(str, r[1:-1]))) & 0x7F), r
    print("  ok CAPS: proto 1, viste vive %s, chk" % pairs)
    # v11 VISTE DI SISTEMA (con un'app collegata: CAPS negli ultimi 10 s) e AZIONE livello
    PROG_ = 3; pbase = PROG_BASE + PROG_ * PROG_STRIDE + PAD_OFF; h.ram_w16(PROG_IDX, PROG_); h.ram_w8(BANKOBJ, 2)
    NM = []
    for p_ in range(48):
        nm = (b'PAD%02d LONGNAME' % p_) if p_ % 5 else b''
        for i, c in enumerate(nm.ljust(16, b'\0')): h.ram_w8(pbase + p_ * PAD_SIZE + i, c)
        l1 = b'' if nm else b'L1 %02d' % p_
        for i, c in enumerate(l1.ljust(16, b'\0')): h.ram_w8(pbase + p_ * PAD_SIZE + 0x18 + i, c)
        h.ram_w8(pbase + p_ * PAD_SIZE + LEVEL, (p_ * 2) % 101); NM.append((nm or l1)[:16].ljust(16, b'\0'))
    ask([0xF0, 0x7D, 0x18, 12, 0, 0xF7], 0x19)
    V = views(fire_v(1500)); last = lambda n_: [v_ for k_, v_ in sorted(V.items()) if k_[0] == n_]
    assert last(10) and last(10)[-1][1] == bytes([PROG_, 0, 2] + [(p_ * 2) % 101 for p_ in range(48)]), last(10)
    assert last(7) and last(7)[-1][1] == bytes([PROG_, 0]) + b''.join(NM), last(7)[-1][1][:40] if last(7) else None
    mm = last(11)[-1][1]; used, tot, ns, fr = struct.unpack('<IIHI', mm[:14])
    assert len(mm) == MEM_LEN and 0 < used < tot and ns >= 3, (used, tot, ns)
    print("  ok viste di sistema: MIX (program, banco, 48 livelli), PADS (48 nomi da 16, layer 1 se vuoto), MEM (%d sample, %d KB usati)" % (ns, used // 1024))
    def act(id_, vista, azione, arg):
        msg = [0xF0, 0x7D, 0x40, id_, vista, azione] + v(arg, 5); x = 0
        for b_ in msg[2:]: x ^= b_
        return ask(msg + [x & 0x7F, 0xF7], 0x41)
    lvl = lambda p_: e.rd(pbase + p_ * PAD_SIZE + LEVEL, 1)
    r = act(13, 10, 0x10, 77 | (5 << 7) | (PROG_ << 13)); assert r[2:4] == [13, 0] and lvl(5) == 77, (r, lvl(5))
    r = act(14, 10, 0x10, 50 | (5 << 7) | (4 << 13)); assert r[2:4] == [14, 3] and lvl(5) == 77, "program cambiato: rifiutata"
    r = act(15, 10, 0x10, 101 | (6 << 7) | (PROG_ << 13)); assert r[2:4] == [15, 3] and lvl(6) == 12, "livello > 100"
    r = act(16, 10, 0x10, 50 | (48 << 7) | (PROG_ << 13)); assert r[2:4] == [16, 3], "pad 48"
    r = act(17, 5, 0x10, 0); assert r[2:4] == [17, 1], "vista che MIDI Screen non conosce"
    assert r[-1] == (eval('^'.join(map(str, r[1:-1]))) & 0x7F)
    V = views(fire_v(600)); assert last(10) and last(10)[-1][1][3 + 5] == 77, "la vista MIX segue il livello nuovo"
    print("  ok azione 'livello' (vista 10): eseguita solo col program giusto, pad < 48, livello <= 100; esiti 0/3/1")
    # OFF
    h.ram_w32(EVQ + 0x68, pg.page); h.ram_w8(ONS, 0); fire_t(40); pkts(); fire_t(400); assert pkts() == []   # (40: l'ultimo pacchetto gia' in coda DIN finisce di uscire)
    print("  ok forma d'onda ripetuta ogni 3 s; coda piena = salta; nessun sample; OFF = silenzio")
    if os.environ.get('O5_FIXTURE'):
        import json; json.dump([[0xF0] + p + [0xF7] for p in ALL], open(os.environ['O5_FIXTURE'], 'w'))
        print("  fixture: %d pacchetti -> %s" % (len(ALL), os.environ['O5_FIXTURE']))
    print("VALIDATO: MIDI Screen v11 (schermo remoto + telecomando MIDI IN + viste + pipe USB) -> MIDISCR.WAV")
