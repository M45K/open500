#!/usr/bin/env python3
# SAMPLE FX v3 (pagina nativa, ASCOLTO IN TEMPO REALE come in TRIM) -> SAMPLEFX.WAV
#   riga 0: selettore sample (rotella)          riga 1: "FADE IN  AMT100"   (FX = scelta con nomi, AMT = quantita')
#   ANTEPRIMA: a ogni cambio di sample/FX/AMT (dopo DEBOUNCE fire) l'addon prepara una COPIA di anteprima (Copy dell'OS
#   la prima volta, poi riusata) e la riempie con effetto(originale) A PEZZI: CHUNK frame per fire, dall'inizio della
#   regione, piu' veloce della riproduzione. Il codice degli addon gira da memoria NON cacheata (P2): elaborare tutto
#   in un fire bloccava il main loop per secondi (HW v2: pad in ritardo, rotella/cursori/CANCEL morti).
#   TUTTI I PAD suonano l'anteprima come in TRIM (evento 0x38 -> AUD_PLAY(player, blocco, modo 0, 127); rilascio -> AUD_STOP).
#   DO IT = finisce l'elaborazione e tiene l'anteprima come sample nuovo (corrente). Uscita (evento 0x24) senza DO IT =
#   anteprima cancellata (Delete dell'OS: e' l'ultimo blocco della lista, non sposta nulla). Originale intatto.
#   Sample degli addon (magic MPCA../MPCH.. a +0x100) = niente anteprima.
#   AMT per effetto:
#     FADE IN / FADE OUT  lunghezza della dissolvenza in % della regione (1..100)
#     GAIN                volume in % (0..400, con clip)
#     BITCRUSH            quantita' 1..100: bit tolti = (AMT*15+50)/100 (100 = 1 bit)
#     DOWNSAMP            fattore di sample&hold (1..64)
#     DRIVE               pre-gain (10+AMT)/10 poi saturazione morbida cubica (0..400)
#     TRIM SIL            soglia in decimi di % del fondo scala: l'anteprima (Extract) tiene solo la parte tra il primo
#                         e l'ultimo campione sopra soglia (ricreata a ogni cambio: la lunghezza cambia)
# Formato audio (da FUN_a0020d76 normalize / FUN_a0020e78 reverse): PCM s16 a +0x100, regione [+0x20, +0x24),
# u16 +0x92 = 2 -> stereo con il canale R a +0x100 + (+0x1C)*2. Dopo la modifica: LOOP_GUARD(blk) (FUN_a00205a8).
# Ascolto TRIM (handler pagina FUN_a003f81e, evento 0x38): play FUN_a001dce6(player 0x8848CD78, blk, modo, vel) legge
# start/end/loop dal blocco e punta direttamente al PCM; stop FUN_a001e076(player).
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import LOAD_SCRATCH
from mpcmod import EVQ as _EVQ

MGR, EXTRACT_CMD, COPY_CMD, DELETE_CMD, LOOP_GUARD = 0x8848ED30, 0x884C5E90, 0x884C5FA0, 0x884C5FFC, 0x884C4F04
PLAYER, AUD_PLAY, AUD_STOP = 0x8848CD78, 0x884C2636, 0x884C29D2
EV_PAD = 0x38
LEAVE_FIRES = 16      # fire di fila con un'altra pagina attiva = uscita vera (0x24 e' il ridisegno, non l'uscita)
FX_NAMES = ["NO FX", "FADE IN", "FADE OUT", "GAIN", "BITCRUSH", "DOWNSAMP", "DRIVE", "VINYL", "TRIM SIL"]
NOFX, FADE_IN, FADE_OUT, GAIN, CRUSH, DOWNS, DRIVE, VINYL, TRIM = range(9)   # TRIM resta l'ultimo   # NO FX: niente anteprima, i pad suonano l'originale
ERR_SILENT, ERR_NOSMP = 0x22, 0x21
DEBOUNCE = 48          # fire di quiete prima di ricalcolare (~0.25 s): scorrendo i sample non si copia a ogni scatto
CHUNK = 2048           # frame per fire (x2 canali se stereo); i cicli girano dalla cache (vedi cached_call)
# HW 2026-09-24: il codice addon gira da P2 NON cacheata a ~1M istruzioni/s (la diag da ~800k passi = ~1 s di attesa):
# i cicli pesanti (kern, dscan) si chiamano tramite l'alias P1 cacheato; icinv invalida prima le righe del nostro codice.
CACHE_SPAN = 0x2000    # byte di codice da invalidare a partire da __cstart (kern + helper)
MAGIC_ADDON, MAGIC_HUB = 0x4143504D, 0x4843504D      # "MPCA" (MPCADDN2/3), "MPCH" (MPCHUB_3)

pg = PageAddon("smpfxB", open_press=None)     # solo dal menu OTHER
pg.other_menu("Sample FX")
pg.far_hend = True                            # handler > 4 KB
pg.msg_fires = 0xC00                          # HW: 0x180 fire = una frazione di secondo
pg.keep_focus = True                          # i messaggi non spostano il cursore (HW v8: il pad lo portava sul tipo di effetto)
pg.selector()
# CATENA (v11, 2026-09-26): NSLOT slot "n EFFETTO AMT"; i campi mostrano lo slot scelto; l'anteprima = tutta la catena
NSLOT = 8
SLOTN = pg.value("", 0, 1, fmt="0", lo=1, hi=NSLOT, default=1)
FXS = pg.choice(" ", 1, 1, FX_NAMES, default=NOFX)
AMT = pg.value(" ", 10, 1, fmt="  0", lo=0, hi=0, default=0)
AMT_FID = [f for f, sl in pg.slots.items() if sl == AMT][0]
# per effetto: (min, max, valore proposto quando scegli l'effetto) -> scritti nel modello del campo AMT
FX_AMT = [(0, 0, 0), (1, 100, 50), (1, 100, 50), (0, 400, 150), (1, 100, 75), (1, 64, 4), (0, 400, 50), (1, 100, 40), (0, 400, 10)]
# VINYL: per campione i (indice assoluto) e canale c: x = hash(i + SEED0 + c*SEEDC); fruscio = media di rumore(i), rumore(i-1)
#   (rumore = x >> 20 con segno) * AMT >> 8; crackle = click se (x & 0xFFFF) < AMT/2 (~13/s a 40), ampiezza
#   (((x >> 16) & 0x1FFF) + 1000) * AMT >> 6, segno = bit 31, + meta' del click precedente. Tutto dall'indice: a pezzi = uguale.
VK1, VK2, VSEED0, VSEEDC = 0x9E3779B1, 0x85EBCA6B, 0x2545F491, 0x6D2B79F5
SHOWMSG = LOAD_SCRATCH + 0x1C  # timer messaggio del framework pagine (fire rimasti; 1 = sparisce al prossimo fire)
DIAG = False          # build di test: pad premuto "S<pk orig> P<pk antepr> D<% diversi> R<rebuild>*", rilasciato "Q<prog>% C<copie> L<uscite> Z<+0x3E>"
DIAG_WIN = 32768      # campioni (canale L, dall'inizio della regione) su cui misurare picchi e differenze

pg.message()
ERR, SRC, FXI, S0, E0, THR, FIRST, LAST, BASE = [pg.alloc_slot() for _ in range(9)]
PREV, PSRC, PFX, LSRC, LFX, LAMT, PEND, ACTIVE = [pg.alloc_slot() for _ in range(8)]
BUSY, POS, KPOS, KN, KSRCP, KDSTP, KT1, KT2 = [pg.alloc_slot() for _ in range(8)]
LFXL = pg.alloc_slot(default=-1)
RCNT, CCNT, LCNT, DG1, DG2, WHY, ENTRYF, OUTCNT = [pg.alloc_slot() for _ in range(8)]
CHFX = [pg.alloc_slot() for _ in range(NSLOT)]      # effetto per slot (contigui: indicizzati *4)
CHAMT = [pg.alloc_slot() for _ in range(NSLOT)]     # AMT per slot
LSLOT, KAMT, RB, STAGE, CHTRIM, TRAMT, PTRIM, ACTN = [pg.alloc_slot() for _ in range(8)]
KCHAN = pg.alloc_slot()                # canale della passata (0 L, 1 R): seme del VINYL
CONF = pg.alloc_slot()                 # 1: primo DO IT premuto, la domanda resta finche' DO IT (salva) o CANCEL (annulla)   # WHY = motivo ultimo cambio (E/S/F/A)   # diagnostica: rebuild, copie, uscite      # effetto per cui sono impostati i limiti di AMT (-1 = mai)

def cached_call(m, label):
    """chiama label tramite l'alias P1 (cacheato) dello stesso indirizzo fisico: codice PC-relativo, gira identico"""
    m.a.movl_pool(label, 2); m.imm32(0xDFFFFFFF, 3); m.a.and_rr(3, 2); m.a.jsr(2); m.a.nop()

def far_call(m, label):
    """chiamata a label lontana (bsr: +-4 KB): indirizzo assoluto nel pool (rilocato da pack_addon3)"""
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    """salto a label lontana (bra: +-4 KB); r2 clobberato"""
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def fire(m):
    """pagina attiva: sample cambiato -> catena azzerata; slot cambiato -> campi caricati; effetto/AMT cambiati -> nello
    slot + anteprima dopo DEBOUNCE fire; poi 1 pezzo dell'elaborazione"""
    a = m.a
    def slotaddr(base0, r):             # r = indirizzo di base0[SLOTN-1] (usa r0, r1)
        ld(m, SLOTN, 0); a.add_imm(-1, 0); a.shll2(0); m.imm32(base0, r); a.add_rr(0, r)
    sti(m, 0, OUTCNT)
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); act = m.newlabel('act'); a.bf_lbl(act)
    sti(m, 1, ACTIVE); sti(m, 0, LSRC); sti(m, 0, PEND); sti(m, 1, ENTRYF); a.deflabel(act)   # ingresso: forza il ricalcolo
    ch = m.newlabel('ch'); noch = m.newlabel('noch')
    # 1) sample corrente (l'anteprima conta come l'originale): cambiato -> catena azzerata
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8)
    ld(m, PREV, 2); a.cmpeq(2, 8); c1 = m.newlabel('c1'); a.bf_lbl(c1); ld(m, LSRC, 8); a.deflabel(c1)
    ld(m, LSRC, 2); a.cmpeq(2, 8); ns = m.newlabel('ns'); m.bt_far(ns)
    st(m, 8, LSRC); sti(m, ord('S'), WHY)
    far_call(m, 'chreset')
    a.bra_lbl(ch); a.nop()
    a.deflabel(ns); m.pool_jump()
    # 2) slot cambiato: carica effetto/AMT nei campi (limiti del modello senza proporre un AMT)
    ld(m, SLOTN, 0); ld(m, LSLOT, 2); a.cmpeq(2, 0); sl = m.newlabel('sl'); m.bt_far(sl)
    st(m, 0, LSLOT)
    slotaddr(CHFX[0], 2); a.movl_ld(2, 9); m.imm32(FXS, 1); a.movb_st(9, 1); st(m, 9, LFXL)
    slotaddr(CHAMT[0], 2); a.movl_ld(2, 10); st(m, 10, AMT)
    far_call(m, 'fxlims')
    m.imm32(_EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    far_bra(m, noch)
    a.deflabel(sl); m.pool_jump()
    # 3) effetto/AMT del campo -> nello slot
    m.imm32(FXS, 1); a.movb_ld(1, 9); a.extu_b(9, 9); a.mov_imm(TRIM, 3); a.cmpgt(3, 9); c2 = m.newlabel('c2'); a.bf_lbl(c2); a.mov_imm(0, 9); a.deflabel(c2)
    ld(m, LFXL, 2); a.cmpeq(2, 9); c3 = m.newlabel('c3'); a.bt_lbl(c3); st(m, 9, LFXL); far_call(m, 'fxlim'); a.deflabel(c3)
    ld(m, AMT, 10)
    slotaddr(CHFX[0], 11); a.movl_ld(11, 2); a.cmpeq(2, 9); chF = m.newlabel('chF'); a.bf_lbl(chF)
    slotaddr(CHAMT[0], 12); a.movl_ld(12, 2); a.cmpeq(2, 10); m.bt_far(noch)
    sti(m, ord('A'), WHY); a.bra_lbl('__wb'); a.nop()
    a.deflabel(chF); sti(m, ord('F'), WHY)
    a.deflabel('__wb')
    slotaddr(CHFX[0], 11); a.movl_st(9, 11); slotaddr(CHAMT[0], 12); a.movl_st(10, 12)
    a.deflabel(ch)
    ld(m, CONF, 0); a.tst_rr(0, 0); cfz = m.newlabel('cfz'); a.bt_lbl(cfz); sti(m, 0, CONF); sti(m, 1, SHOWMSG); a.deflabel(cfz)
    ld(m, ENTRYF, 0); a.tst_rr(0, 0); ce = m.newlabel('ce'); a.bt_lbl(ce); sti(m, ord('E'), WHY); sti(m, 0, ENTRYF); a.deflabel(ce)
    sti(m, DEBOUNCE, PEND)
    a.deflabel(noch)
    ld(m, PEND, 0); a.tst_rr(0, 0); fd = m.newlabel('fd'); m.bt_far(fd)
    a.add_imm(-1, 0); st(m, 0, PEND); a.tst_rr(0, 0); m.bf_far(fd)
    far_call(m, 'rebuild')
    a.deflabel(fd)
    far_call(m, 'step')                                                         # un pezzo (se in corso)
    # l'OS spegne DO IT dopo ogni pressione (HW v3: messaggio solo la prima volta): riacceso a ogni fire, come fa l'HUB in OTHER
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
pg.on_fire(fire)

def ev_pad(m, hret0, hret1):
    """tutti i pad = ascolto dell'anteprima (o del sample corrente se non c'e'), come in TRIM"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on)
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop()
    if DIAG: far_call(m, 'diagmsg2')
    far_bra(m, hret1)
    a.deflabel(on)
    ld(m, PREV, 5); a.tst_rr(5, 5); hv = m.newlabel('hv'); a.bf_lbl(hv); m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 5); a.deflabel(hv)
    a.tst_rr(5, 5); hz = m.newlabel('hz'); a.bf_lbl(hz); far_bra(m, hret1); a.deflabel(hz)
    m.imm32(PLAYER, 4); a.mov_imm(0, 6); a.mov_imm(0x7F, 7); m.imm32(AUD_PLAY, 2); a.jsr(2); a.nop()
    if DIAG: far_call(m, 'diagmsg')
    far_bra(m, hret1)
pg.on_event(EV_PAD, ev_pad)

def on_cancel(m, hret1):
    """CANCEL con la domanda di salvataggio aperta: annulla la domanda (non esce dalla pagina)"""
    a = m.a
    ld(m, CONF, 0); a.tst_rr(0, 0); cx = m.newlabel('cx'); a.bt_lbl(cx)
    sti(m, 0, CONF); sti(m, 1, SHOWMSG); far_bra(m, hret1)
    a.deflabel(cx)
pg.cancel_fn = on_cancel

def inactive(m):
    """un'altra pagina e' attiva: dopo LEAVE_FIRES fire di fila = uscita vera -> via l'anteprima non confermata"""
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    if DIAG: ld(m, LCNT, 0); a.add_imm(1, 0); st(m, 0, LCNT)
    far_call(m, 'delprev'); sti(m, 0, ACTIVE); sti(m, 0, OUTCNT); sti(m, 0, CONF)
    a.deflabel(ix)
pg.on_inactive(inactive)

def doit(m):
    a = m.a
    def clamp(r, lo, hi):
        m.imm32(lo, 3); a.cmpge(3, r); l1 = m.newlabel('cl'); a.bt_lbl(l1); a.mov_rm_rn(3, r); a.deflabel(l1)
        m.imm32(hi, 3); a.cmpgt(3, r); l2 = m.newlabel('ch'); a.bf_lbl(l2); a.mov_rm_rn(3, r); a.deflabel(l2)
    def clip0():                         # r0 in [-32768, 32767] (r6 = 32767, r7 = -32768)
        l1 = m.newlabel('k1'); l2 = m.newlabel('k2')
        a.cmpgt(6, 0); a.bf_lbl(l1); a.mov_rm_rn(6, 0); a.deflabel(l1)
        a.cmpgt(0, 7); a.bf_lbl(l2); a.mov_rm_rn(7, 0); a.deflabel(l2)
    def absr0():
        l = m.newlabel('ab'); a.cmppz(0); a.bt_lbl(l); a.neg(0, 0); a.deflabel(l)
    def rd_src():                        # r0 = *r9++ (originale)
        a.movw_ld(9, 0); a.add_imm(2, 9)
    def wr_dst(loop):                    # *r10++ = r0, ripete r11 volte
        a.movw_st(0, 10); a.add_imm(2, 10); a.dt(11); a.bf_lbl(loop)

    a.deflabel('__cstart')
    # ---- DO IT: NO FX -> niente; 1a pressione -> "Save? DO IT=yes" (resta); 2a -> salva; CANCEL/cambi -> annulla ----
    a.bsr_lbl('chcount'); a.nop(); a.tst_rr(0, 0); hfx = m.newlabel('hfx'); m.bf_far(hfx)
    pg.emit_message(m, "Choose an effect"); far_bra(m, 'fxret')
    a.deflabel(hfx)
    ld(m, CONF, 0); a.tst_rr(0, 0); cyes = m.newlabel('cyes'); m.bf_far(cyes)
    sti(m, 1, CONF); pg.emit_message(m, "Save? DO IT=yes"); m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)   # resta
    far_bra(m, 'fxret')
    a.deflabel(cyes); sti(m, 0, CONF)
    # ---- confermato: finisce l'anteprima, poi diventa un sample vero (corrente) ----
    ld(m, PEND, 0); a.tst_rr(0, 0); np_ = m.newlabel('np'); a.bt_lbl(np_); sti(m, 0, PEND); a.bsr_lbl('rebuild'); a.nop(); a.deflabel(np_)
    fin = m.newlabel('fin'); a.deflabel(fin)
    ld(m, BUSY, 0); a.tst_rr(0, 0); fdn = m.newlabel('fdn'); a.bt_lbl(fdn); a.bsr_lbl('step'); a.nop(); a.bra_lbl(fin); a.nop()
    a.deflabel(fdn)
    ld(m, PREV, 8); a.tst_rr(8, 8); hp = m.newlabel('hp'); m.bf_far(hp)
    ld(m, ERR, 0); a.tst_rr(0, 0); he = m.newlabel('he'); a.bf_lbl(he); sti(m, ERR_NOSMP, ERR); a.deflabel(he)
    a.bsr_lbl('errmsg'); a.nop(); far_bra(m, 'fxret')
    a.deflabel(hp)
    m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1); st(m, 8, LSRC); st(m, 8, SRC); sti(m, 0, PREV)
    a.bsr_lbl('chreset'); a.nop()                                                         # salvato: catena azzerata
    pg.emit_message(m, "Saved new sample")
    far_bra(m, 'fxret')
    m.pool_jump()

    # ---- fxlim: r9 = effetto -> min/max del modello AMT + valore proposto; fxlims: solo min/max (cambio slot) ----
    for name_, with_default in (('fxlim', True), ('fxlims', False)):
        a.deflabel(name_); a.stsl_pr()
        m.imm32(pg.addr['values'][AMT_FID], 1); a.movl_ld_disp(1, 0x24, 2)        # r2 = modello del campo AMT
        fxe = m.newlabel('fxe')
        for k, (lo, hi, dv) in enumerate(FX_AMT):
            nx = m.newlabel('fl'); a.mov_imm(k, 3); a.cmpeq(3, 9); m.bf_far(nx)
            m.imm32(lo, 0); a.movl_st_disp(0, 0x18, 2); m.imm32(hi, 0); a.movl_st_disp(0, 0x1C, 2)
            if with_default: sti(m, dv, AMT)
            a.bra_lbl(fxe); a.nop(); a.deflabel(nx); m.pool_jump()
        a.deflabel(fxe); m.imm32(_EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
        a.ldsl_pr(); a.rts(); a.nop()
        m.pool_jump()

    # ---- chcount: r0 = slot attivi (effetto != NO FX) ----
    a.deflabel('chcount')
    a.mov_imm(0, 0); m.imm32(CHFX[0], 1); a.mov_imm(NSLOT, 3)
    ccl = m.newlabel('ccl'); a.deflabel(ccl); a.movl_ld(1, 2); a.tst_rr(2, 2); cz = m.newlabel('cz'); a.bt_lbl(cz); a.add_imm(1, 0); a.deflabel(cz)
    a.add_imm(4, 1); a.dt(3); a.bf_lbl(ccl)
    a.rts(); a.nop()
    m.pool_jump()
    # ---- chreset: catena azzerata, slot 1, campi a NO FX ----
    a.deflabel('chreset'); a.stsl_pr()
    m.imm32(CHFX[0], 1); a.mov_imm(2 * NSLOT, 3); a.mov_imm(0, 0)                # CHFX e CHAMT sono contigui
    crl = m.newlabel('crl'); a.deflabel(crl); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(3); a.bf_lbl(crl)
    sti(m, 1, SLOTN); sti(m, 1, LSLOT); m.imm32(FXS, 1); a.mov_imm(NOFX, 0); a.movb_st(0, 1); sti(m, 0, AMT); sti(m, NOFX, LFXL)
    a.mov_imm(NOFX, 9); a.bsr_lbl('fxlims'); a.nop()
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    if DIAG:
        MT = pg.addr['msgt']
        def putc(pos, ch): m.imm32(MT + pos, 1); a.mov_imm(ord(ch), 0); a.movb_st(0, 1)
        def puts(text):
            for pos, ch in enumerate(text): putc(pos, ch)
            m.imm32(MT + 16, 1); a.mov_imm(0, 0); a.movb_st(0, 1)
        def putdec(pos, nd):             # r4 = valore (>= 0)
            for k in reversed(range(nd)):
                a.mov_imm(10, 5); a.bsr_lbl('__udiv'); a.nop()
                a.add_imm(0x30, 1); m.imm32(MT + pos + k, 3); a.movb_st(1, 3); a.mov_rm_rn(0, 4)
        def mod100(slot):                # r4 = *slot % 100
            ld(m, slot, 4); a.mov_imm(100, 5); a.bsr_lbl('__udiv'); a.nop(); a.mov_rm_rn(1, 4)
        # ---- dscan: r8 = orig, r9 = anteprima (0 = solo orig) -> DG1 = picco orig %, DG2 = picco antepr %, r0 = % diversi ----
        a.deflabel('dscan'); a.stsl_pr()
        a.movl_ld_disp(8, 0x20, 2); a.movl_ld_disp(8, 0x24, 11); a.sub(2, 11)            # r11 = n
        a.mov_imm(0, 12); a.mov_imm(0, 13); a.mov_imm(0, 14)                             # picchi, diversi
        a.cmppl(11); dsx = m.newlabel('dsx'); m.bf_far(dsx)
        m.imm32(DIAG_WIN, 1); a.cmpgt(1, 11); dw = m.newlabel('dw'); a.bf_lbl(dw); a.mov_rm_rn(1, 11); a.deflabel(dw)
        st(m, 11, DG1)
        a.shll(2); m.imm32(0x100, 1); a.add_rr(1, 2); a.mov_rm_rn(8, 10); a.add_rr(2, 10)   # r10 = &orig[start]
        a.tst_rr(9, 9); dnp = m.newlabel('dnp'); a.bt_lbl(dnp); a.add_rr(2, 9); a.deflabel(dnp)   # r9 = &antepr[start] (o 0)
        dl_ = m.newlabel('dl'); a.deflabel(dl_)
        a.movw_ld(10, 0); a.add_imm(2, 10); a.cmppz(0); p1 = m.newlabel('p1'); a.bt_lbl(p1); a.neg(0, 0); a.deflabel(p1)
        a.cmpgt(12, 0); p2 = m.newlabel('p2'); a.bf_lbl(p2); a.mov_rm_rn(0, 12); a.deflabel(p2)
        a.tst_rr(9, 9); dnx = m.newlabel('dnx'); a.bt_lbl(dnx)
        a.mov_rm_rn(10, 3); a.add_imm(-2, 3); a.movw_ld(3, 3); a.movw_ld(9, 1); a.add_imm(2, 9)
        a.cmpeq(1, 3); p3 = m.newlabel('p3'); a.bt_lbl(p3); a.add_imm(1, 14); a.deflabel(p3)
        a.mov_rm_rn(1, 0); a.cmppz(0); p4 = m.newlabel('p4'); a.bt_lbl(p4); a.neg(0, 0); a.deflabel(p4)
        a.cmpgt(13, 0); p5 = m.newlabel('p5'); a.bf_lbl(p5); a.mov_rm_rn(0, 13); a.deflabel(p5)
        a.deflabel(dnx); a.dt(11); a.bf_lbl(dl_)
        a.deflabel(dsx)
        for r_, slot in ((12, DG1), (13, DG2)):                                          # picchi in % (0..99)
            a.mov_imm(100, 1); a.mul_l(1, r_); a.sts_macl(r_); a.mov_imm(-15, 1); a.shad(1, r_)
            a.mov_imm(99, 1); a.cmpgt(1, r_); q = m.newlabel('q'); a.bf_lbl(q); a.mov_rm_rn(1, r_); a.deflabel(q)
        ld(m, DG1, 5); a.tst_rr(5, 5); dz_ = m.newlabel('dz'); a.mov_imm(0, 0); a.bt_lbl(dz_)
        a.mov_rm_rn(14, 4); a.mov_imm(100, 1); a.mul_l(1, 4); a.sts_macl(4); a.bsr_lbl('__udiv'); a.nop()   # % diversi
        a.mov_imm(99, 1); a.cmpgt(1, 0); q2 = m.newlabel('q2'); a.bf_lbl(q2); a.mov_rm_rn(1, 0); a.deflabel(q2)
        a.deflabel(dz_); st(m, 12, DG1); st(m, 13, DG2)
        a.ldsl_pr(); a.rts(); a.nop()
        m.pool_jump()
        # ---- diagmsg (pad premuto): "R12C12L03 D06 S*" (R rebuild, C copie, L uscite, D % diversi, motivo, * in corso) ----
        a.deflabel('diagmsg'); a.stsl_pr()
        puts("R  C  L   D   ? ")
        mod100(RCNT); putdec(1, 2); mod100(CCNT); putdec(4, 2); mod100(LCNT); putdec(7, 2)
        ld(m, SRC, 8); a.tst_rr(8, 8); ds = m.newlabel('ds'); a.bf_lbl(ds); m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 8); a.deflabel(ds)
        a.tst_rr(8, 8); dmx = m.newlabel('dmx'); m.bt_far(dmx)
        a.bsr_lbl('icinv'); a.nop(); ld(m, PREV, 9); cached_call(m, 'dscan')
        a.mov_rm_rn(0, 4); putdec(11, 2)
        a.deflabel(dmx)
        ld(m, WHY, 0); a.tst_rr(0, 0); dw0 = m.newlabel('dw0'); a.bt_lbl(dw0); m.imm32(pg.addr['msgt'] + 14, 1); a.movb_st(0, 1); a.deflabel(dw0)
        ld(m, BUSY, 0); a.tst_rr(0, 0); db = m.newlabel('db'); a.bt_lbl(db); putc(15, '*'); a.deflabel(db)
        pg._hcall(m, '__showmsg')
        a.ldsl_pr(); a.rts(); a.nop()
        m.pool_jump()
        # ---- diagmsg2 (pad rilasciato): "Q047% C03 L01 Z0" ----
        a.deflabel('diagmsg2'); a.stsl_pr()
        puts("Q   % C   L   Z ")
        a.mov_imm(100, 4); ld(m, BUSY, 0); a.tst_rr(0, 0); qd = m.newlabel('qd'); m.bt_far(qd)
        ld(m, SRC, 8); a.movl_ld_disp(8, 0x20, 2); a.movl_ld_disp(8, 0x24, 5); a.sub(2, 5)   # r5 = n
        ld(m, POS, 4); a.sub(2, 4)                                                           # r4 = fatti
        a.shlr8(4); a.shlr8(5); a.add_imm(1, 5); a.mov_imm(100, 1); a.mul_l(1, 4); a.sts_macl(4)
        a.bsr_lbl('__udiv'); a.nop(); a.mov_rm_rn(0, 4)
        a.deflabel(qd); putdec(1, 3)
        mod100(CCNT); putdec(7, 2); mod100(LCNT); putdec(11, 2)
        ld(m, SRC, 8); a.tst_rr(8, 8); zz = m.newlabel('zz'); a.mov_imm(0, 4); a.bt_lbl(zz)
        m.imm32(0x3E, 1); a.add_rr(8, 1); a.movw_ld(1, 4); a.extu_w(4, 4)
        a.deflabel(zz)
        a.mov_imm(9, 1); a.cmphs(4, 1); zq = m.newlabel('zq'); a.bt_lbl(zq); a.mov_imm(9, 4); a.deflabel(zq)
        putdec(15, 1)
        a.bsr_lbl('__showmsg'); a.nop()
        a.ldsl_pr(); a.rts(); a.nop()
        m.pool_jump()

    # ---- icinv: invalida (scrittura associativa sull'address array della cache SH3, 0xF0000000 | entry | A=8) le righe
    #      del nostro codice [__cstart, +CACHE_SPAN): tocca solo righe con il NOSTRO tag, mai dirty (il codice non si scrive
    #      via P1). Serve perche' l'HUB puo' reinstallare codice diverso nello stesso slot (scritto via P2). Gira da P2. ----
    a.deflabel('icinv')
    a.movl_pool('__cstart', 4); m.imm32(0xFFFFFFF0, 1); a.and_rr(1, 4)
    m.imm32(CACHE_SPAN // 16 + 1, 5); m.imm32(0xF0000008, 6); m.imm32(0x1FFFFC00, 7); m.imm32(0xFF0, 3)
    icl = m.newlabel('icl'); a.deflabel(icl)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0)
    a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(icl)
    a.rts(); a.nop()
    m.pool_jump()

    # ---- errmsg: messaggio per ERR != 0 ----
    a.deflabel('errmsg'); a.stsl_pr()
    ld(m, ERR, 8); a.tst_rr(8, 8); m.bt_far('__emx')
    a.mov_imm(3, 3); a.cmpeq(3, 8); nm = m.newlabel('nm'); a.bf_lbl(nm)
    pg.emit_message(m, "Memory full"); a.bra_lbl('__emx'); a.nop()
    a.deflabel(nm); a.mov_imm(ERR_SILENT, 3); a.cmpeq(3, 8); ns = m.newlabel('ns'); a.bf_lbl(ns)
    pg.emit_message(m, "Only silence"); a.bra_lbl('__emx'); a.nop()
    a.deflabel(ns); a.mov_imm(ERR_NOSMP, 3); a.cmpeq(3, 8); nn = m.newlabel('nn'); a.bf_lbl(nn)
    pg.emit_message(m, "No sample"); a.bra_lbl('__emx'); a.nop()
    a.deflabel(nn); pg.emit_message_dec(m, "Error ", ERR, "")
    a.deflabel('__emx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- delprev: ferma l'ascolto e cancella l'anteprima (ultimo blocco), corrente = SRC ----
    #      2026-10-17 (PIANO.md 1.4): PREV e SRC sono puntatori salvati; se la lista sample e' cambiata nel frattempo (LOAD,
    #      delete: i blocchi si spostano) potrebbero non essere piu' blocchi: solo se lo sono si cancella / si rimette.
    a.deflabel('delprev'); a.stsl_pr()
    sti(m, 0, BUSY)
    ld(m, PREV, 9); a.tst_rr(9, 9); m.bt_far('__dpx')
    m.imm32(PLAYER, 4); m.imm32(AUD_STOP, 2); a.jsr(2); a.nop()
    ld(m, PREV, 4); a.bsr_lbl('blkok'); a.nop(); dpn = m.newlabel('dpn'); m.bf_far(dpn)
    ld(m, PREV, 9); m.imm32(MGR, 4); a.movl_st_disp(9, 8, 4); m.imm32(DELETE_CMD, 2); a.jsr(2); a.nop()
    a.deflabel(dpn); sti(m, 0, PREV)
    ld(m, SRC, 4); a.tst_rr(4, 4); m.bt_far('__dpx'); a.bsr_lbl('blkok'); a.nop(); m.bf_far('__dpx')
    ld(m, SRC, 8); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1)
    a.deflabel('__dpx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- blkok: T = r4 e' l'inizio di un blocco della lista sample (MGR[0] .. MGR[1]). Foglia, r0-r3 ----
    a.deflabel('blkok')
    m.imm32(MGR, 1); a.movl_ld(1, 2); a.movl_ld_disp(1, 4, 3)                              # r2 = blocco, r3 = limite
    bkl = m.newlabel('bkl'); bkf = m.newlabel('bkf'); a.deflabel(bkl)
    a.cmpeq(4, 2); a.bt_lbl(bkf)
    a.cmphs(3, 2); bkn = m.newlabel('bkn'); a.bt_lbl(bkn)                                   # oltre il limite: no
    a.movl_ld(2, 0); a.cmppl(0); a.bf_lbl(bkn); a.add_rr(0, 2); a.bra_lbl(bkl); a.nop()   # size <= 0: fine lista
    a.deflabel(bkn); a.rts(); a.clrt()
    a.deflabel(bkf); a.rts(); a.sett()
    m.pool_jump()

    # ---- rebuild: anteprima della CATENA su LSRC: (ri)crea il blocco se serve, poi le passate a pezzi ----
    #   passata 0 = copia originale -> anteprima (solo se il blocco e' riusato); poi una passata per ogni slot attivo
    #   (TRIM SIL escluso: con TRIM l'anteprima e' l'Extract della parte sopra soglia, rifatta a ogni cambio)
    a.deflabel('rebuild'); a.stsl_pr()
    a.bsr_lbl('icinv'); a.nop()
    if DIAG: ld(m, RCNT, 0); a.add_imm(1, 0); st(m, 0, RCNT)
    sti(m, 0, ERR); sti(m, 0, BUSY)
    ld(m, LSRC, 8); a.tst_rr(8, 8); m.bt_far('__rbx'); st(m, 8, SRC)
    # analisi della catena: attivi, TRIM (primo) e sua soglia
    sti(m, 0, CHTRIM); a.mov_imm(0, 13); m.imm32(CHFX[0], 11); a.mov_imm(NSLOT, 12)
    cl = m.newlabel('cl'); a.deflabel(cl)
    a.movl_ld(11, 0); a.tst_rr(0, 0); cn = m.newlabel('cn'); m.bt_far(cn); a.add_imm(1, 13)
    a.mov_imm(TRIM, 3); a.cmpeq(3, 0); m.bf_far(cn)
    ld(m, CHTRIM, 0); a.tst_rr(0, 0); m.bf_far(cn)
    sti(m, 1, CHTRIM); a.movl_ld_disp(11, 4 * NSLOT, 0); st(m, 0, TRAMT)                  # CHAMT[k] = CHFX[k] + 4*NSLOT
    a.deflabel(cn); a.add_imm(4, 11); a.dt(12); m.bf_far(cl)
    st(m, 13, ACTN)
    a.tst_rr(13, 13); nfx = m.newlabel('nfx'); a.bf_lbl(nfx); a.bsr_lbl('delprev'); a.nop(); far_bra(m, '__rbx'); a.deflabel(nfx)
    # sample di un addon: niente anteprima (via quella vecchia)
    ld(m, SRC, 8); m.imm32(0x100, 2); a.add_rr(8, 2); a.movl_ld(2, 0)
    m.imm32(MAGIC_ADDON, 3); a.cmpeq(3, 0); isad = m.newlabel('isad'); a.bt_lbl(isad)
    m.imm32(MAGIC_HUB, 3); a.cmpeq(3, 0); notad = m.newlabel('notad'); a.bf_lbl(notad)
    a.deflabel(isad); a.bsr_lbl('delprev'); a.nop(); far_bra(m, '__rbx')
    a.deflabel(notad)
    m.pool_jump()
    rc = m.newlabel('rc')
    ld(m, SRC, 8); ld(m, PREV, 9); a.tst_rr(9, 9); m.bt_far(rc)
    ld(m, PSRC, 2); a.cmpeq(2, 8); m.bf_far(rc)
    ld(m, CHTRIM, 2); a.tst_rr(2, 2); m.bf_far(rc); ld(m, PTRIM, 2); a.tst_rr(2, 2); m.bf_far(rc)
    a.movl_ld(8, 2); a.movl_ld(9, 3); a.cmpeq(2, 3); m.bf_far(rc)                    # stessa dimensione del blocco
    sti(m, 0, STAGE); far_bra(m, '__rbgo')                                            # riuso: passata 0 = ricopia
    m.pool_jump()
    a.deflabel(rc)
    a.bsr_lbl('delprev'); a.nop()
    ld(m, SRC, 8); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1)                          # corrente = originale
    ld(m, CHTRIM, 0); a.tst_rr(0, 0); rcp = m.newlabel('rcp'); a.bt_lbl(rcp)
    a.bsr_lbl('trim'); a.nop(); rnew = m.newlabel('rnew'); a.bra_lbl(rnew); a.nop()
    a.deflabel(rcp)
    if DIAG: ld(m, CCNT, 0); a.add_imm(1, 0); st(m, 0, CCNT)
    m.os_extract(8, COPY_CMD)                                                     # r8 = SRC (corrente)
    a.tst_rr(0, 0); cok = m.newlabel('cok'); a.bt_lbl(cok); st(m, 0, ERR); a.deflabel(cok)
    a.deflabel(rnew)
    ld(m, ERR, 0); a.tst_rr(0, 0); rok = m.newlabel('rok'); a.bt_lbl(rok)
    ld(m, SRC, 8); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1); a.bsr_lbl('errmsg'); a.nop(); far_bra(m, '__rbx')
    a.deflabel(rok)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 8, 9); st(m, 9, PREV)                          # nuovo blocco = anteprima
    ld(m, SRC, 8); m.imm32(MGR, 1); a.movl_st_disp(8, 8, 1); st(m, 8, PSRC); ld(m, CHTRIM, 0); st(m, 0, PTRIM)
    sti(m, 0, STAGE); a.bsr_lbl('nextstage'); a.nop(); far_bra(m, '__rbx')           # blocco nuovo: gia' "grezzo"
    m.pool_jump()
    a.deflabel('__rbgo')
    ld(m, PREV, 0); st(m, 0, RB); sti(m, NOFX, FXI); sti(m, 0, KAMT)                  # passata 0: copia
    ld(m, PREV, 8); a.movl_ld_disp(8, 0x20, 2); st(m, 2, POS); sti(m, 1, BUSY)
    a.deflabel('__rbx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- nextstage: prossimo slot attivo (non TRIM) dopo STAGE -> FXI/KAMT, POS = inizio; nessuno -> finito ----
    a.deflabel('nextstage'); a.stsl_pr()
    ld(m, PREV, 0); st(m, 0, RB)
    nl = m.newlabel('nl'); nx = m.newlabel('nx'); a.deflabel(nl)
    ld(m, STAGE, 9); a.mov_imm(NSLOT, 0); a.cmpge(0, 9); m.bt_far(nx)                  # STAGE >= NSLOT: finito
    a.add_imm(1, 9); st(m, 9, STAGE)
    a.mov_rm_rn(9, 0); a.add_imm(-1, 0); a.shll2(0); m.imm32(CHFX[0], 1); a.add_rr(0, 1); a.movl_ld(1, 2)
    a.tst_rr(2, 2); m.bt_far(nl); a.mov_imm(TRIM, 3); a.cmpeq(3, 2); m.bt_far(nl)
    a.movl_ld_disp(1, 4 * NSLOT, 3); st(m, 2, FXI); st(m, 3, KAMT)                       # (st usa r1: prima leggo)
    ld(m, RB, 8); a.movl_ld_disp(8, 0x20, 2); st(m, 2, POS); sti(m, 1, BUSY)
    a.ldsl_pr(); a.rts(); a.nop()
    a.deflabel(nx); sti(m, 0, BUSY)
    ld(m, PREV, 4); a.tst_rr(4, 4); ng = m.newlabel('ng'); a.bt_lbl(ng); m.imm32(LOOP_GUARD, 2); a.jsr(2); a.nop(); a.deflabel(ng)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- step: un pezzo [POS, POS+n) di entrambi i canali della passata corrente ----
    a.deflabel('step'); a.stsl_pr()
    ld(m, BUSY, 0); a.tst_rr(0, 0); m.bt_far('__stx')
    ld(m, RB, 8); a.movl_ld_disp(8, 0x24, 2); ld(m, POS, 3); st(m, 3, KPOS)
    a.sub(3, 2)                                                                     # r2 = end - POS
    a.cmppl(2); sgo = m.newlabel('sgo'); a.bt_lbl(sgo); a.bra_lbl('__stdone'); a.nop(); a.deflabel(sgo)
    m.imm32(CHUNK, 3); a.cmpgt(3, 2); sn = m.newlabel('sn'); a.bf_lbl(sn); a.mov_rm_rn(3, 2); a.deflabel(sn)
    st(m, 2, KN)
    for chan in (0, 1):
        if chan:
            ld(m, RB, 8); m.imm32(0x92, 2); a.add_rr(8, 2); a.movw_ld(2, 0); a.extu_w(0, 0); a.mov_imm(2, 3); a.cmpeq(3, 0)
            smono = m.newlabel('smono'); m.bf_far(smono)
        ld(m, RB, 8); ld(m, KPOS, 2); a.shll(2); m.imm32(0x100, 3); a.add_rr(3, 2)
        if chan: a.movl_ld_disp(8, 0x1C, 3); a.shll(3); a.add_rr(3, 2)                 # offset del canale R
        ld(m, PREV, 3); a.add_rr(2, 3); st(m, 3, KDSTP)
        ld(m, STAGE, 0); a.tst_rr(0, 0); ld(m, SRC, 3); ss = m.newlabel('ss'); a.bt_lbl(ss); ld(m, PREV, 3); a.deflabel(ss)
        a.add_rr(2, 3); st(m, 3, KSRCP)                                                  # passata 0: dall'originale
        sti(m, chan, KCHAN)
        cached_call(m, 'kern')
        if chan: a.deflabel(smono)
        m.pool_jump()
    ld(m, POS, 2); ld(m, KN, 3); a.add_rr(3, 2); st(m, 2, POS)
    ld(m, RB, 8); a.movl_ld_disp(8, 0x24, 3); a.cmpge(3, 2); m.bf_far('__stx')     # POS >= end -> passata finita
    a.deflabel('__stdone'); a.bsr_lbl('nextstage'); a.nop()
    a.deflabel('__stx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- copyn: r11 campioni r9 -> r10 (r11 = 0: niente) ----
    a.deflabel('copyn'); a.tst_rr(11, 11); cpx = m.newlabel('cpx'); a.bt_lbl(cpx)
    cpl = m.newlabel('cpl'); a.deflabel(cpl); rd_src(); wr_dst(cpl)
    a.deflabel(cpx); a.rts(); a.nop()

    # ================= kern: dst[KPOS..+KN) = FXI(src) per un canale =================
    #   r9 = src, r10 = dst, r11 = n; regione [start, end) dell'originale
    a.deflabel('kern'); a.stsl_pr()
    ld(m, KSRCP, 9); ld(m, KDSTP, 10); ld(m, KN, 11)
    a.tst_rr(11, 11); m.bt_far('kret')
    m.imm32(32767, 6); m.imm32(-32768, 7)
    ld(m, KAMT, 14); ld(m, FXI, 0)
    lbl = {k: m.newlabel('fx%d' % k) for k in (FADE_IN, GAIN, CRUSH, DOWNS, DRIVE, VINYL)}
    a.mov_imm(FADE_OUT, 3); a.cmpeq(3, 0); m.bt_far(lbl[FADE_IN])
    for k in (FADE_IN, GAIN, CRUSH, DOWNS, DRIVE, VINYL):
        a.mov_imm(k, 3); a.cmpeq(3, 0); m.bt_far(lbl[k])
    a.bsr_lbl('copyn'); a.nop(); a.bra_lbl('kret'); a.nop()
    m.pool_jump()

    # --- FADE IN / FADE OUT: F = N*AMT/100 campioni da fs (in: start, out: end-F); fuori = copia ---
    a.deflabel(lbl[FADE_IN])
    clamp(14, 1, 100)
    ld(m, RB, 8); a.movl_ld_disp(8, 0x24, 4); a.movl_ld_disp(8, 0x20, 2); a.sub(2, 4)   # r4 = N
    a.mov_imm(100, 5); a.bsr_lbl('__udiv'); a.nop()                                      # r0 = N/100, r1 = N%100
    a.mul_l(14, 0); a.sts_macl(13)
    a.mov_rm_rn(1, 4); a.mul_l(14, 4); a.sts_macl(4); a.mov_imm(100, 5); a.bsr_lbl('__udiv'); a.nop(); a.add_rr(0, 13)
    fnz = m.newlabel('fnz'); a.tst_rr(13, 13); a.bf_lbl(fnz); a.mov_imm(1, 13); a.deflabel(fnz)   # r13 = F >= 1
    m.imm32(1 << 24, 4); a.mov_rm_rn(13, 5); a.bsr_lbl('__udiv'); a.nop(); a.mov_rm_rn(0, 12)   # r12 = passo
    ld(m, RB, 8); a.movl_ld_disp(8, 0x20, 14)                                           # fs = start, g0 = 0
    ld(m, FXI, 0); a.mov_imm(FADE_OUT, 3); a.cmpeq(3, 0); fin_ = m.newlabel('fin'); a.bf_lbl(fin_)
    a.movl_ld_disp(8, 0x24, 14); a.sub(13, 14); a.neg(12, 12)                           # fs = end - F, passo < 0
    a.deflabel(fin_); st(m, 14, KT1)                                                     # KT1 = fs
    # lo = max(KPOS, fs), hi = min(KPOS+n, fs+F); k1 = clamp(lo-KPOS, 0, n); k2 = clamp(hi-lo, 0, n-k1)
    ld(m, KPOS, 2); a.mov_rm_rn(14, 3); a.cmpge(2, 3); l1_ = m.newlabel('lo'); a.bt_lbl(l1_); a.mov_rm_rn(2, 3); a.deflabel(l1_)   # r3 = lo
    st(m, 3, KT2)
    a.mov_rm_rn(3, 11); a.sub(2, 11)                                                     # r11 = k1 = lo - KPOS
    ld(m, KN, 2); a.cmpgt(2, 11); k1c = m.newlabel('k1c'); a.bf_lbl(k1c); a.mov_rm_rn(2, 11); a.deflabel(k1c)
    a.bsr_lbl('copyn_keep'); a.nop()                                                     # copia k1 (r11 intatto dopo)
    # r11 = k1 ancora; k2 = min(hi, KPOS+n) - lo, >= 0, <= n - k1
    ld(m, KT1, 2); a.add_rr(13, 2)                                                       # r2 = fs + F
    ld(m, KPOS, 3); ld(m, KN, 0); a.add_rr(0, 3)                                         # r3 = KPOS + n
    a.cmpgt(3, 2); hi_ = m.newlabel('hi'); a.bf_lbl(hi_); a.mov_rm_rn(3, 2); a.deflabel(hi_)   # r2 = hi
    ld(m, KT2, 3); a.sub(3, 2)                                                           # r2 = hi - lo
    a.cmppz(2); k2p = m.newlabel('k2p'); a.bt_lbl(k2p); a.mov_imm(0, 2); a.deflabel(k2p)
    ld(m, KN, 0); a.sub(11, 0); a.cmpgt(0, 2); k2c = m.newlabel('k2c'); a.bf_lbl(k2c); a.mov_rm_rn(0, 2); a.deflabel(k2c)
    ld(m, KN, 0); a.sub(11, 0); a.sub(2, 0); st(m, 0, KT1)                               # KT1 = k3 = n - k1 - k2
    a.mov_rm_rn(2, 11)                                                                   # r11 = k2
    # g = g0 + (lo - fs)*passo;  g0 = 0 (in) / 2^24 (out)
    ld(m, KT2, 3); ld(m, RB, 8); a.movl_ld_disp(8, 0x20, 2)                             # (fs ricalcolato sotto)
    ld(m, FXI, 0); a.mov_imm(FADE_OUT, 1); a.cmpeq(1, 0); gin = m.newlabel('gin'); a.bf_lbl(gin)
    a.movl_ld_disp(8, 0x24, 2); a.sub(13, 2); a.mov_rm_rn(3, 14); a.sub(2, 14); a.mul_l(12, 14); a.sts_macl(14)
    m.imm32(1 << 24, 0); a.add_rr(0, 14); gok = m.newlabel('gok'); a.bra_lbl(gok); a.nop()
    a.deflabel(gin); a.mov_rm_rn(3, 14); a.sub(2, 14); a.mul_l(12, 14); a.sts_macl(14)
    a.deflabel(gok)
    a.tst_rr(11, 11); fz = m.newlabel('fz'); a.bt_lbl(fz)
    a.mov_imm(-9, 4); a.mov_imm(-15, 5)
    fl = m.newlabel('fl'); a.deflabel(fl)
    rd_src(); a.mov_rm_rn(14, 2); a.shad(4, 2); a.mul_l(2, 0); a.sts_macl(0); a.shad(5, 0)
    a.add_rr(12, 14); wr_dst(fl)
    a.deflabel(fz)
    ld(m, KT1, 11); a.bsr_lbl('copyn'); a.nop()                                          # k3
    a.bra_lbl('kret'); a.nop()
    m.pool_jump()
    # copyn_keep: come copyn ma lascia r11 invariato
    a.deflabel('copyn_keep'); a.stsl_pr(); a.movl_st_dec(11, 15); a.bsr_lbl('copyn'); a.nop(); a.movl_ld_inc(15, 11); a.ldsl_pr(); a.rts(); a.nop()

    # --- GAIN: g = AMT*4096/100 (Q12), clip ---
    a.deflabel(lbl[GAIN])
    clamp(14, 0, 400)
    a.mov_rm_rn(14, 4); m.imm32(4096, 2); a.mul_l(2, 4); a.sts_macl(4); a.mov_imm(100, 5); a.bsr_lbl('__udiv'); a.nop(); a.mov_rm_rn(0, 12)
    a.mov_imm(-12, 13)
    gl = m.newlabel('gl'); a.deflabel(gl)
    rd_src(); a.mul_l(12, 0); a.sts_macl(0); a.shad(13, 0); clip0(); wr_dst(gl)
    a.bra_lbl('kret'); a.nop()
    m.pool_jump()

    # --- BITCRUSH: tiene AMT bit ---
    a.deflabel(lbl[CRUSH])
    clamp(14, 1, 100)                                                    # AMT quantita': bit tolti = (AMT*15+50)/100
    a.mov_rm_rn(14, 4); a.mov_imm(15, 1); a.mul_l(1, 4); a.sts_macl(4); a.add_imm(50, 4); a.mov_imm(100, 5); a.bsr_lbl('__udiv'); a.nop()
    a.mov_rm_rn(0, 3); a.mov_imm(-1, 12); a.shad(3, 12)
    bl_ = m.newlabel('bl'); a.deflabel(bl_)
    rd_src(); a.and_rr(12, 0); wr_dst(bl_)
    a.bra_lbl('kret'); a.nop()

    # --- DOWNSAMP: sample & hold ogni AMT campioni dall'inizio della regione (stato ricavato da KPOS) ---
    a.deflabel(lbl[DOWNS])
    clamp(14, 1, 64)
    a.mov_rm_rn(14, 12)
    ld(m, RB, 8); a.movl_ld_disp(8, 0x20, 2); ld(m, KPOS, 4); a.sub(2, 4); a.mov_rm_rn(12, 5); a.bsr_lbl('__udiv'); a.nop()   # r1 = c
    a.mov_imm(0, 13); a.tst_rr(1, 1); dz = m.newlabel('dz'); a.bt_lbl(dz)
    a.mov_rm_rn(12, 13); a.sub(1, 13)                                                    # contatore = f - c
    a.mov_rm_rn(1, 2); a.shll(2); a.mov_rm_rn(9, 3); a.sub(2, 3); a.movw_ld(3, 14)       # tenuto = src[KPOS - c]
    a.deflabel(dz)
    dl = m.newlabel('dl'); dh = m.newlabel('dh'); a.deflabel(dl)
    rd_src(); a.tst_rr(13, 13); a.bf_lbl(dh); a.mov_rm_rn(0, 14); a.mov_rm_rn(12, 13); a.deflabel(dh)
    a.add_imm(-1, 13); a.mov_rm_rn(14, 0); wr_dst(dl)
    a.bra_lbl('kret'); a.nop()
    m.pool_jump()

    # --- DRIVE: x = clip(s*(10+AMT)/10); y = (3x - x^3/2^30) / 2 ---
    a.deflabel(lbl[DRIVE])
    clamp(14, 0, 400)
    a.add_imm(10, 14); a.mov_rm_rn(14, 4); a.shll8(4); a.mov_imm(10, 5); a.bsr_lbl('__udiv'); a.nop(); a.mov_rm_rn(0, 12)   # Q8
    a.mov_imm(-15, 13); a.mov_imm(-8, 5)
    vl = m.newlabel('vl'); a.deflabel(vl)
    rd_src(); a.mul_l(12, 0); a.sts_macl(0); a.shad(5, 0); clip0()
    a.mov_rm_rn(0, 2); a.mul_l(0, 2); a.sts_macl(2); a.shad(13, 2)          # x^2 >> 15
    a.mul_l(0, 2); a.sts_macl(2); a.shad(13, 2)                             # x^3 >> 30
    a.mov_rm_rn(0, 3); a.add_rr(0, 3); a.add_rr(0, 3); a.sub(2, 3); a.shar(3); a.mov_rm_rn(3, 0); clip0()
    wr_dst(vl)
    a.bra_lbl('kret'); a.nop()
    m.pool_jump()

    # --- VINYL: fruscio + crackle (vedi FX_AMT) ---
    #   r9 src, r10 dst, r11 n, r12 AMT, r8 densita' (AMT/2), r13 = i + seme, r14 = rumore precedente, r5 = meta' click prec.
    a.deflabel(lbl[VINYL])
    clamp(14, 1, 100); a.mov_rm_rn(14, 12); a.mov_rm_rn(12, 8); a.shar(8)
    ld(m, KCHAN, 13); m.imm32(VSEEDC, 1); a.mul_l(1, 13); a.sts_macl(13); m.imm32(VSEED0, 1); a.add_rr(1, 13)
    ld(m, KPOS, 1); a.add_rr(1, 13)                                                     # r13 = KPOS + seme
    a.mov_rm_rn(13, 1); a.add_imm(-1, 1); a.bsr_lbl('vhash'); a.nop()                  # stato dal campione KPOS-1
    a.mov_rm_rn(1, 14); a.mov_imm(-20, 2); a.shad(2, 14)
    a.bsr_lbl('vclick'); a.nop(); a.mov_rm_rn(4, 5); a.shar(5)
    vl2 = m.newlabel('vl2'); a.deflabel(vl2)
    a.mov_rm_rn(13, 1); a.bsr_lbl('vhash'); a.nop(); a.add_imm(1, 13)                  # r1 = hash
    a.mov_rm_rn(1, 2); a.mov_imm(-20, 3); a.shad(3, 2)                                  # r2 = rumore
    a.mov_rm_rn(2, 3); a.add_rr(14, 3); a.shar(3); a.mov_rm_rn(2, 14)                   # r3 = media, prec = rumore
    a.mul_l(12, 3); a.sts_macl(3); a.mov_imm(-8, 2); a.shad(2, 3)                       # fruscio
    a.bsr_lbl('vclick'); a.nop()                                                        # r4 = click
    a.mov_rm_rn(4, 2); a.add_rr(5, 2); a.mov_rm_rn(4, 5); a.shar(5)                     # r2 = click + meta' prec
    rd_src(); a.add_rr(3, 0); a.add_rr(2, 0); clip0(); wr_dst(vl2)
    a.bra_lbl('kret'); a.nop()
    m.pool_jump()
    # vhash: r1 = hash(r1) (usa r2)
    a.deflabel('vhash')
    m.imm32(VK1, 2); a.mul_l(2, 1); a.sts_macl(1); a.mov_rm_rn(1, 2); a.shlr16(2); a.xor_rr(2, 1)
    m.imm32(VK2, 2); a.mul_l(2, 1); a.sts_macl(1); a.mov_rm_rn(1, 2); a.shlr16(2); a.xor_rr(2, 1)
    a.rts(); a.nop()
    m.pool_jump()
    # vclick: r1 = hash -> r4 = click (0 se nessuno); usa r2, r4; r8 = densita', r12 = AMT
    a.deflabel('vclick')
    a.mov_imm(0, 4); a.mov_rm_rn(1, 2); a.extu_w(2, 2); a.cmphs(8, 2); vcx = m.newlabel('vcx'); a.bt_lbl(vcx)   # d >= dens: niente
    a.mov_rm_rn(1, 4); a.shlr16(4); m.imm32(0x1FFF, 2); a.and_rr(2, 4); m.imm32(1000, 2); a.add_rr(2, 4)
    a.mul_l(12, 4); a.sts_macl(4); a.mov_imm(-6, 2); a.shad(2, 4)
    a.cmppz(1); a.bt_lbl(vcx); a.neg(4, 4)
    a.deflabel(vcx); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('kret'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ================= trim: Extract della parte sopra soglia =================
    a.deflabel('trim'); a.stsl_pr()
    ld(m, SRC, 8); a.movl_ld_disp(8, 0x20, 2); st(m, 2, S0); a.movl_ld_disp(8, 0x24, 2); st(m, 2, E0)
    ld(m, TRAMT, 4); clamp(4, 0, 1000); m.imm32(32767, 2); a.mul_l(2, 4); a.sts_macl(4); m.imm32(1000, 5); a.bsr_lbl('__udiv'); a.nop(); st(m, 0, THR)
    ld(m, E0, 2); st(m, 2, FIRST); ld(m, S0, 2); a.add_imm(-1, 2); st(m, 2, LAST)
    ld(m, SRC, 8); m.imm32(0x100, 2); a.add_rr(8, 2); st(m, 2, BASE); a.bsr_lbl('scan'); a.nop()
    ld(m, SRC, 8); m.imm32(0x92, 2); a.add_rr(8, 2); a.movw_ld(2, 0); a.extu_w(0, 0); a.mov_imm(2, 3); a.cmpeq(3, 0)
    tmono = m.newlabel('tmono'); a.bf_lbl(tmono)
    a.movl_ld_disp(8, 0x1C, 2); a.shll(2); a.add_rr(8, 2); m.imm32(0x100, 3); a.add_rr(3, 2); st(m, 2, BASE); a.bsr_lbl('scan'); a.nop()
    a.deflabel(tmono)
    ld(m, FIRST, 2); ld(m, E0, 3); a.cmpge(3, 2); tf = m.newlabel('tf'); a.bf_lbl(tf); sti(m, ERR_SILENT, ERR); a.bra_lbl('tr'); a.nop(); a.deflabel(tf)
    ld(m, SRC, 8); a.movl_st_disp(2, 0x20, 8); ld(m, LAST, 2); a.add_imm(1, 2); a.movl_st_disp(2, 0x24, 8)
    m.os_extract(8)
    a.tst_rr(0, 0); te = m.newlabel('te'); a.bt_lbl(te); st(m, 0, ERR); a.deflabel(te)
    ld(m, SRC, 8); ld(m, S0, 2); a.movl_st_disp(2, 0x20, 8); ld(m, E0, 2); a.movl_st_disp(2, 0x24, 8)   # originale intatto
    a.deflabel('tr'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # scan: su BASE[S0..E0) aggiorna FIRST = min(primo sopra soglia), LAST = max(ultimo sopra soglia)
    a.deflabel('scan')
    ld(m, S0, 12); ld(m, E0, 13); ld(m, THR, 14)
    ld(m, BASE, 10); a.mov_rm_rn(12, 0); a.shll(0); a.add_rr(0, 10); a.mov_rm_rn(12, 11)
    sf = m.newlabel('sf'); sfd = m.newlabel('sfd'); a.deflabel(sf)
    a.cmpge(13, 11); a.bt_lbl(sfd)
    a.movw_ld(10, 0); absr0(); a.cmpgt(14, 0); a.bt_lbl(sfd)
    a.add_imm(2, 10); a.add_imm(1, 11); a.bra_lbl(sf); a.nop()
    a.deflabel(sfd); ld(m, FIRST, 2); a.cmpgt(11, 2); n1 = m.newlabel('n1'); a.bf_lbl(n1); st(m, 11, FIRST); a.deflabel(n1)
    ld(m, BASE, 10); a.mov_rm_rn(13, 0); a.add_imm(-1, 0); a.shll(0); a.add_rr(0, 10); a.mov_rm_rn(13, 11); a.add_imm(-1, 11)
    sb = m.newlabel('sb'); sbd = m.newlabel('sbd'); a.deflabel(sb)
    a.cmpge(12, 11); a.bf_lbl(sbd)
    a.movw_ld(10, 0); absr0(); a.cmpgt(14, 0); a.bt_lbl(sbd)
    a.add_imm(-2, 10); a.add_imm(-1, 11); a.bra_lbl(sb); a.nop()
    a.deflabel(sbd); ld(m, LAST, 2); a.cmpgt(2, 11); n2 = m.newlabel('n2'); a.bf_lbl(n2); st(m, 11, LAST); a.deflabel(n2)
    a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('fxret')
pg.on_doit(doit)

# ---------------- modello Python di riferimento (bit-exact) ----------------
def s16(v): v &= 0xffff; return v - 0x10000 if v & 0x8000 else v
def s32(v): v &= 0xffffffff; return v - (1 << 32) if v & 0x80000000 else v
def clip(v): return max(-32768, min(32767, v))
M32 = 0xFFFFFFFF
def vhash(v):
    x = (v * VK1) & M32; x ^= x >> 16; x = (x * VK2) & M32; x ^= x >> 16; return x
def vclick(x, amt):
    if (x & 0xFFFF) >= (amt >> 1): return 0
    a_ = ((((x >> 16) & 0x1FFF) + 1000) * amt) >> 6
    return -a_ if x & 0x80000000 else a_
def ref_fx(fx, amt, pcm, s, e, chan=0):
    out = list(pcm); n = e - s
    if n <= 0: return out
    if fx in (FADE_IN, FADE_OUT):
        A = max(1, min(100, amt)); F = (n // 100) * A + ((n % 100) * A) // 100; F = F or 1
        step = (1 << 24) // F
        if fx == FADE_OUT: i0, g, step = s + n - F, 1 << 24, -step
        else: i0, g = s, 0
        for i in range(i0, i0 + F):
            out[i] = s16(s32(out[i] * (g >> 9)) >> 15); g += step
    elif fx == GAIN:
        g = (max(0, min(400, amt)) * 4096) // 100
        for i in range(s, e): out[i] = clip(s32(out[i] * g) >> 12)
    elif fx == CRUSH:
        q = max(1, min(100, amt)); mask = (-1 << ((q * 15 + 50) // 100))
        for i in range(s, e): out[i] = s16(out[i] & mask)
    elif fx == DOWNS:
        f = max(1, min(64, amt)); c = 0; h = 0
        for i in range(s, e):
            if c == 0: h = out[i]; c = f
            c -= 1; out[i] = h
    elif fx == VINYL:
        amt = max(1, min(100, amt)); seed = (VSEED0 + chan * VSEEDC) & M32
        x = vhash((s - 1 + seed) & M32); nprev = s32(x) >> 20; cprev = vclick(x, amt) >> 1
        for i in range(s, e):
            x = vhash((i + seed) & M32); nz = s32(x) >> 20; f = (nz + nprev) >> 1; nprev = nz
            hiss = s32(f * amt) >> 8; c = vclick(x, amt); oc = c + cprev; cprev = c >> 1
            out[i] = clip(out[i] + hiss + oc)
    elif fx == DRIVE:
        k = ((max(0, min(400, amt)) + 10) << 8) // 10
        for i in range(s, e):
            x = clip(s32(out[i] * k) >> 8); x2 = (x * x) >> 15; x3 = s32(x2 * x) >> 15
            out[i] = clip((3 * x - x3) >> 1)
    return out



if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'SAMPLEFX.WAV'); pg.build_wav(out)
    import random
    from validate import Harness
    from mpcmod import EVQ, OS_PUSH_PAGE
    from pages import SET_FOCUS, ENABLE_DOIT
    from shasm import SHAsm
    LOG = 0x88E00000          # +0 n copy, +4/+8 extract start/end, +0xC n guard, +0x10 n delete, +0x14 n play, +0x18 blk play, +0x1C modo, +0x20 vel, +0x24 n stop
    LIST_END = 0x884C4FE0
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    def cnt(s, off): s.movl_pool(LOG, 1); s.movl_ld_disp(1, off, 0); s.add_imm(1, 0); s.movl_st_disp(0, off, 1)
    stub(OS_PUSH_PAGE, lambda s: (s.rts(), s.nop()))
    stub(ENABLE_DOIT, lambda s: (cnt(s, 0x28), s.rts(), s.nop())); stub(SET_FOCUS, lambda s: (cnt(s, 0x2C), s.movl_st_disp(5, 0x30, 1), s.rts(), s.nop()))
    def copy_like(s, is_extract):
        # blocco corrente -> copia in fondo alla lista (come l'OS), nome[0]='C'/'X', terminatore, diventa corrente; r0=0
        s.stsl_pr(); s.movl_st_dec(8, 15); s.movl_st_dec(9, 15); s.mov_rm_rn(4, 9)
        s.movl_pool(LIST_END, 2); s.jsr(2); s.nop()                  # r0 = fine lista (r4 = MGR preservato? ricarico)
        s.mov_rm_rn(9, 4); s.movl_ld_disp(4, 8, 1); s.mov_rm_rn(0, 8)  # r1 = sorgente, r8 = destinazione
        s.movl_ld(1, 3); s.shlr2(3); s.mov_rm_rn(8, 2)
        s.deflabel('cp'); s.movl_ld_inc(1, 0); s.movl_st(0, 2); s.add_imm(4, 2); s.dt(3); s.bf_lbl('cp')
        s.mov_imm(0, 0); s.movl_st(0, 2)
        s.mov_imm(ord('X') if is_extract else ord('C'), 0); s.movb_st_disp(0, 4, 8) if False else (s.mov_rm_rn(8, 2), s.add_imm(4, 2), s.movb_st(0, 2))
        s.movl_st_disp(8, 8, 4)
        if is_extract:
            s.movl_ld_disp(8, 0x20, 0); s.movl_pool(LOG, 1); s.movl_st_disp(0, 4, 1); s.movl_ld_disp(8, 0x24, 0); s.movl_st_disp(0, 8, 1)
        else: cnt(s, 0)
        s.movl_ld_inc(15, 9); s.movl_ld_inc(15, 8); s.ldsl_pr(); s.mov_imm(0, 0); s.rts(); s.nop()
    stub(COPY_CMD, lambda s: copy_like(s, False)); stub(EXTRACT_CMD, lambda s: copy_like(s, True))
    # delete del corrente: deve essere l'ultimo blocco -> diventa terminatore; corrente = primo blocco
    stub(DELETE_CMD, lambda s: (cnt(s, 0x10), s.movl_ld_disp(4, 8, 1), s.mov_imm(0, 0), s.movl_st(0, 1), s.movl_ld(4, 0), s.movl_st_disp(0, 8, 4), s.rts(), s.nop()))
    stub(LOOP_GUARD, lambda s: (cnt(s, 0xC), s.rts(), s.nop()))
    stub(AUD_PLAY, lambda s: (cnt(s, 0x14), s.movl_st_disp(5, 0x18, 1), s.movl_st_disp(6, 0x1C, 1), s.movl_st_disp(7, 0x20, 1), s.rts(), s.nop()))
    stub(AUD_STOP, lambda s: (cnt(s, 0x24), s.rts(), s.nop()))
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato dall'HUB"
    MSG = 0x88E10000
    def call(obj, type_, code=0, val=0, ident=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, ident)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 200_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    def fire(n): h.fire(n, maxsteps=200_000_000)
    def addblock(L, R=None, s=None, en=None, name=b'LOOP'):
        blk = h.list_end(); n = len(L); stereo = R is not None
        size = (0x100 + 2 * n * (2 if stereo else 1) + 0x200 + 3) & ~3
        h.ram_w32(blk, size)
        for i, c in enumerate(name.ljust(16, b'\0')): h.ram_w8(blk + 4 + i, c)
        h.ram_w32(blk + 0x18, n); h.ram_w32(blk + 0x1C, n if stereo else 0)
        h.ram_w32(blk + 0x20, 0 if s is None else s); h.ram_w32(blk + 0x24, n if en is None else en)
        h.ram_w16(blk + 0x92, 2 if stereo else 1)
        for i, v in enumerate(L + (R or [])): h.ram_w16(blk + 0x100 + 2 * i, v & 0xffff)
        h.ram_w32(blk + size, 0); return blk
    def rdpcm(blk, off, n): return [s16(e.rd(blk + 0x100 + 2 * (off + i), 2)) for i in range(n)]
    def log(off): return h.ram_r32(LOG + off)
    def cur(): return h.ram_r32(MGR + 8)
    def setfx(fx, amt):              # come l'utente: prima l'effetto (l'addon propone un AMT), poi la rotella su AMT
        e.wr(FXS, fx, 1); h.fire(1, maxsteps=50_000_000); h.ram_w32(AMT, amt)
    for k in range(0, 0x34, 4): h.ram_w32(LOG + k, 0)
    rnd = random.Random(7); n = 2000
    L = [rnd.randint(-32768, 32767) for _ in range(n)]; R = [rnd.randint(-32768, 32767) for _ in range(n)]
    SRCB = addblock(L, R, 150, 1850); h.ram_w32(MGR + 8, SRCB); fire(3)
    # --- pagina NON attiva: niente anteprima ---
    assert h.ram_r32(PREV) == 0 and log(0) == 0, "lavora anche a pagina chiusa"
    # --- ingresso nella pagina: dopo il debounce l'anteprima esiste, corrente = originale ---
    h.ram_w32(EVQ + 0x68, pg.page); fire(2)
    assert e.rd(FXS, 1) == NOFX and h.ram_r32(PREV) == 0, "all'ingresso: NO FX, nessuna anteprima"
    fire(DEBOUNCE + 4); assert h.ram_r32(PREV) == 0 and log(0) == 0, "NO FX non deve copiare"
    print("  ok ingresso con NO FX: nessuna copia, i pad suonano l'originale")
    setfx(GAIN, 250)
    fire(DEBOUNCE - 2); assert h.ram_r32(PREV) == 0, "debounce ignorato"
    fire(6); P = h.ram_r32(PREV)
    assert P and P > SRCB and log(0) == 1 and cur() == SRCB, (hex(P), log(0), hex(cur()))
    assert rdpcm(P, 0, n) == ref_fx(GAIN, 250, L, 150, 1850) and rdpcm(P, n, n) == ref_fx(GAIN, 250, R, 150, 1850)
    assert rdpcm(SRCB, 0, n) == L and rdpcm(SRCB, n, n) == R, "originale modificato"
    # limiti di AMT nel modello + valore proposto al cambio effetto
    mdl = e.rd(pg.values[AMT_FID] + 0x24, 4)
    for k, (lo, hi, dv) in enumerate(FX_AMT):
        e.wr(FXS, k, 1); fire(1)
        assert (s32(e.rd(mdl + 0x18, 4)), e.rd(mdl + 0x1C, 4), h.ram_r32(AMT)) == (lo, hi, dv), (FX_NAMES[k], s32(e.rd(mdl + 0x18, 4)), e.rd(mdl + 0x1C, 4), h.ram_r32(AMT))
    fx_w = pg.values[[it[1] for it in pg.items if it[0] == 'choice'][0]]
    assert e.rd(fx_w + 0x1C, 4) == 0x88507D1C and e.rd(fx_w + 0x30, 2) == e.rd(fx_w + 0x10, 2) and e.rd(fx_w + 0x35, 1) == len(FX_NAMES) - 1
    print("  ok campo effetto = widget COMPOSITO OS (vt 0x88507D1C, rect +0x30, max +0x35 = %d)" % (len(FX_NAMES) - 1))
    print("  ok limiti/valore AMT per effetto nel modello (es. BITCRUSH 1..100, proposto 75)")
    ne0 = log(0x28); fire(5); assert log(0x28) >= ne0 + 5, "DO IT non riabilitato a ogni fire"   # (anche il framework, dal 2026-10-24)
    print("  ok DO IT riabilitato a ogni fire (pagina attiva)")
    setfx(GAIN, 250); fire(DEBOUNCE + 4); P = h.ram_r32(PREV)
    print("  ok ingresso: anteprima GAIN 250 dopo il debounce, originale intatto, corrente = originale")
    # --- pad: ascolto dell'anteprima; rilascio: stop ---
    assert call(pg.page, EV_PAD, code=3, val=90) == 1 and log(0x14) == 1 and log(0x18) == P and log(0x1C) == 0 and log(0x20) == 0x7F
    if DIAG:
        txt = bytes(e.rd(pg.msgt + i, 1) for i in range(16)).decode()
        pv = ref_fx(GAIN, 250, L, 150, 1850)[150:1850]; sv = L[150:1850]
        dd = min(99, sum(1 for a_, b_ in zip(sv, pv) if a_ != b_) * 100 // len(sv))
        exp = "R%02dC%02dL%02d D%02d %s " % (h.ram_r32(RCNT) % 100, h.ram_r32(CCNT) % 100, h.ram_r32(LCNT) % 100, dd, chr(h.ram_r32(WHY)))
        assert txt == exp, (txt, exp)
        print("  ok diag pad premuto: %r" % txt)
    assert call(pg.page, EV_PAD, code=3, val=0) == 1 and log(0x24) == 1
    if DIAG:
        txt = bytes(e.rd(pg.msgt + i, 1) for i in range(16)).decode()
        exp = "Q100%% C%02d L%02d Z0" % (h.ram_r32(CCNT) % 100, h.ram_r32(LCNT) % 100)
        assert txt == exp, (txt, exp)
        print("  ok diag pad rilasciato: %r" % txt)
    if DIAG:
        pass
    print("  ok pad -> play(anteprima, modo 0, 127), rilascio -> stop")
    # --- cambio AMT e FX: ricalcolo IN PLACE (stesso blocco, nessuna copia nuova, nessuno stop) ---
    for fx, amt in ((GAIN, 60), (DRIVE, 90), (CRUSH, 5), (FADE_OUT, 40), (DOWNS, 6), (FADE_IN, 100), (VINYL, 40), (VINYL, 100)):
        setfx(fx, amt); fire(DEBOUNCE + 2)
        assert h.ram_r32(PREV) == P and log(0) == 1 and log(0x24) == 1, ("non in place", FX_NAMES[fx])
        assert rdpcm(P, 0, n) == ref_fx(fx, amt, L, 150, 1850) and rdpcm(P, n, n) == ref_fx(fx, amt, R, 150, 1850, 1), FX_NAMES[fx]
    print("  ok 8 cambi FX/AMT (anche VINYL) ricalcolati in place, bit-exact, senza fermare l'ascolto")
    # --- TRIM SIL: anteprima ricreata con Extract (delete della vecchia) ---
    setfx(TRIM, 10); fire(DEBOUNCE + 2)
    assert log(0x10) == 1 and log(0x24) == 2, "vecchia anteprima non cancellata / ascolto non fermato"
    P2 = h.ram_r32(PREV); assert P2 == P and cur() == SRCB and e.rd(P2 + 4, 1) == ord('X')   # stesso posto (ultimo blocco)
    setfx(GAIN, 100); fire(DEBOUNCE + 2); P3 = h.ram_r32(PREV)
    assert log(0x10) == 2 and log(0) == 2 and P3 == P and rdpcm(P3, 0, n) == L, "ritorno da TRIM"
    print("  ok TRIM SIL ricrea l'anteprima (Extract), ritorno a GAIN la ricrea (Copy)")
    # --- DO IT: l'anteprima diventa un sample vero (corrente), poi nessuna nuova anteprima finche' non cambi ---
    setfx(DRIVE, 30); fire(3)                                   # cambio pendente: DO IT deve ricalcolare prima
    msg = lambda: bytes(e.rd(pg.msgt + i, 1) for i in range(16))
    assert call(pg.page, 2, code=0x2E) == 1                     # 1a pressione: solo la domanda
    assert msg() == b'Save? DO IT=yes ', msg(); assert h.ram_r32(PREV) == P and cur() == SRCB, "salvato senza conferma"
    fire(0x2000); assert h.ram_r32(CONF) == 1 and h.ram_r32(SHOWMSG) > 0x1000, "la domanda deve restare"
    assert call(pg.page, 2, code=0x2D) == 1 and h.ram_r32(CONF) == 0 and h.ram_r32(PREV) == P, "CANCEL deve solo annullare"
    fire(2); assert h.ram_r32(SHOWMSG) == 0 and h.ram_r32(EVQ + 0x68) == pg.page, "domanda non chiusa / uscito dalla pagina"
    assert call(pg.page, 2, code=0x2E) == 1 and h.ram_r32(CONF) == 1
    setfx(DRIVE, 31); fire(2); assert h.ram_r32(CONF) == 0 and h.ram_r32(SHOWMSG) == 0, "un cambio annulla la domanda"
    setfx(DRIVE, 30); fire(2)
    assert call(pg.page, 2, code=0x2E) == 1 and h.ram_r32(PREV) == P
    assert call(pg.page, 2, code=0x2E) == 1                     # 2a pressione entro la finestra: salva
    assert cur() == P and h.ram_r32(PREV) == 0 and rdpcm(P, 0, n) == ref_fx(DRIVE, 30, L, 150, 1850)
    assert msg() == b'Saved new sample', msg()
    assert e.rd(FXS, 1) == NOFX, "dopo il salvataggio l'effetto torna a NO FX"
    ncopy = log(0); fire(DEBOUNCE + 4); assert log(0) == ncopy, "ricalcolo senza cambi"
    assert call(pg.page, EV_PAD, code=0, val=100) == 1 and log(0x18) == P, "il pad deve suonare il sample salvato"
    print("  ok DO IT con conferma (resta, CANCEL annulla senza uscire, un cambio annulla, salvataggio), poi NO FX")
    # focus: un messaggio con il cursore su AMT non lo sposta; con il cursore sul selettore lo sposta e poi lo rimette
    amt_w = pg.values[AMT_FID]
    sel_w = pg.page + (pg.addr['sel'] - pg.addr['page'])        # (pg.addr = ultima build a +0x1000)
    h.ram_w32(SHOWMSG, 1); fire(2); e.wr(pg.page + 0x24, amt_w, 4); nf0 = log(0x2C)
    setfx(GAIN, 120); fire(2); call(pg.page, 2, code=0x2E)
    assert log(0x2C) == nf0, "il messaggio ha spostato il cursore da AMT"
    call(pg.page, 2, code=0x2D); fire(2); e.wr(pg.page + 0x24, sel_w, 4); nf0 = log(0x2C); call(pg.page, 2, code=0x2E)
    assert log(0x2C) == nf0 + 1, "selettore nascosto: il focus deve spostarsi"
    call(pg.page, 2, code=0x2D); fire(2); assert log(0x2C) == nf0 + 2 and log(0x30) == sel_w, "a fine messaggio il focus torna sul selettore"
    print("  ok focus: i messaggi non spostano il cursore (salvo dal selettore, poi rimesso)")
    # cambio sample -> effetto azzerato a NO FX
    e.wr(FXS, CRUSH, 1); fire(2); h.ram_w32(MGR + 8, SRCB); fire(2)
    assert e.rd(FXS, 1) == NOFX, "cambiando sample l'effetto deve tornare NO FX"
    print("  ok cambio sample -> NO FX")
    h.ram_w32(MGR + 8, P); fire(2)                             # torna al sample salvato (per la cascata)
    # NO FX + DO IT -> "Choose an effect", niente conferma
    assert call(pg.page, 2, code=0x2E) == 1 and msg() == b'Choose an effect' and h.ram_r32(CONF) == 0
    # --- nuovo cambio -> anteprima del sample confermato (effetti in cascata); uscita -> cancellata ---
    setfx(GAIN, 200); fire(DEBOUNCE + 2); P4 = h.ram_r32(PREV)
    assert P4 > P and cur() == P and rdpcm(P4, 0, n) == ref_fx(GAIN, 200, ref_fx(DRIVE, 30, L, 150, 1850), 150, 1850)
    nd = log(0x10); call(pg.page, 0x24); fire(DEBOUNCE + 4)                   # 0x24 = ridisegno: NON e' un'uscita
    assert log(0x10) == nd and h.ram_r32(PREV) == P4, "0x24 ha cancellato l'anteprima"
    h.ram_w32(EVQ + 0x68, 0x8849A4EC); fire(LEAVE_FIRES - 2); assert h.ram_r32(PREV) == P4, "uscita troppo precoce"
    fire(4); assert log(0x10) == nd + 1 and h.ram_r32(PREV) == 0 and cur() == P and h.list_end() == P4
    fire(DEBOUNCE + 2); assert h.ram_r32(PREV) == 0
    print("  ok cascata; 0x24 (ridisegno) ignorato; uscita vera (altra pagina per %d fire) cancella l'anteprima" % LEAVE_FIRES)
    # --- errori ---
    h.ram_w32(EVQ + 0x68, pg.page); fire(1)
    mk = addblock([5, -5] * 100); h.ram_w32(MGR + 8, mk); fire(2); setfx(TRIM, 10); fire(DEBOUNCE + 2)
    assert h.ram_r32(PREV) == 0 and h.ram_r32(ERR) == ERR_SILENT and cur() == mk
    txt = bytes(e.rd(pg.msgt + i, 1) for i in range(16)); assert txt.startswith(b'Only silence'), txt
    print("  ok 'Only silence' senza anteprima, corrente invariato")
    # --- sample di un addon selezionato: niente anteprima, niente copia ---
    nc0 = log(0); addon_blk = h.LIST_HEAD + e.rd(h.LIST_HEAD, 4)          # 2o blocco = SAMPLEFX.WAV
    assert e.rd(addon_blk + 0x100, 4) == MAGIC_ADDON
    h.ram_w32(MGR + 8, addon_blk); fire(2); setfx(GAIN, 150); fire(DEBOUNCE + 4)
    assert h.ram_r32(PREV) == 0 and log(0) == nc0 and cur() == addon_blk, "anteprima di un addon"
    print("  ok sample di un addon: nessuna anteprima")
    # --- LAVORO PER FIRE LIMITATO (HW v2: 6M passi in un fire = main loop bloccato per secondi) ---
    nb = 88200; Lb = [(i * 37) % 20000 - 10000 for i in range(nb)]; Rb = [(i * 53) % 30000 - 15000 for i in range(nb)]
    big = addblock(Lb, Rb); h.ram_w32(MGR + 8, big); fire(2); setfx(DRIVE, 90)
    steps = h.fire(DEBOUNCE - 1, maxsteps=50_000_000)
    stp = h.fire(1, maxsteps=50_000_000)[0]                                  # fire del rebuild (Copy stub + 1 pezzo)
    run = []
    while h.ram_r32(BUSY): run += h.fire(1, maxsteps=50_000_000)
    Pb = h.ram_r32(PREV)
    assert rdpcm(Pb, 0, nb) == ref_fx(DRIVE, 90, Lb, 0, nb) and rdpcm(Pb, nb, nb) == ref_fx(DRIVE, 90, Rb, 0, nb), "big"
    print("  ok 2 s stereo: %d fire di elaborazione, max %d passi/fire (fire di avvio con Copy %d)" % (len(run) + 1, max(run), stp))
    assert max(run) < 300_000, "troppo lavoro per fire"
    # pad durante l'elaborazione: risponde subito con l'anteprima parziale
    setfx(DRIVE, 20); h.fire(DEBOUNCE + 1, maxsteps=50_000_000); assert h.ram_r32(BUSY) == 1
    np0 = log(0x14); assert call(pg.page, EV_PAD, code=1, val=80) == 1 and log(0x14) == np0 + 1 and log(0x18) == Pb
    print("  ok pad durante l'elaborazione -> play immediato")
    # ================= CATENA =================
    h.ram_w32(EVQ + 0x68, pg.page)
    rnd2 = random.Random(11); nc = 3000
    Lc = [rnd2.randint(-30000, 30000) for _ in range(nc)]; Rc = [rnd2.randint(-30000, 30000) for _ in range(nc)]
    CB = addblock(Lc, Rc, 100, 2900); h.ram_w32(MGR + 8, CB); fire(4)
    assert all(h.ram_r32(x) == 0 for x in CHFX + CHAMT) and h.ram_r32(SLOTN) == 1, "nuovo sample: catena vuota"
    def slot_set(k, fx, amt):          # come l'utente: numero di slot, poi effetto (AMT proposto), poi AMT
        h.ram_w32(SLOTN, k); fire(2); setfx(fx, amt); fire(2)
    slot_set(1, CRUSH, 60); slot_set(2, FADE_OUT, 40); slot_set(3, DRIVE, 30)
    assert [h.ram_r32(x) for x in CHFX[:4]] == [CRUSH, FADE_OUT, DRIVE, 0] and [h.ram_r32(x) for x in CHAMT[:3]] == [60, 40, 30]
    fire(DEBOUNCE + 8); P = h.ram_r32(PREV); assert P and h.ram_r32(BUSY) == 0
    def chain(pcm, stages, s_, e_, ch=0):
        for fx, amt in stages: pcm = ref_fx(fx, amt, pcm, s_, e_, ch)
        return pcm
    stages = [(CRUSH, 60), (FADE_OUT, 40), (DRIVE, 30)]
    assert rdpcm(P, 0, nc) == chain(Lc, stages, 100, 2900) and rdpcm(P, nc, nc) == chain(Rc, stages, 100, 2900), "catena"
    print("  ok catena BITCRUSH 60 -> FADE OUT 40 -> DRIVE 30 (3 slot), stereo, bit-exact")
    h.ram_w32(SLOTN, 2); fire(3)
    mdl = e.rd(pg.values[AMT_FID] + 0x24, 4)
    assert e.rd(FXS, 1) == FADE_OUT and h.ram_r32(AMT) == 40 and (s32(e.rd(mdl + 0x18, 4)), e.rd(mdl + 0x1C, 4)) == FX_AMT[FADE_OUT][:2]
    h.ram_w32(SLOTN, 4); fire(3); assert e.rd(FXS, 1) == NOFX and h.ram_r32(AMT) == 0
    fire(DEBOUNCE + 4); assert h.ram_r32(PREV) == P and rdpcm(P, 0, nc) == chain(Lc, stages, 100, 2900), "il cambio slot non deve ricalcolare"
    print("  ok cambio slot: campi = slot (effetto, AMT, limiti), catena e anteprima invariate")
    h.ram_w32(SLOTN, 1); fire(2); h.ram_w32(AMT, 90); fire(DEBOUNCE + 8)
    stages[0] = (CRUSH, 90)
    assert h.ram_r32(PREV) == P and rdpcm(P, 0, nc) == chain(Lc, stages, 100, 2900), "slot 1 cambiato"
    h.ram_w32(SLOTN, 2); fire(2); setfx(NOFX, 0); fire(DEBOUNCE + 8)
    assert rdpcm(P, 0, nc) == chain(Lc, [stages[0], stages[2]], 100, 2900), "slot NO FX saltato"
    print("  ok slot modificato -> catena ricalcolata; slot NO FX saltato")
    slot_set(4, VINYL, 70); fire(DEBOUNCE + 8)
    vst = [stages[0], stages[2], (VINYL, 70)]
    assert rdpcm(P, 0, nc) == chain(Lc, vst, 100, 2900) and rdpcm(P, nc, nc) == chain(Rc, vst, 100, 2900, 1), "VINYL in catena"
    dL = [a_ - b_ for a_, b_ in zip(rdpcm(P, 0, nc), chain(Lc, vst[:2], 100, 2900))]
    dR = [a_ - b_ for a_, b_ in zip(rdpcm(P, nc, nc), chain(Rc, vst[:2], 100, 2900, 1))]
    assert dL != dR and any(abs(x) > 3000 for x in dL[100:2900]), "VINYL: L/R diversi, crackle presente"
    print("  ok VINYL 70 in coda alla catena (a pezzi), bit-exact, L/R decorrelati")
    h.ram_w32(SLOTN, 4); fire(2); setfx(NOFX, 0); fire(DEBOUNCE + 8)
    Lt = [0] * 400 + [rnd2.randint(-20000, 20000) for _ in range(1600)] + [0] * 1000
    TB = addblock(Lt, None); h.ram_w32(MGR + 8, TB); fire(3)
    slot_set(1, GAIN, 50); slot_set(5, TRIM, 10); fire(DEBOUNCE + 8)
    P = h.ram_r32(PREV); s_t, e_t = h.ram_r32(P + 0x20), h.ram_r32(P + 0x24)
    first = next(i for i in range(len(Lt)) if abs(Lt[i]) > 327); last = max(i for i in range(len(Lt)) if abs(Lt[i]) > 327)
    assert (s_t, e_t) == (first, last + 1), (s_t, e_t, first, last)
    assert rdpcm(P, 0, len(Lt)) == ref_fx(GAIN, 50, Lt, s_t, e_t), "TRIM prima, poi GAIN sulla parte tagliata"
    assert h.ram_r32(TB + 0x20) == 0 and h.ram_r32(TB + 0x24) == len(Lt), "originale intatto"
    print("  ok TRIM SIL nello slot 5 applicato per primo, GAIN sulla parte tagliata")
    assert call(pg.page, 2, code=0x2E) == 1 and call(pg.page, 2, code=0x2E) == 1
    assert h.ram_r32(MGR + 8) == P and all(h.ram_r32(x) == 0 for x in CHFX) and h.ram_r32(SLOTN) == 1 and e.rd(FXS, 1) == NOFX
    fire(3); assert call(pg.page, 2, code=0x2E) == 1 and bytes(e.rd(pg.msgt + i, 1) for i in range(16)).startswith(b'Choose an effect')
    print("  ok DO IT salva tutta la catena e la azzera; catena vuota -> 'Choose an effect'")
    # --- (2026-10-17) la lista sample cambia mentre c'e' un'anteprima: un sample PRIMA viene cancellato (i blocchi scendono),
    #     PREV/SRC salvati non sono piu' blocchi -> all'uscita niente DELETE su un puntatore vecchio, lista sana ---
    SB = addblock([rnd2.randint(-9000, 9000) for _ in range(1500)], None, name=b'SRC2'); h.ram_w32(MGR + 8, SB); fire(3)
    setfx(GAIN, 150); fire(DEBOUNCE + 2); P6 = h.ram_r32(PREV); assert P6 and P6 > SB
    h.ram_w32(P6 + h.ram_r32(P6), 0)                                                      # terminatore (lo stub di Copy non lo scrive)
    q_, prev_ = h.LIST_HEAD, None
    while q_ < SB: prev_ = q_; q_ += h.ram_r32(q_)
    assert q_ == SB and prev_
    nd = log(0x10); h.remove_sample(prev_); h.ram_w32(MGR + 8, h.LIST_HEAD)               # il blocco prima di SB: tutto scende
    h.ram_w32(EVQ + 0x68, 0x8849A4EC); fire(LEAVE_FIRES + 4)
    assert log(0x10) == nd and h.ram_r32(PREV) == 0 and h.ram_r32(MGR + 8) == h.LIST_HEAD, "DELETE/corrente su puntatori vecchi"
    p_ = h.LIST_HEAD
    while h.ram_r32(p_): sz = h.ram_r32(p_); assert sz >= 0x100 and sz & 3 == 0, "lista rotta"; p_ += sz
    print("  ok lista cambiata con l'anteprima aperta: nessun DELETE su un puntatore vecchio, lista sample sana")
    print("VALIDATO: Sample FX v11 (CATENA di %d effetti) -> %s" % (NSLOT, os.path.basename(out)))
