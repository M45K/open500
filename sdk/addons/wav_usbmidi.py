#!/usr/bin/env python3
# USB MIDI (MODE -> OTHER -> "USB MIDI") -> USBMIDI.WAV - prova: la presa USB dell'MPC come interfaccia USB-MIDI (2026-10-03).
#   riga 1: "USB MIDI OFF/ON" (rotella)
#   riga 2: OFF = stato della porta lasciato dall'OS "S18 E00 V1 X0000": STBCR3, USBIER0, VBUS (cavo), EXPFC
#           ON  = "R01 Q05 D3 V1 F10": reset di bus, richieste SETUP, ultimo descrittore, cavo (VBUS), USBIFR0
#   v2 (HW v1: R00 Q00): sulla scheda dell'MPC il pull-up e' ATTIVO BASSO (OS, gestore VBUSF 0x884F2CAA: cavo presente ->
#   PULLUP_E = 0, assente -> 1; init -> 1). Ora come l'OS: a ogni fire PULLUP_E = !VBUSMN.
#   ON: prende la porta (interrupt USB spenti, polling dal fire), si presenta come "Open500 / MPC500 MIDI" (USB-MIDI class
#   compliant, VID:PID 1209:0001 pid.codes di prova), rimanda indietro ogni pacchetto MIDI ricevuto (loopback) e manda
#   ogni 0,5 s il SysEx F0 7D 20 <n> F7. OFF: pull-up spento (il Mac vede lo scollegamento), registri dell'OS rimessi.
# Hardware (drivers/USB.md, manuale SH7727 sez. 22-23): USB function controller 0xA4000240.. ; STBCR3 0xA4000230 bit4 =
#   MSTP14 (clock USBF), EXCPGCR 0xA4000236 = 0x28 (48 MHz dal clock di bus, come l'OS), EXPFC 0xA4000234 bit0 = USB
#   function, bit1 = 0 transceiver interno; USBDMAR 0xA4000251 bit2 = PULLUP_E (sulla scheda: 0 = collegato). L'hardware risponde da
#   solo a SET_ADDRESS/SET_CONFIGURATION/...; il software solo a GET_DESCRIPTOR (EP0 da 8 byte) e ai dati EP1/EP2.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti
from mpcmod import EVQ
from upipe import emit_base, UPIPE_MAGIC, U_MAGIC, U_ALIVE, U_TXW, U_TXR, U_RXW, U_RXR, U_TX, U_TXSZ, U_RX, U_RXSZ
# v4 (2026-10-16) PIPE: niente piu' loopback. Con la porta presa l'add-on apre la pipe (sdk/upipe.py) e da' il battito:
#   EP1 (dal telefono/computer) -> pacchetti USB-MIDI tradotti in byte MIDI -> coda RX (li legge MIDI Screen: CAPS, telecomando)
#   coda TX (pacchetti SysEx di MIDI Screen) -> pacchetti USB-MIDI (CIN 4 / 5-6-7 fine SysEx) -> EP2, max 16 per giro.
#   L'hello F0 7D 20 n F7 parte solo a coda vuota e fuori da un SysEx a meta'.
# v7 (2026-10-26, HW: "c'e' l'heartbeat ma se schiaccio i pad non succede nulla"; scelta utente: interfaccia MIDI vera
#   nelle due direzioni, nella 0.2). L'USB fa da seconda MIDI OUT / MIDI IN dell'MPC:
#   - MPC -> computer: la coda MIDI OUT dell'OS (128 B 0x8848CB78..CBF7, scrittura [0x881C0674]) letta con un indice
#     nostro (MRD, l'OS non se ne accorge) e tradotta in pacchetti USB-MIDI (running status, tempo reale, SysEx, comuni).
#     I SysEx F0 7D (dello schermo, se va sulla DIN) non si copiano. Tempo reale (F8 clock, FA/FB/FC...) subito, anche
#     dentro un SysEx dello schermo (lo permette il MIDI); note/controller solo fuori da un SysEx dello schermo (MIDSX),
#     per non spezzarlo: nel buffer di EP2 prima il tempo reale, poi il resto, poi lo schermo.
#   - computer -> MPC: pacchetti EP1 dei SysEx F0 7D -> pipe (MIDI Screen) come prima; tutto il resto -> coda MIDI IN
#     dell'OS (128 B 0x8848CBF8..CC77, scrittura [0x881C067C], l'OS legge con [0x881C0680]) a interrupt bloccati, come
#     la ricezione della DIN: l'OS suona, si sincronizza, registra. Coda piena = byte perso (mai sopra la lettura).
#   - Se i puntatori dell'OS non sono dentro le loro code non si legge e non si scrive niente.

U = 0xA4000240
IFR0, IFR1, EPDR0I, EPDR0O, TRG, FCLR, EPSZ0O, EPDR0S, DASTS, EPDR2, ISR0, EPSTL, IER0, IER1, EPDR1, EPSZ1, ISR1, DMAR = (
    U + 0, U + 1, U + 2, U + 3, U + 4, U + 5, U + 6, U + 7, U + 8, U + 9, U + 0xA, U + 0xB, U + 0xC, U + 0xD, U + 0xE, U + 0xF, U + 0x10, U + 0x11)
STBCR3, EXPFC, EXCPGCR, PDCR, PECR = 0xA4000230, 0xA4000234, 0xA4000236, 0xA4000106, 0xA4000108
FRAMES = 0x881C05F0
HELLO_GAP = 22050
TXQ, TXQ_END, TXWR = 0x8848CB78, 0x8848CBF7, 0x881C0674                # MIDI OUT dell'OS (v7)
RXQ, RXQ_END, RXWR, RXRD = 0x8848CBF8, 0x8848CC77, 0x881C067C, 0x881C0680   # MIDI IN dell'OS (v7)
MQN, RTN = 32, 8                                                       # eventi in attesa di EP2: MIDI / tempo reale

def _s(t): b = t.encode('utf-16-le'); return bytes([len(b) + 2, 3]) + b
DEV = bytes([0x12, 0x01, 0x10, 0x01, 0, 0, 0, 8, 0x09, 0x12, 0x01, 0x00, 0x00, 0x01, 1, 2, 0, 1])
MS = (bytes([7, 0x24, 1, 0, 1, 0x41, 0]) +
      bytes([6, 0x24, 2, 1, 1, 0]) + bytes([6, 0x24, 2, 2, 2, 0]) +
      bytes([9, 0x24, 3, 1, 3, 1, 2, 1, 0]) + bytes([9, 0x24, 3, 2, 4, 1, 1, 1, 0]) +
      bytes([9, 5, 0x01, 2, 64, 0, 0, 0, 0]) + bytes([5, 0x25, 1, 1, 1]) +
      bytes([9, 5, 0x82, 2, 64, 0, 0, 0, 0]) + bytes([5, 0x25, 1, 1, 3]))
BODY = (bytes([9, 4, 0, 0, 0, 1, 1, 0, 0]) + bytes([9, 0x24, 1, 0, 1, 9, 0, 1, 1]) +
        bytes([9, 4, 1, 0, 2, 1, 3, 0, 0]) + MS)
CFG = bytes([9, 2, (9 + len(BODY)) & 0xFF, (9 + len(BODY)) >> 8, 2, 1, 0, 0x80, 50]) + BODY
assert len(CFG) == 101 and len(MS) == 0x41
STR = [bytes([4, 3, 0x09, 0x04]), _s("Open500"), _s("MPC500 MIDI")]
DESCS = [(1, 0, DEV), (2, 0, CFG)] + [(3, i, s) for i, s in enumerate(STR)]   # (tipo, indice, dati)

pg = PageAddon("usbmidi1", open_press=None)
pg.other_menu("USB MIDI")
pg.far_hend = True
pg.far_fire = True                                     # v7: la parte a pagina aperta supera i 4 KB
ONS = pg.choice("USB MIDI ", 0, 0, ["OFF", "ON "], default=0)
pg.label("L", 13, 0)
LOGI = pg.value("", 14, 0, fmt="00", lo=0, hi=24, default=0)        # = NLOG
L2 = pg.dlabel(0, 1, 16)
TAKEN, SV_ST, SV_IE0, SV_IE1, SV_IS0, SV_IS1, SV_DMA, SV_EXP = [pg.alloc_slot() for _ in range(8)]
NRST, NSET, LASTD, NIN, CPTR, CLEFT, CZLP, HELLOF, HCNT = [pg.alloc_slot() for _ in range(9)]
LBN = pg.alloc_slot()                                  # byte in attesa di andare su EP2 (pipe + hello)
MIDSX = pg.alloc_slot()                                # 1 = l'ultimo pacchetto mandato ha lasciato un SysEx a meta'
LB = [pg.alloc_slot() for _ in range(16)][0]           # 64 byte
SETUPB = [pg.alloc_slot() for _ in range(2)][0]        # 8 byte del SETUP
NPK, NTS, NOS, NTR = [pg.alloc_slot() for _ in range(4)]   # diagnosi: pacchetti EP0 scritti, EP0i TS, EP0o TS, EP0i TR
# v5 (2026-10-17, PIANO.md 3.2): REGISTRO delle prime NLOG richieste SETUP dopo la presa (8 byte + contatori EP0i TS e reset
#   in quel momento), da leggere a schermo: campo "L" (rotella) sulla riga 1, la riga 2 mostra la richiesta L
#   "bmRT bReq wValH wValL wLenL TS R" = "80060100 40 03 1". Per capire l'enumerazione diretta (iPad/Mac: R08 Q10 D1).
#   EP0 piu' robusto: al SETUP si cancellano anche i flag EP0 vecchi (un TS rimasto faceva scrivere un secondo pacchetto
#   nel FIFO da 8 byte ancora pieno); alla fase di stato (host ha finito) niente piu' pacchetti e FIFO EP0i svuotato.
#   OFF: flag USB cancellati e interrupt bloccati PRIMA di rimettere gli interrupt dell'OS (prima: interrupt USB dell'OS
#   con flag nostri ancora alzati; candidato per i riavvii con ON/OFF ripetuti).
NLOG, LOGE = 24, 12
# v6 (2026-10-17, HW: "se passo velocemente da ON a OFF l'MPC si riavvia e resta bloccato all'avvio"):
#   - ON/OFF con calma: il campo deve restare fermo ON_SETTLE frame prima di agire, e fra una presa e un rilascio
#     passano almeno ON_GAP frame (il host vede lo scollegamento, la porta non viene presa e lasciata a raffica);
#   - OFF rimette TUTTO come prima della presa, anche i piedini (PDCR/PECR) e il clock USB (EXCPGCR): il manuale SH7727
#     dice che PDCR/PECR NON tornano ai valori iniziali con un manual reset (riavvio da errore): l'OS ripartiva con i
#     piedini e il clock lasciati da noi;
#   - la porta non si prende se l'OS ha gia' il modulo USB acceso (modalita' disco).
ON_SETTLE, ON_GAP = 13230, 44100                      # 0,3 s / 1 s
EFF, ONREQ, ONT, LASTCHG, SV_PD, SV_PE, SV_CPG = [pg.alloc_slot() for _ in range(7)]
pg.defaults[LASTCHG] = (-ON_GAP) & 0xFFFFFFFF          # ultimo cambio "da tanto": la prima presa non aspetta
LOGN = pg.alloc_slot()
LOGB = [pg.alloc_slot() for _ in range(NLOG * LOGE // 4)][0]
# v7: interfaccia MIDI
MRD, MST, MCUR, MNEED, MCNT, MB1, MSX, MSXN, MSXB0, MSXB1, RXSX = [pg.alloc_slot() for _ in range(11)]
MQW, MQR, RTW, RTR, NOSRX, NMIR = [pg.alloc_slot() for _ in range(6)]
MQ = pg.alloc_buf(4 * MQN); RTQ = pg.alloc_buf(4 * RTN)

def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()
def rb(m, addr, r): m.imm32(addr, 1); m.a.movb_ld(1, r); m.a.extu_b(r, r)
def wb(m, addr, v): m.imm32(addr, 1); m.a.mov_imm(v if v < 128 else v - 256, 0); m.a.movb_st(0, 1)

def fire(m):
    a = m.a
    call(m, 'um_poll')
    # riga 2
    m.imm32(pg.addr['dlabels'][L2], 12)
    def putc(ch): a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    def hx(r, n): a.mov_rm_rn(r, 4); a.mov_imm(n, 13); call(m, 'um_hex')
    ld(m, TAKEN, 0); a.tst_rr(0, 0); tk = m.newlabel('tk'); m.bf_far(tk)
    putc('S'); rb(m, STBCR3, 7); hx(7, 2); putc(' '); putc('E'); rb(m, IER0, 7); hx(7, 2); putc(' ')
    putc('V'); rb(m, IFR1, 7); a.mov_rm_rn(7, 0); a.shlr2(0); a.shlr(0); a.and_imm(1); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    putc(' '); putc('X'); m.imm32(EXPFC, 1); a.movw_ld(1, 7); a.extu_w(7, 7); hx(7, 4); putc(' ')
    rl = m.newlabel('rl'); a.bra_lbl(rl); a.nop()
    m.pool_jump()
    a.deflabel(tk)
    # v5: campo L > 0 -> voce L del registro SETUP
    vx = m.newlabel('vx'); nlg = m.newlabel('nlg'); ld(m, LOGI, 8); a.tst_rr(8, 8); m.bt_far(nlg)
    a.add_imm(-1, 8); ld(m, LOGN, 0); a.cmphs(0, 8); lgv = m.newlabel('lgv'); a.bf_lbl(lgv)
    for ch in "--              ": putc(ch)
    a.movl_pool(vx, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(lgv); a.mov_imm(LOGE, 0); a.mul_l(0, 8); a.sts_macl(8); m.imm32(LOGB, 0); a.add_rr(0, 8)
    for off, n in ((0, 2), (1, 2), (3, 2), (2, 2)):
        a.mov_rm_rn(8, 1); a.add_imm(off, 1); a.movb_ld(1, 7); a.extu_b(7, 7); a.movl_st_dec(8, 15); hx(7, n); a.movl_ld_inc(15, 8)
    putc(' '); a.mov_rm_rn(8, 1); a.add_imm(6, 1); a.movb_ld(1, 7); a.extu_b(7, 7); a.movl_st_dec(8, 15); hx(7, 2); a.movl_ld_inc(15, 8)
    putc(' '); a.mov_rm_rn(8, 1); a.add_imm(8, 1); a.movb_ld(1, 7); a.extu_b(7, 7); a.movl_st_dec(8, 15); hx(7, 2); a.movl_ld_inc(15, 8)
    putc(' '); a.mov_rm_rn(8, 1); a.add_imm(9, 1); a.movb_ld(1, 7); a.extu_b(7, 7); hx(7, 1)
    a.movl_pool(vx, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(nlg)
    # v3 diagnosi: la riga alterna (ogni ~1,5 s) contatori / ultimo SETUP (8 byte hex) / "P.. T.. O.. N.."
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); a.shlr16(0); a.mov_rm_rn(0, 6); a.mov_imm(3, 1); a.and_rr(1, 6)
    v1 = m.newlabel('v1'); v2 = m.newlabel('v2')
    a.mov_imm(1, 1); a.cmpeq(1, 6); m.bt_far(v1); a.mov_imm(2, 1); a.cmpeq(1, 6); m.bt_far(v2)
    a.movl_pool('um_v0', 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(v1)
    m.imm32(SETUPB, 8); a.mov_imm(8, 9)
    sb = m.newlabel('sb'); a.deflabel(sb); a.movb_ld(8, 7); a.extu_b(7, 7); a.add_imm(1, 8)
    a.movl_st_dec(9, 15); a.movl_st_dec(8, 15); hx(7, 2); a.movl_ld_inc(15, 8); a.movl_ld_inc(15, 9); a.dt(9); a.bf_lbl(sb)
    a.movl_pool(vx, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(v2)
    putc('P'); ld(m, NPK, 7); hx(7, 2); putc(' '); putc('T'); ld(m, NTS, 7); hx(7, 2); putc(' ')
    putc('O'); ld(m, NOS, 7); hx(7, 2); putc(' '); putc('N'); ld(m, NTR, 7); hx(7, 2)
    a.movl_pool(vx, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel('um_v0')
    putc('R'); ld(m, NRST, 7); hx(7, 2); putc(' '); putc('Q'); ld(m, NSET, 7); hx(7, 2); putc(' ')
    putc('D'); ld(m, LASTD, 7); hx(7, 1); putc(' ')
    putc('V'); rb(m, IFR1, 7); a.mov_rm_rn(7, 0); a.shlr2(0); a.shlr(0); a.and_imm(1); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
    putc(' '); putc('F'); rb(m, IFR0, 7); hx(7, 2)
    a.deflabel(vx)
    a.deflabel(rl)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.movl_pool('um_end', 2); a.jmp(2); a.nop()
    m.pool_jump()
    # ---- um_hex: r4 in esadecimale, r13 cifre, a r12 ----
    a.deflabel('um_hex'); a.add_rr(13, 12); a.mov_rm_rn(12, 11)
    hl = m.newlabel('hl'); a.deflabel(hl); a.mov_rm_rn(4, 0); a.and_imm(15); a.add_imm(0x30, 0)
    d9 = m.newlabel('d9'); a.mov_imm(0x3A, 1); a.cmpge(1, 0); a.bf_lbl(d9); a.add_imm(7, 0); a.deflabel(d9)
    a.add_imm(-1, 11); a.movb_st(0, 11); a.shlr2(4); a.shlr2(4); a.dt(13); a.bf_lbl(hl)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- um_poll: a ogni fire (anche fuori pagina) ----
    a.deflabel('um_poll'); a.stsl_pr()
    # v6: stato effettivo EFF = campo ONS solo dopo ON_SETTLE frame fermo e ON_GAP frame dall'ultimo cambio
    m.imm32(ONS, 1); a.movb_ld(1, 4); a.extu_b(4, 4); m.imm32(FRAMES, 1); a.movl_ld(1, 5)        # r4 = campo, r5 = frame
    efd = m.newlabel('efd'); efs = m.newlabel('efs')
    ld(m, ONREQ, 0); a.cmpeq(4, 0); a.bt_lbl(efs)
    st(m, 4, ONREQ); st(m, 5, ONT); a.bra_lbl(efd); a.nop()                                 # campo appena cambiato
    a.deflabel(efs)
    ld(m, EFF, 0); a.cmpeq(4, 0); a.bt_lbl(efd)
    ld(m, ONT, 0); a.mov_rm_rn(5, 2); a.sub(0, 2); m.imm32(ON_SETTLE, 3); a.cmphs(3, 2); a.bf_lbl(efd)
    ld(m, LASTCHG, 0); a.mov_rm_rn(5, 2); a.sub(0, 2); m.imm32(ON_GAP, 3); a.cmphs(3, 2); a.bf_lbl(efd)
    st(m, 4, EFF); st(m, 5, LASTCHG)
    a.deflabel(efd)
    m.pool_jump()
    ld(m, EFF, 0); a.tst_rr(0, 0); on = m.newlabel('on'); m.bf_far(on)
    # OFF: se avevamo la porta, la rendiamo
    ld(m, TAKEN, 0); a.tst_rr(0, 0); m.bt_far('um_px')
    a.stc_sr(13); a.mov_rm_rn(13, 0); a.or_imm(0xF0); a.ldc_sr(0)                          # v5: interrupt bloccati
    rb(m, DMAR, 0); a.or_imm(4); m.imm32(DMAR, 1); a.movb_st(0, 1)                         # PULLUP_E = 1: scollegato
    wb(m, FCLR, 0x73); wb(m, IFR0, 0); wb(m, IFR1, 0)                                      # v5: niente flag nostri per l'OS
    for slot, reg in ((SV_IE0, IER0), (SV_IE1, IER1), (SV_IS0, ISR0), (SV_IS1, ISR1)):
        ld(m, slot, 0); m.imm32(reg, 1); a.movb_st(0, 1)
    ld(m, SV_DMA, 0); a.or_imm(4); m.imm32(DMAR, 1); a.movb_st(0, 1)                      # (resta scollegato)
    ld(m, SV_EXP, 0); m.imm32(EXPFC, 1); a.movw_st(0, 1)
    ld(m, SV_ST, 0); m.imm32(STBCR3, 1); a.movb_st(0, 1)
    ld(m, SV_PD, 0); m.imm32(PDCR, 1); a.movw_st(0, 1); ld(m, SV_PE, 0); m.imm32(PECR, 1); a.movw_st(0, 1)   # v6
    ld(m, SV_CPG, 0); m.imm32(EXCPGCR, 1); a.movb_st(0, 1)
    a.ldc_sr(13)
    call(m, 'um_pb'); a.tst_rr(0, 0); po = m.newlabel('po'); a.bt_lbl(po); a.mov_imm(0, 1); a.movl_st_disp(1, U_ALIVE, 0); a.deflabel(po)
    sti(m, 0, TAKEN); a.movl_pool('um_px', 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(on)
    ld(m, TAKEN, 0); a.tst_rr(0, 0); tkn = m.newlabel('tkn'); m.bf_far(tkn)
    # presa: salva, interrupt USB spenti, clock 48 MHz, pin, funzione, FIFO puliti, pull-up acceso
    rb(m, STBCR3, 0); a.tst_imm(0x10); m.bt_far('um_px')                                   # v6: modulo USB gia' acceso (OS): non si prende
    a.stc_sr(13); a.mov_rm_rn(13, 0); a.or_imm(0xF0); a.ldc_sr(0)                          # v5: interrupt bloccati
    m.imm32(PDCR, 1); a.movw_ld(1, 0); a.extu_w(0, 0); st(m, 0, SV_PD)                    # v6: piedini e clock da rimettere
    m.imm32(PECR, 1); a.movw_ld(1, 0); a.extu_w(0, 0); st(m, 0, SV_PE)
    rb(m, EXCPGCR, 0); st(m, 0, SV_CPG)
    rb(m, STBCR3, 0); st(m, 0, SV_ST); rb(m, IER0, 0); st(m, 0, SV_IE0); rb(m, IER1, 0); st(m, 0, SV_IE1)
    rb(m, ISR0, 0); st(m, 0, SV_IS0); rb(m, ISR1, 0); st(m, 0, SV_IS1); rb(m, DMAR, 0); st(m, 0, SV_DMA)
    m.imm32(EXPFC, 1); a.movw_ld(1, 0); a.extu_w(0, 0); st(m, 0, SV_EXP)
    wb(m, IER0, 0); wb(m, IER1, 0); wb(m, ISR0, 0); wb(m, ISR1, 0)
    a.ldc_sr(13)
    rb(m, STBCR3, 0); a.or_imm(0x18); m.imm32(STBCR3, 1); a.movb_st(0, 1)                  # moduli USB fermi per il clock
    wb(m, EXCPGCR, 0x28)
    m.imm32(PDCR, 1); a.movw_ld(1, 0); m.imm32(0xCFFF, 2); a.and_rr(2, 0); a.movw_st(0, 1)
    m.imm32(PECR, 1); a.movw_ld(1, 0); m.imm32(0xFFCF, 2); a.and_rr(2, 0); a.movw_st(0, 1)
    m.imm32(EXPFC, 1); a.movw_ld(1, 0); a.and_imm(0xFD); a.or_imm(1); a.movw_st(0, 1)
    rb(m, STBCR3, 0); a.and_imm(0xE7); m.imm32(STBCR3, 1); a.movb_st(0, 1)                 # USB function acceso
    wb(m, FCLR, 0x73); wb(m, IFR0, 0); wb(m, IFR1, 0)
    for s_ in (NRST, NSET, LASTD, NIN, CLEFT, CZLP, LBN, HCNT, NPK, NTS, NOS, NTR, LOGN): sti(m, 0, s_)
    for s_ in (MRD, MST, MCUR, MSX, MSXN, RXSX, MQW, MQR, RTW, RTR): sti(m, 0, s_)       # v7: MIDI da qui in poi
    m.imm32(FRAMES, 1); a.movl_ld(1, 0); st(m, 0, HELLOF)
    rb(m, DMAR, 0); a.or_imm(4); m.imm32(DMAR, 1); a.movb_st(0, 1)                         # come l'OS: parte scollegato
    sti(m, 0, MIDSX)
    call(m, 'um_pb'); a.tst_rr(0, 0); pi = m.newlabel('pi'); a.bt_lbl(pi)                  # pipe vuota, firma
    a.mov_imm(0, 1)
    for off in (U_TXW, U_TXR, U_RXW, U_RXR): a.movl_st_disp(1, off, 0)
    m.imm32(UPIPE_MAGIC, 1); a.movl_st(1, 0)
    a.deflabel(pi)
    sti(m, 1, TAKEN)
    a.deflabel(tkn)
    m.pool_jump()
    call(m, 'um_pb'); a.tst_rr(0, 0); pa = m.newlabel('pa'); a.bt_lbl(pa)
    m.imm32(FRAMES, 1); a.movl_ld(1, 1); a.movl_st_disp(1, U_ALIVE, 0); a.deflabel(pa)          # battito: MIDI Screen ci scrive
    # ---- polling ----
    rb(m, IFR1, 0); a.tst_imm(0x01); vf = m.newlabel('vf'); a.bt_lbl(vf); wb(m, IFR1, 0xFE); a.deflabel(vf)   # VBUSF visto
    rb(m, IFR1, 2); rb(m, DMAR, 0); a.or_imm(4); a.mov_rm_rn(0, 3); a.mov_rm_rn(2, 0); a.tst_imm(0x08)
    vb = m.newlabel('vb'); a.bt_lbl(vb); a.mov_rm_rn(3, 0); a.and_imm(0xFB); a.mov_rm_rn(0, 3); a.deflabel(vb)
    m.imm32(DMAR, 1); a.movb_st(3, 1)                                                      # cavo presente -> collegato (bit 0)
    rb(m, IFR0, 8)                                                                         # r8 = IFR0
    # reset di bus
    nr = m.newlabel('nr'); a.mov_rm_rn(8, 0); a.tst_imm(0x80); a.bt_lbl(nr)
    wb(m, IFR0, 0x7F); wb(m, FCLR, 0x73); sti(m, 0, CLEFT); sti(m, 0, CZLP); sti(m, 0, LBN); sti(m, 0, MIDSX)
    call(m, 'um_pb'); a.tst_rr(0, 0); br = m.newlabel('br'); a.bt_lbl(br); a.movl_ld_disp(0, U_TXW, 1); a.movl_st_disp(1, U_TXR, 0); a.deflabel(br)
    ld(m, NRST, 0); a.add_imm(1, 0); st(m, 0, NRST)
    a.deflabel(nr)
    m.pool_jump()
    # SETUP
    ns = m.newlabel('ns'); a.mov_rm_rn(8, 0); a.tst_imm(0x08); m.bt_far(ns)
    wb(m, IFR0, 0xF0); wb(m, FCLR, 0x03); sti(m, 0, CLEFT); sti(m, 0, CZLP)               # v5: anche TS/TR/EP0o vecchi
    m.imm32(SETUPB, 2); a.mov_imm(8, 3); m.imm32(EPDR0S, 1)
    sl = m.newlabel('sl'); a.deflabel(sl); a.movb_ld(1, 0); a.movb_st(0, 2); a.add_imm(1, 2); a.dt(3); a.bf_lbl(sl)
    # v5: registro (prime NLOG richieste): 8 byte, EP0i TS e reset contati fin qui
    lgx = m.newlabel('lgx'); ld(m, LOGN, 4); a.mov_imm(NLOG, 0); a.cmphs(0, 4); m.bt_far(lgx)
    a.mov_rm_rn(4, 0); a.add_imm(1, 0); st(m, 0, LOGN)
    a.mov_imm(LOGE, 0); a.mul_l(0, 4); a.sts_macl(4); m.imm32(LOGB, 0); a.add_rr(0, 4)
    m.imm32(SETUPB, 2); a.mov_imm(8, 3)
    lgl = m.newlabel('lgl'); a.deflabel(lgl); a.movb_ld(2, 0); a.movb_st(0, 4); a.add_imm(1, 2); a.add_imm(1, 4); a.dt(3); a.bf_lbl(lgl)
    ld(m, NTS, 0); a.movb_st(0, 4); a.add_imm(1, 4); ld(m, NRST, 0); a.movb_st(0, 4)
    a.deflabel(lgx)
    wb(m, TRG, 0x04)                                                                       # EP0s letto: via alla fase dati
    ld(m, NSET, 0); a.add_imm(1, 0); st(m, 0, NSET)
    m.imm32(SETUPB, 9); a.movb_ld(9, 10); a.extu_b(10, 10)                                 # r10 = bmRequestType
    a.mov_rm_rn(9, 1); a.add_imm(1, 1); a.movb_ld(1, 11); a.extu_b(11, 11)                 # r11 = bRequest
    stall = m.newlabel('stall'); okc = m.newlabel('okc')
    m.imm32(0x80, 0); a.cmpeq(0, 10); m.bf_far(stall); a.mov_imm(6, 0); a.cmpeq(0, 11); m.bf_far(stall)
    a.mov_rm_rn(9, 1); a.add_imm(3, 1); a.movb_ld(1, 12); a.extu_b(12, 12)                 # r12 = tipo
    a.mov_rm_rn(9, 1); a.add_imm(2, 1); a.movb_ld(1, 13); a.extu_b(13, 13)                 # r13 = indice
    st(m, 12, LASTD)
    a.mov_rm_rn(9, 1); a.add_imm(6, 1); a.movb_ld(1, 14); a.extu_b(14, 14); a.add_imm(1, 1); a.movb_ld(1, 0); a.extu_b(0, 0)
    a.shll8(0); a.or_rr(0, 14)                                                             # r14 = wLength
    # cerca il descrittore nella tabella (tipo, indice, offset, lunghezza)
    a.movl_pool('um_dtab', 4); a.mov_imm(len(DESCS), 5)
    dl = m.newlabel('dl'); dn = m.newlabel('dn'); a.deflabel(dl)
    a.movb_ld(4, 0); a.extu_b(0, 0); a.cmpeq(12, 0); a.bf_lbl(dn)
    a.mov_rm_rn(4, 1); a.add_imm(1, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.cmpeq(13, 0); a.bf_lbl(dn)
    a.mov_rm_rn(4, 1); a.add_imm(2, 1); a.movw_ld(1, 6); a.extu_w(6, 6); a.movl_pool('um_ddata', 7); a.add_rr(7, 6)   # r6 = dati
    a.mov_rm_rn(4, 1); a.add_imm(4, 1); a.movw_ld(1, 7); a.extu_w(7, 7)                     # r7 = lunghezza
    a.movl_pool(okc, 2); a.jmp(2); a.nop()
    a.deflabel(dn); a.add_imm(8, 4); a.dt(5); a.bf_lbl(dl)
    m.pool_jump()
    a.deflabel(stall)                                                                      # richiesta che non gestiamo
    rb(m, EPSTL, 0); a.or_imm(1); m.imm32(EPSTL, 1); a.movb_st(0, 1); a.movl_pool(ns, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(okc)
    zl = m.newlabel('zl'); a.cmphs(7, 14); a.bf_lbl(zl)                                     # wLength >= len: tutto (+ ZLP se multiplo di 8)
    a.mov_rm_rn(7, 0); a.and_imm(7); a.tst_rr(0, 0); zz = m.newlabel('zz'); a.bf_lbl(zz)
    a.cmpeq(7, 14); a.bt_lbl(zz); sti(m, 1, CZLP)
    a.deflabel(zz); a.mov_rm_rn(7, 14)
    a.deflabel(zl)                                                                         # r14 = byte da mandare
    st(m, 6, CPTR); st(m, 14, CLEFT); call(m, 'um_ep0tx')
    a.deflabel(ns)
    m.pool_jump()
    # EP0i trasmesso: pacchetto successivo
    n0 = m.newlabel('n0'); rb(m, IFR0, 8); a.mov_rm_rn(8, 0); a.tst_imm(0x01); a.bt_lbl(n0)
    wb(m, IFR0, 0xFE); ld(m, NTS, 0); a.add_imm(1, 0); st(m, 0, NTS); call(m, 'um_ep0tx')
    a.deflabel(n0)
    # EP0o ricevuto (fase di stato di un control-in): via
    no = m.newlabel('no'); rb(m, IFR0, 8); a.mov_rm_rn(8, 0); a.tst_imm(0x04); a.bt_lbl(no)
    wb(m, IFR0, 0xFB); wb(m, TRG, 0x02); ld(m, NOS, 0); a.add_imm(1, 0); st(m, 0, NOS)
    ld(m, CLEFT, 0); ld(m, CZLP, 1); a.or_rr(1, 0); a.tst_rr(0, 0); sfl = m.newlabel('sfl'); a.bt_lbl(sfl)
    sti(m, 0, CLEFT); sti(m, 0, CZLP); wb(m, FCLR, 0x01)                                   # v5: risposta piu' lunga di quanto letto
    a.deflabel(sfl)
    a.deflabel(no)
    nt = m.newlabel('nt'); rb(m, IFR0, 8); a.mov_rm_rn(8, 0); a.tst_imm(0x02); a.bt_lbl(nt)       # EP0i TR (contato e cancellato)
    wb(m, IFR0, 0xFD); ld(m, NTR, 0); a.add_imm(1, 0); st(m, 0, NTR)
    a.deflabel(nt)
    m.pool_jump()
    # EP1: pacchetti USB-MIDI dal telefono/computer. v7: SysEx F0 7D -> coda RX della pipe (MIDI Screen); tutto il resto
    # -> coda MIDI IN dell'OS. Pieno: si perdono.
    n1 = m.newlabel('n1'); rb(m, IFR0, 8); a.mov_rm_rn(8, 0); a.tst_imm(0x40); m.bt_far(n1)
    call(m, 'um_pb'); a.mov_rm_rn(0, 10)                                                    # r10 = pipe
    rb(m, EPSZ1, 5); a.shlr2(5)                                                            # r5 = pacchetti da 4 byte
    e1 = m.newlabel('e1'); e1x = m.newlabel('e1x'); a.tst_rr(5, 5); m.bt_far(e1x)
    a.deflabel(e1)
    m.imm32(EPDR1, 1); a.movb_ld(1, 9); a.mov_rm_rn(9, 0); a.and_imm(15); a.mov_rm_rn(0, 9)  # r9 = CIN
    a.movb_ld(1, 11); a.extu_b(11, 11); a.movb_ld(1, 12); a.extu_b(12, 12); a.movb_ld(1, 13); a.extu_b(13, 13)
    a.movl_pool('um_cin', 2); a.add_rr(9, 2); a.movb_ld(2, 6)                             # r6 = byte utili
    # r14 = destinazione: 1 = pipe (SysEx dello schermo), 0 = OS
    a.mov_imm(0, 14); a.mov_rm_rn(9, 0); a.add_imm(-4, 0); a.mov_imm(3, 1); a.cmphs(0, 1); nsx = m.newlabel('nsx'); m.bf_far(nsx)   # CIN 4..7
    m.imm32(0xF0, 1); a.cmpeq(1, 11); nf0 = m.newlabel('nf0'); a.bf_lbl(nf0)
    a.mov_imm(2, 0); a.mov_imm(0x7D, 1); a.cmpeq(1, 12); n7d = m.newlabel('n7d'); a.bf_lbl(n7d); a.mov_imm(1, 0); a.deflabel(n7d)
    st(m, 0, RXSX)                                                                         # inizio: dello schermo (1) o no (2)
    a.deflabel(nf0)
    ld(m, RXSX, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); a.movt(14)
    a.mov_imm(4, 0); a.cmpeq(0, 9); ncon = m.newlabel('ncon'); a.bt_lbl(ncon); sti(m, 0, RXSX)   # CIN 5/6/7: fine del SysEx
    a.deflabel(ncon)
    a.deflabel(nsx)
    eb = m.newlabel('eb'); en = m.newlabel('en'); a.deflabel(eb)
    a.tst_rr(6, 6); m.bt_far(en); a.add_imm(-1, 6)
    a.mov_rm_rn(11, 4); a.mov_rm_rn(12, 11); a.mov_rm_rn(13, 12)
    a.tst_rr(14, 14); topipe = m.newlabel('topipe'); ebn = m.newlabel('ebn'); a.bf_lbl(topipe)
    call(m, 'um_osrx'); a.bra_lbl(ebn); a.nop()
    a.deflabel(topipe); call(m, 'um_rxpipe')
    a.deflabel(ebn); a.bra_lbl(eb); a.nop()
    a.deflabel(en); a.dt(5); m.bf_far(e1)
    a.deflabel(e1x)
    wb(m, TRG, 0x20)
    ld(m, NIN, 0); a.add_imm(1, 0); st(m, 0, NIN)
    a.deflabel(n1)
    m.pool_jump()
    # v7: MIDI OUT dell'OS -> eventi USB-MIDI in attesa
    call(m, 'um_mparse')
    # buffer di EP2 (solo se vuoto), fino a 16 pacchetti: tempo reale, poi MIDI dell'OS (non dentro un SysEx dello
    # schermo), poi la coda TX della pipe
    nt2 = m.newlabel('nt2'); ld(m, LBN, 0); a.tst_rr(0, 0); m.bf_far(nt2)
    m.imm32(LB, 6); a.mov_imm(16, 7)
    def qcopy(base, W, R, n):
        ql = m.newlabel('ql'); qx = m.newlabel('qx'); ld(m, R, 4); ld(m, W, 5)
        a.deflabel(ql); a.cmpeq(5, 4); a.bt_lbl(qx); a.tst_rr(7, 7); a.bt_lbl(qx)
        m.imm32(base, 1); a.mov_rm_rn(4, 0); a.shll2(0); a.add_rr(0, 1); a.movl_ld(1, 0)
        a.movl_st(0, 6); a.add_imm(4, 6); a.add_imm(-1, 7)
        a.add_imm(1, 4); a.mov_imm(n - 1, 0); a.and_rr(0, 4); a.bra_lbl(ql); a.nop()
        a.deflabel(qx); st(m, 4, R)
    qcopy(RTQ, RTW, RTR, RTN)
    nq = m.newlabel('nq'); ld(m, MIDSX, 0); a.tst_rr(0, 0); m.bf_far(nq)
    qcopy(MQ, MQW, MQR, MQN)
    a.deflabel(nq); m.pool_jump()
    pdone = m.newlabel('pdone')
    call(m, 'um_pb'); a.tst_rr(0, 0); m.bt_far(pdone); a.mov_rm_rn(0, 10)
    a.movl_ld_disp(10, U_TXR, 8); a.movl_ld_disp(10, U_TXW, 9)                             # r8 = lettura, r9 = scrittura
    pl = m.newlabel('pl'); pe = m.newlabel('pe'); a.deflabel(pl)
    a.cmpeq(9, 8); m.bt_far(pe); a.tst_rr(7, 7); m.bt_far(pe)
    a.mov_imm(0, 0); a.movl_st(0, 6); a.mov_imm(0, 2); a.mov_imm(0, 3)                     # r2 = byte nel pacchetto, r3 = F7 visto
    gl = m.newlabel('gl'); ge = m.newlabel('ge'); a.deflabel(gl)
    a.cmpeq(9, 8); a.bt_lbl(ge)
    a.mov_rm_rn(10, 1); m.imm32(U_TX, 0); a.add_rr(0, 1); a.add_rr(8, 1); a.movb_ld(1, 4)  # r4 = byte
    a.add_imm(1, 8); m.imm32(U_TXSZ - 1, 0); a.and_rr(0, 8)
    a.mov_rm_rn(6, 1); a.add_imm(1, 1); a.add_rr(2, 1); a.movb_st(4, 1); a.add_imm(1, 2)
    a.mov_rm_rn(4, 0); a.and_imm(0xFF); m.imm32(0xF7, 1); a.cmpeq(1, 0); nf7 = m.newlabel('nf7'); a.bf_lbl(nf7); a.mov_imm(1, 3); a.bra_lbl(ge); a.nop()
    a.deflabel(nf7); a.mov_imm(3, 0); a.cmpeq(0, 2); a.bf_lbl(gl)
    a.deflabel(ge)
    a.mov_imm(4, 0); a.tst_rr(3, 3); c4 = m.newlabel('c4'); a.bt_lbl(c4); a.add_rr(2, 0); a.deflabel(c4)   # CIN 4 / 4+n
    a.movb_st(0, 6); a.add_imm(4, 6); a.add_imm(-1, 7)
    a.mov_imm(1, 0); a.xor_rr(3, 0); st(m, 0, MIDSX)                                       # SysEx a meta' se non finito
    a.movl_pool(pl, 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(pe)
    a.movl_st_disp(8, U_TXR, 10)
    a.deflabel(pdone)
    m.imm32(LB, 0); a.sub(0, 6); st(m, 6, LBN)
    a.deflabel(nt2)
    m.pool_jump()
    # hello ogni 0,5 s: F0 7D 20 n F7 = pacchetti USB-MIDI 04 F0 7D 20 / 06 n F7 00
    nh = m.newlabel('nh'); m.imm32(FRAMES, 1); a.movl_ld(1, 9); ld(m, HELLOF, 0); a.mov_rm_rn(9, 2); a.sub(0, 2)
    m.imm32(HELLO_GAP, 1); a.cmphs(1, 2); m.bf_far(nh)
    ld(m, LBN, 6); a.tst_rr(6, 6); m.bf_far(nh); ld(m, MIDSX, 0); a.tst_rr(0, 0); m.bf_far(nh)   # solo a buffer vuoto, fuori da un SysEx
    st(m, 9, HELLOF); m.imm32(LB, 7); a.add_rr(6, 7)
    for v in (0x04, 0xF0, 0x7D, 0x20, 0x06): a.mov_imm(v if v < 128 else v - 256, 0); a.movb_st(0, 7); a.add_imm(1, 7)
    ld(m, HCNT, 0); a.add_imm(1, 0); a.and_imm(0x7F); st(m, 0, HCNT); a.movb_st(0, 7); a.add_imm(1, 7)
    a.mov_imm(-9, 0); a.movb_st(0, 7); a.add_imm(1, 7); a.mov_imm(0, 0); a.movb_st(0, 7)
    a.add_imm(8, 6); st(m, 6, LBN)
    a.deflabel(nh)
    m.pool_jump()
    # EP2: manda la coda se il FIFO ha posto
    n2 = m.newlabel('n2'); ld(m, LBN, 5); a.tst_rr(5, 5); m.bt_far(n2)
    rb(m, IFR0, 8); a.mov_rm_rn(8, 0); a.tst_imm(0x10); m.bt_far(n2)
    m.imm32(LB, 7); m.imm32(EPDR2, 1)
    e2 = m.newlabel('e2'); a.deflabel(e2); a.movb_ld(7, 0); a.add_imm(1, 7); a.movb_st(0, 1); a.dt(5); a.bf_lbl(e2)
    wb(m, TRG, 0x10); sti(m, 0, LBN)
    a.deflabel(n2)
    a.deflabel('um_px'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ================= v7: interfaccia MIDI =================
    def qput(base, W, R, n):               # r0 = evento (4 byte: CIN, b1, b2, b3) in coda; piena: perso. Usa r1-r3
        qf = m.newlabel('qf'); ld(m, W, 2); a.mov_rm_rn(2, 3); a.add_imm(1, 3); a.mov_imm(n - 1, 1); a.and_rr(1, 3)
        m.imm32(R, 1); a.movl_ld(1, 1); a.cmpeq(1, 3); a.bt_lbl(qf)
        m.imm32(base, 1); a.shll2(2); a.add_rr(2, 1); a.movl_st(0, 1); st(m, 3, W)
        ld(m, NMIR, 2); a.add_imm(1, 2); st(m, 2, NMIR)
        a.deflabel(qf)
    def inring(r, lo, hi, bad):            # salta a bad se r non e' dentro [lo, hi]. Usa r1
        m.imm32(lo, 1); a.cmphs(1, r); m.bf_far(bad); m.imm32(hi, 1); a.cmphi(1, r); m.bt_far(bad)
    # ---- um_mparse: byte nuovi della coda MIDI OUT dell'OS (al massimo 64 per giro) -> um_mbyte ----
    a.deflabel('um_mparse'); a.stsl_pr()
    for r in (8, 9, 11): a.movl_st_dec(r, 15)
    mpx = m.newlabel('mpx'); mpd = m.newlabel('mpd'); mpg = m.newlabel('mpg')
    m.imm32(TXWR, 1); a.movl_ld(1, 9); inring(9, TXQ, TXQ_END, mpx)                      # r9 = scrittura dell'OS
    ld(m, MRD, 8); a.tst_rr(8, 8); a.bf_lbl(mpg); st(m, 9, MRD)
    a.bra_lbl(mpx); a.nop()
    mfix = m.newlabel('mfix'); mgo = m.newlabel('mgo')
    a.deflabel(mpg); inring(8, TXQ, TXQ_END, mfix); a.bra_lbl(mgo); a.nop()
    a.deflabel(mfix); a.mov_rm_rn(9, 8); a.bra_lbl(mpd); a.nop()                          # indice fuori coda: da qui
    a.deflabel(mgo)
    a.mov_imm(64, 11)
    mpl = m.newlabel('mpl'); a.deflabel(mpl)
    a.cmpeq(9, 8); m.bt_far(mpd)
    a.movb_ld(8, 4); a.extu_b(4, 4)
    mw = m.newlabel('mw'); mw2 = m.newlabel('mw2'); m.imm32(TXQ_END, 1); a.cmpeq(1, 8); a.bf_lbl(mw); m.imm32(TXQ, 8); a.bra_lbl(mw2); a.nop()
    a.deflabel(mw); a.add_imm(1, 8); a.deflabel(mw2)
    call(m, 'um_mbyte')
    a.dt(11); m.bf_far(mpl)
    a.deflabel(mpd); st(m, 8, MRD)
    a.deflabel(mpx)
    for r in (11, 9, 8): a.movl_ld_inc(15, r)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- um_mbyte: r4 = un byte della MIDI OUT -> eventi USB-MIDI (MQ / RTQ). Usa r0-r7 ----
    a.deflabel('um_mbyte'); a.mov_rm_rn(4, 5); mbx = m.newlabel('mbx')
    # tempo reale F8..FF (non F9/FD): subito, anche dentro un SysEx o un messaggio a meta'
    nrt = m.newlabel('nrt'); m.imm32(0xF8, 0); a.cmphs(0, 5); m.bf_far(nrt)
    m.imm32(0xF9, 0); a.cmpeq(0, 5); m.bt_far(mbx); m.imm32(0xFD, 0); a.cmpeq(0, 5); m.bt_far(mbx)
    a.mov_rm_rn(5, 0); a.shll8(0); a.or_imm(0x0F); qput(RTQ, RTW, RTR, RTN); a.bra_lbl(mbx); a.nop()
    a.deflabel(nrt); m.pool_jump()
    # F0: inizio SysEx (dello schermo se il 2o byte e' 7D: non si copia)
    nsx0 = m.newlabel('nsx0'); m.imm32(0xF0, 0); a.cmpeq(0, 5); m.bf_far(nsx0)
    sti(m, 1, MSX); sti(m, 1, MSXN); st(m, 5, MSXB0); sti(m, 0, MST); sti(m, 0, MCUR); a.bra_lbl(mbx); a.nop()
    a.deflabel(nsx0); m.pool_jump()
    # F7: fine SysEx -> pacchetto CIN 5/6/7 con i byte rimasti + F7
    nf7 = m.newlabel('nf7'); m.imm32(0xF7, 0); a.cmpeq(0, 5); m.bf_far(nf7)
    ld(m, MSX, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); sxe = m.newlabel('sxe'); m.bf_far(sxe)   # solo i SysEx da copiare
    ld(m, MSXN, 6)
    e0 = m.newlabel('e0'); e1_ = m.newlabel('e1'); e2_ = m.newlabel('e2')
    a.tst_rr(6, 6); m.bt_far(e0); a.mov_imm(1, 1); a.cmpeq(1, 6); m.bt_far(e1_)
    # 2 byte nel pezzo + F7 = CIN 7
    ld(m, MSXB1, 0); a.shll16(0); ld(m, MSXB0, 1); a.shll8(1); a.or_rr(1, 0); m.imm32(0xF7000007, 1); a.or_rr(1, 0)
    qput(MQ, MQW, MQR, MQN); a.bra_lbl(sxe); a.nop()
    m.pool_jump()
    a.deflabel(e1_); ld(m, MSXB0, 0); a.shll8(0); m.imm32(0x00F70006, 1); a.or_rr(1, 0); qput(MQ, MQW, MQR, MQN); a.bra_lbl(sxe); a.nop()
    a.deflabel(e0); m.imm32(0x0000F705, 0); qput(MQ, MQW, MQR, MQN)
    a.deflabel(sxe); sti(m, 0, MSX); sti(m, 0, MSXN); a.bra_lbl(mbx); a.nop()
    a.deflabel(nf7); m.pool_jump()
    # altri status 80..EF / F1..F6
    nst = m.newlabel('nst'); m.imm32(0x80, 0); a.cmphs(0, 5); m.bf_far(nst)
    sti(m, 0, MSX); sti(m, 0, MSXN)                                                        # un SysEx si interrompe
    nch = m.newlabel('nch'); m.imm32(0xF0, 0); a.cmphs(0, 5); m.bt_far(nch)
    st(m, 5, MST); st(m, 5, MCUR); sti(m, 0, MCNT)                                         # canale: anche running status
    a.mov_rm_rn(5, 0); a.shlr2(0); a.shlr2(0); a.mov_imm(2, 3); a.mov_imm(0xC, 2); a.cmphs(2, 0); n12 = m.newlabel('n12'); a.bf_lbl(n12)
    a.mov_imm(0xE, 2); a.cmphs(2, 0); a.bt_lbl(n12); a.mov_imm(1, 3); a.deflabel(n12)     # C, D: 1 dato
    st(m, 3, MNEED); a.bra_lbl(mbx); a.nop()
    m.pool_jump()
    a.deflabel(nch); sti(m, 0, MST); sti(m, 0, MCUR); sti(m, 0, MCNT)                      # comuni: niente running status
    nf6 = m.newlabel('nf6'); m.imm32(0xF6, 0); a.cmpeq(0, 5); m.bf_far(nf6)
    m.imm32(0x0000F605, 0); qput(MQ, MQW, MQR, MQN); a.bra_lbl(mbx); a.nop()
    a.deflabel(nf6); m.pool_jump()
    for code, need in ((0xF1, 1), (0xF2, 2), (0xF3, 1)):
        nx = m.newlabel('nc'); m.imm32(code, 0); a.cmpeq(0, 5); m.bf_far(nx)
        st(m, 5, MCUR); sti(m, need, MNEED); a.bra_lbl(mbx); a.nop()
        a.deflabel(nx)
    a.bra_lbl(mbx); a.nop()                                                                # F4, F5: niente
    m.pool_jump()
    a.deflabel(nst)
    # byte di dati
    nsd = m.newlabel('nsd'); ld(m, MSX, 0); a.tst_rr(0, 0); m.bt_far(nsd)
    ld(m, MSXN, 6); a.mov_imm(1, 1); a.cmpeq(1, 6); ns2 = m.newlabel('ns2'); m.bf_far(ns2)
    a.mov_imm(0x7D, 1); a.cmpeq(1, 5); n2d = m.newlabel('n2d'); a.bf_lbl(n2d); ld(m, MSXB0, 0); m.imm32(0xF0, 1); a.cmpeq(1, 0); a.bf_lbl(n2d)
    sti(m, 2, MSX)                                                                         # F0 7D: dello schermo
    a.deflabel(n2d); st(m, 5, MSXB1); sti(m, 2, MSXN); a.bra_lbl(mbx); a.nop()
    a.deflabel(ns2)
    a.tst_rr(6, 6); nz = m.newlabel('nz'); m.bf_far(nz); st(m, 5, MSXB0); sti(m, 1, MSXN); a.bra_lbl(mbx); a.nop()
    a.deflabel(nz)                                                                         # terzo byte: pacchetto CIN 4
    sti(m, 0, MSXN); ld(m, MSX, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); m.bf_far(mbx)
    a.mov_rm_rn(5, 0); a.shll16(0); a.shll8(0); ld(m, MSXB1, 1); a.shll16(1); a.or_rr(1, 0); ld(m, MSXB0, 1); a.shll8(1); a.or_rr(1, 0)
    a.or_imm(4); qput(MQ, MQW, MQR, MQN); a.bra_lbl(mbx); a.nop()
    m.pool_jump()
    a.deflabel(nsd)
    ld(m, MCUR, 7); a.tst_rr(7, 7); hc = m.newlabel('hc'); m.bf_far(hc)
    ld(m, MST, 7); a.tst_rr(7, 7); m.bt_far(mbx)                                           # dato senza status: niente
    st(m, 7, MCUR); sti(m, 0, MCNT)                                                        # running status
    a.mov_rm_rn(7, 0); a.shlr2(0); a.shlr2(0); a.mov_imm(2, 3); a.mov_imm(0xC, 2); a.cmphs(2, 0); r12_ = m.newlabel('r12'); a.bf_lbl(r12_)
    a.mov_imm(0xE, 2); a.cmphs(2, 0); a.bt_lbl(r12_); a.mov_imm(1, 3); a.deflabel(r12_); st(m, 3, MNEED)
    a.deflabel(hc)
    ld(m, MCNT, 6); a.tst_rr(6, 6); sec = m.newlabel('sec'); m.bf_far(sec)
    st(m, 5, MB1); sti(m, 1, MCNT); ld(m, MNEED, 0); a.mov_imm(1, 1); a.cmpeq(1, 0); m.bf_far(mbx)
    a.mov_imm(0, 6); a.bra_lbl('um_mev'); a.nop()                                          # 1 dato: evento (d2 = 0)
    a.deflabel(sec); a.mov_rm_rn(5, 6); a.shll16(6); a.shll8(6)                            # r6 = d2 << 24
    a.deflabel('um_mev')                                                                   # evento: MCUR, MB1, r6
    ld(m, MCUR, 7); a.mov_rm_rn(7, 0); a.shlr2(0); a.shlr2(0)                              # CIN = status >> 4 (canale)
    m.imm32(0xF0, 1); a.cmphs(1, 7); ncm = m.newlabel('ncm'); a.bf_lbl(ncm)
    ld(m, MNEED, 0); a.add_imm(1, 0)                                                       # comuni: 2 = 1 dato, 3 = 2 dati
    a.deflabel(ncm)
    a.mov_rm_rn(7, 1); a.shll8(1); a.or_rr(1, 0); ld(m, MB1, 1); a.shll16(1); a.or_rr(1, 0); a.or_rr(6, 0)
    qput(MQ, MQW, MQR, MQN); sti(m, 0, MCUR); sti(m, 0, MCNT)
    a.deflabel(mbx); a.rts(); a.nop()
    m.pool_jump()
    # ---- um_rxpipe: r4 = byte -> coda RX della pipe (r10 = pipe, 0 = niente). Usa r0-r3 ----
    a.deflabel('um_rxpipe'); rpx = m.newlabel('rpx')
    a.tst_rr(10, 10); a.bt_lbl(rpx)
    a.movl_ld_disp(10, U_RXW, 2); a.mov_rm_rn(2, 3); a.add_imm(1, 3); m.imm32(U_RXSZ - 1, 0); a.and_rr(0, 3)
    a.movl_ld_disp(10, U_RXR, 0); a.cmpeq(0, 3); a.bt_lbl(rpx)                            # pieno
    a.mov_rm_rn(10, 1); m.imm32(U_RX, 0); a.add_rr(0, 1); a.add_rr(2, 1); a.movb_st(4, 1)
    a.movl_st_disp(3, U_RXW, 10)
    a.deflabel(rpx); a.rts(); a.nop()
    m.pool_jump()
    # ---- um_osrx: r4 = byte -> coda MIDI IN dell'OS (come la ricezione della DIN). Piena: perso. Usa r0-r3, r7 ----
    a.deflabel('um_osrx'); orx = m.newlabel('orx'); orf = m.newlabel('orf')
    a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0)
    m.imm32(RXWR, 1); a.movl_ld(1, 2); inring(2, RXQ, RXQ_END, orx)                      # r2 = scrittura
    m.imm32(RXQ_END, 3); a.cmpeq(3, 2); a.mov_rm_rn(2, 3); a.add_imm(1, 3); onw = m.newlabel('onw'); a.bf_lbl(onw); m.imm32(RXQ, 3); a.deflabel(onw)
    m.imm32(RXRD, 0); a.movl_ld(0, 0); a.cmpeq(0, 3); a.bt_lbl(orx)                       # piena: la lettura dell'OS e' li'
    a.movb_st(4, 2); m.imm32(RXWR, 1); a.movl_st(3, 1)
    ld(m, NOSRX, 0); a.add_imm(1, 0); st(m, 0, NOSRX)
    a.deflabel(orx); a.ldc_sr(7); a.rts(); a.nop()
    m.pool_jump()
    # ---- um_ep0tx: prossimo pacchetto di EP0 (max 8) da CPTR/CLEFT, poi eventuale pacchetto vuoto finale ----
    a.deflabel('um_ep0tx')
    ld(m, CLEFT, 5); a.tst_rr(5, 5); ez = m.newlabel('ez'); m.bt_far(ez)
    a.mov_imm(8, 0); e8 = m.newlabel('e8'); a.cmphs(0, 5); a.bf_lbl(e8); a.mov_rm_rn(0, 5); a.deflabel(e8)   # r5 = min(8, left)
    ld(m, CLEFT, 0); a.sub(5, 0); st(m, 0, CLEFT)
    ld(m, CPTR, 7); m.imm32(EPDR0I, 1)
    el = m.newlabel('el'); a.deflabel(el); a.movb_ld(7, 0); a.add_imm(1, 7); a.movb_st(0, 1); a.dt(5); a.bf_lbl(el)
    st(m, 7, CPTR); wb(m, TRG, 0x01); ld(m, NPK, 0); a.add_imm(1, 0); st(m, 0, NPK); a.rts(); a.nop()
    a.deflabel(ez)
    ld(m, CZLP, 0); a.tst_rr(0, 0); ezx = m.newlabel('ezx'); a.bt_lbl(ezx)
    sti(m, 0, CZLP); wb(m, TRG, 0x01)                                                      # pacchetto vuoto
    a.deflabel(ezx); a.rts(); a.nop()
    m.pool_jump()
    emit_base(m, 'um_pb')
    a.deflabel('um_cin')                                   # byte MIDI utili per Code Index Number (USB MIDI 1.0 tab. 4-1)
    for i in range(0, 16, 2):
        n_ = [0, 0, 2, 3, 3, 1, 2, 3, 3, 3, 3, 3, 2, 2, 3, 1]; a._emit(n_[i] | n_[i + 1] << 8)
    # ---- tabelle ----
    a.deflabel('um_dtab')
    off = 0; tab = b''; data = b''
    for typ, idx, dsc in DESCS:
        tab += struct.pack('<BBHHH', typ, idx, len(data), len(dsc), 0); data += dsc
    for i in range(0, len(tab), 2): a._emit(struct.unpack('<H', tab[i:i + 2])[0])
    a.deflabel('um_ddata')
    data += bytes(len(data) & 1)
    for i in range(0, len(data), 2): a._emit(struct.unpack('<H', data[i:i + 2])[0])
    a.deflabel('um_end')
pg.on_fire(fire)
pg.on_inactive(lambda m: call(m, 'um_poll'))

# ================= modello del controller USB (per l'emulatore) =================
class USBF:
    """Il minimo del USB function controller SH7727 visto dal software, con un "host" che enumera."""
    def __init__(self, e):
        self.e = e; self.r = {a_: 0 for a_ in range(U, U + 0x13)}; self.r[IFR0] = 0x10
        self.vbus = 1
        self.ep0i = []; self.ep0s = []; self.ep1 = []; self.ep2 = []; self.sent_ep2 = []; self.ctl_in = []; self.stb = 0x18
        # v5 strict: EP0i come sul ferro (FIFO da 8 byte: un pacchetto "armato" esce solo con un IN del host); scrivere nel
        # FIFO mentre un pacchetto e' armato = overflow; interrupt dell'OS rimessi con flag alzati = irq_dirty
        self.strict = False; self.armed = None; self.overflow = 0; self.irq_dirty = 0
        for a_ in range(U, U + 0x13):
            e.mmio_r[a_] = self.rd; e.mmio_w[a_] = self.wr
        e.mmio_r[STBCR3] = lambda a_: self.stb; e.mmio_w[STBCR3] = lambda a_, v: setattr(self, 'stb', v & 0xFF)
        self.exp = 0; e.mmio_r[EXPFC] = lambda a_: self.exp; e.mmio_w[EXPFC] = lambda a_, v: setattr(self, 'exp', v & 0xFFFF)
        self.excpg = 0x05; e.mmio_w[EXCPGCR] = lambda a_, v: setattr(self, 'excpg', v & 0xFF); e.mmio_r[EXCPGCR] = lambda a_: self.excpg
        self.pfc = {PDCR: 0xAAAA, PECR: 0xAAAA}                 # (v6: valori veri, scritti e riletti)
        for p in (PDCR, PECR): e.mmio_r[p] = lambda a_: self.pfc[a_]; e.mmio_w[p] = lambda a_, v: self.pfc.__setitem__(a_, v & 0xFFFF)
    def rd(self, a_):
        if a_ == EPDR0S: return self.ep0s.pop(0) if self.ep0s else 0
        if a_ == EPDR1: return self.ep1.pop(0) if self.ep1 else 0
        if a_ == EPSZ1: return len(self.ep1)
        if a_ == IFR1: return (self.r[IFR1] & ~0x08) | (8 if self.vbus else 0)
        return self.r[a_]
    def connected(self): return self.vbus and not (self.r[DMAR] & 4)
    def wr(self, a_, v):
        v &= 0xFF
        if a_ == IFR0: self.r[IFR0] &= (v | 0x50); return                 # scrivere 0 cancella (FULL/EMPTY sono stati)
        if a_ == IFR1: self.r[IFR1] &= (v | 0x08); return
        if a_ == EPDR0I:
            if self.strict and self.armed is not None: self.overflow += 1
            self.ep0i.append(v); return
        if a_ in (IER0, IER1) and v and (self.r[IFR0] & v if a_ == IER0 else self.r[IFR1] & v & ~0x08): self.irq_dirty += 1
        if a_ == EPDR2: self.ep2.append(v); return
        if a_ == TRG:
            if v & 0x01:
                if self.strict:
                    if self.armed is not None: self.overflow += 1
                    self.armed = bytes(self.ep0i); self.ep0i = []
                else: self.ctl_in.append(bytes(self.ep0i)); self.ep0i = []; self.r[IFR0] |= 0x01
            if v & 0x10: self.sent_ep2.append(bytes(self.ep2)); self.ep2 = []
            if v & 0x20: self.ep1 = []; self.r[IFR0] &= ~0x40
            if v & 0x02: self.r[IFR0] &= ~0x04
            return
        if a_ == FCLR:
            if v & 0x01: self.armed = None; self.ep0i = []
            return
        self.r[a_] = v
    # host
    def bus_reset(self): self.r[IFR0] |= 0x80; self.armed = None
    def in_token(self):
        """(strict) IN del host su EP0: prende il pacchetto armato (TS) o riceve NAK (TR)"""
        if self.armed is None: self.r[IFR0] |= 0x02; return None
        p = self.armed; self.armed = None; self.ctl_in.append(p); self.r[IFR0] |= 0x01; return p
    def setup(self, pkt): self.ep0s = list(pkt); self.ctl_in = []; self.r[IFR0] |= 0x08
    def status_out(self): self.r[IFR0] |= 0x04
    def ep1_send(self, data): self.ep1 = list(data); self.r[IFR0] |= 0x40

if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'USBMIDI.WAV'); pg.build_wav(out)
    from validate import Harness
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    TXRD_ = 0x881C0678
    h.ram_w32(TXWR, TXQ); h.ram_w32(TXRD_, TXQ); h.ram_w32(RXWR, RXQ); h.ram_w32(RXRD, RXQ)   # code MIDI dell'OS (v7)
    def os_out(bs):                    # l'OS scrive nella sua coda MIDI OUT (qui la DIN l'ha gia' trasmessa)
        w = h.ram_r32(TXWR)
        for b in bs: h.ram_w8(w, b); w = TXQ if w == TXQ_END else w + 1
        h.ram_w32(TXWR, w); h.ram_w32(TXRD_, w)
    def os_in():                       # quello che l'OS legge dalla sua coda MIDI IN
        out = []; r = h.ram_r32(RXRD); w = h.ram_r32(RXWR)
        while r != w: out.append(e.rd(r, 1)); r = RXQ if r == RXQ_END else r + 1
        h.ram_w32(RXRD, r); return out
    usb = USBF(e)
    FPF = 128
    def fire_t(n=1):
        for _ in range(n): h.ram_w32(FRAMES, (h.ram_r32(FRAMES) + FPF) & 0xFFFFFFFF); h.fire(1)
    h.load_hub(); h.load_wav(out); h.ram_w32(EVQ + 0x68, pg.page); fire_t(5)
    txt = lambda: bytes(e.rd(pg.dlabels[L2] + i, 1) for i in range(16)).decode()
    assert h.ram_r32(TAKEN) == 0 and txt().startswith("S18 E00 V1 X0000"), txt()
    print("  ok OFF: porta non toccata, stato mostrato %r" % txt())
    usb.r[IER0] = 0xEF; usb.r[DMAR] = 0                                    # come se l'OS avesse acceso i suoi interrupt
    SETTLE = ON_SETTLE // FPF + 3
    # v6: raffica di ON/OFF (ogni 2 giri, come la rotella girata avanti e indietro): la porta non viene toccata
    usb.r[IER0] = 0xEF; usb.r[DMAR] = 0
    for k in range(30): h.ram_w8(ONS, k & 1 ^ 1); fire_t(2)
    h.ram_w8(ONS, 0); fire_t(SETTLE); assert h.ram_r32(TAKEN) == 0 and usb.r[IER0] == 0xEF and usb.pfc[PDCR] == 0xAAAA, "raffica ON/OFF"
    print("  ok raffica ON/OFF ogni 2 giri: porta mai presa (serve il campo fermo 0,3 s)")
    h.ram_w8(ONS, 1); fire_t(SETTLE)
    assert h.ram_r32(TAKEN) == 1 and usb.connected() and usb.r[IER0] == 0 and usb.excpg == 0x28 and usb.stb & 0x10 == 0 and usb.exp & 3 == 1
    usb.vbus = 0; fire_t(1); assert not usb.connected() and usb.r[DMAR] & 4
    usb.vbus = 1; fire_t(1); assert usb.connected() and usb.r[DMAR] & 4 == 0
    print("  ok ON: interrupt USB spenti, clock 0x28, funzione USB; pull-up come l'OS (cavo -> PULLUP_E 0, senza -> 1)")
    def get(typ, idx, wlen):
        usb.setup(bytes([0x80, 6, idx, typ, 0, 0, wlen & 0xFF, wlen >> 8]))
        for _ in range(60):
            fire_t(1)
        data = b''.join(usb.ctl_in); usb.status_out(); fire_t(2); return data, [len(p) for p in usb.ctl_in]
    usb.bus_reset(); fire_t(2); assert h.ram_r32(NRST) == 1
    d, pk = get(1, 0, 64); assert d == DEV and pk == [8, 8, 2], (d.hex(), pk)
    d, pk = get(2, 0, 9); assert d == CFG[:9], d.hex()
    d, pk = get(2, 0, 255); assert d == CFG and pk[-1] == 5, (len(d), pk)
    for i, s in enumerate(STR):
        d, pk = get(3, i, 255); assert d == s, (i, d)
    d, pk = get(2, 0, 104); assert d == CFG and len(pk) == 13
    print("  ok enumerazione: dispositivo (18 B), configurazione USB-MIDI (101 B in 13 pacchetti), stringhe 'Open500' / 'MPC500 MIDI'")
    # v5: host realistico (FIFO EP0i vero). Come xHCI/iPad: prima lettura del descrittore con wLength 64 e pacchetto
    # massimo presunto 64 -> 8 byte = pacchetto corto, il host chiude subito; reset; poi lettura vera a 8 byte per pacchetto.
    usb.strict = True
    def xfer(typ, idx, wlen, mps, race=None):
        usb.setup(bytes([0x80, 6, idx, typ, 0, 0, wlen & 0xFF, wlen >> 8])); usb.ctl_in = []; data = b''
        for _ in range(80):
            fire_t(1); p = usb.in_token()
            if p is None: continue
            data += p
            if len(data) >= wlen or len(p) < mps: break
        usb.status_out()
        if race: race()                           # il SETUP dopo arriva PRIMA che l'MPC abbia visto TS/stato
        fire_t(2); return data
    nr0 = h.ram_r32(NRST)
    d = xfer(1, 0, 64, 64); assert d == DEV[:8], d.hex()
    usb.bus_reset(); fire_t(2)
    d = xfer(1, 0, 18, 8); assert d == DEV, d.hex()
    d = xfer(2, 0, 9, 8); assert d == CFG[:9], d.hex()
    d = xfer(2, 0, 255, 8); assert d == CFG, len(d)
    # corsa: il host legge 1 pacchetto di 2 e chiude, e il SETUP successivo arriva subito (prima del giro dell'MPC)
    d1 = []; d = xfer(3, 1, 255, 64, race=lambda: d1.append(usb.setup(bytes([0x80, 6, 2, 3, 0, 0, 255, 0]))))
    usb.ctl_in = []; d2 = b''
    for _ in range(80):
        fire_t(1); p = usb.in_token()
        if p is None: continue
        d2 += p
        if len(p) < 8: break
    usb.status_out(); fire_t(2)
    assert d2 == STR[2], ("dati vecchi nella risposta dopo la corsa", d2.hex())
    assert usb.overflow == 0, "pacchetti EP0 scritti nel FIFO ancora pieno: %d" % usb.overflow
    LOGN_ = h.ram_r32(LOGN); assert LOGN_ >= 6
    lg = bytes(e.rd(LOGB + i, 1) for i in range(12))
    assert lg[:8] == bytes([0x80, 6, 0, 1, 0, 0, 64, 0]) and lg[9] == nr0, lg.hex()
    h.ram_w32(LOGI, 1); fire_t(1); assert txt() == "80060100 40 00 1", repr(txt())
    h.ram_w32(LOGI, 0)
    print("  ok host realistico (FIFO EP0 vero): lettura corta a 64, reset, lettura vera; corsa SETUP/TS senza dati vecchi; registro a schermo %r" % "80060100 40 00 1")
    usb.strict = False
    usb.setup(bytes([0x80, 6, 0, 6, 0, 0, 10, 0])); fire_t(3); assert usb.r[EPSTL] & 1          # descrittore che non abbiamo
    usb.r[EPSTL] = 0; usb.setup(bytes([0xA1, 1, 0, 1, 0, 2, 2, 0])); fire_t(3); assert usb.r[EPSTL] & 1   # richiesta di classe
    print("  ok richieste sconosciute -> stall di EP0")
    usb.r[EPSTL] = 0; usb.sent_ep2 = []
    from upipe import UPIPE_OFF, UPIPE_MAGIC, U_ALIVE, U_TXW, U_TXR, U_RXW, U_RXR, U_TX, U_TXSZ, U_RX, U_RXSZ
    UP = h.ram_r32(0x8848ED34) + UPIPE_OFF
    assert h.ram_r32(UP) == UPIPE_MAGIC and h.ram_r32(UP + U_ALIVE) == h.ram_r32(FRAMES)
    print("  ok pipe aperta ('O5U1') con battito a ogni giro")
    CINLEN = [0, 0, 2, 3, 3, 1, 2, 3, 3, 3, 3, 3, 2, 2, 3, 1]
    def usb_to_bytes(pkts):
        out = []
        for p in pkts:
            for i in range(0, len(p), 4): out += list(p[i + 1:i + 1 + CINLEN[p[i] & 15]])
        return out
    def rx_ring():
        out = []; r = h.ram_r32(UP + U_RXR); w = h.ram_r32(UP + U_RXW)
        while r != w: out.append(e.rd(UP + U_RX + r, 1)); r = (r + 1) % U_RXSZ
        h.ram_w32(UP + U_RXR, r); return out
    def tx_push(bs):
        w = h.ram_r32(UP + U_TXW)
        for b in bs: h.ram_w8(UP + U_TX + w, b); w = (w + 1) % U_TXSZ
        h.ram_w32(UP + U_TXW, w)
    # telefono -> MPC: nota + SysEx CAPS spezzato in pacchetti USB-MIDI (CIN 9, 4, 6)
    pkt = bytes([0x09, 0x90, 60, 100, 0x04, 0xF0, 0x7D, 0x18, 0x06, 0x05, 0xF7, 0x00])
    usb.ep1_send(pkt); fire_t(3); assert rx_ring() == [0xF0, 0x7D, 0x18, 0x05, 0xF7] and h.ram_r32(NIN) == 1
    assert os_in() == [0x90, 60, 100], "v7: la nota va alla MIDI IN dell'OS"
    print("  ok EP1: SysEx dello schermo -> coda RX della pipe, nota -> MIDI IN dell'OS")
    # MPC -> telefono: due SysEx (38 e 20 byte) dalla coda TX -> pacchetti USB-MIDI, ricomposti uguali
    m1 = [0xF0, 0x7D, 0x01] + list(range(34)) + [0xF7]; m2 = [0xF0, 0x7D, 0x03] + [5] * 16 + [0xF7]
    usb.sent_ep2 = []; tx_push(m1 + m2); fire_t(6)
    got = usb_to_bytes(usb.sent_ep2); assert got == m1 + m2, got
    assert all(len(p) % 4 == 0 and len(p) <= 64 for p in usb.sent_ep2) and h.ram_r32(UP + U_TXR) == h.ram_r32(UP + U_TXW)
    print("  ok coda TX -> EP2: %d byte in %d trasferimenti USB-MIDI (max 16 pacchetti), ricomposti uguali" % (len(got), len(usb.sent_ep2)))
    # EP2 occupato: niente si perde, riparte quando si libera; giro della coda
    usb.r[IFR0] &= ~0x10; usb.sent_ep2 = []; big = []
    for k in range(40): big += [0xF0, 0x7D, 0x30, k, 1, 2, 3, 4, 5, 0xF7]
    h.ram_w32(UP + U_TXR, U_TXSZ - 30); h.ram_w32(UP + U_TXW, U_TXSZ - 30); tx_push(big); fire_t(5); assert usb.sent_ep2 == []
    def no_hello(bs):                                                      # toglie gli hello F0 7D 20 n F7 (legittimi a coda vuota)
        out = []; i = 0
        while i < len(bs):
            if bs[i:i + 3] == [0xF0, 0x7D, 0x20] and bs[i + 4:i + 5] == [0xF7]: i += 5; continue
            out.append(bs[i]); i += 1
        return out
    usb.r[IFR0] |= 0x10; fire_t(60); assert no_hello(usb_to_bytes(usb.sent_ep2)) == big
    print("  ok EP2 occupato: si aspetta; 400 byte attraverso il giro della coda, nessun byte perso")
    # ---- v7: MIDI OUT dell'OS -> USB, USB -> MIDI IN dell'OS ----
    def evs(pkts): return [tuple(p[i:i + 4]) for p in pkts for i in range(0, len(p), 4)]
    def nohello(ev):
        out = []; i = 0
        while i < len(ev):
            if ev[i][:4] == (0x04, 0xF0, 0x7D, 0x20) and i + 1 < len(ev) and ev[i + 1][0] == 0x06: i += 2; continue
            out.append(ev[i]); i += 1
        return out
    usb.sent_ep2 = []; fire_t(2)
    os_out([0x90, 60, 100, 62, 90, 0xF8, 0xB0, 7, 100, 0xF0, 1, 2, 3, 4, 0xF7, 0xF0, 0x7D, 0x01, 0xF7, 0xC1, 5, 0xF2, 0x10, 0x20,
            0x80, 60, 0, 0xF0, 0x43, 0xF7, 0xFE])
    fire_t(6); ev = nohello(evs(usb.sent_ep2))
    rt = [x for x in ev if x[0] == 0x0F]; oth = [x for x in ev if x[0] != 0x0F]
    assert rt == [(0x0F, 0xF8, 0, 0), (0x0F, 0xFE, 0, 0)], rt
    assert oth == [(0x09, 0x90, 60, 100), (0x09, 0x90, 62, 90), (0x0B, 0xB0, 7, 100), (0x04, 0xF0, 1, 2), (0x07, 3, 4, 0xF7),
                   (0x0C, 0xC1, 5, 0), (0x03, 0xF2, 0x10, 0x20), (0x08, 0x80, 60, 0), (0x07, 0xF0, 0x43, 0xF7)], oth
    print("  ok MIDI OUT dell'OS -> USB: note (anche running status), CC, program, song position, clock, SysEx; il SysEx F0 7D no")
    # dentro un SysEx dello schermo: il clock passa subito, la nota aspetta la fine (F7)
    usb.r[IFR0] &= ~0x10; usb.sent_ep2 = []
    tx_push([0xF0, 0x7D, 0x31] + list(range(60)) + [0xF7]); fire_t(1)
    os_out([0xF8, 0x90, 61, 99]); fire_t(2)
    usb.r[IFR0] |= 0x10; fire_t(10); ev = nohello(evs(usb.sent_ep2))
    i_end = next(i for i, x in enumerate(ev) if x[0] in (5, 6, 7)); i_note = ev.index((0x09, 0x90, 61, 99)); i_clk = ev.index((0x0F, 0xF8, 0, 0))
    assert i_clk < i_end < i_note, (i_clk, i_end, i_note)
    print("  ok dentro un SysEx dello schermo: il clock passa subito (pacchetto %d), la nota dopo la fine (%d > %d)" % (i_clk, i_note, i_end))
    # computer -> MPC: nota, start, SysEx dello schermo (-> pipe), SysEx di un altro (-> OS), CC
    pkt = bytes([0x09, 0x90, 64, 80, 0x0F, 0xFA, 0, 0, 0x04, 0xF0, 0x7D, 0x10, 0x06, 0x22, 0xF7, 0, 0x04, 0xF0, 0x43, 0x01,
                 0x07, 0x02, 0x03, 0xF7, 0x0B, 0xB0, 1, 64])
    rx_ring(); usb.ep1_send(pkt); fire_t(3)
    assert os_in() == [0x90, 64, 80, 0xFA, 0xF0, 0x43, 0x01, 0x02, 0x03, 0xF7, 0xB0, 1, 64] and rx_ring() == [0xF0, 0x7D, 0x10, 0x22, 0xF7]
    w0 = h.ram_r32(RXWR); h.ram_w32(RXRD, RXQ + ((w0 - RXQ + 3) % 128)); usb.ep1_send(bytes([0x09, 0x90, 1, 2, 0x09, 0x90, 3, 4])); fire_t(3)
    assert (h.ram_r32(RXWR) - RXQ) % 128 == (w0 - RXQ + 2) % 128, "coda MIDI IN piena: mai sopra la lettura dell'OS"
    h.ram_w32(RXRD, h.ram_r32(RXWR)); h.ram_w32(RXWR, 0); usb.ep1_send(bytes([0x09, 0x90, 1, 2])); fire_t(3); assert h.ram_r32(RXWR) == 0
    h.ram_w32(RXWR, RXQ); h.ram_w32(RXRD, RXQ)
    print("  ok computer -> MIDI IN dell'OS: nota, start, CC, SysEx di altri; F0 7D alla pipe; coda piena e puntatore strano: niente danni")
    # hello solo a coda vuota e mai dentro un SysEx
    usb.sent_ep2 = []; fire_t(int(HELLO_GAP / FPF) + 3)
    assert any(p.startswith(bytes([0x04, 0xF0, 0x7D, 0x20, 0x06])) for p in usb.sent_ep2), usb.sent_ep2
    print("  ok hello SysEx F0 7D 20 n F7 ogni 0,5 s (a coda vuota)")
    h.ram_w8(ONS, 0); fire_t(SETTLE)
    assert usb.pfc[PDCR] == 0xAAAA and usb.pfc[PECR] == 0xAAAA and usb.excpg == 0x05, "v6: piedini e clock rimessi"
    h.ram_w8(ONS, 1); fire_t(SETTLE); assert h.ram_r32(TAKEN) == 0, "v6: meno di 1 s dal rilascio: non si riprende"
    fire_t(ON_GAP // FPF); assert h.ram_r32(TAKEN) == 1; h.ram_w8(ONS, 0); fire_t(ON_GAP // FPF + SETTLE); assert h.ram_r32(TAKEN) == 0
    usb.stb = 0x08; h.ram_w8(ONS, 1); fire_t(ON_GAP // FPF + SETTLE); assert h.ram_r32(TAKEN) == 0, "v6: modulo USB dell'OS acceso"
    usb.stb = 0x18; h.ram_w8(ONS, 0); fire_t(SETTLE)
    print("  ok v6: OFF rimette piedini (PDCR/PECR) e clock USB; ripresa solo dopo 1 s; porta lasciata all'OS se la usa")
    assert h.ram_r32(TAKEN) == 0 and not usb.connected() and usb.r[IER0] == 0xEF and h.ram_r32(UP + U_ALIVE) == 0
    print("  ok OFF: scollegato (PULLUP_E 1), interrupt dell'OS rimessi, pipe spenta (battito 0)")
    print("VALIDATO: USB MIDI v7 (interfaccia MIDI + pipe dello schermo) -> USBMIDI.WAV")
