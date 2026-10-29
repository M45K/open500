#!/usr/bin/env python3
# MASTER FX v2 (MODE -> OTHER -> "Master FX") -> MASTERFX.WAV - 12 effetti IN TEMPO REALE stile SP-404 sull'uscita.
#   Nella pagina i 12 PAD = 12 effetti, NOTE ON: l'effetto e' attivo finche' tieni premuto il pad (i pad non suonano i
#   sample, la sequenza continua). Premendo un pad lo schermo mostra l'effetto e i suoi parametri:
#     riga 1: "FILT+DRV CUT  64"   nome + parametro principale (SLIDER, o rotella)
#     riga 2: "RES  30 DRIVE 40"   altri due parametri (rotella, cursore)
#   Lo slider (canale A/D 0x8848C3E0, 0..1023: da confermare su HW) muove il parametro principale dell'effetto scelto.
#   Uscendo dalla pagina gli effetti si fermano. Delay/Reverb/Looper/Downer usano il buffer da 512 KB del loader v10
#   (B+0x60000, marcatore B+0x4C): con un loader vecchio restano senza effetto.
# Aggancio audio (drivers/AUDIO.md, sonda HW 2026-09-27): literal SDRAM 0x884C10C8 (render in X-RAM 0xA50089B0) -> nostro
#   wrapper (alias P1, cache): render originale, poi il blocco di 64 frame [R0+64, R0+128) del ring d'uscita 0x884842A8
#   viene spacchettato in una struttura per frame (32 byte: WL WR DL DR X0 X1 X2 X3), elaborato dall'effetto (W = wet),
#   miscelato con rampa (G -> 0/4096 in un blocco: niente click) e rimesso nel ring. r8-r14 salvati (l'ISR non lo fa).
# Parametri: il main loop traduce i 3 valori 0..99 dell'effetto scelto in coefficienti CF0..CF2 (descrittori + tabelle,
#   tempo sequenza per i valori a tempo); l'ISR usa solo CF e il proprio stato S0..S23 (azzerato a ogni pressione).
import sys, os, struct, math
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon
import pages as _pages
# Variabili condivise con l'ISR: SEMPRE via alias P1 (cache), sia nell'ISR sia nel main loop (stesso alias = coerenza).
# HW v2.4: via P2 (non cache) ~1 us per accesso -> decine per interrupt da 90 us -> ISR in sovraccarico, MPC bloccata.
# ATTENZIONE rilocazione: m.imm32 trasla solo le costanti nella LOAD_SCRATCH (P2): l'alias P1 va calcolato DOPO la
# traslazione di build, altrimenti le due costruzioni sono uguali e pack_addon3 non lo riloca (bug trovato nel test slot 1).
CSET = set()
def _p1(m, slot):
    from mpcmod import SCRATCH
    v = slot
    if LOAD_SCRATCH <= v < LOAD_SCRATCH + 0x1000 and m.scratch != LOAD_SCRATCH and m.scratch != SCRATCH:
        v = m.scratch + (v - LOAD_SCRATCH)
    return v & 0xDFFFFFFF
def ld(m, slot, r):
    if slot in CSET: m.a.movl_pool(_p1(m, slot), 1); m.a.movl_ld(1, r)
    else: _pages.ld(m, slot, r)
def st(m, r, slot):
    if slot in CSET: m.a.movl_pool(_p1(m, slot), 1); m.a.movl_st(r, 1)
    else: _pages.st(m, r, slot)
def sti(m, val, slot):
    if slot in CSET: m.a.movl_pool(_p1(m, slot), 1); m.imm32(val, 0); m.a.movl_st(0, 1)
    else: _pages.sti(m, val, slot)
from mpcmod import EVQ, LOAD_SCRATCH, SLOT_CODE_MAX, RESERVE, FXBUF_OFF, FXBUF_SIZE, FXMARK_OFF, SCRATCH_MAGIC

RLIT, RENDER, ISR_EXIT = 0x884C10C8, 0xA50089B0, 0x884C0D50
OUT_RPTR, OUT_BASE, OUT_SIZE = 0x881C05F4, 0x884842A8, 0x4000
LEAD = 64                                  # long: il blocco scritto dal render e' a R0 + 64 long (= 32 frame)
LAT = 3                                    # latenza in blocchi (HW v3.3: con 2 i blocchi pesanti arrivavano tardi, M -> 999)
SH = 14                                    # HW PROBE5 2026-09-27: picchi ~0x06C2E000 (5% del fondo scala s32): campione = long >> SH
#                                            (prima >> 16 = solo ~11 bit utili -> filtri/riverbero/chorus metallici)
BLK, BSH = 32, 5                           # frame per blocco di elaborazione (frame = 2 long L, R s32, s16 nei bit alti)
GSTEP = 1024                               # rampa on/off: 4 blocchi = 128 frame (~3 ms)
# v3 (HW v2.6: per-chiamata TX = cache fredda 11000 volte/s -> quasi tutto in sovraccarico): ARCHITETTURA A 2 LIVELLI
#  - ISR TX (mf_tx, ~40 istruzioni): copia i 4 frame definitivi in IN[b%4] e spedisce i 4 frame di OUT[(b-2)%4] se quel
#    blocco e' stato elaborato (OUTID), altrimenti gli stessi 4 frame "a secco" da IN[(b-2)%4]: latenza fissa 2 blocchi
#    = 64 frame = 1.45 ms. b = contatore frame OS [0x881C05F0] >> 5, 4 frame per chiamata.
#  - l'uscita di OGNI ISR passa dal literal 0x884C10B4 (letto solo a 0x884C1086: jmp @r1 all'uscita 0x884C0D50, e PR del
#    render): lo puntiamo a mf_trig. mf_trig (poche istruzioni): se c'e' un blocco nuovo da elaborare e non siamo gia'
#    occupati lancia mf_proc con gli interrupt abilitati: dal contesto ISR (banco 1) come fa l'OS col render
#    (PR = uscita, SPC = mf_proc, rte); dal ritorno del render (banco 0) con un salto. Altrimenti jmp all'uscita.
#  - mf_proc elabora l'ultimo blocco completo (32 frame) con gli effetti, in cache calda, e scrive OUT.
EXITLIT, FRAMES = 0x884C10B4, 0x881C05F0
# AGGANCIO v2.4 (HW PROBE5: il render riscrive TUTTO il ring [R0, R0+256) a ogni giro -> i dati sono definitivi solo
# quando partono): l'ISR TX a 0x884C0E4A manda 8 long con 8 x (mov.l @r2+,r0; mov.l r0,@(0x60,gbr)). Lo sostituiamo
# (32 byte, interrupt bloccati, solo se i byte sono quelli originali) con: mov.l @(pc),r0; jsr @r0; nop; bra 0x884C0E6A;
# nop; .long mf_tx (P1); nop x9. mf_tx: r1 = 0x881C05F0, r2 = 4 frame, gbr = 0xA4000080 -> elabora, scrive la FIFO, r2 += 32.
TXPATCH = 0x884C0E4A
TXORIG = [0x6026, 0xC218] * 8
MGR, SIG2 = 0x8848ED30, 0x8B2A3D01
SLIDER = 0x8848C3E4                        # canale A/D B (HW 2026-09-27: cambia con lo slider; C3E0 no)
SL_LO0, SL_HI0 = 0x3FF, 0x000              # corsa appresa da zero (HW: 0x030..0x3D0 fisso non arrivava a 0/99): muovilo tutto una volta
SYS_PARAM, ID_TEMPO = 0x884E5CD0, 0x2000C
EV_PAD = 0x38
FS = 44100
ARR_OFF, SBUF_OFF = 0x800, 0x2800          # struttura per frame (2 KB) e buffer piccoli (8 KB) sotto la scratch dello slot
CODE_MAX = SLOT_CODE_MAX - SBUF_OFF        # il codice deve finire prima dei buffer
CACHE_SPAN = CODE_MAX
FW, FWR, FDL, FDR, FX0, FX1, FX2, FX3 = 0, 4, 8, 12, 16, 20, 24, 28
NST = 24
DLY_FR = 0x10000                           # frame (long) del buffer delay/looper/downer: 256 KB da BUF
REV_OFF = 0x40000                          # riverbero: da BUF + 0x40000
COMBS = [1116, 1188, 1277, 1356]          # v3: banco di comb MONO (prima 4 per canale) + 2 allpass in serie; R = 3o allpass
APS = [556, 441, 341]
AP_IO = [(20, 20), (20, 20), (20, 24)]     # (campo sorgente, campo destinazione) = X1 -> X1, X1 -> X1, X1 -> X2
# il 3o stadio (R) e' un RITARDO PURO di 341 campioni (Haas): l'allpass di Freeverb arriva a x2 -> R saturava
REV_ADDR = []
_o = REV_OFF
for n in COMBS + APS: REV_ADDR.append(_o); _o += 2 * n
REV_BYTES = _o - REV_OFF
assert _o <= FXBUF_SIZE
CH_FR = 1024                               # chorus: SBUF + 0 (4 KB); wow: SBUF + 0x1000 (L), + 0x1200 (R); IN +0x1400, OUT +0x1800 (1 KB)
IN_OFF, OUT_OFF = 0x1400, 0x1800
WOW_OFF = 0x1000
CH_BASE8 = 529 * 256
WOWINC = 53564
VK1, VK2, VSEED0, VSEEDC = 0x9E3779B1, 0x85EBCA6B, 0x2545F491, 0x6D2B79F5
SIN = [int(round(16384 * math.sin(2 * math.pi * i / 256))) for i in range(256)]
M32 = 0xFFFFFFFF

# ---------------- tabelle (0..99 -> coefficiente) ----------------
def expmap(p, lo, hi): return lo * (hi / lo) ** (p / 99)
def onepole(fc): return int(round((1 - math.exp(-2 * math.pi * fc / FS)) * 4096))
TABLES = {
    'F':     ('H', [int(round(2 * math.sin(math.pi * expmap(p, 40, 8000) / (2 * FS)) * 4096)) for p in range(100)]),   # SVF 2x oversampling
    'Q':     ('H', [int(round((1.4 - 1.15 * p / 99) * 4096)) for p in range(100)]),   # RES max meno esasperata (HW: glitch a fondo scala)
    'DRV':   ('H', [int(round(256 * 8 ** (p / 99))) for p in range(100)]),
    'ISO':   ('H', [0 if p == 0 else int(round(4096 * 10 ** (((p - 80) * 6 / 19 if p >= 80 else -60 * (80 - p) / 80) / 20))) for p in range(100)]),
    'HOLD':  ('H', [int(round(32 ** (p / 99))) for p in range(100)]),
    'TONE':  ('H', [onepole(expmap(p, 500, 16000)) for p in range(100)]),
    'VTONE': ('H', [onepole(expmap(p, 1500, 9000)) for p in range(100)]),
    'COMPT': ('H', [int(round(16000 * 10 ** (-p * 36 / 99 / 20))) for p in range(100)]),   # segnale tipico ~7000 (SH 14)
    'COMPR': ('H', [max(1, int(round((1 - math.exp(-1 / (expmap(p, 0.02, 0.8) * FS))) * 65536))) for p in range(100)]),
    'MAKEUP':('H', [int(round(4096 * 10 ** ((-6 + 24 * p / 99) / 20))) for p in range(100)]),
    'REVFB': ('H', [int(round((0.70 + 0.23 * p / 99) * 4096)) for p in range(100)]),   # max 0.93 (HW: gracchiava)
    'REVD':  ('H', [int(round((0.25 + 0.70 * p / 99) * 4096)) for p in range(100)]),
    'DLYB':  ('H', [512, 1024, 1365, 2048, 3072, 4096, 6144, 8192]),     # battute Q12: 1/32 1/16 1/8T 1/8 1/8D 1/4 1/4D 1/2
    'LOOPB': ('H', [512, 1024, 2048, 4096, 8192, 16384]),                 # 1/32 1/16 1/8 1/4 1/2 1 battuta
    'CHRATE':('I', [int(round(expmap(p, 0.1, 5) * 2 ** 32 / FS)) for p in range(100)]),
    'TRRATE':('I', [int(round(expmap(p, 0.5, 12) * 2 ** 32 / FS)) for p in range(100)]),
    'DOWN':  ('I', [int(round(2 ** 24 / (expmap(p, 0.15, 3.0) * FS))) for p in range(100)]),
    'SIN':   ('h', SIN),
    'STUTB': ('H', [4096, 2048, 1024, 512, 256]),                          # STUTTER: 1/4 1/8 1/16 1/32 1/64 (slider su = piu' veloce)
    'GATEB': ('H', [1024, 512]),                                           # TRANCE GATE: passo 1/16 o 1/32
    'GATEP': ('H', [0xAAAA, 0xCCCC, 0xEEEE, 0xB6B6, 0x8888, 0xDB6C, 0xF0F0, 0xADAD]),   # 8 pattern a 16 passi (bit 15 = primo)
    'REVB':  ('H', [1024, 2048, 4096, 8192]),                              # REVERSE: fette da 1/16, 1/8, 1/4, 1/2 battuta
    'SCATB': ('H', [512, 1024, 2048]),                                     # SCATTER: fette da 1/32, 1/16, 1/8 di battuta
}
TABOFF = {}; _t = 0
for k, (fmt, vals) in TABLES.items():
    _t = (_t + 3) & ~3; TABOFF[k] = _t; _t += struct.calcsize('<%d%s' % (len(vals), fmt))
TABLEN = (_t + 3) & ~3
K_RAW, K_T16, K_T32, K_LIN, K_CH16 = 0, 1, 2, 3, 4
SYNC = 1
def RAW(): return (K_RAW, 0, 0, 0, 0)
def T16(t): return (K_T16, 0, 0, 0, 0, t)
def T32(t): return (K_T32, 0, 0, 0, 0, t)
def LIN(lo, hi): return (K_LIN, 0, 0, lo, hi)
def CH(t, lo, hi): return (K_CH16, len(TABLES[t][1]), SYNC, lo, hi, t)
FX = [  # nome8, etichette (4, 5, 6), descrittori, valori proposti
    ("STUTTER",  ("RATE", "GATE ", "      "), (CH('STUTB', 64, DLY_FR - 1), LIN(1024, 4096), RAW()), (40, 99, 0)),
    ("SUPERFLT", ("CUT ", "RES  ", "TYPE  "), (T16('F'), T16('Q'), RAW()), (50, 60, 0)),
    ("TR.GATE",  ("PATT", "DEPTH", "RATE  "), (RAW(), LIN(0, 4096), CH('GATEB', 32, DLY_FR - 1)), (0, 90, 0)),
    ("LO-FI",    ("RATE", "BITS ", "TONE  "), (T16('HOLD'), LIN(0, 12), T16('TONE')), (40, 50, 70)),
    ("VINYL",    ("AMT ", "WOW  ", "TONE  "), (LIN(1, 100), LIN(0, 7168), T16('VTONE')), (50, 40, 35)),
    ("SCATTER",  ("AMT ", "RATE ", "      "), (LIN(0, 256), CH('SCATB', 32, 32767), RAW()), (60, 50, 0)),
    ("DELAY",    ("MIX ", "TIME ", "FDBK  "), (LIN(0, 4096), CH('DLYB', 64, DLY_FR - 1), LIN(0, 3482)), (50, 40, 40)),
    ("REVERB",   ("MIX ", "SIZE ", "TONE  "), (LIN(0, 4096), T16('REVFB'), T16('REVD')), (50, 60, 50)),
    ("REVERSE",  ("LEN ", "     ", "      "), (CH('REVB', 64, 32767), RAW(), RAW()), (35, 0, 0)),
    ("DOWNER",   ("TIME", "     ", "      "), (T32('DOWN'), RAW(), RAW()), (50, 0, 0)),
    ("CHORUS",   ("MIX ", "RATE ", "DEPTH "), (LIN(0, 4096), T32('CHRATE'), LIN(0, 220 * 256)), (60, 30, 50)),
    ("TREMOLO",  ("DPTH", "RATE ", "MODE  "), (LIN(0, 4096), T32('TRRATE'), RAW()), (70, 50, 0)),
]
NFX = len(FX)
E_STUT, E_SF, E_TG, E_LOFI, E_VIN, E_SCAT, E_DLY, E_REV, E_RVS, E_DOWN, E_CHO, E_TREM = range(NFX)
NEEDBUF = (E_STUT, E_SCAT, E_DLY, E_REV, E_RVS, E_DOWN)

def coef(desc, p, fpb):
    """modello Python dell'interprete dei descrittori (identico all'asm)"""
    kind = desc[0]
    if kind == K_RAW: v = p
    elif kind == K_T16: v = TABLES[desc[5]][1][p]
    elif kind == K_T32: v = TABLES[desc[5]][1][p]
    elif kind == K_LIN: v = desc[3] + (desc[4] - desc[3]) * p // 99
    else: v = TABLES[desc[5]][1][p * desc[1] // 100]
    if kind == K_CH16 and desc[2] & SYNC:
        v = ((v * fpb) & M32) >> 12
        v = max(desc[3], min(desc[4], v))
    return v

# ---------------- pagina ----------------
pg = PageAddon("mstfx2", open_press=None)
pg.reserve = SLOT_CODE_MAX                # HUB v2: i buffer (ARR, SBUF) stanno sotto la scratch -> tutte le 7 pagine (HW 2026-10-17: crash)
pg.other_menu("Master FX")
pg.far_hend = True
NAMEL = pg.dlabel(0, 0, 8)
L0L = pg.dlabel(9, 0, 4)
MAINV = pg.value("", 13, 0, fmt="  0", lo=0, hi=99, default=FX[0][3][0])
L1L = pg.dlabel(0, 1, 5)
P1V = pg.value("", 5, 1, fmt=" 0", lo=0, hi=99, default=FX[0][3][1])
L2L = pg.dlabel(8, 1, 6)
P2V = pg.value("", 14, 1, fmt=" 0", lo=0, hi=99, default=FX[0][3][2])
# Variabili condivise con l'ISR (CSET): in un blocco a parte della scratch (CPOOL, righe di cache da 16 byte tutte sue),
# lette e scritte SOLO via P1. Prima stavano mescolate agli slot della pagina (via P2) nelle stesse righe: con la cache
# copy-back dell'MPC una riga sporca via P1 riscritta in memoria cancellava le scritture P2 vicine (memguard 2026-10-17:
# 18 righe miste). Azzerato via P1 all'avvio (cp_init), dopo aver invalidato le sue righe.
CPOOL = LOAD_SCRATCH + 0x800
_cp = [CPOOL]
def cs(n=None):
    if n is None: s_ = _cp[0]; _cp[0] += 4; CSET.add(s_); return s_
    return [cs() for _ in range(n)]
def ps(n): return [pg.alloc_slot() for _ in range(n)]
HOOKED = pg.alloc_slot(); SEL, ACT, PN, LASTPN = cs(4); SELCHG, LASTS, FPB, FCNT = ps(4)
G, FXRUN = cs(2); R0S = pg.alloc_slot(); ARRP, SBUFP, BUFP, SINP = cs(4); TABP = pg.alloc_slot()
CF = cs(3)
TXP = cs()
INP, OUTP, LASTJ = cs(3)
OUTID = cs(4)
BUSY2 = cs()
CFR = cs(3)                                          # coefficienti bloccati per FXRUN (l'ISR usa solo questi)
CFFX, CFOK, SWPEND, MISS = cs(4)
MSGB = [pg.alloc_slot() for _ in range(4)]
PV = [[pg.alloc_slot(default=FX[e][3][k]) for k in range(3)] for e in range(NFX)]
S = cs(NST)
LASTV = [pg.alloc_slot() for _ in range(3)]
SL_EDGE = 24                               # entro 24 punti (~2.5%) dagli estremi appresi: 0 / 99, aggiornato sempre
SLO, SHI = pg.alloc_slot(default=SL_LO0), pg.alloc_slot(default=SL_HI0)
SF, SLACT = pg.alloc_slot(), pg.alloc_slot()   # slider filtrato (x16) e fire rimasti di 'slider in movimento'
TENT, DSUM, NCALL, TWT, TWC, LOADP, KILL = cs(7)
TTK = cs()                            # tick OS all'inizio di mf_proc (HW: durate > 1 tick venivano contate modulo tick)   # guardia di carico (TMU1 + tick OS)
CPOOL_END = (_cp[0] + 15) & ~15
assert pg.next_slot <= CPOOL and CPOOL_END <= LOAD_SCRATCH + 0x1000
def cp_init(m):
    """una volta, a scratch appena azzerata: (1) se l'ISR dell'OS ha ancora l'aggancio di un Master FX precedente (HUB
    ricaricato o add-on reinstallato con un effetto attivo: HOOKED qui e' 0) lo stacca PRIMA di azzerare le variabili che
    quell'ISR usa; (2) invalida (con riscrittura) le righe P1 di CPOOL rimaste in cache da chi occupava prima queste
    pagine, poi azzera CPOOL via P1. Gira da P2."""
    a = m.a
    m.imm32(TXPATCH, 1); a.movw_ld(1, 0); a.extu_w(0, 0); m.imm32(0xD002, 2); a.cmpeq(2, 0); nsh = m.newlabel('nsh'); a.bf_lbl(nsh)
    a.add_imm(2, 1); a.movw_ld(1, 0); a.extu_w(0, 0); m.imm32(0x400B, 2); a.cmpeq(2, 0); a.bf_lbl(nsh)
    a.movl_pool('mf_txoff', 2); a.jsr(2); a.nop()                                        # aggancio rimasto: via
    a.deflabel(nsh)
    m.imm32(_p1(m, CPOOL), 4); m.imm32((CPOOL_END - CPOOL) // 16, 5); m.imm32(0xF0000008, 6); m.imm32(0x1FFFFC00, 7); m.imm32(0xFF0, 3)
    l = m.newlabel('cpi'); a.deflabel(l)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0); a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(l)
    m.imm32(_p1(m, CPOOL), 1); m.imm32((CPOOL_END - CPOOL) // 4, 2); a.mov_imm(0, 0)
    l = m.newlabel('cpz'); a.deflabel(l); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(l)
pg.init_fn = cp_init
TCOR1, TCNT1, TICKS = 0xFFFFFEA0, 0xFFFFFEA4, 0x881C05C8
# v2.6: la v2.5 confrontava ogni chiamata col periodo dall'ultima -> l'interrupt TX arriva a raffiche -> OVERLOAD sempre.
# Ora: carico = somma delle durate / tempo trascorso (tick OS * (TCOR1+1) + TCNT) su NWIN chiamate; > LOADMAX% -> KILL.
NWIN, LOADMAX = 32, 60                       # finestra = 32 blocchi elaborati (~23 ms)
pg.defaults[SELCHG] = 1
pg.defaults[LASTS] = -1
pg.defaults[FPB] = 22050

def hook_on(m):  pass

def udiv_call(m):
    m.a.movl_pool('__udiv', 2); m.a.jsr(2); m.a.nop()

def clip(m, r, kmax=14, tmp=1):
    """r in [-32768, 32767] (kmax = 32767)"""
    a = m.a; l1 = m.newlabel('k1'); l2 = m.newlabel('k2')
    a.cmpgt(kmax, r); a.bf_lbl(l1); a.mov_rm_rn(kmax, r); a.deflabel(l1)
    a.not_rr(kmax, tmp); a.cmpgt(r, tmp); a.bf_lbl(l2); a.mov_rm_rn(tmp, r); a.deflabel(l2)

KNEE = 24576
def softclip(m, r, tmp):
    """limitatore morbido: oltre +-KNEE pendenza 1/4, poi taglio a s16 (HW: il taglio netto gracchiava)"""
    a = m.a; l1 = m.newlabel('s1'); l2 = m.newlabel('s2')
    m.imm32(KNEE, tmp); a.cmpgt(tmp, r); a.bf_lbl(l1); a.sub(tmp, r); a.shar(r); a.shar(r); a.add_rr(tmp, r); a.deflabel(l1)
    m.imm32(-KNEE, tmp); a.cmpgt(r, tmp); a.bf_lbl(l2); a.sub(tmp, r); a.shar(r); a.shar(r); a.add_rr(tmp, r); a.deflabel(l2)
    m.imm32(32767, tmp); l3 = m.newlabel('s3'); a.cmpgt(tmp, r); a.bf_lbl(l3); a.mov_rm_rn(tmp, r); a.deflabel(l3)
    a.not_rr(tmp, tmp); l4 = m.newlabel('s4'); a.cmpgt(r, tmp); a.bf_lbl(l4); a.mov_rm_rn(tmp, r); a.deflabel(l4)

def sar(m, r, n, tmp):
    m.a.mov_imm(-n, tmp); m.a.shad(tmp, r)

def ldS(m, k, r): ld(m, S[k], r)
def jt_abs(m, lbl):
    """se T: salto assoluto a lbl (r2 clobberato)"""
    a = m.a; sk = m.newlabel('ja'); a.bf_lbl(sk); a.movl_pool('P1:' + lbl, 2); a.jmp(2); a.nop(); a.deflabel(sk)
def jf_abs(m, lbl):
    a = m.a; sk = m.newlabel('jb'); a.bt_lbl(sk); a.movl_pool('P1:' + lbl, 2); a.jmp(2); a.nop(); a.deflabel(sk)
# ATTENZIONE (HW v3.4, cache ACCESA CCR=5): nel codice dell'ISR ogni salto assoluto deve usare l'alias P1 ('P1:lbl'):
# un literal P2 portava tutto l'effetto nella SDRAM senza cache (~5-8x piu' lento -> blocchi persi, M -> 999)
def stS(m, r, k): st(m, r, S[k])

# ================= main loop =================
def maint(m):
    """a ogni fire (pagina attiva o no): costanti, buffer, tempo, coefficienti per SEL, aggancio/sgancio"""
    a = m.a
    a.deflabel('mf_maint'); a.stsl_pr()
    # puntatori P1
    m.imm32(LOAD_SCRATCH, 0); m.imm32(ARR_OFF, 1); a.sub(1, 0); m.imm32(0xDFFFFFFF, 1); a.and_rr(1, 0); st(m, 0, ARRP)
    m.imm32(LOAD_SCRATCH, 0); m.imm32(SBUF_OFF, 1); a.sub(1, 0); m.imm32(0xDFFFFFFF, 1); a.and_rr(1, 0); st(m, 0, SBUFP)
    a.movl_pool('mf_tabs', 0); m.imm32(TABOFF['SIN'], 1); a.add_rr(1, 0); m.imm32(0xDFFFFFFF, 1); a.and_rr(1, 0); st(m, 0, SINP)
    a.movl_pool('mf_tabs', 0); st(m, 0, TABP)
    ld(m, SBUFP, 0); m.imm32(IN_OFF, 1); a.add_rr(1, 0); st(m, 0, INP)
    ld(m, SBUFP, 0); m.imm32(OUT_OFF, 1); a.add_rr(1, 0); st(m, 0, OUTP)
    # buffer grande: loader v10 (firma + marcatore in B = MGR[1])
    sti(m, 0, BUFP)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 8)
    a.mov_rm_rn(8, 1); a.add_imm(0x40, 1); a.movl_ld(1, 0); m.imm32(SCRATCH_MAGIC, 2); a.cmpeq(2, 0); nb = m.newlabel('nb'); m.bf_far(nb)
    a.mov_rm_rn(8, 1); a.add_imm(0x44, 1); a.movl_ld(1, 0); m.imm32(SIG2, 2); a.cmpeq(2, 0); m.bf_far(nb)
    a.mov_rm_rn(8, 1); a.add_imm(FXMARK_OFF, 1); a.movl_ld(1, 0); m.imm32(RESERVE, 2); a.cmpeq(2, 0); m.bf_far(nb)
    m.imm32(FXBUF_OFF, 0); a.add_rr(8, 0); m.imm32(0xDFFFFFFF, 1); a.and_rr(1, 0); st(m, 0, BUFP)
    a.deflabel(nb)
    m.pool_jump()
    # tempo sequenza ogni 32 fire -> FPB = frame per battuta = 26460000 / BPM10
    ld(m, FCNT, 0); a.add_imm(1, 0); st(m, 0, FCNT); a.and_imm(31); a.tst_rr(0, 0); nt = m.newlabel('nt'); m.bf_far(nt)
    sti(m, 0, MSGB[1]); sti(m, 0x10, MSGB[0]); sti(m, ID_TEMPO, MSGB[3]); sti(m, 0, MSGB[2])
    m.imm32(MSGB[0], 4); m.imm32(SYS_PARAM, 2); a.jsr(2); a.nop()
    ld(m, MSGB[1], 5); m.imm32(300, 3); a.cmpge(3, 5); tb = m.newlabel('tb'); a.bf_lbl(tb)
    m.imm32(3000, 3); a.cmpgt(3, 5); a.bt_lbl(tb)
    m.imm32(26460000, 4); udiv_call(m); st(m, 0, FPB)
    a.deflabel(tb)
    a.deflabel(nt)
    m.pool_jump()
    # coefficienti dell'effetto scelto: CF[k] = descrittore(SEL, k)(PV[SEL][k])
    ld(m, SEL, 9); a.mov_rm_rn(9, 0); a.shll2(0); a.mov_rm_rn(0, 1); a.add_rr(0, 0); a.add_rr(1, 0)   # r0 = SEL*12
    m.imm32(PV[0][0], 10); a.add_rr(0, 10)                                               # r10 = &PV[SEL][0]
    a.mov_rm_rn(9, 0); a.mov_imm(36, 1); a.mul_l(1, 0); a.sts_macl(0); a.movl_pool('mf_desc', 11); a.add_rr(0, 11)   # r11 = &desc[SEL][0]
    sti(m, -1, CFFX)                                                                    # l'ISR non copia CF a meta' calcolo
    ld(m, SEL, 9)
    m.imm32(_p1(m, CF[0]), 12); a.mov_imm(3, 13)                                     # CF e' in CPOOL: solo via P1
    m.pool_jump(); kl = m.newlabel('kl'); a.deflabel(kl)
    a.movl_ld(10, 8)                                                                    # r8 = p
    a.movb_ld(11, 0); a.extu_b(0, 0)                                                    # kind
    a.mov_rm_rn(11, 1); a.add_imm(2, 1); a.movw_ld(1, 3); a.extu_w(3, 3); ld(m, TABP, 2); a.add_rr(2, 3)   # r3 = tabella
    lraw, lt16, lt32, llin, lch, kdone = [m.newlabel(x) for x in ('lraw', 'lt16', 'lt32', 'llin', 'lch', 'kd')]
    a.mov_imm(K_T16, 1); a.cmpeq(1, 0); m.bt_far(lt16)
    a.mov_imm(K_T32, 1); a.cmpeq(1, 0); m.bt_far(lt32)
    a.mov_imm(K_LIN, 1); a.cmpeq(1, 0); m.bt_far(llin)
    a.mov_imm(K_CH16, 1); a.cmpeq(1, 0); m.bt_far(lch)
    a.deflabel(lraw); a.mov_rm_rn(8, 7); a.bra_lbl(kdone); a.nop()
    a.deflabel(lt16); a.mov_rm_rn(8, 0); a.shll(0); a.add_rr(3, 0); a.movw_ld(0, 7); a.extu_w(7, 7); a.bra_lbl(kdone); a.nop()
    a.deflabel(lt32); a.mov_rm_rn(8, 0); a.shll2(0); a.add_rr(3, 0); a.movl_ld(0, 7); a.bra_lbl(kdone); a.nop()
    a.deflabel(llin)
    a.movl_ld_disp(11, 4, 1); a.movl_ld_disp(11, 8, 4); a.sub(1, 4); a.mul_l(8, 4); a.sts_macl(4)      # (hi-lo)*p
    a.mov_imm(99, 5); udiv_call(m); a.movl_ld_disp(11, 4, 1); a.add_rr(1, 0); a.mov_rm_rn(0, 7)
    a.bra_lbl(kdone); a.nop()
    m.pool_jump()
    a.deflabel(lch)
    a.mov_rm_rn(11, 1); a.add_imm(1, 1); a.movb_ld(1, 5); a.extu_b(5, 5)                 # n
    a.mov_rm_rn(8, 4); a.mul_l(5, 4); a.sts_macl(4); a.mov_imm(100, 5); udiv_call(m)       # idx = p*n/100
    a.shll(0); a.add_rr(3, 0); a.movw_ld(0, 7); a.extu_w(7, 7)
    ld(m, FPB, 0); a.mul_l(0, 7); a.sts_macl(7); a.shlr8(7); a.shlr2(7); a.shlr2(7)      # (v*FPB) >> 12
    a.movl_ld_disp(11, 4, 1); cl1 = m.newlabel('cl1'); a.cmphs(1, 7); a.bt_lbl(cl1); a.mov_rm_rn(1, 7); a.deflabel(cl1)
    a.movl_ld_disp(11, 8, 1); cl2 = m.newlabel('cl2'); a.cmphs(7, 1); a.bt_lbl(cl2); a.mov_rm_rn(1, 7); a.deflabel(cl2)
    a.deflabel(kdone)
    a.movl_st(7, 12); a.add_imm(4, 12); a.add_imm(4, 10); a.add_imm(12, 11)
    a.dt(13); m.bf_far(kl)
    ld(m, SEL, 0); st(m, 0, CFFX)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

def ui(m):
    """pagina attiva: SEL cambiato -> campi/etichette; campi -> PV; slider -> parametro principale"""
    a = m.a
    ld(m, SEL, 9); a.mov_rm_rn(9, 0); a.shll2(0); a.mov_rm_rn(0, 1); a.add_rr(0, 0); a.add_rr(1, 0)
    m.imm32(PV[0][0], 10); a.add_rr(0, 10)                                               # r10 = &PV[SEL][0]
    ld(m, SELCHG, 0); a.tst_rr(0, 0); nsc = m.newlabel('nsc'); m.bt_far(nsc)
    sti(m, 0, SELCHG)
    for k, slot in enumerate((MAINV, P1V, P2V)):
        a.movl_ld_disp(10, 4 * k, 0); st(m, 0, slot); st(m, 0, LASTV[k])
    # testi: nome 8, etichette 4/5/6 (tabella di 23 byte per effetto)
    a.mov_rm_rn(9, 0); a.mov_imm(23, 1); a.mul_l(1, 0); a.sts_macl(0); a.movl_pool('mf_names', 11); a.add_rr(0, 11)
    for lbl, w in ((NAMEL, 8), (L0L, 4), (L1L, 5), (L2L, 6)):
        m.imm32(pg.addr['dlabels'][lbl], 12); a.mov_imm(w, 13)
        cl = m.newlabel('cl'); a.deflabel(cl); a.movb_ld(11, 0); a.movb_st(0, 12); a.add_imm(1, 11); a.add_imm(1, 12); a.dt(13); a.bf_lbl(cl)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.bra_lbl('mf_uisl'); a.nop()
    m.pool_jump()
    a.deflabel(nsc)
    m.pool_jump()
    for k, slot in enumerate((MAINV, P1V, P2V)):
        ld(m, slot, 0); a.movl_st_disp(0, 4 * k, 10)
    a.deflabel('mf_uisl')
    m.pool_jump()
    # riga 1 mentre il pad e' premuto: "FILT 23%" (carico misurato dell'interrupt); dopo KILL: "OVL  87%"
    ld(m, ACT, 0); a.tst_rr(0, 0); nk = m.newlabel('nk'); m.bt_far(nk)
    ld(m, LOADP, 4); a.cmppz(4); m.bf_far(nk)
    m.imm32(pg.addr['dlabels'][NAMEL], 12)
    ld(m, KILL, 0); a.tst_rr(0, 0); ko = m.newlabel('ko'); a.bt_lbl(ko)
    for ch in "OVL ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.bra_lbl('mf_ldd'); a.nop()
    a.deflabel(ko)
    ld(m, MISS, 0); a.tst_rr(0, 0); km = m.newlabel('km'); a.bt_lbl(km)                  # blocchi persi: "M012 23%"
    a.mov_rm_rn(4, 11); a.mov_rm_rn(0, 4); m.imm32(999, 0); c99 = m.newlabel('c99'); a.cmpgt(0, 4); a.bf_lbl(c99); a.mov_rm_rn(0, 4); a.deflabel(c99)
    a.mov_imm(ord('M'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_imm(100, 5); udiv_call(m); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(1, 4)
    a.mov_imm(10, 5); udiv_call(m); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 12)
    a.add_imm(1, 12); a.mov_rm_rn(11, 4)
    a.bra_lbl('mf_ldd'); a.nop()
    a.deflabel(km); a.add_imm(4, 12)
    a.deflabel('mf_ldd')
    a.mov_imm(ord(' '), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.mov_imm(99, 0); c9 = m.newlabel('c9'); a.cmpgt(0, 4); a.bf_lbl(c9); a.mov_rm_rn(0, 4); a.deflabel(c9)
    a.mov_imm(10, 5); udiv_call(m); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 12)   # (udiv non tocca r12)
    m.imm32(pg.addr['dlabels'][NAMEL] + 7, 1); a.mov_imm(ord('%'), 0); a.movb_st(0, 1)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel(nk)
    # slider (A/D rumoroso, HW): media IIR 1/8 (x16), auto-calibrazione sul filtrato; prende il controllo solo se si
    # sposta di >= 16 da fermo (>= 6 mentre lo stai muovendo, per 200 fire): altrimenti la rotella resta libera
    m.imm32(SLIDER, 1); a.movl_ld(1, 2); m.imm32(0x3FF, 1); a.and_rr(1, 2)
    ld(m, LASTS, 3); a.cmppz(3); fs_ = m.newlabel('fs'); a.bt_lbl(fs_)
    st(m, 2, LASTS); a.mov_rm_rn(2, 0); a.shll2(0); a.shll2(0); st(m, 0, SF); a.bra_lbl('mf_uix'); a.nop()
    a.deflabel(fs_)
    # pad premuto (effetto in uso): filtro 1/2 e soglia 3 = reattivo per la performance (HW: troppo "pesante"); altrimenti 1/16
    # pad premuto: NESSUN filtro (HW: con l'effetto attivo i fire sono radi e il filtro restava indietro: 85/95)
    ld(m, SF, 0); a.shll2(2); a.shll2(2)
    ld(m, ACT, 7); a.tst_rr(7, 7); fa = m.newlabel('fa'); fd = m.newlabel('fd'); a.bt_lbl(fa); a.mov_rm_rn(2, 0); a.bra_lbl(fd); a.nop()
    a.deflabel(fa); a.sub(0, 2); a.shar(2); a.shar(2); a.shar(2); a.shar(2); a.add_rr(2, 0)
    a.deflabel(fd); st(m, 0, SF)
    a.mov_rm_rn(0, 2); a.shar(2); a.shar(2); a.shar(2); a.shar(2)                       # r2 = filtrato
    ld(m, SLO, 0); c1 = m.newlabel('c1'); a.cmpgt(2, 0); a.bf_lbl(c1); st(m, 2, SLO); a.deflabel(c1)
    ld(m, SHI, 0); c2 = m.newlabel('c2'); a.cmpgt(0, 2); a.bf_lbl(c2); st(m, 2, SHI); a.deflabel(c2)
    ld(m, SLACT, 0); a.mov_imm(16, 5); a.tst_rr(0, 0); sa = m.newlabel('sa'); a.bt_lbl(sa); a.add_imm(-1, 0); st(m, 0, SLACT); a.mov_imm(8, 5); a.deflabel(sa)
    a.tst_rr(7, 7); fb_ = m.newlabel('fb'); a.bt_lbl(fb_); a.mov_imm(3, 5); a.deflabel(fb_)                   # pad premuto: soglia 3
    # vicino agli estremi (HW: a volte non arrivava a 0/99) e corsa gia' calibrata: soglia 4 invece di 16/8
    ed = m.newlabel('ed'); ld(m, SHI, 0); ld(m, SLO, 3); a.sub(3, 0); m.imm32(256, 1); a.cmpge(1, 0); a.bf_lbl(ed)
    ld(m, SHI, 0); a.sub(2, 0); m.imm32(SL_EDGE, 1); e4 = m.newlabel('e4'); a.cmpge(1, 0); a.bf_lbl(e4)
    a.mov_rm_rn(2, 0); a.sub(3, 0); a.cmpge(1, 0); a.bt_lbl(ed)
    a.deflabel(e4); a.mov_imm(4, 5)
    a.deflabel(ed)
    ld(m, LASTS, 3); a.mov_rm_rn(2, 0); a.sub(3, 0); a.cmppz(0); ab = m.newlabel('ab'); a.bt_lbl(ab); a.neg(0, 0); a.deflabel(ab)
    a.cmpge(5, 0); m.bf_far('mf_uix')
    st(m, 2, LASTS)
    a.mov_imm(16, 1); ra = m.newlabel('ra'); a.cmpge(1, 0); a.bf_lbl(ra); sti(m, 200, SLACT); a.deflabel(ra)   # solo i movimenti grandi (>=16) tengono 'in movimento'
    ld(m, SLO, 0); a.mov_rm_rn(2, 4); a.sub(0, 4); a.mov_imm(100, 1); a.mul_l(1, 4); a.sts_macl(4)
    ld(m, SHI, 5); a.sub(0, 5); a.add_imm(1, 5); udiv_call(m); a.mov_rm_rn(0, 2)
    a.mov_imm(99, 0); c3 = m.newlabel('c3'); a.cmpgt(0, 2); a.bf_lbl(c3); a.mov_rm_rn(0, 2); a.deflabel(c3)
    ld(m, SHI, 0); ld(m, SLO, 3); a.sub(3, 0); m.imm32(256, 1); sn = m.newlabel('sn'); a.cmpge(1, 0); a.bf_lbl(sn)   # calibrato?
    ld(m, LASTS, 3); ld(m, SHI, 0); a.sub(3, 0); m.imm32(SL_EDGE, 1); e9 = m.newlabel('e9'); a.cmpge(1, 0); a.bt_lbl(e9); a.mov_imm(99, 2); a.deflabel(e9)
    ld(m, SLO, 0); a.sub(0, 3); m.imm32(SL_EDGE, 1); e0 = m.newlabel('e0'); a.cmpge(1, 3); a.bt_lbl(e0); a.mov_imm(0, 2); a.deflabel(e0)
    a.deflabel(sn)
    st(m, 2, MAINV); a.movl_st(2, 10)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel('mf_uix')

def fire(m):
    a = m.a
    ui(m)
    a.movl_pool('mf_maint', 2); a.jsr(2); a.nop()
    # aggancio (solo sull'OS atteso)
    ld(m, HOOKED, 0); a.tst_rr(0, 0); hk = m.newlabel('hk'); m.bf_far(hk)
    a.movl_pool('mf_txchk', 2); a.jsr(2); a.nop(); a.tst_rr(0, 0); m.bt_far(hk)            # byte OS inattesi: niente
    a.movl_pool('mf_icinv', 2); a.jsr(2); a.nop()
    sti(m, 0, ACT); sti(m, 0, G); m.imm32(-1, 0); st(m, 0, FXRUN); ld(m, PN, 0); st(m, 0, LASTPN)
    for k in range(4): sti(m, -1, OUTID[k])
    ld(m, INP, 1); m.imm32(0x800 // 4, 2); a.mov_imm(0, 3)                          # IN + OUT (contigui) = 0: niente residui
    zi = m.newlabel('zi'); a.deflabel(zi); a.movl_st(3, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(zi)
    sti(m, -1, LASTJ); sti(m, 0, BUSY2); sti(m, 0, NCALL)
    a.movl_pool('mf_txon', 2); a.jsr(2); a.nop()
    sti(m, 1, HOOKED)
    a.deflabel(hk)
pg.on_fire(fire)

def bulk(m):
    """codice grande (main loop + ISR) nella sezione DO IT del framework (dopo l'epilogo): DO IT non fa nulla"""
    a = m.a
    a.movl_pool('mf_dx', 2); a.jmp(2); a.nop()
    m.pool_jump()
    maint(m)
    isr_code(m)
    a.deflabel('mf_dx')
pg.on_doit(bulk)

def inactive(m):
    """pagina non attiva: effetto rilasciato; sgancio a dissolvenza finita"""
    a = m.a
    sti(m, 0, ACT)
    ld(m, HOOKED, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, G, 0); a.tst_rr(0, 0); m.bf_far(ix)
    ld(m, BUSY2, 0); a.tst_rr(0, 0); m.bf_far(ix)
    a.movl_pool('mf_txoff', 2); a.jsr(2); a.nop(); sti(m, 0, HOOKED)
    a.deflabel(ix)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """pad 0..11: pressione = effetto attivo (NOTE ON), rilascio = spento. Consumati: non suonano i sample"""
    a = m.a
    a.mov_rm_rn(8, 1); a.add_imm(2, 1); a.movw_ld(1, 9); a.extu_w(9, 9)                 # r9 = pad
    a.mov_imm(NFX, 0); a.cmphs(0, 9); pz = m.newlabel('pz'); a.bf_lbl(pz)
    a.movl_pool(hret0, 2); a.jmp(2); a.nop()
    a.deflabel(pz)
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); rel = m.newlabel('rel'); m.bt_far(rel)
    # buffer da azzerare: qui (main loop, via P1 come l'ISR) e non nell'ISR (24 KB = troppo per un interrupt)
    for eff, slot, off, nb in ((E_REV, BUFP, REV_OFF, (REV_BYTES + 3) & ~3), (E_CHO, SBUFP, 0, CH_FR * 4), (E_VIN, SBUFP, WOW_OFF, 0x400)):
        zx = m.newlabel('zx'); a.mov_imm(eff, 0); a.cmpeq(0, 9); a.bf_lbl(zx)
        ld(m, slot, 1); a.tst_rr(1, 1); a.bt_lbl(zx)
        if off: m.imm32(off, 2); a.add_rr(2, 1)
        m.imm32(nb // 4, 2); a.mov_imm(0, 3)
        zl = m.newlabel('zl'); a.deflabel(zl); a.movl_st(3, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(zl)
        a.deflabel(zx); m.pool_jump()
    sti(m, -1, LOADP); sti(m, 0, MISS)
    st(m, 9, SEL)
    a.movl_st_dec(9, 15); a.movl_pool('mf_maint', 2); a.jsr(2); a.nop(); a.movl_ld_inc(15, 9)   # coefficienti subito
    a.add_imm(1, 9); st(m, 9, ACT); ld(m, PN, 0); a.add_imm(1, 0); st(m, 0, PN); sti(m, 1, SELCHG)
    a.movl_pool(hret1, 2); a.jmp(2); a.nop()
    a.deflabel(rel)
    a.add_imm(1, 9); ld(m, ACT, 0); a.cmpeq(9, 0); nr = m.newlabel('nr'); a.bf_lbl(nr); sti(m, 0, ACT); sti(m, 1, SELCHG); a.deflabel(nr)
    a.movl_pool(hret1, 2); a.jmp(2); a.nop()
    m.pool_jump()
pg.on_event(EV_PAD, ev_pad)

# ================= ISR (alias P1) =================
def frame_loop(m, body, n=BLK):
    """r12 = struttura frame (P1), r4 = contatore; body(m) usa r12 (+campi)"""
    a = m.a
    ld(m, ARRP, 12); a.mov_imm(n, 4)
    m.pool_jump(); l = m.newlabel('fl'); a.deflabel(l)
    body(m)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)

def isr_code(m):
    a = m.a
    # ---- mf_icinv ----
    a.deflabel('mf_icinv')
    a.movl_pool('__cstart', 4); m.imm32(0xFFFFFFF0, 1); a.and_rr(1, 4)
    m.imm32(CACHE_SPAN // 16 + 1, 5); m.imm32(0xF0000008, 6); m.imm32(0x1FFFFC00, 7); m.imm32(0xFF0, 3)
    icl = m.newlabel('icl'); a.deflabel(icl)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0)
    a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(icl)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- patch: mf_txchk (r0 = 1 se TX e literal d'uscita sono gli originali), mf_txon / mf_txoff (interrupt bloccati) ----
    a.deflabel('mf_txchk')
    m.imm32(EXITLIT, 1); a.movl_ld(1, 3); m.imm32(ISR_EXIT, 4); a.cmpeq(4, 3); bad = m.newlabel('bad'); a.bf_lbl(bad)
    m.imm32(TXPATCH, 1); a.mov_imm(8, 2); a.mov_imm(1, 0)
    ck = m.newlabel('ck'); a.deflabel(ck)
    a.movw_ld(1, 3); a.extu_w(3, 3); m.imm32(0x6026, 4); a.cmpeq(4, 3); a.bf_lbl(bad); a.add_imm(2, 1)
    a.movw_ld(1, 3); a.extu_w(3, 3); m.imm32(0xC218, 4); a.cmpeq(4, 3); a.bf_lbl(bad); a.add_imm(2, 1)
    a.dt(2); a.bf_lbl(ck); a.rts(); a.nop()
    a.deflabel(bad); a.rts(); a.mov_imm(0, 0)
    m.pool_jump()
    def words(ws, lit, exitval):
        a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0)                   # IMASK = 15
        m.imm32(TXPATCH, 1)
        for w in ws:
            if w == 'LO': a.mov_rm_rn(lit, 0)
            elif w == 'HI': a.mov_rm_rn(lit, 0); a.shlr16(0)
            else: m.imm32(w, 0)
            a.movw_st(0, 1); a.add_imm(2, 1)
        exitval(); m.imm32(EXITLIT, 1); a.movl_st(0, 1)
        a.ldc_sr(7); a.rts(); a.nop()
        m.pool_jump()
    a.deflabel('mf_txon')
    a.movl_pool('mf_tx', 6); m.imm32(0xDFFFFFFF, 1); a.and_rr(1, 6)
    words([0xD002, 0x400B, 0x0009, 0xA00B, 0x0009, 'LO', 'HI'] + [0x0009] * 9, 6,
          lambda: (a.movl_pool('mf_trig', 0), m.imm32(0xDFFFFFFF, 1), a.and_rr(1, 0)))
    a.deflabel('mf_txoff')
    words(TXORIG, 6, lambda: m.imm32(ISR_EXIT, 0))
    a.deflabel('__cstart')
    # ---- mf_tx (ISR TX, al posto dell'invio di 8 long): r1 = 0x881C05F0, r2 = 4 frame, gbr = FIFO; r2 += 32 ----
    a.deflabel('mf_tx')
    for r in (3, 4, 5, 6, 7): a.movl_st_dec(r, 15)
    a.movl_ld(1, 3)                                                                     # F = frame OS
    a.mov_rm_rn(3, 0); a.shlr2(0); a.and_imm(7); a.shll2(0); a.shll2(0); a.shll(0); a.mov_rm_rn(0, 5)   # r5 = s*32
    a.shlr2(3); a.shlr2(3); a.shlr(3)                                                   # r3 = b
    a.mov_rm_rn(3, 0); a.and_imm(3); a.shll8(0); m.a.movl_pool(_p1(m, INP), 4); a.movl_ld(4, 4); a.add_rr(0, 4); a.add_rr(5, 4)   # r4 = &IN[b%4][s] (r1 dell'OS intatto)
    for _ in range(8): a.movl_ld(2, 0); a.add_imm(4, 2); a.movl_st(0, 4); a.add_imm(4, 4)
    a.add_imm(-LAT, 3); a.mov_rm_rn(3, 0); a.and_imm(3); a.mov_rm_rn(0, 6)               # r3 = b-LAT, r6 = (b-LAT)%4
    a.shll2(0); m.a.movl_pool(_p1(m, OUTID[0]), 7); a.add_rr(0, 7); a.movl_ld(7, 7)      # OUTID[(b-2)%4]
    a.shll8(6); a.add_rr(5, 6)
    m.a.movl_pool(_p1(m, INP), 4); a.cmpeq(3, 7); dry = m.newlabel('dry'); a.bf_lbl(dry); m.a.movl_pool(_p1(m, OUTP), 4); a.deflabel(dry)
    a.movl_ld(4, 4)
    a.add_rr(6, 4)
    for _ in range(8): a.movl_ld(4, 0); a.add_imm(4, 4); a.movl_st_gbr(0x60)
    # diagnosi: blocco b-2 non pronto mentre b-3 lo era -> MISS++ (solo al primo slot del blocco)
    mx = m.newlabel('mx'); a.tst_rr(5, 5); a.bf_lbl(mx)
    m.a.movl_pool(_p1(m, OUTID[0]), 7)
    a.mov_rm_rn(3, 0); a.and_imm(3); a.shll2(0); a.add_rr(7, 0); a.movl_ld(0, 6); a.cmpeq(3, 6); a.bt_lbl(mx)
    a.mov_rm_rn(3, 4); a.add_imm(-1, 4); a.mov_rm_rn(4, 0); a.and_imm(3); a.shll2(0); a.add_rr(7, 0); a.movl_ld(0, 6)
    a.cmpeq(4, 6); a.bf_lbl(mx)
    m.a.movl_pool(_p1(m, MISS), 6); a.movl_ld(6, 0); a.add_imm(1, 0); a.movl_st(0, 6)
    a.deflabel(mx)
    for r in (7, 6, 5, 4, 3): a.movl_ld_inc(15, r)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- mf_trig (uscita di ogni ISR / ritorno del render): lancia mf_proc se c'e' un blocco nuovo ----
    a.deflabel('mf_trig')
    ld(m, BUSY2, 0); a.tst_rr(0, 0); tx = m.newlabel('tx'); a.bf_lbl(tx)
    ld(m, ACT, 0); ld(m, G, 1); a.or_rr(1, 0); a.tst_rr(0, 0); a.bt_lbl(tx)              # niente effetto: niente lavoro
    m.imm32(FRAMES, 1); a.movl_ld(1, 3); a.shlr2(3); a.shlr2(3); a.shlr(3); a.add_imm(-1, 3)   # j = F/32 - 1
    ld(m, LASTJ, 0); a.cmpeq(3, 0); a.bt_lbl(tx)
    st(m, 3, LASTJ); sti(m, 1, BUSY2)
    m.imm32(ISR_EXIT, 1); a.lds_pr(1)
    a.movl_pool('mf_proc', 3); m.imm32(0xDFFFFFFF, 1); a.and_rr(1, 3)
    a.stc_sr(0); m.imm32(0x20000000, 1); a.tst_rr(1, 0); rb0 = m.newlabel('rb0'); a.bt_lbl(rb0)
    a.ldc_spc(3); a.rte(); a.nop()                                                      # contesto ISR: come l'OS col render
    a.deflabel(rb0); a.jmp(3); a.nop()                                                   # ritorno del render: salto
    a.deflabel(tx); m.imm32(ISR_EXIT, 1); a.jmp(1); a.nop()
    m.pool_jump()
    # ---- mf_proc: elabora il blocco LASTJ (IN -> OUT), interrupt abilitati; torna con rts a PR = uscita ISR ----
    a.deflabel('mf_proc')
    a.stsl_pr()
    for r in range(8, 15): a.movl_st_dec(r, 15)
    m.imm32(TCNT1, 1); a.movl_ld(1, 3); st(m, 3, TENT); m.imm32(TICKS, 1); a.movl_ld(1, 3); st(m, 3, TTK)
    # nuova pressione: se un effetto sta suonando (G > 0) prima SFUMA (SWPEND), poi si cambia (HW: blip cambiando effetto)
    ld(m, PN, 0); ld(m, LASTPN, 2); a.cmpeq(2, 0); np_ = m.newlabel('np'); m.bt_far(np_)
    st(m, 0, LASTPN); sti(m, 0, KILL); sti(m, 0, NCALL); sti(m, -1, LOADP)
    sw = m.newlabel('sw'); ld(m, G, 0); a.tst_rr(0, 0); m.bt_far(sw); ld(m, FXRUN, 0); a.cmppz(0); m.bf_far(sw)
    sti(m, 1, SWPEND)
    a.deflabel(np_)
    ld(m, SWPEND, 0); a.tst_rr(0, 0); ns = m.newlabel('ns'); m.bt_far(ns); ld(m, G, 0); a.tst_rr(0, 0); m.bf_far(ns)
    a.deflabel(sw)
    sti(m, 0, SWPEND); ld(m, SEL, 0); st(m, 0, FXRUN); sti(m, 0, G); sti(m, 0, CFOK)
    a.movl_pool('P1:mf_reset', 2); a.jsr(2); a.nop()
    a.deflabel(ns)
    # coefficienti: copiati in CFR solo se calcolati per l'effetto che suona
    ld(m, CFFX, 0); ld(m, FXRUN, 2); a.cmpeq(2, 0); nc = m.newlabel('nc'); m.bf_far(nc)
    for k in range(3): ld(m, CF[k], 2); st(m, 2, CFR[k])
    sti(m, 1, CFOK)
    a.deflabel(nc)
    ld(m, KILL, 0); a.tst_rr(0, 0); nkl = m.newlabel('nkl'); a.bt_lbl(nkl); sti(m, 0, G)
    a.movl_pool('P1:mf_pdone', 2); a.jmp(2); a.nop()
    a.deflabel(nkl)
    ld(m, FXRUN, 0); a.cmppz(0); jf_abs(m, 'mf_pdone')
    # sorgente: IN[j%4]
    ld(m, LASTJ, 0); a.and_imm(3); a.shll8(0); ld(m, INP, 3); a.add_rr(0, 3); st(m, 3, TXP)
    a.mov_imm(-SH, 5); m.imm32(32767, 14)
    def unpack(m):
        a.movl_ld(3, 0); a.shad(5, 0); clip(m, 0, 14, 2); a.movl_ld_disp(3, 4, 1); a.shad(5, 1); clip(m, 1, 14, 2)   # s16 = long >> SH
        a.movl_st_disp(0, FW, 12); a.movl_st_disp(0, FDL, 12); a.movl_st_disp(1, FWR, 12); a.movl_st_disp(1, FDR, 12)
        a.add_imm(8, 3)
    frame_loop(m, unpack)
    ld(m, CFOK, 0); a.tst_rr(0, 0); jt_abs(m, 'mf_mix')                                 # coefficienti non pronti: a secco
    ld(m, FXRUN, 0)
    for e in range(NFX):
        a.mov_imm(e, 1); a.cmpeq(1, 0); jt_abs(m, 'fx%d' % e)
    a.movl_pool('P1:mf_mix', 2); a.jmp(2); a.nop()
    m.pool_jump()
    for e, fn in enumerate(FXCODE):
        a.deflabel('fx%d' % e)
        if e in NEEDBUF: ld(m, BUFP, 0); a.tst_rr(0, 0); jt_abs(m, 'mf_mix')
        fn(m)
        a.movl_pool('P1:mf_mix', 2); a.jmp(2); a.nop()
        m.pool_jump()
    # ---- mix con rampa (G -> GT a passi di GSTEP per blocco), bit bassi originali -> OUT[j%4] ----
    a.deflabel('mf_mix')
    ld(m, G, 8); a.mov_imm(0, 9); ld(m, ACT, 0); a.tst_rr(0, 0); ga = m.newlabel('ga'); a.bt_lbl(ga)
    ld(m, SWPEND, 0); a.tst_rr(0, 0); a.bf_lbl(ga); m.imm32(4096, 9); a.deflabel(ga)
    a.sub(8, 9); m.imm32(GSTEP, 0); c1 = m.newlabel('c1'); a.cmpgt(0, 9); a.bf_lbl(c1); a.mov_rm_rn(0, 9); a.deflabel(c1)
    a.neg(0, 0); c2 = m.newlabel('c2'); a.cmpgt(9, 0); a.bf_lbl(c2); a.mov_rm_rn(0, 9); a.deflabel(c2)   # r9 = passo
    a.mov_rm_rn(8, 0); a.add_rr(9, 0); st(m, 0, G)
    ld(m, TXP, 3); ld(m, LASTJ, 0); a.and_imm(3); a.shll8(0); ld(m, OUTP, 11); a.add_rr(0, 11)   # r3 = IN, r11 = OUT
    m.imm32(32767, 14); a.mov_imm(0, 10)                                                 # r10 = i
    a.mov_imm(SH, 5); m.imm32((1 << SH) - 1, 13)                                          # repack: (y << SH) | bassi originali
    def mix(m):
        a.mov_rm_rn(9, 2); a.mul_l(10, 2); a.sts_macl(2); sar(m, 2, BSH, 0); a.add_rr(8, 2)   # g = G + passo*i >> BSH
        for fw, fd, lo in ((FW, FDL, 0), (FWR, FDR, 4)):
            a.movl_ld_disp(12, fw, 0); a.movl_ld_disp(12, fd, 7); a.sub(7, 0); a.mul_l(2, 0); a.sts_macl(0)
            sar(m, 0, 12, 1); a.add_rr(7, 0); clip(m, 0)
            a.movl_ld_disp(3, lo, 1); a.and_rr(13, 1); a.shad(5, 0); a.or_rr(1, 0); a.movl_st_disp(0, lo, 11)   # bit bassi originali
        a.add_imm(8, 3); a.add_imm(8, 11); a.add_imm(1, 10)
    # effetto pieno (G = 4096, passo 0): uscita = W (identico alla formula, senza moltiplicazioni)
    m.imm32(4096, 0); a.cmpeq(0, 8); slow = m.newlabel('slow'); a.bf_lbl(slow); a.tst_rr(9, 9); a.bf_lbl(slow)
    def fastmix(m):
        for fw, lo in ((FW, 0), (FWR, 4)):
            a.movl_ld_disp(12, fw, 0); clip(m, 0)
            a.movl_ld_disp(3, lo, 1); a.and_rr(13, 1); a.shad(5, 0); a.or_rr(1, 0); a.movl_st_disp(0, lo, 11)
        a.add_imm(8, 3); a.add_imm(8, 11)
    frame_loop(m, fastmix)
    a.movl_pool('P1:mf_mixd', 2); a.jmp(2); a.nop()
    m.pool_jump()
    a.deflabel(slow)
    frame_loop(m, mix)
    a.deflabel('mf_mixd')
    ld(m, LASTJ, 0); a.mov_rm_rn(0, 3); a.and_imm(3); a.shll2(0); m.a.movl_pool(_p1(m, OUTID[0]), 1); a.add_rr(0, 1); a.movl_st(3, 1)   # OUTID
    # carico: durata in DSUM; ogni NWIN blocchi LOADP = DSUM*100 / trascorso; > LOADMAX -> KILL
    m.imm32(TCOR1, 1); a.movl_ld(1, 8); a.add_imm(1, 8)
    m.imm32(TCNT1, 1); a.movl_ld(1, 9); m.imm32(TICKS, 1); a.movl_ld(1, 10)
    ld(m, TTK, 0); a.mov_rm_rn(10, 2); a.sub(0, 2); a.mul_l(8, 2); a.sts_macl(2)           # (tick ora - tick inizio) * periodo
    ld(m, TENT, 0); a.sub(9, 0); a.add_rr(2, 0)                                          # + (TCNT inizio - TCNT ora)
    ld(m, DSUM, 2); a.add_rr(0, 2); st(m, 2, DSUM)
    ld(m, NCALL, 0); a.tst_rr(0, 0); w0 = m.newlabel('w0'); a.bf_lbl(w0)
    st(m, 10, TWT); ld(m, TENT, 2); st(m, 2, TWC)
    sti(m, 0, DSUM); a.deflabel(w0)
    a.add_imm(1, 0); st(m, 0, NCALL); m.imm32(NWIN, 1); a.cmpge(1, 0); dn_ = m.newlabel('dn'); m.bf_far(dn_)
    ld(m, TWT, 0); a.sub(0, 10); a.mul_l(8, 10); a.sts_macl(5)
    ld(m, TWC, 0); a.sub(9, 0); a.add_rr(0, 5)
    ld(m, DSUM, 4); a.mov_imm(100, 0); a.mul_l(0, 4); a.sts_macl(4)
    a.bsr_lbl('mf_udiv'); a.nop(); st(m, 0, LOADP)
    a.mov_rm_rn(0, 3); sti(m, 0, NCALL); a.mov_imm(LOADMAX, 1); a.cmpgt(1, 3); m.bf_far(dn_); sti(m, 1, KILL)   # (sti usa r0)
    a.deflabel(dn_)
    a.deflabel('mf_pdone')
    sti(m, 0, BUSY2)
    for r in range(14, 7, -1): a.movl_ld_inc(15, r)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- reset dello stato dell'effetto FXRUN ----
    a.deflabel('mf_reset')
    m.a.movl_pool(_p1(m, S[0]), 1); a.mov_imm(0, 0); a.mov_imm(NST, 2)            # via P1 come l'ISR
    rz = m.newlabel('rz'); a.deflabel(rz); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(rz)
    ld(m, FXRUN, 0)
    for eg in (E_TG,):
        a.mov_imm(eg, 1); a.cmpeq(1, 0); r1_ = m.newlabel('r1'); a.bf_lbl(r1_); m.imm32(4096, 2); stS(m, 2, 3); a.rts(); a.nop(); a.deflabel(r1_)
    a.mov_imm(E_DOWN, 1); a.cmpeq(1, 0); r2_ = m.newlabel('r2'); a.bf_lbl(r2_); m.imm32(1 << 24, 2); stS(m, 2, 4); a.rts(); a.nop(); a.deflabel(r2_)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- divisione senza segno (copia nel codice in cache): r0 = r4 / r5, r1 = resto ----
    a.deflabel('mf_udiv'); a.mov_imm(0, 0); a.mov_imm(0, 1); a.tst_rr(5, 5); dz = m.newlabel('dz'); a.bt_lbl(dz); a.mov_imm(32, 2)
    dl = m.newlabel('dl'); a.deflabel(dl); a.shll(4); a.rotcl(1); a.cmphs(5, 1); ds = m.newlabel('ds'); a.bf_lbl(ds); a.sub(5, 1); a.sett(); a.deflabel(ds); a.rotcl(0); a.dt(2); a.bf_lbl(dl)
    a.deflabel(dz); a.rts(); a.nop()
    m.pool_jump()
    # ---- tabelle ----
    a.deflabel('mf_tabs')
    blob = bytearray(TABLEN)
    for k, (fmt, vals) in TABLES.items():
        struct.pack_into('<%d%s' % (len(vals), fmt), blob, TABOFF[k], *vals)
    for i in range(0, len(blob), 2): a._emit(struct.unpack('<H', blob[i:i+2])[0])
    a.deflabel('mf_desc')
    d = bytearray()
    for e in range(NFX):
        for desc in FX[e][2]:
            kind, n, fl, lo, hi = desc[:5]; off = TABOFF[desc[5]] if len(desc) > 5 else 0
            d += struct.pack('<BBHII', kind, n, off, lo & M32, hi & M32)
    for i in range(0, len(d), 2): a._emit(struct.unpack('<H', d[i:i+2])[0])
    a.deflabel('mf_names')
    t = b''.join(FX[e][0].ljust(8).encode() + FX[e][1][0].encode() + FX[e][1][1].encode() + FX[e][1][2].encode() for e in range(NFX))
    assert len(t) == 23 * NFX
    t += bytes(len(t) & 1)
    for i in range(0, len(t), 2): a._emit(struct.unpack('<H', t[i:i+2])[0])
    a.deflabel('mf_end')

# ================= effetti (ISR) =================
def fx_svf(m, drive):
    """E0 FILT+DRV (drive) / E1 SUPERFLT (tipo LP/BP/HP): SVF Q12, f con rampa per blocco"""
    a = m.a
    ldS(m, 0, 5); ld(m, CFR[0], 1); a.sub(5, 1); a.shar(1); a.shar(1); a.add_rr(1, 5); stS(m, 5, 0)
    ld(m, CFR[1], 6)
    if drive: ld(m, CFR[2], 2)
    else:
        ld(m, CFR[2], 0); a.mov_imm(0, 2); t1 = m.newlabel('t1'); a.mov_imm(33, 1); a.cmpge(1, 0); a.bf_lbl(t1); a.mov_imm(1, 2)
        a.mov_imm(66, 1); a.cmpge(1, 0); a.bf_lbl(t1); a.mov_imm(2, 2); a.deflabel(t1)
    ldS(m, 1, 8); ldS(m, 2, 9); ldS(m, 3, 10); ldS(m, 4, 11)
    ld(m, ARRP, 12); ld(m, ARRP, 3); a.add_imm(4, 3); a.mov_imm(BLK, 4); a.mov_imm(-12, 13); m.imm32(32767, 14)
    m.pool_jump(); l = m.newlabel('sv'); a.deflabel(l)
    for lo_r, bd_r, pr in ((8, 9, 12), (10, 11, 3)):
        a.movl_ld(pr, 0)
        for _ in range(2):                                                               # 2x oversampling (stesso ingresso)
            a.mul_l(5, bd_r); a.sts_macl(1); a.shad(13, 1); a.add_rr(1, lo_r)
            a.mul_l(6, bd_r); a.sts_macl(1); a.shad(13, 1)
            a.mov_rm_rn(0, 7); a.sub(lo_r, 7); a.sub(1, 7)
            a.mul_l(5, 7); a.sts_macl(1); a.shad(13, 1); a.add_rr(1, bd_r)
        if drive:
            a.mov_rm_rn(lo_r, 0); a.mul_l(2, 0); a.sts_macl(0); sar(m, 0, 8, 1); clip(m, 0)
            a.mov_rm_rn(0, 1); a.mul_l(0, 1); a.sts_macl(1); a.shad(13, 1); a.shar(1); a.shar(1); a.shar(1)   # x2
            a.mul_l(0, 1); a.sts_macl(1); a.shad(13, 1); a.shar(1); a.shar(1); a.shar(1)                      # x3
            a.mov_rm_rn(0, 7); a.add_rr(0, 7); a.add_rr(0, 7); a.sub(1, 7)
            m.imm32(21845, 1); a.mul_l(1, 7); a.sts_macl(7); sar(m, 7, 16, 1); a.mov_rm_rn(7, 0)
        else:
            sel = m.newlabel('sel'); a.mov_rm_rn(lo_r, 0); a.tst_rr(2, 2); a.bt_lbl(sel); a.mov_rm_rn(bd_r, 0)
            a.mov_imm(2, 1); a.cmpeq(1, 2); a.bf_lbl(sel); a.mov_rm_rn(7, 0); a.deflabel(sel)
            softclip(m, 0, 1)
        a.movl_st(0, pr)
    a.add_imm(32, 12); a.add_imm(32, 3); a.dt(4); m.bf_far(l)
    stS(m, 8, 1); stS(m, 9, 2); stS(m, 10, 3); stS(m, 11, 4)

def fx_iso(m):
    a = m.a
    ld(m, CFR[0], 5); ld(m, CFR[1], 6); ld(m, CFR[2], 2)
    ldS(m, 1, 8); ldS(m, 2, 9); ldS(m, 3, 10); ldS(m, 4, 11)
    ld(m, ARRP, 12); ld(m, ARRP, 3); a.add_imm(4, 3); a.mov_imm(BLK, 4); a.mov_imm(-12, 13); m.imm32(32767, 14)
    m.pool_jump(); l = m.newlabel('is'); a.deflabel(l)
    for l1, l2, pr in ((8, 9, 12), (10, 11, 3)):
        a.movl_ld(pr, 0)
        a.mov_rm_rn(0, 1); a.sub(l1, 1); m.imm32(143, 7); a.mul_l(7, 1); a.sts_macl(1); a.shad(13, 1); a.add_rr(1, l1)
        a.mov_rm_rn(0, 1); a.sub(l2, 1); m.imm32(1427, 7); a.mul_l(7, 1); a.sts_macl(1); a.shad(13, 1); a.add_rr(1, l2)
        a.sub(l2, 0); a.mov_rm_rn(l2, 7); a.sub(l1, 7)                                   # r0 = alti, r7 = medi
        a.mov_rm_rn(l1, 1); a.mul_l(5, 1); a.sts_macl(1)
        a.mul_l(6, 7); a.sts_macl(7); a.add_rr(7, 1)
        a.mul_l(2, 0); a.sts_macl(0); a.add_rr(1, 0); a.shad(13, 0); clip(m, 0)
        a.movl_st(0, pr)
    a.add_imm(32, 12); a.add_imm(32, 3); a.dt(4); m.bf_far(l)
    stS(m, 8, 1); stS(m, 9, 2); stS(m, 10, 3); stS(m, 11, 4)

def fx_lofi(m):
    a = m.a
    ld(m, CFR[0], 5); ld(m, CFR[1], 0); a.mov_imm(-1, 6); a.shad(0, 6); ld(m, CFR[2], 7)
    ldS(m, 1, 8); ldS(m, 2, 9); ldS(m, 3, 10); ldS(m, 4, 11); ldS(m, 5, 2)
    ld(m, ARRP, 12); a.mov_imm(BLK, 4); a.mov_imm(-12, 13)
    m.pool_jump(); l = m.newlabel('lf'); a.deflabel(l)
    h = m.newlabel('h'); a.cmppl(8); a.bt_lbl(h)
    a.movl_ld_disp(12, FW, 9); a.movl_ld_disp(12, FWR, 10); a.mov_rm_rn(5, 8); a.deflabel(h)
    a.add_imm(-1, 8)
    for hr, tr, fo in ((9, 11, FW), (10, 2, FWR)):
        a.mov_rm_rn(hr, 0); a.and_rr(6, 0); a.sub(tr, 0); a.mul_l(7, 0); a.sts_macl(0); a.shad(13, 0); a.add_rr(0, tr)
        a.movl_st_disp(tr, fo, 12)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 8, 1); stS(m, 9, 2); stS(m, 10, 3); stS(m, 11, 4); stS(m, 2, 5)

def fx_vinyl(m):
    a = m.a
    # A: tono (HP 1 polo ~150 Hz + LP CF2) per canale
    for c, (fo, hs, ts) in enumerate(((FW, 1, 2), (FWR, 3, 4))):
        ldS(m, hs, 8); ldS(m, ts, 9); ld(m, CFR[2], 5); ld(m, ARRP, 12); a.mov_imm(BLK, 4); a.mov_imm(-12, 13)
        m.pool_jump(); l = m.newlabel('vt'); a.deflabel(l)
        a.movl_ld_disp(12, fo, 0); a.mov_rm_rn(0, 1); a.sub(8, 1); a.mov_imm(87, 7); a.mul_l(7, 1); a.sts_macl(1); a.shad(13, 1); a.add_rr(1, 8)
        a.sub(8, 0); a.sub(9, 0); a.mul_l(5, 0); a.sts_macl(0); a.shad(13, 0); a.add_rr(0, 9)
        a.movl_st_disp(9, fo, 12)
        a.add_imm(32, 12); a.dt(4); m.bf_far(l)
        stS(m, 8, hs); stS(m, 9, ts)
    # B: ritardo del wow per frame -> X0 (Q8)
    ldS(m, 10, 6); ld(m, CFR[1], 8); ld(m, SINP, 9); ld(m, ARRP, 12); a.mov_imm(BLK, 4); m.imm32(WOWINC, 10)
    m.pool_jump(); l = m.newlabel('vb'); a.deflabel(l)
    a.mov_rm_rn(6, 0); a.shlr16(0); a.shlr8(0); a.shll(0); a.add_rr(9, 0); a.movw_ld(0, 0)
    a.mul_l(8, 0); a.sts_macl(0); sar(m, 0, 14, 1); m.imm32(32 * 256, 1); a.add_rr(1, 0); a.movl_st_disp(0, FX0, 12)
    a.add_rr(10, 6); a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 6, 10)
    # C: linea di ritardo mono s16 (256) per canale, scrittura poi lettura interpolata
    for c, fo in enumerate((FW, FWR)):
        ld(m, SBUFP, 10); m.imm32(WOW_OFF + 0x200 * c, 0); a.add_rr(0, 10)
        ldS(m, 9, 5); ld(m, ARRP, 12); a.mov_imm(BLK, 4); m.imm32(32767, 14)
        m.pool_jump(); l = m.newlabel('vc'); a.deflabel(l)
        a.movl_ld_disp(12, fo, 0); clip(m, 0)
        a.mov_rm_rn(5, 1); a.extu_b(1, 1); a.shll(1); a.add_rr(10, 1); a.movw_st(0, 1)                 # buf[w] = x
        a.movl_ld_disp(12, FX0, 2); a.mov_rm_rn(2, 3); a.shlr8(3); a.mov_rm_rn(5, 1); a.sub(3, 1); a.extu_b(1, 1)   # ri
        a.mov_rm_rn(1, 7); a.shll(7); a.add_rr(10, 7); a.movw_ld(7, 0)                                  # a
        a.add_imm(-1, 1); a.extu_b(1, 1); a.shll(1); a.add_rr(10, 1); a.movw_ld(1, 1)                   # b
        a.sub(0, 1); a.extu_b(2, 2); a.mul_l(2, 1); a.sts_macl(1); sar(m, 1, 8, 7); a.add_rr(1, 0)
        a.movl_st_disp(0, fo, 12)
        a.add_imm(1, 5); a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    # D: fruscio + crackle (come VINYL di Sample FX) per canale, indice IDX continuo
    for c, (fo, ns, cs) in enumerate(((FW, 5, 6), (FWR, 7, 8))):
        ld(m, CFR[0], 9); a.mov_rm_rn(9, 8); a.shar(8)                                     # densita' crackle = AMT/2
        ldS(m, 9, 13); m.imm32((VSEED0 + c * VSEEDC) & M32, 0); a.add_rr(0, 13)
        ldS(m, ns, 10); ldS(m, cs, 11); ld(m, ARRP, 12); a.mov_imm(BLK, 4); m.imm32(32767, 14)
        m.pool_jump(); l = m.newlabel('vd'); a.deflabel(l)
        a.mov_rm_rn(13, 1); m.imm32(VK1, 2); a.mul_l(2, 1); a.sts_macl(1); a.mov_rm_rn(1, 2); a.shlr16(2); a.xor_rr(2, 1)
        m.imm32(VK2, 2); a.mul_l(2, 1); a.sts_macl(1); a.mov_rm_rn(1, 2); a.shlr16(2); a.xor_rr(2, 1)
        a.add_imm(1, 13)
        a.mov_rm_rn(1, 2); sar(m, 2, 20, 3)
        a.mov_rm_rn(2, 3); a.add_rr(10, 3); a.shar(3); a.mov_rm_rn(2, 10)
        a.mul_l(9, 3); a.sts_macl(3); sar(m, 3, 7, 5)                                    # fruscio x2
        a.mov_imm(0, 5); a.mov_rm_rn(1, 6); a.extu_w(6, 6); nc = m.newlabel('nc'); a.cmphs(8, 6); a.bt_lbl(nc)
        a.mov_rm_rn(1, 5); a.shlr16(5); m.imm32(0x1FFF, 6); a.and_rr(6, 5); m.imm32(1000, 6); a.add_rr(6, 5)
        a.mul_l(9, 5); a.sts_macl(5); sar(m, 5, 6, 6); a.cmppz(1); a.bt_lbl(nc); a.neg(5, 5)   # click (segnale ~7000)
        a.deflabel(nc)
        a.mov_rm_rn(5, 6); a.add_rr(11, 6); a.mov_rm_rn(5, 11); a.shar(11)
        a.movl_ld_disp(12, fo, 0); a.add_rr(3, 0); a.add_rr(6, 0); clip(m, 0); a.movl_st_disp(0, fo, 12)
        a.add_imm(32, 12); a.dt(4); m.bf_far(l)
        stS(m, 10, ns); stS(m, 11, cs)
    ldS(m, 9, 0); a.add_imm(BLK, 0); stS(m, 0, 9)

def fx_comp(m):
    a = m.a
    # passata 1: inviluppo (Q8) su max(|L|,|R|)
    ldS(m, 1, 8); ld(m, CFR[1], 9); ld(m, ARRP, 12); a.mov_imm(BLK, 4); a.mov_imm(-12, 13)
    m.pool_jump(); l = m.newlabel('ce'); a.deflabel(l)
    a.movl_ld_disp(12, FW, 0); p1 = m.newlabel('p1'); a.cmppz(0); a.bt_lbl(p1); a.neg(0, 0); a.deflabel(p1)
    a.movl_ld_disp(12, FWR, 1); p2 = m.newlabel('p2'); a.cmppz(1); a.bt_lbl(p2); a.neg(1, 1); a.deflabel(p2)
    mx = m.newlabel('mx'); a.cmpgt(0, 1); a.bf_lbl(mx); a.mov_rm_rn(1, 0); a.deflabel(mx)
    a.shll8(0); a.sub(8, 0)
    rl = m.newlabel('rl'); dn = m.newlabel('dn'); a.cmppl(0); a.bf_lbl(rl)
    a.mov_imm(92, 1); a.mul_l(1, 0); a.sts_macl(0); a.shad(13, 0); a.bra_lbl(dn); a.nop()
    a.deflabel(rl); a.mul_l(9, 0); a.sts_macl(0); sar(m, 0, 16, 1)
    a.deflabel(dn); a.add_rr(0, 8)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 8, 1)
    # guadagno di fine blocco
    a.mov_rm_rn(8, 10); sar(m, 10, 8, 0); ld(m, CFR[0], 11); m.imm32(4096, 0)
    gn = m.newlabel('gn'); a.cmpgt(11, 10); a.bf_lbl(gn)
    a.mov_rm_rn(10, 4); a.sub(11, 4); a.shar(4); a.shar(4); a.shar(4); a.add_rr(11, 4); a.shll8(4); a.shll2(4); a.shll2(4)   # 8:1
    a.mov_rm_rn(10, 5); a.bsr_lbl('mf_udiv'); a.nop()
    a.deflabel(gn)
    ld(m, CFR[2], 1); a.mul_l(1, 0); a.sts_macl(0); a.shlr8(0); a.shlr2(0); a.shlr2(0)    # gT (positivo)
    ldS(m, 2, 8); a.mov_rm_rn(0, 9); a.sub(8, 9); stS(m, 0, 2)                           # r8 = gP, r9 = gT-gP
    # passata 2: guadagno interpolato
    ld(m, ARRP, 12); a.mov_imm(BLK, 4); a.mov_imm(0, 10); m.imm32(32767, 14)
    m.pool_jump(); l = m.newlabel('cg'); a.deflabel(l)
    a.mov_rm_rn(9, 2); a.mul_l(10, 2); a.sts_macl(2); sar(m, 2, BSH, 0); a.add_rr(8, 2)
    for fo in (FW, FWR):
        a.movl_ld_disp(12, fo, 0); a.mul_l(2, 0); a.sts_macl(0); a.shad(13, 0); clip(m, 0); a.movl_st_disp(0, fo, 12)
    a.add_imm(1, 10); a.add_imm(32, 12); a.dt(4); m.bf_far(l)

def fx_delay(m):
    a = m.a
    # passata 1: letture ritardate -> X0/X1 (0 se non ancora scritte dopo l'attivazione)
    ldS(m, 1, 5); ld(m, CFR[1], 6); ld(m, BUFP, 10); ldS(m, 4, 11); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    m.pool_jump(); l = m.newlabel('d1'); a.deflabel(l)
    a.mov_rm_rn(5, 1); a.sub(6, 1); a.extu_w(1, 1); a.shll2(1); a.add_rr(10, 1); a.movl_ld(1, 0)
    ok = m.newlabel('ok'); a.cmphs(6, 11); a.bt_lbl(ok); a.mov_imm(0, 0); a.deflabel(ok)
    a.mov_rm_rn(0, 1); a.exts_w(0, 0); a.shlr16(1); a.exts_w(1, 1); a.movl_st_disp(0, FX0, 12); a.movl_st_disp(1, FX1, 12)
    a.add_imm(1, 5); a.add_imm(1, 11); a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    m.imm32(0x10000, 0); sat = m.newlabel('sat'); a.cmpgt(0, 11); a.bf_lbl(sat); a.mov_rm_rn(0, 11); a.deflabel(sat); stS(m, 11, 4)
    # passata 2: smorzamento, retroazione nel buffer, uscita
    ldS(m, 1, 5); ldS(m, 2, 8); ldS(m, 3, 9); ld(m, CFR[2], 2); ld(m, CFR[0], 6); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    a.mov_imm(-12, 13); m.imm32(32767, 14)
    m.pool_jump(); l = m.newlabel('d2'); a.deflabel(l)
    for fo, xo, dr, out in ((FW, FX0, 8, 7), (FWR, FX1, 9, 11)):
        a.movl_ld_disp(12, xo, 0); a.sub(8 if dr == 8 else 9, 0); a.shar(0); a.add_rr(0, dr)
        a.mov_rm_rn(dr, 0); a.mul_l(2, 0); a.sts_macl(0); a.shad(13, 0); a.movl_ld_disp(12, fo, 1); a.add_rr(1, 0); clip(m, 0, 14, 3)
        a.mov_rm_rn(0, out)
    a.extu_w(7, 7); a.shll16(11); a.or_rr(11, 7); a.mov_rm_rn(5, 1); a.extu_w(1, 1); a.shll2(1); a.add_rr(10, 1); a.movl_st(7, 1)
    m.imm32(4096, 7); a.mov_rm_rn(6, 1); a.shar(1); a.sub(1, 7)                        # r7 = guadagno originale 4096 - mix/2
    for fo, xo in ((FW, FX0), (FWR, FX1)):
        a.movl_ld_disp(12, fo, 1); a.mul_l(7, 1); a.sts_macl(1); a.shad(13, 1)
        a.movl_ld_disp(12, xo, 0); a.mul_l(6, 0); a.sts_macl(0); a.shad(13, 0); a.add_rr(1, 0)
        softclip(m, 0, 3); a.movl_st_disp(0, fo, 12)
    a.add_imm(1, 5); a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    a.extu_w(5, 5); stS(m, 5, 1); stS(m, 8, 2); stS(m, 9, 3)

def fx_reverb(m):
    a = m.a
    # ingresso mono (L+R) >> 6 -> X0; accumulatori X1 (L), X2 (R)
    def pre(m):
        a.movl_ld_disp(12, FW, 0); a.movl_ld_disp(12, FWR, 1); a.add_rr(1, 0); sar(m, 0, 5, 1); a.movl_st_disp(0, FX0, 12)
        a.mov_imm(0, 0); a.movl_st_disp(0, FX1, 12); a.movl_st_disp(0, FX2, 12)
    frame_loop(m, pre)
    ld(m, CFR[1], 2); ld(m, CFR[2], 3)
    for c in range(len(COMBS)):
        acc = FX1
        ld(m, BUFP, 9); m.imm32(REV_ADDR[c], 0); a.add_rr(0, 9)
        ldS(m, c, 8); ldS(m, 8 + c, 5); m.imm32(COMBS[c], 6)
        ld(m, ARRP, 12); a.mov_imm(BLK, 4); a.mov_imm(-12, 13); m.imm32(32767, 14)
        m.pool_jump(); l = m.newlabel('cb'); a.deflabel(l)
        a.mov_rm_rn(5, 1); a.shll(1); a.add_rr(9, 1); a.movw_ld(1, 7)                        # y
        a.mov_rm_rn(7, 0); a.sub(8, 0); a.mul_l(3, 0); a.sts_macl(0); a.shad(13, 0); a.add_rr(0, 8)   # filt
        a.mov_rm_rn(8, 0); a.mul_l(2, 0); a.sts_macl(0); a.shad(13, 0); a.movl_ld_disp(12, FX0, 10); a.add_rr(10, 0)
        clip(m, 0, 14, 10); a.movw_st(0, 1)
        a.movl_ld_disp(12, acc, 0); a.add_rr(7, 0); a.movl_st_disp(0, acc, 12)
        a.add_imm(1, 5); wr = m.newlabel('wr'); a.cmpeq(6, 5); a.bf_lbl(wr); a.mov_imm(0, 5); a.deflabel(wr)
        a.add_imm(32, 12); a.dt(4); m.bf_far(l)
        stS(m, 8, c); stS(m, 5, 8 + c)
    for j in range(len(APS)):
        src, dst = AP_IO[j]
        ld(m, BUFP, 9); m.imm32(REV_ADDR[len(COMBS) + j], 0); a.add_rr(0, 9)
        ldS(m, 16 + j, 5); m.imm32(APS[j], 6); ld(m, ARRP, 12); a.mov_imm(BLK, 4); m.imm32(32767, 14)
        m.pool_jump(); l = m.newlabel('ap'); a.deflabel(l)
        a.mov_rm_rn(5, 1); a.shll(1); a.add_rr(9, 1); a.movw_ld(1, 7)                        # bo
        if j == 2:
            a.movl_ld_disp(12, src, 0); a.mov_rm_rn(0, 8); clip(m, 8, 14, 10); a.movw_st(8, 1); a.movl_st_disp(7, dst, 12)
        else:
            a.movl_ld_disp(12, src, 0); a.mov_rm_rn(7, 8); a.shar(8); a.add_rr(0, 8); clip(m, 8, 14, 10); a.movw_st(8, 1)
            a.mov_rm_rn(7, 8); a.sub(0, 8); a.movl_st_disp(8, dst, 12)
        a.add_imm(1, 5); wr = m.newlabel('wr'); a.cmpeq(6, 5); a.bf_lbl(wr); a.mov_imm(0, 5); a.deflabel(wr)
        a.add_imm(32, 12); a.dt(4); m.bf_far(l)
        stS(m, 5, 16 + j)
    # uscita: soft(x*(4096 - mix/2) + soft(acc*8) * mix)  (HW: x + acc*6*mix con taglio netto gracchiava; x3 troppo debole)
    ld(m, CFR[0], 6); m.imm32(4096, 7); a.mov_rm_rn(6, 1); a.shar(1); a.sub(1, 7)
    def out(m):
        for fo, acc in ((FW, FX1), (FWR, FX2)):
            a.movl_ld_disp(12, acc, 0); a.shll2(0)                                             # *4 (ingresso >> 5)
            softclip(m, 0, 1); a.mul_l(6, 0); a.sts_macl(0); sar(m, 0, 12, 1)
            a.movl_ld_disp(12, fo, 1); a.mul_l(7, 1); a.sts_macl(1); sar(m, 1, 12, 2); a.add_rr(1, 0); softclip(m, 0, 1); a.movl_st_disp(0, fo, 12)
    frame_loop(m, out)

def fx_looper(m):
    a = m.a
    ldS(m, 1, 5); ldS(m, 2, 6); ld(m, CFR[0], 8); ld(m, CFR[1], 9); ld(m, BUFP, 10); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    m.pool_jump(); l = m.newlabel('lp'); a.deflabel(l)
    pl = m.newlabel('pl'); nx = m.newlabel('nx')
    a.cmpge(8, 5); a.bt_lbl(pl)                                                           # REC >= LEN -> suona
    a.movl_ld_disp(12, FW, 0); a.extu_w(0, 0); a.movl_ld_disp(12, FWR, 1); a.shll16(1); a.or_rr(1, 0)
    a.mov_rm_rn(5, 1); a.shll2(1); a.add_rr(10, 1); a.movl_st(0, 1); a.add_imm(1, 5); a.bra_lbl(nx); a.nop()
    a.deflabel(pl)
    z = m.newlabel('z'); a.cmpge(8, 6); a.bf_lbl(z); a.mov_imm(0, 6); a.deflabel(z)
    a.mov_rm_rn(6, 1); fw = m.newlabel('fw'); a.mov_imm(50, 0); a.cmpge(0, 9); a.bf_lbl(fw)
    a.mov_rm_rn(8, 1); a.add_imm(-1, 1); a.sub(6, 1); a.deflabel(fw)
    a.shll2(1); a.add_rr(10, 1); a.movl_ld(1, 0); a.mov_rm_rn(0, 1); a.exts_w(0, 0); a.shlr16(1); a.exts_w(1, 1)
    # micro-dissolvenza ai bordi del loop (HW: click): e = min(PP, LEN-1-PP) < 16 -> x * (e+1) / 16
    a.mov_rm_rn(8, 2); a.add_imm(-1, 2); a.sub(6, 2); e1 = m.newlabel('e1'); a.cmpgt(6, 2); a.bf_lbl(e1); a.mov_rm_rn(6, 2); a.deflabel(e1)
    a.mov_imm(16, 3); nf = m.newlabel('nf'); a.cmpge(3, 2); a.bt_lbl(nf); a.add_imm(1, 2)
    a.mul_l(2, 0); a.sts_macl(0); sar(m, 0, 4, 3); a.mul_l(2, 1); a.sts_macl(1); sar(m, 1, 4, 3)
    a.deflabel(nf)
    a.movl_st_disp(0, FW, 12); a.movl_st_disp(1, FWR, 12); a.add_imm(1, 6)
    a.deflabel(nx)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 5, 1); stS(m, 6, 2)

def fx_downer(m):
    a = m.a
    ldS(m, 1, 5); ldS(m, 2, 6); ldS(m, 3, 8); ldS(m, 4, 9); ld(m, CFR[0], 11); ld(m, BUFP, 10); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    a.mov_imm(-12, 13)
    m.pool_jump(); l = m.newlabel('dn'); a.deflabel(l)
    a.movl_ld_disp(12, FW, 0); a.extu_w(0, 0); a.movl_ld_disp(12, FWR, 1); a.shll16(1); a.or_rr(1, 0)
    a.mov_rm_rn(5, 1); a.shll2(1); a.add_rr(10, 1); a.movl_st(0, 1)
    m.imm32(DLY_FR - 1, 0); wm = m.newlabel('wm'); a.cmphs(0, 5); a.bt_lbl(wm); a.add_imm(1, 5); a.deflabel(wm)
    sil = m.newlabel('sil'); nx = m.newlabel('nx')
    a.tst_rr(9, 9); a.bt_lbl(sil)
    a.mov_rm_rn(6, 1); a.shll2(1); a.add_rr(10, 1); a.movl_ld(1, 2); a.movl_ld_disp(1, 4, 3)   # a, b
    a.mov_rm_rn(8, 7); a.shlr8(7); a.shlr2(7); a.shlr2(7)                                    # frac Q12
    for fo, hi in ((FW, False), (FWR, True)):
        a.mov_rm_rn(2, 0); a.mov_rm_rn(3, 1)
        if hi: a.shlr16(0); a.shlr16(1)
        a.exts_w(0, 0); a.exts_w(1, 1); a.sub(0, 1); a.mul_l(7, 1); a.sts_macl(1); a.shad(13, 1); a.add_rr(1, 0)
        a.movl_st_disp(0, fo, 12)
    a.add_rr(9, 8); m.imm32(1 << 24, 0); ad = m.newlabel('ad'); a.cmphs(0, 8); a.bf_lbl(ad); a.sub(0, 8); a.add_imm(1, 6); a.deflabel(ad)
    a.sub(11, 9); pz = m.newlabel('pz'); a.cmppz(9); a.bt_lbl(pz); a.mov_imm(0, 9); a.deflabel(pz)
    a.bra_lbl(nx); a.nop()
    a.deflabel(sil); a.mov_imm(0, 0); a.movl_st_disp(0, FW, 12); a.movl_st_disp(0, FWR, 12)
    a.deflabel(nx)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 5, 1); stS(m, 6, 2); stS(m, 8, 3); stS(m, 9, 4)

def fx_chorus(m):
    a = m.a
    ldS(m, 1, 5); ldS(m, 2, 6); ld(m, CFR[1], 8); ld(m, CFR[0], 9); ld(m, SBUFP, 10); ld(m, CFR[2], 11); ld(m, SINP, 13)
    ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    m.pool_jump(); l = m.newlabel('ch'); a.deflabel(l)
    a.movl_ld_disp(12, FW, 0); a.extu_w(0, 0); a.movl_ld_disp(12, FWR, 1); a.shll16(1); a.or_rr(1, 0)
    a.mov_rm_rn(5, 1); a.shll2(1); a.add_rr(10, 1); a.movl_st(0, 1)
    for fo, ph_off, hofs in ((FW, 0, 0), (FWR, 0x40000000, 2)):
        a.mov_rm_rn(6, 0)
        if ph_off: m.imm32(ph_off, 1); a.add_rr(1, 0)
        a.shlr16(0); a.shlr8(0); a.shll(0); a.add_rr(13, 0); a.movw_ld(0, 0)                  # s
        a.mul_l(11, 0); a.sts_macl(0); sar(m, 0, 14, 1); m.imm32(CH_BASE8, 1); a.add_rr(1, 0)  # D8
        a.mov_rm_rn(0, 2); a.shlr8(2); a.mov_rm_rn(5, 3); a.sub(2, 3); m.imm32(CH_FR - 1, 1); a.and_rr(1, 3)   # ri
        a.extu_b(0, 7)                                                                        # frac
        a.mov_rm_rn(3, 2); a.shll2(2); a.add_rr(10, 2)
        if hofs: a.add_imm(hofs, 2)
        a.movw_ld(2, 0)                                                                       # a
        a.add_imm(-1, 3); a.and_rr(1, 3); a.shll2(3); a.add_rr(10, 3)
        if hofs: a.add_imm(hofs, 3)
        a.movw_ld(3, 3); a.sub(0, 3); a.mul_l(7, 3); a.sts_macl(3); sar(m, 3, 8, 1); a.add_rr(3, 0)   # y
        a.movl_ld_disp(12, fo, 1); a.sub(1, 0); a.mul_l(9, 0); a.sts_macl(0); sar(m, 0, 13, 2); a.add_rr(1, 0)
        a.movl_st_disp(0, fo, 12)
    a.add_imm(1, 5); m.imm32(CH_FR - 1, 1); a.and_rr(1, 5); a.add_rr(8, 6)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 5, 1); stS(m, 6, 2)

def fx_trem(m):
    a = m.a
    ldS(m, 1, 6); ld(m, CFR[1], 8); ld(m, CFR[0], 9); ld(m, CFR[2], 10); ld(m, SINP, 13); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    m.pool_jump(); l = m.newlabel('tr'); a.deflabel(l)
    a.mov_rm_rn(6, 0); a.shlr16(0); a.shlr8(0); a.shll(0); a.add_rr(13, 0); a.movw_ld(0, 0); m.imm32(16384, 1); a.add_rr(1, 0)   # u
    m.imm32(32768, 1); a.sub(0, 1)                                                        # r1 = 32768-u
    a.mov_rm_rn(1, 2); a.mul_l(9, 2); a.sts_macl(2); sar(m, 2, 15, 3); m.imm32(4096, 3); a.sub(2, 3); a.mov_rm_rn(3, 2)   # g(32768-u)
    pan = m.newlabel('pan'); gd = m.newlabel('gd'); a.mov_imm(50, 3); a.cmpge(3, 10); a.bt_lbl(pan)
    a.mov_rm_rn(2, 7); a.bra_lbl(gd); a.nop()                                             # tremolo: gL = gR
    a.deflabel(pan)
    a.mov_rm_rn(2, 7); a.mul_l(9, 0); a.sts_macl(0); sar(m, 0, 15, 3); m.imm32(4096, 2); a.sub(0, 2)   # gL = 4096 - d*u>>15
    a.deflabel(gd)
    for fo, gr in ((FW, 2), (FWR, 7)):
        a.movl_ld_disp(12, fo, 0); a.mul_l(gr, 0); a.sts_macl(0); sar(m, 0, 12, 1); a.movl_st_disp(0, fo, 12)
    a.add_rr(8, 6); a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 6, 1)


def fx_stutter(m):
    """STUTTER: registra una fetta di LEN frame (a tempo) poi la ripete; GATE accorcia ogni ripetizione (bordi sfumati)"""
    a = m.a
    ldS(m, 1, 5); ldS(m, 2, 6); ld(m, CFR[0], 8); ld(m, CFR[1], 9); ld(m, BUFP, 10); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    a.mov_rm_rn(8, 11); a.mul_l(9, 11); a.sts_macl(11); sar(m, 11, 12, 0)                  # r11 = glen = LEN*gate >> 12
    g1 = m.newlabel('g1'); a.cmppl(11); a.bt_lbl(g1); a.mov_imm(1, 11); a.deflabel(g1)
    m.pool_jump(); l = m.newlabel('st'); a.deflabel(l)
    pl = m.newlabel('pl'); nx = m.newlabel('nx')
    a.cmpge(8, 5); a.bt_lbl(pl)                                                           # REC >= LEN -> ripete
    a.movl_ld_disp(12, FW, 0); a.extu_w(0, 0); a.movl_ld_disp(12, FWR, 1); a.shll16(1); a.or_rr(1, 0)
    a.mov_rm_rn(5, 1); a.shll2(1); a.add_rr(10, 1); a.movl_st(0, 1); a.add_imm(1, 5); a.bra_lbl(nx); a.nop()
    a.deflabel(pl)
    z = m.newlabel('z'); a.cmpge(8, 6); a.bf_lbl(z); a.mov_imm(0, 6); a.deflabel(z)
    sil = m.newlabel('sil'); a.cmpge(11, 6); a.bt_lbl(sil)                                  # PP >= glen -> silenzio
    a.mov_rm_rn(6, 1); a.shll2(1); a.add_rr(10, 1); a.movl_ld(1, 0); a.mov_rm_rn(0, 1); a.exts_w(0, 0); a.shlr16(1); a.exts_w(1, 1)
    a.mov_rm_rn(11, 2); a.add_imm(-1, 2); a.sub(6, 2); e1 = m.newlabel('e1'); a.cmpgt(6, 2); a.bf_lbl(e1); a.mov_rm_rn(6, 2); a.deflabel(e1)
    a.mov_imm(16, 3); nf = m.newlabel('nf'); a.cmpge(3, 2); a.bt_lbl(nf); a.add_imm(1, 2)
    a.mul_l(2, 0); a.sts_macl(0); sar(m, 0, 4, 3); a.mul_l(2, 1); a.sts_macl(1); sar(m, 1, 4, 3)
    a.deflabel(nf)
    a.movl_st_disp(0, FW, 12); a.movl_st_disp(1, FWR, 12); a.add_imm(1, 6); a.bra_lbl(nx); a.nop()
    a.deflabel(sil); a.mov_imm(0, 0); a.movl_st_disp(0, FW, 12); a.movl_st_disp(0, FWR, 12); a.add_imm(1, 6)
    a.deflabel(nx)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 5, 1); stS(m, 6, 2)

def fx_trgate(m):
    """TRANCE GATE: 16 passi (pattern), passo = RATE a tempo; guadagno on / 4096-DEPTH, rampa 1/32 per campione"""
    a = m.a
    ld(m, CFR[0], 0); a.mov_rm_rn(0, 1); a.shll2(0); a.add_rr(1, 0); a.shlr2(0); a.shlr2(0); a.shlr2(0)   # (p*5) >> 6
    a.mov_imm(7, 1); c7 = m.newlabel('c7'); a.cmpgt(1, 0); a.bf_lbl(c7); a.mov_rm_rn(1, 0); a.deflabel(c7)
    a.shll(0); ld(m, SINP, 8); m.imm32(TABOFF['GATEP'] - TABOFF['SIN'], 1); a.add_rr(1, 8); a.add_rr(0, 8); a.movw_ld(8, 8); a.extu_w(8, 8)   # r8 = pattern
    ld(m, CFR[1], 9); ld(m, CFR[2], 10); ldS(m, 1, 5); ldS(m, 2, 6); ldS(m, 3, 11)
    ld(m, ARRP, 12); a.mov_imm(BLK, 4); m.imm32(4096, 13)
    m.pool_jump(); l = m.newlabel('tg'); a.deflabel(l)
    a.mov_rm_rn(6, 1); a.add_imm(-15, 1); a.mov_rm_rn(8, 0); a.shad(1, 0); a.and_imm(1)      # bit (15 - idx)
    a.mov_rm_rn(13, 2); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on); a.sub(9, 2); a.deflabel(on)   # r2 = gt
    a.sub(11, 2); sar(m, 2, 5, 0); a.add_rr(2, 11)                                        # g += (gt - g) >> 5
    for fo in (FW, FWR):
        a.movl_ld_disp(12, fo, 0); a.mul_l(11, 0); a.sts_macl(0); sar(m, 0, 12, 1); a.movl_st_disp(0, fo, 12)
    a.add_imm(1, 5); sp = m.newlabel('sp'); a.cmpge(10, 5); a.bf_lbl(sp); a.mov_imm(0, 5); a.add_imm(1, 6); a.mov_rm_rn(6, 0); a.and_imm(15); a.mov_rm_rn(0, 6)
    a.deflabel(sp)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 5, 1); stS(m, 6, 2); stS(m, 11, 3)

def fx_scatter(m, rev=False):
    """SCATTER (v3.7, al posto di PUMP): l'ingresso va sempre in un anello (BUF, 65536 frame); l'uscita e' fatta a fette
    di SL frame (a tempo). A ogni fetta si sceglie a caso (probabilita' AMT) cosa suona la fetta DOPO: dal vivo, ripeti
    l'ultima fetta, l'ultima al contrario, quella di 2 fette fa, l'ultima a mezza velocita'. Scelta una fetta in anticipo:
    cosi' si sfuma (16 frame) solo ai bordi che toccano una fetta non dal vivo (niente click, il vivo resta pulito).
    Stato: S1 W, S2 k, S3 frame scritti, S4 base, S5 passo (x2), S6 flag (b0 questa, b1 prima, b2 dopo non dal vivo),
    S7 seme, S8 azione della fetta dopo.
    rev=True: REVERSE (v3.7, al posto di LOOPER): stesse fette, lunghezza = slider (CF0), ogni fetta suona la
    precedente AL CONTRARIO (la prima dopo la pressione e' dal vivo). Latenza = una fetta, come ogni reverse live."""
    a = m.a
    ldS(m, 1, 5); ldS(m, 2, 6); ldS(m, 3, 14); ldS(m, 4, 9); ldS(m, 5, 11); ldS(m, 6, 13)
    ld(m, CFR[0 if rev else 1], 8); ld(m, BUFP, 10); ld(m, ARRP, 12); a.mov_imm(BLK, 4)
    m.pool_jump(); l = m.newlabel('sc'); a.deflabel(l)
    a.movl_ld_disp(12, FW, 0); a.extu_w(0, 0); a.movl_ld_disp(12, FWR, 1); a.shll16(1); a.or_rr(1, 0)
    a.mov_rm_rn(5, 1); a.shll2(1); a.add_rr(10, 1); a.movl_st(0, 1)                        # buf[W] = ingresso
    nk = m.newlabel('nk'); a.cmpge(8, 6); a.bf_lbl(nk); a.mov_imm(0, 6); a.deflabel(nk)     # k >= SL -> nuova fetta
    ns = m.newlabel('ns'); a.tst_rr(6, 6); m.bf_far(ns)
    # ---- inizio fetta ----
    a.mov_rm_rn(13, 0); a.and_imm(1); a.shll(0); a.mov_rm_rn(0, 13)                     # prima = questa
    ldS(m, 8, 7); a0 = m.newlabel('a0'); a.tst_rr(7, 7); a.bt_lbl(a0); a.mov_rm_rn(13, 0); a.or_imm(1); a.mov_rm_rn(0, 13); a.deflabel(a0)
    a.mov_rm_rn(5, 9); a.mov_imm(2, 11); ad = m.newlabel('ad')
    n1, n2, n3 = m.newlabel('n1'), m.newlabel('n2'), m.newlabel('n3')
    a.mov_imm(1, 0); a.cmpeq(0, 7); a.bf_lbl(n1); a.sub(8, 9); a.bra_lbl(ad); a.nop()                     # ripeti
    a.deflabel(n1); a.mov_imm(2, 0); a.cmpeq(0, 7); a.bf_lbl(n2); a.add_imm(-1, 9); a.mov_imm(-2, 11); a.bra_lbl(ad); a.nop()   # al contrario
    a.deflabel(n2); a.mov_imm(3, 0); a.cmpeq(0, 7); a.bf_lbl(n3); a.sub(8, 9); a.sub(8, 9); a.bra_lbl(ad); a.nop()   # 2 fette fa
    a.deflabel(n3); a.mov_imm(4, 0); a.cmpeq(0, 7); a.bf_lbl(ad); a.sub(8, 9); a.mov_imm(1, 11)          # mezza velocita'
    a.deflabel(ad)
    a.mov_imm(0, 7); nn = m.newlabel('nn')
    if rev:
        a.cmpge(8, 14); a.bf_lbl(nn); a.mov_imm(2, 7)                                     # sempre al contrario
    else:
        ldS(m, 7, 0); sd = m.newlabel('sd'); a.tst_rr(0, 0); a.bf_lbl(sd)
        ld(m, PN, 0); m.imm32(VK1, 1); a.mul_l(1, 0); a.sts_macl(0); a.or_imm(1); a.deflabel(sd)
        m.imm32(1664525, 1); a.mul_l(1, 0); a.sts_macl(0); m.imm32(1013904223, 1); a.add_rr(1, 0); stS(m, 0, 7)
        a.cmpge(8, 14); a.bf_lbl(nn)                                                      # anello non ancora pieno: dal vivo
        a.mov_rm_rn(0, 2); a.shlr16(2); a.shlr8(2); ld(m, CFR[0], 3); a.cmphs(3, 2); a.bt_lbl(nn)   # (seme >> 24) >= AMT: dal vivo
        a.shlr16(0); a.and_imm(3); a.add_imm(1, 0); a.mov_rm_rn(0, 7)
    a.deflabel(nn); stS(m, 7, 8)
    nf = m.newlabel('nf'); a.tst_rr(7, 7); a.bt_lbl(nf); a.mov_rm_rn(13, 0); a.or_imm(4); a.mov_rm_rn(0, 13); a.deflabel(nf)
    m.pool_jump()
    a.deflabel(ns)
    # sfumatura ai bordi: r3 = 1..16 (16 = niente)
    a.mov_rm_rn(8, 2); a.add_imm(-1, 2); a.sub(6, 2); a.mov_rm_rn(13, 0)                  # r2 = SL-1-k
    fo = m.newlabel('fo'); fd = m.newlabel('fd'); a.cmpgt(6, 2); a.bf_lbl(fo)
    a.mov_rm_rn(6, 2); a.and_imm(3); a.bra_lbl(fd); a.nop()                              # prima meta': bordo d'inizio
    a.deflabel(fo); a.and_imm(5)                                                          # seconda: bordo di fine
    a.deflabel(fd); a.mov_imm(16, 3); nof = m.newlabel('nof'); a.tst_rr(0, 0); a.bt_lbl(nof)
    a.mov_imm(16, 1); a.cmpge(1, 2); a.bt_lbl(nof); a.mov_rm_rn(2, 3); a.add_imm(1, 3); a.deflabel(nof)
    # lettura: buf[(base + passo*k/2) & 0xFFFF]
    a.mov_rm_rn(11, 1); a.mul_l(6, 1); a.sts_macl(1); a.shar(1); a.add_rr(9, 1); a.extu_w(1, 1); a.shll2(1); a.add_rr(10, 1); a.movl_ld(1, 0)
    a.mov_rm_rn(0, 1); a.exts_w(0, 0); a.shlr16(1); a.exts_w(1, 1)
    a.mul_l(3, 0); a.sts_macl(0); sar(m, 0, 4, 2); a.mul_l(3, 1); a.sts_macl(1); sar(m, 1, 4, 2)
    a.movl_st_disp(0, FW, 12); a.movl_st_disp(1, FWR, 12)
    a.add_imm(1, 5); a.extu_w(5, 5); a.add_imm(1, 6); a.add_imm(1, 14)
    a.add_imm(32, 12); a.dt(4); m.bf_far(l)
    stS(m, 5, 1); stS(m, 6, 2); stS(m, 14, 3); stS(m, 9, 4); stS(m, 11, 5); stS(m, 13, 6)

FXCODE = [fx_stutter, lambda m: fx_svf(m, False), fx_trgate, fx_lofi, fx_vinyl, fx_scatter,
          fx_delay, fx_reverb, lambda m: fx_scatter(m, True), fx_downer, fx_chorus, fx_trem]

# ================= modello Python (bit-exact) =================
def s32(v): v &= M32; return v - (1 << 32) if v & 0x80000000 else v
def c16(v): return max(-32768, min(32767, v))
def soft(v):
    if v > KNEE: v = KNEE + ((v - KNEE) >> 2)
    elif v < -KNEE: v = -KNEE + ((v + KNEE) >> 2)
    return c16(v)
def vhash(v):
    x = (v * VK1) & M32; x ^= x >> 16; x = (x * VK2) & M32; x ^= x >> 16; return x

class Model:
    def __init__(self):
        self.S = [0] * NST; self.G = 0; self.fxrun = -1; self.lastpn = 0
        self.buf = {}; self.sbuf = {}; self.bufok = True; self.swpend = False; self.cfr = [0, 0, 0]; self.cfok = False
    def switch(self, sel):
        self.swpend = False; self.fxrun = sel; self.G = 0; self.cfok = False; self.reset(sel)
    def reset(self, e):
        self.S = [0] * NST
        if e == E_TG: self.S[3] = 4096
        if e == E_DOWN: self.S[4] = 1 << 24
        if e == E_REV: self.buf.update({REV_OFF + i: 0 for i in range(0, REV_BYTES, 2)})
        if e == E_CHO: self.sbuf.update({i: 0 for i in range(0, CH_FR * 4, 2)})
        if e == E_VIN: self.sbuf.update({WOW_OFF + i: 0 for i in range(0, 0x400, 2)})
    def block(self, L, R, sel, act, pn, cf):
        if pn != self.lastpn:
            self.lastpn = pn
            if self.G != 0 and self.fxrun >= 0: self.swpend = True          # prima sfuma l'effetto che suona
            else: self.switch(sel)
        if self.swpend and self.G == 0: self.switch(sel)
        if self.fxrun == sel: self.cfr = list(cf); self.cfok = True          # CFFX == FXRUN: coefficienti bloccati
        if self.fxrun < 0: return L, R
        e = self.fxrun; WL, WR = list(L), list(R)
        if self.cfok and not (e in NEEDBUF and not self.bufok): FXREF[e](self, WL, WR, self.cfr)
        gt = 4096 if act and not self.swpend else 0; g0 = self.G; step = max(-GSTEP, min(GSTEP, gt - g0)); self.G = g0 + step
        oL, oR = [], []
        for i in range(BLK):
            g = g0 + ((step * i) >> BSH)
            oL.append(c16(L[i] + (((WL[i] - L[i]) * g) >> 12))); oR.append(c16(R[i] + (((WR[i] - R[i]) * g) >> 12)))
        return oL, oR
    def b16(self, d, off): return d.get(off, 0)

def ref_svf(md, WL, WR, cf, drive):
    S = md.S; S[0] += (cf[0] - S[0]) >> 2; f, q = S[0], cf[1]
    typ = 0 if drive else (0 if cf[2] < 33 else 1 if cf[2] < 66 else 2)
    for W, (li, bi) in ((WL, (1, 2)), (WR, (3, 4))):
        pass
    for i in range(BLK):
        for W, li, bi in ((WL, 1, 2), (WR, 3, 4)):
            x = W[i]; lo, bd = S[li], S[bi]
            for _ in range(2):
                lo += s32(f * bd) >> 12; hi = x - lo - (s32(q * bd) >> 12); bd += s32(f * hi) >> 12
            S[li], S[bi] = lo, bd
            if drive:
                x = c16(s32(lo * cf[2]) >> 8); x2 = (x * x) >> 15; x3 = (x2 * x) >> 15
                y = ((3 * x - x3) * 21845) >> 16
            else:
                y = soft(lo if typ == 0 else bd if typ == 1 else hi)
            W[i] = y

def ref_iso(md, WL, WR, cf):
    S = md.S
    for i in range(BLK):
        for W, a1, a2 in ((WL, 1, 2), (WR, 3, 4)):
            x = W[i]; S[a1] += ((x - S[a1]) * 143) >> 12; S[a2] += ((x - S[a2]) * 1427) >> 12
            hi = x - S[a2]; mid = S[a2] - S[a1]
            W[i] = c16((S[a1] * cf[0] + mid * cf[1] + hi * cf[2]) >> 12)

def ref_lofi(md, WL, WR, cf):
    S = md.S; mask = -1 << cf[1]
    for i in range(BLK):
        if S[1] <= 0: S[2], S[3], S[1] = WL[i], WR[i], cf[0]
        S[1] -= 1
        for W, h, t in ((WL, 2, 4), (WR, 3, 5)):
            S[t] += ((S[h] & mask) - S[t]) * cf[2] >> 12; W[i] = S[t]

def ref_vinyl(md, WL, WR, cf):
    S = md.S
    for W, h, t in ((WL, 1, 2), (WR, 3, 4)):
        for i in range(BLK):
            x = W[i]; S[h] += ((x - S[h]) * 87) >> 12; S[t] += ((x - S[h] - S[t]) * cf[2]) >> 12; W[i] = S[t]
    d8 = []; ph = S[10]
    for i in range(BLK):
        d8.append((s32(cf[1] * SIN[ph >> 24]) >> 14) + 32 * 256); ph = (ph + WOWINC) & M32
    S[10] = ph
    for c, W in enumerate((WL, WR)):
        base = WOW_OFF + 0x200 * c
        for i in range(BLK):
            w = (S[9] + i) & 255; md.sbuf[base + 2 * w] = c16(W[i])
            ri = (w - (d8[i] >> 8)) & 255; fr = d8[i] & 255
            av = md.b16(md.sbuf, base + 2 * ri); bv = md.b16(md.sbuf, base + 2 * ((ri - 1) & 255))
            W[i] = av + (((bv - av) * fr) >> 8)
    amt = cf[0]; dens = amt >> 1
    for c, (W, ns, cs) in enumerate(((WL, 5, 6), (WR, 7, 8))):
        idx = (S[9] + VSEED0 + c * VSEEDC) & M32
        for i in range(BLK):
            x = vhash(idx); idx = (idx + 1) & M32
            nz = s32(x) >> 20; fh = (nz + S[ns]) >> 1; S[ns] = nz
            hiss = s32(fh * amt) >> 7
            ck = 0
            if (x & 0xFFFF) < dens:
                ck = ((((x >> 16) & 0x1FFF) + 1000) * amt) >> 6
                if x & 0x80000000: ck = -ck
            oc = ck + S[cs]; S[cs] = ck >> 1
            W[i] = c16(W[i] + hiss + oc)
    S[9] = (S[9] + BLK) & M32

def ref_comp(md, WL, WR, cf):
    S = md.S; env = S[1]
    for i in range(BLK):
        e = max(abs(WL[i]), abs(WR[i])) << 8; d = e - env
        env += (d * 92) >> 12 if d > 0 else s32(d * cf[1]) >> 16
    S[1] = env; ev = env >> 8; T = cf[0]
    gt = (((T + ((ev - T) >> 3)) << 12) & M32) // ev if ev > T else 4096
    gt = ((gt * cf[2]) & M32) >> 12
    gp = S[2]; S[2] = gt
    for i in range(BLK):
        g = gp + (((gt - gp) * i) >> BSH)
        WL[i] = c16(s32(WL[i] * g) >> 12); WR[i] = c16(s32(WR[i] * g) >> 12)

def rd_long(md, idx):
    v = md.buf.get(idx, 0); l = v & 0xffff; r = (v >> 16) & 0xffff
    return l - 0x10000 if l & 0x8000 else l, r - 0x10000 if r & 0x8000 else r
def wr_long(md, idx, l, r): md.buf[idx] = (l & 0xffff) | ((r & 0xffff) << 16)

def ref_delay(md, WL, WR, cf):
    S = md.S; D = cf[1]; y = []
    W = S[1]; N = S[4]
    for i in range(BLK):
        rd = (W + i - D) & 0xFFFF
        y.append(rd_long(md, rd) if N + i >= D else (0, 0))
    S[4] = min(N + BLK, 0x10000)
    for i in range(BLK):
        yl, yr = y[i]
        S[2] += (yl - S[2]) >> 1; S[3] += (yr - S[3]) >> 1
        wl = c16(WL[i] + (s32(S[2] * cf[2]) >> 12)); wr = c16(WR[i] + (s32(S[3] * cf[2]) >> 12))
        wr_long(md, (W + i) & 0xFFFF, wl, wr)
        dg = 4096 - (cf[0] >> 1)
        WL[i] = soft((s32(WL[i] * dg) >> 12) + (s32(yl * cf[0]) >> 12)); WR[i] = soft((s32(WR[i] * dg) >> 12) + (s32(yr * cf[0]) >> 12))
    S[1] = (W + BLK) & 0xFFFF

def ref_reverb(md, WL, WR, cf):
    S = md.S; inp = [(WL[i] + WR[i]) >> 5 for i in range(BLK)]; acc = {20: [0] * BLK, 24: [0] * BLK}
    def b(off): return md.buf.get(off, 0)
    for c in range(len(COMBS)):
        base = REV_ADDR[c]; filt, pos = S[c], S[8 + c]; A = acc[20]
        for i in range(BLK):
            y = b(base + 2 * pos); filt += ((y - filt) * cf[2]) >> 12
            md.buf[base + 2 * pos] = c16(inp[i] + ((filt * cf[1]) >> 12)); A[i] += y
            pos += 1
            if pos == COMBS[c]: pos = 0
        S[c], S[8 + c] = filt, pos
    for j in range(len(APS)):
        base = REV_ADDR[len(COMBS) + j]; pos = S[16 + j]; A, D = acc[AP_IO[j][0]], acc[AP_IO[j][1]]
        for i in range(BLK):
            bo = b(base + 2 * pos); x = A[i]
            if j == 2: md.buf[base + 2 * pos] = c16(x); D[i] = bo
            else: md.buf[base + 2 * pos] = c16(x + (bo >> 1)); D[i] = bo - x
            pos += 1
            if pos == APS[j]: pos = 0
        S[16 + j] = pos
    for i in range(BLK):
        dg = 4096 - (cf[0] >> 1)
        WL[i] = soft((s32(WL[i] * dg) >> 12) + (s32(soft(acc[20][i] * 4) * cf[0]) >> 12))
        WR[i] = soft((s32(WR[i] * dg) >> 12) + (s32(soft(acc[24][i] * 4) * cf[0]) >> 12))

def ref_looper(md, WL, WR, cf):
    S = md.S; LEN = cf[0]
    for i in range(BLK):
        if S[1] < LEN:
            wr_long(md, S[1], WL[i], WR[i]); S[1] += 1
        else:
            if S[2] >= LEN: S[2] = 0
            idx = LEN - 1 - S[2] if cf[1] >= 50 else S[2]
            l, r = rd_long(md, idx); ed = min(S[2], LEN - 1 - S[2])
            if ed < 16: l = (l * (ed + 1)) >> 4; r = (r * (ed + 1)) >> 4
            WL[i], WR[i] = l, r; S[2] += 1

def ref_downer(md, WL, WR, cf):
    S = md.S
    for i in range(BLK):
        wr_long(md, S[1], WL[i], WR[i])
        if S[1] < DLY_FR - 1: S[1] += 1
        if S[4] == 0: WL[i] = WR[i] = 0; continue
        (al, ar), (bl, br) = rd_long(md, S[2]), rd_long(md, S[2] + 1)
        fr = S[3] >> 12
        WL[i] = al + (((bl - al) * fr) >> 12); WR[i] = ar + (((br - ar) * fr) >> 12)
        S[3] += S[4]
        if S[3] >= 1 << 24: S[3] -= 1 << 24; S[2] += 1
        S[4] = max(0, S[4] - cf[0])

def ref_chorus(md, WL, WR, cf):
    S = md.S
    for i in range(BLK):
        w = S[1]; md.sbuf[4 * w] = WL[i] & 0xffff; md.sbuf[4 * w + 2] = WR[i] & 0xffff
        for W, po, ho in ((WL, 0, 0), (WR, 0x40000000, 2)):
            s = SIN[((S[2] + po) & M32) >> 24]; d8 = (s32(s * cf[2]) >> 14) + CH_BASE8
            ri = (w - (d8 >> 8)) & (CH_FR - 1); fr = d8 & 255
            def r16(k):
                v = md.sbuf.get(4 * k + ho, 0); return v - 0x10000 if v & 0x8000 else v
            av = r16(ri); bv = r16((ri - 1) & (CH_FR - 1)); y = av + (((bv - av) * fr) >> 8)
            W[i] = W[i] + (s32((y - W[i]) * cf[0]) >> 13)
        S[1] = (w + 1) & (CH_FR - 1); S[2] = (S[2] + cf[1]) & M32

def ref_trem(md, WL, WR, cf):
    S = md.S
    for i in range(BLK):
        u = SIN[S[1] >> 24] + 16384
        g = 4096 - ((cf[0] * (32768 - u)) >> 15)
        gl = gr = g
        if cf[2] >= 50: gr = g; gl = 4096 - ((cf[0] * u) >> 15)
        WL[i] = s32(WL[i] * gl) >> 12; WR[i] = s32(WR[i] * gr) >> 12
        S[1] = (S[1] + cf[1]) & M32

def ref_stutter(md, WL, WR, cf):
    S = md.S; LEN = cf[0]; glen = (LEN * cf[1]) >> 12
    if glen < 1: glen = 1
    for i in range(BLK):
        if S[1] < LEN:
            wr_long(md, S[1], WL[i], WR[i]); S[1] += 1
        else:
            if S[2] >= LEN: S[2] = 0
            if S[2] >= glen: WL[i] = WR[i] = 0
            else:
                l, r = rd_long(md, S[2]); ed = min(S[2], glen - 1 - S[2])
                if ed < 16: l = (l * (ed + 1)) >> 4; r = (r * (ed + 1)) >> 4
                WL[i], WR[i] = l, r
            S[2] += 1

def ref_trgate(md, WL, WR, cf):
    S = md.S; idx = min(7, (cf[0] * 5) >> 6); pat = TABLES['GATEP'][1][idx]
    for i in range(BLK):
        on = (pat >> (15 - S[2])) & 1
        gt = 4096 if on else 4096 - cf[1]
        S[3] += (gt - S[3]) >> 5
        WL[i] = s32(WL[i] * S[3]) >> 12; WR[i] = s32(WR[i] * S[3]) >> 12
        S[1] += 1
        if S[1] >= cf[2]: S[1] = 0; S[2] = (S[2] + 1) & 15

SCAT_SEEN = set()
def ref_scatter(md, WL, WR, cf, rev=False):
    S = md.S; AMT, SL = (0, cf[0]) if rev else (cf[0], cf[1])
    for i in range(BLK):
        W = S[1]; wr_long(md, W, WL[i], WR[i])
        if S[2] >= SL: S[2] = 0
        if S[2] == 0:
            fl = (S[6] & 1) << 1; act = S[8]
            if not rev: SCAT_SEEN.add(act)
            if act: fl |= 1
            base, step = W, 2
            if act == 1: base = W - SL
            elif act == 2: base, step = W - 1, -2
            elif act == 3: base = W - 2 * SL
            elif act == 4: base, step = W - SL, 1
            S[4], S[5] = base, step
            if rev: nxt = 2 if S[3] >= SL else 0
            else:
                seed = S[7]
                if seed == 0: seed = ((md.lastpn * VK1) & M32) | 1
                seed = (seed * 1664525 + 1013904223) & M32; S[7] = seed
                nxt = ((seed >> 16) & 3) + 1 if S[3] >= SL and (seed >> 24) < AMT else 0
            S[8] = nxt
            if nxt: fl |= 4
            S[6] = fl
        k = S[2]; l, r = rd_long(md, (S[4] + ((S[5] * k) >> 1)) & 0xFFFF)
        e2 = SL - 1 - k
        e, msk = (k, S[6] & 3) if k < e2 else (e2, S[6] & 5)
        g = e + 1 if msk and e < 16 else 16
        WL[i] = (l * g) >> 4; WR[i] = (r * g) >> 4
        S[1] = (W + 1) & 0xFFFF; S[2] += 1; S[3] += 1

FXREF = [ref_stutter, lambda md, l, r, c: ref_svf(md, l, r, c, False), ref_trgate, ref_lofi,
         ref_vinyl, ref_scatter, ref_delay, ref_reverb, lambda md, l, r, c: ref_scatter(md, l, r, c, True), ref_downer, ref_chorus, ref_trem]

if __name__ == '__main__':
    import random
    out = os.path.join(os.path.dirname(__file__), '..', 'MASTERFX.WAV'); pg.build_wav(out)
    print("  tabelle %d B, descrittori %d B" % (TABLEN, NFX * 36))
    from validate import Harness
    from shasm import SHAsm
    from pages import SET_FOCUS, ENABLE_DOIT
    from mpcmod import OS_PUSH_PAGE
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s_ = SHAsm(addr); fn(s_); blob = s_.assemble()
        for i in range(0, len(blob), 2): e.wr(addr + i, struct.unpack('<H', blob[i:i+2])[0], 2)
    for ad in (OS_PUSH_PAGE, ENABLE_DOIT, SET_FOCUS): stub(ad, lambda s_: (s_.rts(), s_.nop()))
    TEMPO = [3000]
    stub(SYS_PARAM, lambda s_: (s_.movl_pool(0x88E50000, 2), s_.movl_ld(2, 2), s_.movl_st_disp(2, 4, 4), s_.mov_imm(1, 0), s_.rts(), s_.nop()))
    h.ram_w32(0x88E50000, TEMPO[0])
    for k, w in enumerate(TXORIG): h.ram_w16(TXPATCH + 2 * k, w)
    h.ram_w32(SLIDER, 512)
    TC = {'v': 0x7FFFFF, 'n': 0, 'slow': False}
    def tcnt(a_):
        TC['n'] += 1; TC['v'] -= (1000 if TC['slow'] and TC['n'] % 2 == 0 else 10); return TC['v'] & 0xFFFFFF
    e.mmio_r[TCNT1] = tcnt; e.mmio_r[TCOR1] = lambda a_: 0xFFFFFF
    # rilocazione: installato nello slot 1 (+0x8000), nel codice non deve restare nessun indirizzo dello slot 0 (P1 o P2)
    import hub as _H
    h1 = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h1.fill_scratch()
    h1.load_hub(); h1.load_wav(os.path.join(os.path.dirname(__file__), '..', 'INTHRU.WAV')); h1.fire(3); h1.load_wav(out); h1.fire(5)
    assert h1.ram_r32(_H.L.INST(1)) == 1
    s1 = _H.L.SLOT(1); s0 = _H.L.SLOT(0); clen = 0x7000 - SBUF_OFF
    left = [hex(s1 + o) for o in range(0, clen, 4)
            if (s0 & 0x1FFFFFFF) <= (h1.ram_r32(s1 + o) & 0x1FFFFFFF) < (s0 & 0x1FFFFFFF) + 0x8000 and h1.ram_r32(s1 + o) >> 29 in (4, 5)]
    assert not left, "indirizzi non rilocati: %s" % left[:8]
    print("  ok rilocazione nello slot 1: nessun riferimento allo slot 0 (P1/P2)")
    h.load_hub(); h.load_wav(out); h.fire(10)
    MSG = 0x88E10000
    def call(type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG + 4, val & M32); h.ram_w32(MSG + 8, 0); h.ram_w32(MSG + 12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = pg.page; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 5_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d" % i
        return e.r[0]
    patched = lambda: [e.rd(TXPATCH + 2 * k, 2) for k in range(16)]
    h.ram_w32(EXITLIT, ISR_EXIT)
    OTHER = 0x88123456
    h.ram_w32(EVQ + 0x68, OTHER); h.fire(3); assert patched() == TXORIG and h.ram_r32(EXITLIT) == ISR_EXIT, "fuori pagina: niente aggancio"
    h.ram_w32(EVQ + 0x68, pg.page); h.fire(40)
    pw = patched(); txent = pw[5] | (pw[6] << 16); trig = h.ram_r32(EXITLIT)
    assert pw[:5] == [0xD002, 0x400B, 0x0009, 0xA00B, 0x0009] and pw[7:] == [9] * 9 and txent >> 28 == 0x8 and h.ram_r32(HOOKED) == 1, [hex(x) for x in pw]
    assert trig >> 28 == 0x8 and trig != ISR_EXIT, hex(trig)
    fpb = h.ram_r32(FPB); assert fpb == 26460000 // TEMPO[0], fpb
    assert h.ram_r32(BUFP) == (0x88F00000 + FXBUF_OFF) & 0xDFFFFFFF, hex(h.ram_r32(BUFP))
    txt = lambda k, w: bytes(e.rd(pg.dlabels[k] + i, 1) for i in range(w)).decode()
    assert txt(NAMEL, 8) == "STUTTER " and txt(L0L, 4) == "RATE" and h.ram_r32(MAINV) == 40
    print("  ok patch: TX %08x -> mf_tx %08x, uscita ISR -> mf_trig %08x; buffer v10; tempo %d -> %d frame/battuta" % (TXPATCH, txent, trig, TEMPO[0], fpb))
    rnd = random.Random(7); md = Model(); md.lastpn = h.ram_r32(PN)
    FR = [0x7C0 * 32]                                    # contatore frame OS (multiplo di 32); il ring fa il giro durante il test
    hist = {}                                            # blocco -> (ingresso long, uscita attesa long)
    stepsmax = {}; steady = {}; procsteps = []
    e.logrange = (0xA40000E0, 0xA40000E4)
    sx = lambda v: v - 0x10000 if v & 0x8000 else v
    def tick(longs):
        F = FR[0]; p = OUT_BASE + (F * 8) % OUT_SIZE
        for k in range(8): h.ram_w32(p + 4 * k, longs[k])
        h.ram_w32(FRAMES, F)
        regs = [0x33330000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[1] = FRAMES; regs[2] = p
        e.r = regs[:]; e.wlog = []; e.gbr = 0xA4000080; e.maxsteps = 200_000; e.pc = TXPATCH; e.pr = 0x12345678
        e.forbid = (0xA8F18000, 0xA8F58000)                           # l'ISR non deve MAI eseguire dall'alias senza cache
        assert e.run(stop_pc=TXPATCH + 0x20), "TX non torna"
        assert e.r[2] == p + 32 and e.r[1] == FRAMES, (hex(e.r[2]), hex(e.r[1]))
        for i in range(3, 16): assert e.r[i] == regs[i], "TX: r%d corrotto" % i
        fifo = [v for pc_, a_, v, sz in e.wlog if a_ == 0xA40000E0 and sz == 4]; assert len(fifo) == 8
        FR[0] = F + 4; h.ram_w32(FRAMES, FR[0])
        regs = [0x44440000 + i for i in range(16)]; regs[15] = 0x88400000 - 256
        e.srv = 0x20000000 if (F // 32) % 2 else 0                     # alterna: contesto ISR (banco 1, via rte) / ritorno del render
        e.r = regs[:]; e.maxsteps = 3_000_000; e.pc = trig; e.pr = 0x12345678
        assert e.run(stop_pc=ISR_EXIT), "mf_trig non arriva all'uscita"
        e.forbid = None
        for i in range(8, 16): assert e.r[i] == regs[i], "trig/proc: r%d corrotto" % i
        return fifo, e.steps
    inputs = {}; mlast = [None]; ctx = {'sel': None, 'act': 0, 'cf': None}
    def pack(el, er, lows): return [((el[jj] << SH) & M32) | lows[2 * jj] if kk == 0 else ((er[jj] << SH) & M32) | lows[2 * jj + 1] for jj in range(BLK) for kk in (0, 1)]
    def block(L, R, sel=None, act=None, cf=None, check=True):
        """8 interrupt TX = 32 frame. Il modello segue mf_trig: dopo ogni interrupt, se c'e' un effetto (ACT o G) e un blocco
        completo nuovo j = F/32 - 1, lo elabora (anche quello appena prima della pressione). Uscita del blocco b = elaborato
        di b-2 se elaborato, altrimenti b-2 a secco."""
        if sel is not None: ctx.update(sel=sel, act=act, cf=cf)
        b = FR[0] // 32; lows = [rnd.randrange(1 << SH) for _ in range(2 * BLK)]
        longs = []
        for jj in range(BLK): longs += [((L[jj] << SH) & M32) | lows[2 * jj], ((R[jj] << SH) & M32) | lows[2 * jj + 1]]
        inputs[b] = (L, R, lows); hist[b] = longs
        out = []; stp = 0
        for k in range(8):
            f, st_ = tick(longs[8 * k:8 * k + 8]); out += f; stp = max(stp, st_)
            jb = FR[0] // 32 - 1
            if ctx['sel'] is not None and (ctx['act'] or md.G) and jb != mlast[0] and jb in inputs:
                mlast[0] = jb; Lj, Rj, lj = inputs[jb]
                el, er = md.block(Lj, Rj, ctx['sel'], ctx['act'], h.ram_r32(PN), ctx['cf'])
                hist[jb] = pack(el, er, lj)
        if check and b - LAT in hist:
            if out != hist[b - LAT]:
                bad = [q for q in range(2 * BLK) if out[q] != hist[b - LAT][q]][:4]
                raise AssertionError("blocco %d: long %s emu=%s atteso=%s" % (b, bad, [hex(out[q]) for q in bad], [hex(hist[b - LAT][q]) for q in bad]))
        return stp
    # a riposo (nessun effetto): uscita = ingresso ritardato di LAT blocchi, esatto (bit bassi compresi)
    for _ in range(4):
        L = [rnd.randint(-30000, 30000) for _ in range(BLK)]; R = [rnd.randint(-30000, 30000) for _ in range(BLK)]; block(L, R, check=False)
    for _ in range(3):
        L = [rnd.randint(-30000, 30000) for _ in range(BLK)]; R = [rnd.randint(-30000, 30000) for _ in range(BLK)]; block(L, R)
    print("  ok a riposo: uscita = ingresso ritardato di %d blocchi (%d frame)" % (LAT, LAT * BLK) + ", esatto")
    def cfs(sel):
        return [coef(FX[sel][2][k], h.ram_r32(PV[sel][k]), h.ram_r32(FPB)) for k in range(3)]
    def run(sel, nblk, act, loud=20000, tone=False):
        for n in range(nblk):
            if tone:
                L = [int(loud * math.sin(2 * math.pi * 440 * (FR[0] + jj) / FS)) for jj in range(BLK)]; R = [x // 2 for x in L]
            else:
                L = [rnd.randint(-loud, loud) for _ in range(BLK)]; R = [rnd.randint(-loud, loud) for _ in range(BLK)]
            cf = [h.ram_r32(CF[k]) for k in range(3)]
            assert cf == [x & M32 for x in cfs(sel)], ("coefficienti", FX[sel][0], cf, cfs(sel))
            try:
                stp = block(L, R, sel, act, cfs(sel))
            except AssertionError as ex:
                raise AssertionError("%s blocco %d: %s" % (FX[sel][0], n, ex))
            stepsmax[sel] = max(stepsmax.get(sel, 0), stp)
            if n > 2: steady[sel] = max(steady.get(sel, 0), stp)
    def setp(sel, vals):
        for k, v in enumerate(vals):
            if v is not None: h.ram_w32((MAINV, P1V, P2V)[k], v)
        h.fire(1)
    PLAN = {E_DLY: (None, 0, 60), E_RVS: (0, None, None), E_DOWN: (20, None, None), E_REV: (60, 90, 40), E_STUT: (60, 70, None), E_TG: (50, None, 99), E_SCAT: (99, 0, None)}
    NFADE = 4096 // GSTEP + 2
    for sel in range(NFX):
        call(EV_PAD, sel, 100); h.fire(1)
        assert txt(NAMEL, 8) == FX[sel][0].ljust(8), txt(NAMEL, 8)
        if sel in PLAN: setp(sel, PLAN[sel])
        nb = 400 if sel in (E_SCAT, E_RVS) else 60 if sel in NEEDBUF else 12
        run(sel, nb, 1, tone=sel in (E_SF,))
        call(EV_PAD, sel, 0); h.fire(1)
        run(sel, NFADE, 0)
        print("  ok %-8s  %d blocchi da %d frame + rilascio, bit-exact (uscita ritardata %d blocchi), passi max per interrupt: regime %d, primo %d" % (FX[sel][0], nb, BLK, LAT, steady[sel], stepsmax[sel]))
    # cambio rapido (HW: blip fortissimi): un pad premuto mentre un altro effetto suona -> prima sfuma, poi cambia;
    # i coefficienti del vecchio restano bloccati durante la dissolvenza
    call(EV_PAD, E_TREM, 100); h.fire(1); run(E_TREM, 6, 1)
    call(EV_PAD, E_SF, 100); h.fire(1); run(E_SF, 10, 1)
    call(EV_PAD, E_TREM, 0); call(EV_PAD, E_DLY, 100); h.fire(1); run(E_DLY, 6, 1)
    call(EV_PAD, E_SF, 0); call(EV_PAD, E_DLY, 0); h.fire(1); run(E_DLY, NFADE, 0)
    assert SCAT_SEEN == {0, 1, 2, 3, 4}, SCAT_SEEN
    print("  ok SCATTER: tutte le azioni (vivo, ripeti, al contrario, 2 fette fa, mezza velocita') provate bit-exact")
    print("  ok cambio rapido TREMOLO -> SUPERFLT -> DELAY senza rilasciare: dissolvenza e coefficienti coerenti, bit-exact")
    # slider
    call(EV_PAD, E_SF, 100); h.fire(1)
    def mapv(v): return min(99, (v - h.ram_r32(SLO)) * 100 // (h.ram_r32(SHI) - h.ram_r32(SLO) + 1))
    def sl(v, n=80, jit=0):
        for k in range(n): h.ram_w32(SLIDER, v + (rnd.randint(-jit, jit) if jit else 0)); h.fire(1)
        return h.ram_r32(MAINV)
    assert abs(sl(0x3E8) - 99) <= 1 and sl(0x010) <= 1                  # calibrazione: una corsa completa
    for v in range(0x300, 0x3E9, 5): h.ram_w32(SLIDER, v); h.fire(2)       # arrivo LENTO in cima (passi sotto soglia)
    h.fire(40); assert h.ram_r32(MAINV) == 99, h.ram_r32(MAINV)
    for v in range(0x100, 0x00F, -5): h.ram_w32(SLIDER, v); h.fire(2)      # e in fondo
    h.fire(40); assert h.ram_r32(MAINV) == 0, h.ram_r32(MAINV)
    v = sl(0x280); assert abs(v - mapv(0x280)) <= 1 and h.ram_r32(PV[E_SF][0]) == v, (v, mapv(0x280))
    # pad premuto: reattivo (passo di 20 punti -> valore aggiornato entro 3 fire)
    before = h.ram_r32(MAINV); h.ram_w32(SLIDER, 0x280 + 20); h.fire(3); assert h.ram_r32(MAINV) > before, (before, h.ram_r32(MAINV))
    # corsa veloce con pochi fire (HW: con l'effetto attivo si fermava a 85/95): 1 fire in basso -> 0, 1 fire in alto -> 99
    h.ram_w32(SLIDER, 0x010); h.fire(1); assert h.ram_r32(MAINV) == 0, h.ram_r32(MAINV)
    h.ram_w32(SLIDER, 0x3E8); h.fire(1); assert h.ram_r32(MAINV) == 99, h.ram_r32(MAINV)
    h.ram_w32(SLIDER, 0x280); h.fire(10)
    call(EV_PAD, E_SF, 0); h.fire(1)                                       # pad rilasciato: filtro pesante, rotella libera
    for k in range(250): h.ram_w32(SLIDER, 0x280 + rnd.randint(-10, 10)); h.fire(1)
    v0 = h.ram_r32(MAINV); sl(0x280, 300, 10); assert h.ram_r32(MAINV) == v0, "rumore sposta il valore"
    h.ram_w32(MAINV, 12); sl(0x280, 300, 10); assert h.ram_r32(MAINV) == 12 and h.ram_r32(PV[E_SF][0]) == 12, "rotella sovrascritta"
    assert abs(sl(0x3E8) - 99) <= 1 and h.ram_r32(SHI) >= 0x3E0
    assert sl(0x010) <= 1 and h.ram_r32(SLO) <= 0x018
    run(E_SF, NFADE, 0)
    print("  ok slider (A/D 0x%X): filtrato, fermo col rumore +-10, rotella libera, auto-calibrazione" % SLIDER)
    # sovraccarico: interrupt che durano piu' di meta' periodo per 16 volte -> passthrough + OVERLOAD, la MPC resta viva
    call(EV_PAD, E_TREM, 100); h.fire(1); TC['slow'] = True; TC['n'] = 0
    rb = lambda: ([rnd.randint(-9000, 9000) for _ in range(BLK)], [rnd.randint(-9000, 9000) for _ in range(BLK)])
    for k in range(NWIN + 2): block(*rb(), check=False)
    assert h.ram_r32(KILL) == 1 and h.ram_r32(G) == 0, "sovraccarico non intercettato"
    for k in range(2): block(*rb(), check=False)
    for k in range(3): block(*rb())                          # dopo KILL: uscita = ingresso a secco (ritardato)
    h.fire(1); assert txt(NAMEL, 8) == "OVL  %02d%%" % h.ram_r32(LOADP) and h.ram_r32(LOADP) > LOADMAX, (txt(NAMEL, 8), h.ram_r32(LOADP))
    TC['slow'] = False; call(EV_PAD, E_TREM, 0); h.fire(1); block(*rb())
    md.G = 0
    print("  ok sovraccarico: carico > %d%% su %d blocchi -> effetto spento (a secco), %r" % (LOADMAX, NWIN, txt(NAMEL, 8)))
    # pagina lasciata: rilascio + sgancio (byte originali rimessi) a dissolvenza finita
    call(EV_PAD, E_TREM, 100); h.fire(1); run(E_TREM, 5, 1)
    h.ram_w32(EVQ + 0x68, OTHER); h.fire(1); assert h.ram_r32(ACT) == 0 and patched() != TXORIG
    run(E_TREM, NFADE, 0); h.fire(1); assert patched() == TXORIG and h.ram_r32(HOOKED) == 0 and h.ram_r32(EXITLIT) == ISR_EXIT
    print("  ok uscita dalla pagina: effetto rilasciato, codice OS ripristinato a dissolvenza finita")
    # byte OS inattesi: nessun aggancio
    h.ram_w16(TXPATCH + 6, 0x1234); h.ram_w32(EVQ + 0x68, pg.page); h.fire(3); assert h.ram_r32(HOOKED) == 0 and patched()[3] == 0x1234
    for k, w in enumerate(TXORIG): h.ram_w16(TXPATCH + 2 * k, w)
    print("  ok OS inatteso: nessuna patch")
    # loader senza buffer FX
    h.ram_w32(0x88F00000 + FXMARK_OFF, 0); h.fire(3); assert h.ram_r32(BUFP) == 0 and h.ram_r32(HOOKED) == 1
    pw = patched(); txent = pw[5] | (pw[6] << 16)
    md.bufok = False; md.lastpn = h.ram_r32(PN); md.G = 0
    for _ in range(LAT): block(*rb(), check=False)           # riaggancio: LAT blocchi di silenzio (buffer azzerati)
    call(EV_PAD, E_DLY, 100); h.fire(1); run(E_DLY, 3, 1); call(EV_PAD, E_DLY, 0); h.fire(1); run(E_DLY, NFADE, 0)
    print("  ok loader senza buffer FX: DELAY/REVERB/REVERSE/DOWNER in passthrough")
    # reinstallazione con l'aggancio attivo (HUB ricaricato / SHIFT+ERASE con la pagina aperta): l'avvio stacca il vecchio
    # aggancio PRIMA di azzerare le variabili dell'ISR (prima: ISR vivo con puntatori a 0)
    assert h.ram_r32(HOOKED) == 1 and patched() != TXORIG
    h.ram_w32(EVQ + 0x68, OTHER); h.ram_w32(LOAD_SCRATCH + 0x40, 0); h.fire(1)
    assert patched() == TXORIG and h.ram_r32(EXITLIT) == ISR_EXIT and h.ram_r32(HOOKED) == 0, "aggancio vecchio rimasto"
    print("  ok reinstallazione con l'effetto agganciato: ISR dell'OS rimesso prima di azzerare lo stato")
    print("VALIDATO: Master FX v3 (12 effetti; ISR TX leggero + elaborazione a blocchi) -> MASTERFX.WAV; passi max per interrupt %d" % max(stepsmax.values()))
