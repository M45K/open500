#!/usr/bin/env python3
# HUB: gestore di addon MULTIPLI -> HUB.WAV. v2 (2026-10-16): 16 slot allocati a pagine da 4 KB (vedi PAGE_SH). E' l'addon (formato loader flash, MPCADDN2) che il loader v7 carica in
# EXEC; a ogni fire:
#   - se la lista sample e' cambiata (LOAD/delete) fa un SYNC: ogni blocco con magic MPCADDN3 non ancora installato
#     viene copiato nel primo slot libero (ADDON_BASE + k*SLOT_SIZE) con verifica somma e RILOCAZIONE (tabella nel
#     payload); gli addon il cui blocco e' sparito dalla lista vengono disinstallati. Identita' = nome8 + somma.
#   - chiama gli addon installati (jsr slot), r0-r14 sono gia' salvati dal loader flash;
#   - menu OTHER: per ogni addon con pagina (header page_off != -1) aggiunge una voce (menu16) dopo "Version":
#     tabella enum {8+N, stringhe originali + nomi}, N label "Press DO IT" (maschera 1<<(8+j)) agganciate dopo
#     l'ultimo widget dell'OS, wrapper dell'handler (DO IT sulla voce j -> push_page della pagina dello slot).
#     Tutto ricostruito a ogni sync. Originali salvati nel registro del loader flash (0x88F00060..6C) cosi'
#     SHIFT+ERASE (che scarica l'HUB) ripristina OTHER.
#   - messaggi (riga 2, ~256 fire): "LOADED <nome>", "BAD <blocco>", "NO FREE SLOT", "LOW MEM: ADDONS!" (lista
#     sample oltre 0x88E00000: la RAM degli addon 0x88F00000.. e' a rischio; MGR[1] sul ferro = 0x90000000).
# SHIFT+ERASE = il loader flash ricarica l'HUB, che al primo fire risincronizza: reset totale senza perdere nulla.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from mpcmod import (Addon, HUB_SCRATCH, SCRATCH, EXEC, ADDON_BASE, NSLOTS, SLOT_SIZE, SLOT_CODE_MAX, ADDON3_MAGIC, ADDON3_HDR,
                    EVQ, OS_PUSH_PAGE, SCRATCH_MAGIC_OFF, LCD_CMD, LCD_DATA, LCD_DRV_DATA)
FRAMES = 0x881C05F0                                     # contatore frame audio dell'OS (44100/s)

class Lay:
    """indirizzi dell'HUB relativi alla base di costruzione D (0 o 0x1000: pack_addon3 li riloca a runtime)"""
    def __init__(self, D):
        S = HUB_SCRATCH + D; self.S = S; self.D = D
        self.LASTEND, self.PREVK, self.SHOW, self.MSGKIND, self.SHOWSLOT = S+0x04, S+0x08, S+0x0C, S+0x10, S+0x14
        self.BADADDR, self.BADGOT, self.BADEXP, self.FOUND, self.NMENU, self.HOOKED = S+0x18, S+0x1C, S+0x20, S+0x24, S+0x28, S+0x2C
        # tabelle per slot, contigue (indicizzate con k*4 / k*8 / k*16); la firma di init_scratch sta a +0x40..+0x43
        self.INST  = lambda k: S + 0x44 + 4*k        # 16 slot (v2 2026-10-16)
        self.SUM   = lambda k: S + 0x84 + 4*k
        self.PAGE  = lambda k: S + 0xC4 + 4*k
        self.MSLOT = lambda j: S + 0x104 + 4*j
        self.BASE  = lambda k: S + 0x144 + 4*k       # indirizzo dove e' installato lo slot k (allocazione a pagine)
        self.LEN   = lambda k: S + 0x184 + 4*k       # byte del body (codice + tabella reloc)
        self.NAME  = lambda k: S + 0x1C4 + 8*k
        self.MENU  = lambda k: S + 0x244 + 16*k
        self.MAP   = S + 0x344                       # NPAGES byte: pagina occupata (ricostruita a ogni install)
        self.SCR   = lambda k: S + 0x384 + 4*k       # v3: indirizzo della scratch dello slot k (subito dopo il codice)
        self.STRBUF, self.DESC, self.LABELS, self.HINT = S+0x400, S+0x600, S+0x620, S+0x8A0   # oggetti (ricostruiti a ogni sync)
        R = SCRATCH + D
        self.REG_ACTIVE, self.REG_ORIG_HANDLER, self.REG_ORIG_DESC, self.REG_PREV = R+0x60, R+0x64, R+0x68, R+0x6C   # registro del loader flash
        self.SLOT = lambda k: ADDON_BASE + D + k*SLOT_SIZE   # SLOT(0) = inizio dell'area addon
        self.MEM_WARN = R - 0x100000                 # fine lista sample oltre questo = avviso
        # v3 SCATOLA NERA (PIANO.md 0.4) nella zona libera B+0x1000: NON e' la scratch dell'HUB (azzerata a ogni
        # ricaricamento), cosi' dice anche cosa girava prima di un SHIFT+ERASE o di un riavvio con la RAM intatta
        X = R + 0x1000; self.BB = X
        self.BB_MAGIC, self.BB_CUR, self.BB_GEN, self.BB_NERR = X, X + 4, X + 8, X + 0xC   # CUR = slot+1 in esecuzione (0 = nessuno)
        self.BB_CRASH, self.BB_ERRN = X + 0x10, X + 0x18          # 8 byte: add-on in corso all'ultimo stop / codice cambiato
        self.WK, self.WOFF, self.WSUM, self.BB_CRPEND = X + 0x20, X + 0x24, X + 0x28, X + 0x2C   # controllo del codice a rotazione
        self.SUMC = lambda k: X + 0x40 + 4*k                       # somma (u32) del codice immutabile dopo la rilocazione
        self.TXT  = lambda k: X + 0x80 + 4*k                       # byte di codice immutabile (fino agli oggetti pagina)
        self.BBNAME = lambda k: X + 0xC0 + 8*k                     # nomi (fino a X + 0x140)
        # scritta iniziale (2026-10-26): solo al primo avvio dell'HUB dopo l'accensione (scatola nera non valida)
        self.SPL_ON, self.SPL_T0, self.SPL_F, self.SPL_POS = X + 0x180, X + 0x184, X + 0x188, X + 0x18C
        self.SPL_DUMMY = X + 0x190                                 # qui finiscono i caratteri dell'OS durante la scritta
        self.SPL_CGS = X + 0x1A0                                   # 64 byte: CGRAM dell'OS, rimessa alla fine
        self.LDR_SHOW = R + 0x24                                   # loader flash: fire rimasti del suo "LOADED <nome>" (v10)
# Allocazione a PAGINE. v2: codice a [p, p+ceil(len/4K)) e scratch a p+7 (come nella costruzione, +SLOT_CODE_MAX), gli altri
# add-on nei buchi in mezzo -> chi usava memoria fra codice e scratch senza dichiararla scriveva sopra un altro add-on
# (HW 2026-10-17: Master FX su Grid, Trans-Chop su USB MIDI). v3 (2026-10-17): BLOCCO CONTIGUO = pagine del codice + la
# scratch SUBITO DOPO (pagina p+ceil(len/4K)); nessun buco dentro un add-on. Gli add-on restano costruiti con la scratch
# a +0x7000: la rilocazione guarda il valore di ogni parola e usa due delta (codice / scratch). Gli add-on con buffer fra
# codice e scratch dichiarano la riserva (len = 0x7000) e restano identici a prima (scratch a +0x7000).
PAGE_SH, NPAGES = 12, 8 * SLOT_SIZE >> 12
SCR_PAGE = SLOT_CODE_MAX >> PAGE_SH
SCRATCH_LEN = 0x3C4
L = Lay(0)                                           # indirizzi "di costruzione" (validi nell'emulatore con delta 0)
assert NSLOTS == 16 and L.MAP + NPAGES <= L.SCR(0) and L.SCR(NSLOTS) <= L.S + SCRATCH_LEN <= L.STRBUF and L.LABELS + 0x28 * NSLOTS <= L.HINT
assert SLOT_CODE_MAX + 0x1000 == SLOT_SIZE and NPAGES == 64
INST, SUM, PAGE, MSLOT, NAME, MENU = L.INST, L.SUM, L.PAGE, L.MSLOT, L.NAME, L.MENU
LASTEND, SHOW, MSGKIND, BADADDR, BADGOT, BADEXP, FOUND, NMENU, DESC, LABELS, S = L.LASTEND, L.SHOW, L.MSGKIND, L.BADADDR, L.BADGOT, L.BADEXP, L.FOUND, L.NMENU, L.DESC, L.LABELS, L.S

MGR, LIST_END = 0x8848ED30, 0x884C4FE0
BB_MAGIC_V, BB_LEN, WCHUNK = 0x42423554, 0x140, 64       # 'O5BB'; byte di codice controllati per fire
# ---- scritta "OPEN 500  <versione>" che scorre al primo avvio (font a blocchi 3x2 caratteri, 8 forme in CGRAM 0-7) ----
VERSION = "0.2"                                         # versione pubblica (la seconda release)
SPL_SHAPES = {
 'LT': ["..###", ".####", "#####", "#####", "#####", "#####", "#####", "#####"],   # angolo alto sinistra
 'UB': ["#####", "#####", "#####", ".....", ".....", ".....", ".....", "....."],   # barra alta
 'RT': ["###..", "####.", "#####", "#####", "#####", "#####", "#####", "#####"],   # angolo alto destra
 'LL': ["#####", "#####", "#####", "#####", "#####", "#####", ".####", "..###"],   # angolo basso sinistra
 'LB': [".....", ".....", ".....", ".....", ".....", "#####", "#####", "#####"],   # barra bassa
 'LR': ["#####", "#####", "#####", "#####", "#####", "#####", "####.", "###.."],   # angolo basso destra
 'UMB': ["#####", "#####", "#####", ".....", ".....", ".....", "#####", "#####"],  # barra alta + di mezzo
 'DG': ["##...", "###..", ".###.", "..###", "...##", "....#", ".....", "....."],   # diagonale (N)
}
SPL_ORDER = ['LT', 'UB', 'RT', 'LL', 'LB', 'LR', 'UMB', 'DG']                      # = caratteri 0..7
SPL_FONT = {   # (riga 1, riga 2); 'F' = blocco pieno della ROM, '_' = spazio
 'O': (['LT', 'UB', 'RT'], ['LL', 'LB', 'LR']), '0': (['LT', 'UB', 'RT'], ['LL', 'LB', 'LR']),
 'P': (['F', 'UMB', 'RT'], ['F', '_', '_']), 'E': (['F', 'UMB', 'UMB'], ['F', 'LB', 'LB']),
 'N': (['F', 'DG', 'F'], ['F', '_', 'F']), '1': (['UB', 'F', '_'], ['LB', 'F', 'LB']),
 '2': (['UMB', 'UMB', 'RT'], ['LL', 'LB', 'LB']), '3': (['UMB', 'UMB', 'RT'], ['LB', 'LB', 'LR']),
 '4': (['F', '_', 'F'], ['UB', 'UB', 'F']), '5': (['F', 'UMB', 'UMB'], ['LB', 'LB', 'LR']),
 '6': (['LT', 'UMB', 'UMB'], ['LL', 'LB', 'LR']), '7': (['UB', 'UB', 'F'], ['_', '_', 'F']),
 '8': (['LT', 'UMB', 'RT'], ['LL', 'LB', 'LR']), '9': (['LT', 'UMB', 'RT'], ['LB', 'LB', 'LR']),
 '.': (['_'], ['LB']), ' ': (['_'], ['_']),
}
def spl_rows(text):
    code = lambda c: 0xFF if c == 'F' else 0x20 if c == '_' else SPL_ORDER.index(c)
    top = [code(c) for ch in text for c in SPL_FONT[ch][0]]; bot = [code(c) for ch in text for c in SPL_FONT[ch][1]]
    return bytes(top), bytes(bot)
SPL_TEXT = "OPEN 500  " + VERSION
SPL_TOP, SPL_BOT = spl_rows(SPL_TEXT)
SPL_N = len(SPL_TOP); SPL_STEPS = SPL_N + 16             # finestra di 16 colonne: entra da destra, esce a sinistra
SPL_GLYPH = bytes(int(row.replace('#', '1').replace('.', '0'), 2) for k in SPL_ORDER for row in SPL_SHAPES[k])
# velocita': una colonna ogni ~5512 frame audio (125 ms: ~8 colonne/s, la scritta dura ~6 s) = frame * 761 >> 22.
# Solo l'orologio audio: i fire sull'MPC sono molti di piu' quando l'OS e' libero (HW 2026-10-26: con max(frame, fire)
# la scritta volava). Ogni 16 fire solo se i frame sono fermi (emulatore).
FPC_MUL, FPC_SH, FPF_SH = 761, 22, 4
OTHER_PAGE, OTHER_MODEL, OTHER_VAR, OTHER_LAST = 0x88490640, 0x88490684, 0x88490680, 0x884909DC
INPUT_OBJ, ENABLE_DOIT, VT_LABEL, EV_DOIT = 0x88483FDC, 0x884E7372, 0x88507C44, 0x2E
PCM_OFF = 0x100
# header MPCADDN3 dentro il blocco sample (offset da blocco)
H_LEN, H_SUM, H_NAME, H_CODELEN, H_PAGEOFF, H_MENU, H_BODY = PCM_OFF+8, PCM_OFF+0xC, PCM_OFF+0x10, PCM_OFF+0x18, PCM_OFF+0x1C, PCM_OFF+0x20, PCM_OFF+ADDON3_HDR
MAGIC_HW = [ADDON3_MAGIC[i] | (ADDON3_MAGIC[i+1] << 8) for i in range(0, 8, 2)]
RECT_HINT = (1 << 9) | (16 << 4) | (1 << 2)     # rect(0,1,16)

def body(m):
    a = m.a; L = Lay(m.a.base - EXEC)   # tutto relativo alla base di costruzione (rilocato dal loader)
    def pages_of(src, dst):                                                 # dst = ceil(src / 4K). Clobbera r3
        a.mov_rm_rn(src, dst); m.imm32((1 << PAGE_SH) - 1, 3); a.add_rr(3, dst); a.shlr8(dst); a.shlr2(dst); a.shlr2(dst)
    m.init_scratch(SCRATCH_LEN)
    done = m.newlabel('done')
    # --- HUB appena (ri)partito (scratch azzerata: LASTEND = 0): scatola nera ---
    nfr = m.newlabel('nfr'); nbb = m.newlabel('nbb'); nbb2 = m.newlabel('nbb2')
    m.imm32(L.LASTEND, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); m.bf_far(nfr)
    m.imm32(L.BB_MAGIC, 1); a.movl_ld(1, 0); m.imm32(BB_MAGIC_V, 2); a.cmpeq(2, 0); m.bf_far(nbb)
    m.imm32(L.BB_CUR, 1); a.movl_ld(1, 0); a.add_imm(-1, 0); a.mov_imm(NSLOTS, 2); a.cmphs(2, 0); m.bt_far(nbb2)   # nessuno in corso
    a.shll2(0); a.shll(0); m.imm32(L.BBNAME(0), 1); a.add_rr(0, 1); m.imm32(L.BB_CRASH, 2)   # l'MPC si era fermato DENTRO un add-on
    a.movl_ld(1, 3); a.movl_st(3, 2); a.movl_ld_disp(1, 4, 3); a.movl_st_disp(3, 4, 2)
    m.imm32(L.BB_CRPEND, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.bra_lbl(nbb2); a.nop()
    a.deflabel(nbb); m.imm32(L.BB, 1); m.imm32(BB_LEN // 4, 2); a.mov_imm(0, 0)              # prima volta: tutta a 0
    bbz = m.newlabel('bbz'); a.deflabel(bbz); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(bbz)
    m.imm32(L.BB_MAGIC, 1); m.imm32(BB_MAGIC_V, 0); a.movl_st(0, 1)
    m.imm32(L.SPL_ON, 1); a.mov_imm(1, 0); a.movl_st(0, 1)                                 # primo avvio: scritta
    m.imm32(FRAMES, 2); a.movl_ld(2, 0); m.imm32(L.SPL_T0, 1); a.movl_st(0, 1)
    m.imm32(L.SPL_F, 1); a.mov_imm(0, 0); a.movl_st(0, 1); m.imm32(L.SPL_POS, 1); a.mov_imm(-1, 0); a.movl_st(0, 1)
    a.deflabel(nbb2)
    m.imm32(L.BB_CUR, 1); a.mov_imm(0, 0); a.movl_st(0, 1); m.imm32(L.WK, 1); a.movl_st(0, 1)
    m.imm32(L.WOFF, 1); a.movl_st(0, 1); m.imm32(L.WSUM, 1); a.movl_st(0, 1)
    m.imm32(L.BB_GEN, 1); a.movl_ld(1, 0); a.add_imm(1, 0); a.movl_st(0, 1)
    m.imm32(L.SPL_ON, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bf_lbl(nfr)                 # con la scritta niente messaggio
    m.imm32(L.MSGKIND, 1); a.mov_imm(6, 0); a.movl_st(0, 1); m.imm32(L.SHOW, 1); m.imm32(256, 0); a.movl_st(0, 1)   # "HUB v3 LOADED"
    a.deflabel(nfr); m.pool_jump()
    # --- lista sample cambiata (o primo fire dopo il caricamento) -> sync ---
    m.imm32(MGR, 4); m.imm32(LIST_END, 2); a.jsr(2); a.nop()
    m.imm32(L.LASTEND, 1); a.movl_ld(1, 2); a.cmpeq(0, 2); same = m.newlabel('same'); a.bt_lbl(same)
    # v3: con la pagina di un add-on aperta niente sync (l'OS tiene puntatori ai suoi oggetti: spostarlo o toglierlo
    # = chiamate virtuali su memoria riscritta, cf. HW PC 884E6336). Si rifa' appena l'utente esce.
    m.imm32(EVQ + 0x68, 3); a.movl_ld(3, 3); m.imm32(0x1FFFFFFF, 4); a.and_rr(4, 3)     # r3 = pagina corrente (fisico)
    m.imm32(L.INST(0), 5); a.mov_imm(NSLOTS, 6); pgl = m.newlabel('pgl'); pgn = m.newlabel('pgn')
    a.deflabel(pgl); a.movl_ld(5, 7); a.tst_rr(7, 7); a.bt_lbl(pgn)
    m.imm32(L.BASE(0) - L.INST(0), 2); a.add_rr(5, 2); a.movl_ld(2, 7); a.and_rr(4, 7)     # r7 = base dello slot (fisico)
    a.mov_rm_rn(3, 2); a.sub(7, 2)                                                         # r2 = pagina - base
    m.imm32(L.LEN(0) - L.INST(0), 7); a.add_rr(5, 7); a.movl_ld(7, 7)
    a.cmphs(7, 2); a.bt_lbl(pgn); m.imm32(same, 2); a.jmp(2); a.nop()                     # pagina di questo add-on: aspetta
    a.deflabel(pgn); a.add_imm(4, 5); a.dt(6); a.bf_lbl(pgl)
    a.movl_st(0, 1); m.imm32('sync', 2); a.jsr(2); a.nop()
    a.deflabel(same); m.pool_jump()
    # --- dispatch: chiama ogni slot installato (gli addon salvano PR da soli; r0-r14 sono salvati dal loader flash) ---
    # v3: solo gli slot sani (INST = 1; 2 = codice cambiato, fermato) e prima di ogni chiamata la briciola BB_CUR
    for k in range(NSLOTS):
        sk = m.newlabel('sk'); m.imm32(L.INST(k), 1); a.movl_ld(1, 0); a.mov_imm(1, 2); a.cmpeq(2, 0); a.bf_lbl(sk)
        m.imm32(L.BB_CUR, 1); a.mov_imm(k + 1, 0); a.movl_st(0, 1)
        m.imm32(L.BASE(k), 2); a.movl_ld(2, 2); a.jsr(2); a.nop()
        a.deflabel(sk)
        if k % 4 == 3: m.pool_jump()
    m.imm32(L.BB_CUR, 1); a.mov_imm(0, 0); a.movl_st(0, 1)
    m.imm32('wdog', 2); a.jsr(2); a.nop()
    # --- OTHER aperta sulla voce di un addon -> DO IT abilitato ---
    nod = m.newlabel('nod')
    m.imm32(L.HOOKED, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(nod)
    m.imm32(EVQ + 0x68, 1); a.movl_ld(1, 0); m.imm32(OTHER_PAGE, 2); a.cmpeq(2, 0); a.bf_lbl(nod)
    m.imm32(OTHER_VAR, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(8, 2); a.cmphs(2, 0); a.bf_lbl(nod)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
    a.deflabel(nod); m.pool_jump()
    # --- crash precedente da riferire: appena non c'e' altro messaggio ---
    cpx = m.newlabel('cpx')
    m.imm32(L.SHOW, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bf_lbl(cpx)
    m.imm32(L.BB_CRPEND, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(cpx)
    a.mov_imm(0, 0); a.movl_st(0, 1); m.imm32(L.MSGKIND, 1); a.mov_imm(5, 0); a.movl_st(0, 1)
    m.imm32(L.SHOW, 1); m.imm32(768, 0); a.movl_st(0, 1)
    a.deflabel(cpx); m.pool_jump()
    # --- scritta iniziale (al posto dei messaggi finche' scorre) ---
    nsp = m.newlabel('nsp'); m.imm32(L.SPL_ON, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(nsp)
    m.imm32('splash', 2); a.jsr(2); a.nop(); m.imm32(done, 2); a.jmp(2); a.nop()
    a.deflabel(nsp); m.pool_jump()
    # --- messaggio persistente (riga 2) ---
    m.imm32(L.SHOW, 9); a.movl_ld(9, 13); a.tst_rr(13, 13); a.bt_lbl(done)
    a.add_imm(-1, 13); a.movl_st(13, 9); a.tst_rr(13, 13); nz = m.newlabel('nz'); a.bf_lbl(nz); m.os_redraw(); a.bra_lbl(done); a.nop()
    a.deflabel(nz); a.mov_rm_rn(13, 0); a.and_imm(0xF); a.tst_rr(0, 0); a.bf_lbl(done)
    m.imm32('showmsg', 2); a.jsr(2); a.nop()
    a.deflabel(done)
    end = m.newlabel('end'); m.imm32(end, 2); a.jmp(2); a.nop(); m.pool_jump()   # (v2: codice oltre la portata di bra)

    # ================= sync: percorre la lista sample, installa i nuovi, scarica gli spariti, ricostruisce OTHER =====
    a.deflabel('sync'); a.stsl_pr()
    m.imm32(L.FOUND, 1); a.mov_imm(0, 0); a.movl_st(0, 1)
    m.imm32(MGR, 1); a.movl_ld(1, 11)                                      # r11 = blocco corrente
    sl = m.newlabel('sl'); sn = m.newlabel('sn'); se = m.newlabel('se'); a.deflabel(sl)
    m.imm32(MGR, 1); a.movl_ld_disp(1, 4, 3); a.cmphs(3, 11); m.bt_far(se)  # r11 >= limite -> fine
    a.movl_ld(11, 12); a.cmppl(12); m.bf_far(se)                           # size <= 0 -> fine
    a.mov_rm_rn(11, 2); m.imm32(PCM_OFF, 0); a.add_rr(0, 2); m.imm32('chkmagic', 1); a.jsr(1); a.nop(); m.bf_far(sn)
    m.imm32('identify', 2); a.jsr(2); a.nop(); a.cmppz(0); fnd = m.newlabel('fnd'); a.bt_lbl(fnd)
    m.imm32('install', 2); a.jsr(2); a.nop(); a.bra_lbl(sn); a.nop()
    a.deflabel(fnd); m.imm32('setfound', 3); a.jsr(3); a.nop()
    a.deflabel(sn); a.movl_ld(11, 12); a.add_rr(12, 11); a.bra_lbl(sl); a.nop()
    a.deflabel(se); m.pool_jump()
    m.imm32(L.FOUND, 1); a.movl_ld(1, 4); m.imm32(L.INST(0), 1); a.mov_imm(1, 2); a.mov_imm(NSLOTS, 3)   # spariti -> INST=0
    kl = m.newlabel('kl'); kk = m.newlabel('kk'); a.deflabel(kl)
    a.tst_rr(2, 4); a.bf_lbl(kk); a.mov_imm(0, 0); a.movl_st(0, 1)
    a.deflabel(kk); a.shll(2); a.add_imm(4, 1); a.dt(3); a.bf_lbl(kl)
    m.imm32('rebuild_other', 2); a.jsr(2); a.nop()
    # la lista sample cresce verso la nostra RAM (SCRATCH 0x88F00000, HUB, slot): sotto 1 MB di margine avvisa
    m.imm32(L.LASTEND, 1); a.movl_ld(1, 0); m.imm32(L.MEM_WARN, 2); a.cmphs(2, 0); nw = m.newlabel('nw'); a.bf_lbl(nw)
    m.imm32(L.MSGKIND, 1); a.mov_imm(3, 0); a.movl_st(0, 1); m.imm32(L.SHOW, 1); m.imm32(256, 0); a.movl_st(0, 1)
    a.deflabel(nw)
    a.ldsl_pr(); a.rts(); a.nop(); m.pool_jump()
    # setfound: L.FOUND |= 1<<r0 (r0 = slot). Clobbera r0-r3.
    a.deflabel('setfound'); a.mov_imm(1, 2); a.tst_rr(0, 0); sf = m.newlabel('sf'); a.bt_lbl(sf)
    sfl = m.newlabel('sfl'); a.deflabel(sfl); a.shll(2); a.dt(0); a.bf_lbl(sfl)
    a.deflabel(sf); m.imm32(L.FOUND, 1); a.movl_ld(1, 3); a.or_rr(2, 3); a.movl_st(3, 1); a.rts(); a.nop()
    # identify(r11) -> r0 = slot con stesso nome8+somma, o -1. Clobbera r0-r3, r10.
    a.deflabel('identify'); m.imm32(H_SUM, 0); a.add_rr(11, 0); a.mov_rm_rn(0, 10)   # r10 -> sum, name @+4
    idn = m.newlabel('idn')
    for k in range(NSLOTS):
        nk = m.newlabel('nk')
        m.imm32(L.INST(k), 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(nk)
        m.imm32(L.SUM(k), 1); a.movl_ld(1, 2); a.movl_ld(10, 3); a.cmpeq(3, 2); a.bf_lbl(nk)
        m.imm32(L.NAME(k), 1); a.movl_ld(1, 2); a.movl_ld_disp(10, 4, 3); a.cmpeq(3, 2); a.bf_lbl(nk)
        a.movl_ld_disp(1, 4, 2); a.movl_ld_disp(10, 8, 3); a.cmpeq(3, 2); a.bf_lbl(nk)
        a.mov_imm(k, 0); a.rts(); a.nop()
        a.deflabel(nk)
    a.mov_imm(-1, 0); a.rts(); a.nop(); m.pool_jump()
    # install(r11 = blocco): primo slot libero, copia+somma, rilocazione, registrazione. Clobbera r0-r3, r8-r10, r12-r14. Salva PR.
    a.deflabel('install'); a.stsl_pr()
    a.mov_imm(0, 8)                                                         # r8 = k
    fl = m.newlabel('fl'); fnd2 = m.newlabel('fnd2'); nofree = m.newlabel('nofree'); a.deflabel(fl)
    a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.INST(0), 1); a.add_rr(0, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(fnd2)
    a.add_imm(1, 8); a.mov_imm(NSLOTS, 0); a.cmpeq(0, 8); a.bf_lbl(fl)
    a.deflabel(nofree); m.imm32(L.MSGKIND, 1); a.mov_imm(2, 0); a.movl_st(0, 1); a.bra_lbl('inst_msg'); a.nop()
    a.deflabel(fnd2); m.pool_jump()
    m.imm32(H_LEN, 0); a.add_rr(11, 0); a.movl_ld(0, 12)                    # r12 = len body
    m.imm32(H_SUM, 0); a.add_rr(11, 0); a.movl_ld(0, 14)                    # r14 = somma attesa
    bad = m.newlabel('bad'); ok = m.newlabel('ok')
    m.imm32(SLOT_CODE_MAX + 1, 3); a.cmphs(3, 12); m.bt_far(bad)
    a.tst_rr(12, 12); m.bt_far(bad)
    m.imm32('alloc', 2); a.jsr(2); a.nop(); a.tst_rr(13, 13); m.bt_far(nofree)       # r13 = base (pagine libere) o 0
    a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.BASE(0), 1); a.add_rr(0, 1); a.movl_st(13, 1)
    m.imm32(L.LEN(0), 1); a.add_rr(0, 1); a.movl_st(12, 1)
    pages_of(12, 2); a.shll8(2); a.shll2(2); a.shll2(2); a.add_rr(13, 2)               # v3: scratch = base + pagine del codice
    m.imm32(L.SCR(0), 1); a.add_rr(0, 1); a.movl_st(2, 1)
    m.pool_jump()
    # v3: pulizia della cache sul blocco (codice + scratch) PRIMA della copia: righe P1 rimaste da chi c'era prima
    # (scritte e non ancora riscritte in memoria) sovrascriverebbero il codice nuovo scritto via P2. Scrittura associativa
    # nell'address array (A = 1, V = U = 0): riscrive la riga se sporca e la invalida; se non c'e', niente.
    a.mov_rm_rn(13, 4); m.imm32(0x1FFFFC00, 7); m.imm32(0xF0000008, 6); m.imm32(0xFF0, 3)
    pages_of(12, 5); a.add_imm(1, 5); a.shll8(5)                                        # righe = pagine * 256
    pcl = m.newlabel('pcl'); a.deflabel(pcl)
    a.mov_rm_rn(4, 0); a.and_rr(3, 0); a.or_rr(6, 0); a.mov_rm_rn(4, 1); a.and_rr(7, 1); a.movl_st(1, 0)
    a.add_imm(16, 4); a.dt(5); a.bf_lbl(pcl)
    m.pool_jump()
    m.imm32(H_BODY, 9); a.add_rr(11, 9)                                     # r9 = sorgente
    a.mov_rm_rn(13, 10); a.mov_imm(0, 2); a.shlr(12); a.add_imm(1, 12)      # r10 = dst, r2 = somma, r12 = halfword (+1: dt)
    cp = m.newlabel('cp'); a.deflabel(cp); a.dt(12); a.bt_lbl(ok)
    a.movw_ldinc(9, 0); a.extu_w(0, 0); a.add_rr(0, 2); a.movw_st(0, 10); a.add_imm(2, 10); a.bra_lbl(cp); a.nop()
    a.deflabel(ok); a.cmpeq(14, 2); rl = m.newlabel('rl'); a.bt_lbl(rl)
    a.deflabel(bad)
    m.imm32(L.BADADDR, 1); a.movl_st(11, 1); m.imm32(L.BADGOT, 1); a.movl_st(2, 1); m.imm32(L.BADEXP, 1); a.movl_st(14, 1)
    m.imm32(L.MSGKIND, 1); a.mov_imm(1, 0); a.movl_st(0, 1); a.bra_lbl('inst_msg'); a.nop()
    a.deflabel(rl); m.pool_jump()
    # rilocazione: p = slot + code_len; n = *p++; per n: *(slot + *p++) += delta. v3: due delta, scelti dal valore
    # (offset dalla base di costruzione >= SLOT_CODE_MAX = indirizzo nella scratch -> delta della scratch)
    m.imm32(H_CODELEN, 0); a.add_rr(11, 0); a.movl_ld(0, 9); a.add_rr(13, 9)   # r9 = tabella
    a.movl_ld_inc(9, 12)                                                    # r12 = n
    m.imm32(ADDON_BASE + 1, 14); a.add_imm(-1, 14); a.mov_rm_rn(13, 10); a.sub(14, 10)   # r10 = delta (base di COSTRUZIONE degli addon: costante, +1 per non collidere in pool con la base dinamica)
    a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.SCR(0), 1); a.add_rr(0, 1); a.movl_ld(1, 14)
    a.movl_pool(ADDON_BASE + SLOT_CODE_MAX + 1, 1); a.add_imm(-1, 1); a.sub(1, 14)       # r14 = delta della scratch (literal grezzo: m.imm32 lo tradurrebbe nella scratch dell'HUB)
    m.imm32(0x1FFFFFFF, 5); m.imm32((ADDON_BASE & 0x1FFFFFFF) + 1, 6); a.add_imm(-1, 6); m.imm32(SLOT_CODE_MAX, 7)
    rle = m.newlabel('rle'); rll = m.newlabel('rll'); rls = m.newlabel('rls'); rlw = m.newlabel('rlw'); a.deflabel(rll); a.tst_rr(12, 12); a.bt_lbl(rle)
    a.movl_ld_inc(9, 0); a.add_rr(13, 0); a.movl_ld(0, 3)
    a.mov_rm_rn(3, 1); a.and_rr(5, 1); a.sub(6, 1); a.cmphs(7, 1); a.bt_lbl(rls)
    a.add_rr(10, 3); a.bra_lbl(rlw); a.nop()
    a.deflabel(rls); a.add_rr(14, 3)
    a.deflabel(rlw); a.movl_st(3, 0); a.add_imm(-1, 12); a.bra_lbl(rll); a.nop()
    a.deflabel(rle)
    a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.SCR(0), 1); a.add_rr(0, 1); a.movl_ld(1, 0)
    a.add_imm(SCRATCH_MAGIC_OFF, 0); a.mov_imm(0, 3); a.movl_st(3, 0)                  # scratch dell'addon: init riparte
    # v3: codice immutabile = fino agli oggetti pagina (o tutto il codice) -> TXT(k), somma u32 -> SUMC(k), nome -> BBNAME(k)
    m.imm32(H_PAGEOFF, 3); a.add_rr(11, 3); a.movl_ld(3, 3); a.mov_imm(-1, 2); a.cmpeq(2, 3); tx1 = m.newlabel('tx1'); a.bf_lbl(tx1)
    m.imm32(H_CODELEN, 3); a.add_rr(11, 3); a.movl_ld(3, 3)
    a.deflabel(tx1); a.mov_imm(-4, 2); a.and_rr(2, 3)
    a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.TXT(0), 1); a.add_rr(0, 1); a.movl_st(3, 1)
    a.mov_rm_rn(13, 5); a.mov_rm_rn(3, 6); a.shlr2(6); a.mov_imm(0, 7); tsd = m.newlabel('tsd'); a.tst_rr(6, 6); a.bt_lbl(tsd)
    tsl = m.newlabel('tsl'); a.deflabel(tsl); a.movl_ld_inc(5, 0); a.add_rr(0, 7); a.dt(6); a.bf_lbl(tsl)
    a.deflabel(tsd); a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.SUMC(0), 1); a.add_rr(0, 1); a.movl_st(7, 1)
    a.mov_rm_rn(8, 0); a.shll2(0); a.shll(0); m.imm32(L.BBNAME(0), 1); a.add_rr(0, 1)
    m.imm32(H_NAME, 3); a.add_rr(11, 3); a.movl_ld(3, 2); a.movl_st(2, 1); a.movl_ld_disp(3, 4, 2); a.movl_st_disp(2, 4, 1)
    m.pool_jump()
    # registrazione slot k (r8): INST, SUM, NAME, PAGE, MENU
    a.mov_rm_rn(8, 0); a.shll2(0); m.imm32(L.INST(0), 1); a.add_rr(0, 1); a.mov_imm(1, 3); a.movl_st(3, 1)
    m.imm32(L.SUM(0), 1); a.add_rr(0, 1); m.imm32(H_SUM, 3); a.add_rr(11, 3); a.movl_ld(3, 3); a.movl_st(3, 1)   # (r14 e' stato riusato per la rilocazione)
    m.imm32(L.PAGE(0), 1); a.add_rr(0, 1); m.imm32(H_PAGEOFF, 3); a.add_rr(11, 3); a.movl_ld(3, 3)
    a.mov_imm(-1, 2); a.cmpeq(2, 3); np_ = m.newlabel('np'); a.bf_lbl(np_); a.mov_imm(0, 3); a.bra_lbl('pgst'); a.nop()
    a.deflabel(np_); a.add_rr(13, 3); a.deflabel('pgst'); a.movl_st(3, 1)
    a.mov_rm_rn(8, 0); a.shll2(0); a.shll(0); m.imm32(L.NAME(0), 1); a.add_rr(0, 1)                     # NAME[k] = 8 byte
    m.imm32(H_NAME, 3); a.add_rr(11, 3); a.movl_ld(3, 2); a.movl_st(2, 1); a.movl_ld_disp(3, 4, 2); a.movl_st_disp(2, 4, 1)
    a.mov_rm_rn(8, 0); a.shll2(0); a.shll2(0); m.imm32(L.MENU(0), 1); a.add_rr(0, 1)                    # MENU[k] = 16 byte
    m.imm32(H_MENU, 3); a.add_rr(11, 3)
    for d in (0, 4, 8, 12): a.movl_ld_disp(3, d, 2); a.movl_st_disp(2, d, 1)
    a.mov_rm_rn(8, 0); m.imm32('setfound', 3); a.jsr(3); a.nop()
    m.imm32(L.MSGKIND, 1); a.mov_imm(0, 0); a.movl_st(0, 1); m.imm32(L.SHOWSLOT, 1); a.movl_st(8, 1)
    a.deflabel('inst_msg'); m.imm32(L.SHOW, 1); m.imm32(256, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop(); m.pool_jump()
    # alloc(r12 = len body) -> r13 = base del primo buco di pagine libero (codice + pagina scratch a +SCR_PAGE), 0 = niente.
    # Ricostruisce la mappa dagli slot installati (cosi' gli addon scaricati liberano le pagine). Foglia, clobbera r0-r7.
    a.deflabel('alloc')
    m.imm32(L.MAP, 1); a.mov_imm(0, 0); a.mov_imm(NPAGES // 4, 2)
    acl = m.newlabel('acl'); a.deflabel(acl); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(acl)
    m.imm32(L.INST(0), 4); m.imm32(L.BASE(0), 5); m.imm32(L.LEN(0), 6); a.mov_imm(NSLOTS, 7)
    amk = m.newlabel('amk'); amn = m.newlabel('amn'); a.deflabel(amk)
    a.movl_ld(4, 0); a.tst_rr(0, 0); a.bt_lbl(amn)
    a.movl_ld(5, 1); m.imm32(L.SLOT(0), 0); a.sub(0, 1); a.shlr8(1); a.shlr2(1); a.shlr2(1)     # r1 = pagina iniziale
    m.imm32(L.MAP, 0); a.add_rr(0, 1); a.mov_imm(1, 0)
    a.movl_ld(6, 2); pages_of(2, 2); a.add_imm(1, 2)                                              # v3: codice + scratch, contigui
    am1 = m.newlabel('am1'); a.deflabel(am1); a.movb_st(0, 1); a.add_imm(1, 1); a.dt(2); a.bf_lbl(am1)
    a.deflabel(amn); a.add_imm(4, 4); a.add_imm(4, 5); a.add_imm(4, 6); a.dt(7); a.bf_lbl(amk)
    m.pool_jump()
    pages_of(12, 7); a.add_imm(1, 7)                                        # r7 = pagine del blocco (codice + scratch)
    m.imm32(L.MAP, 4); a.mov_imm(NPAGES + 1, 6); a.sub(7, 6)                # r4 = &map[p], r6 = NPAGES - r7 + 1 posizioni
    anx = m.newlabel('anx'); anz = m.newlabel('anz'); a.cmppl(6); a.bf_lbl(anz)
    asl = m.newlabel('asl'); a.deflabel(asl)
    a.mov_rm_rn(4, 1); a.mov_rm_rn(7, 2)
    ac2 = m.newlabel('ac2'); a.deflabel(ac2); a.movb_ld(1, 0); a.tst_rr(0, 0); a.bf_lbl(anx); a.add_imm(1, 1); a.dt(2); a.bf_lbl(ac2)
    a.mov_rm_rn(4, 13); m.imm32(L.MAP, 0); a.sub(0, 13); a.shll8(13); a.shll2(13); a.shll2(13)
    m.imm32(L.SLOT(0), 0); a.add_rr(0, 13); a.rts(); a.nop()
    a.deflabel(anx); a.add_imm(1, 4); a.dt(6); a.bf_lbl(asl)
    a.deflabel(anz); a.mov_imm(0, 13); a.rts(); a.nop()
    m.pool_jump()

    # ================= rebuild_other: tabella enum, label, wrapper, clamp della voce =================
    a.deflabel('rebuild_other'); a.stsl_pr()
    m.imm32(L.HOOKED, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); hk = m.newlabel('hk'); a.bf_lbl(hk)
    a.mov_imm(1, 0); a.movl_st(0, 1)
    m.imm32(OTHER_PAGE + 0x30, 1); a.movl_ld(1, 0); m.imm32(L.REG_ORIG_HANDLER, 2); a.movl_st(0, 2)
    m.imm32(OTHER_MODEL + 0x20, 1); a.movl_ld(1, 0); m.imm32(L.REG_ORIG_DESC, 2); a.movl_st(0, 2)
    m.imm32(L.REG_PREV, 1); m.imm32(OTHER_LAST, 0); a.movl_st(0, 1)
    m.imm32(L.REG_ACTIVE, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    m.imm32(L.HINT, 1)
    for ch in b"Press DO IT\0": a.mov_imm(ch, 0); a.movb_st(0, 1); a.add_imm(1, 1)
    a.deflabel(hk); m.pool_jump()
    # stringhe originali -> L.STRBUF (r9 = dst)
    m.imm32(L.REG_ORIG_DESC, 1); a.movl_ld(1, 1); a.movl_ld(1, 12); a.movl_ld_disp(1, 4, 8)   # r12 = count orig, r8 = src
    m.imm32(L.STRBUF, 9)
    so = m.newlabel('so'); soc = m.newlabel('soc'); a.deflabel(so)
    a.deflabel(soc); a.movb_ldinc(8, 0); a.movb_st(0, 9); a.add_imm(1, 9); a.tst_rr(0, 0); a.bf_lbl(soc)
    a.dt(12); a.bf_lbl(so)
    # voci degli addon con pagina: r10 = j
    a.mov_imm(0, 10)
    for k in range(NSLOTS):
        nm = m.newlabel('nm')
        m.imm32(L.INST(k), 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(nm)
        m.imm32(L.PAGE(k), 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(nm)
        m.imm32(L.MENU(k), 8); a.mov_imm(16, 12)
        mc = m.newlabel('mc'); mce = m.newlabel('mce'); a.deflabel(mc)
        a.movb_ldinc(8, 0); a.tst_rr(0, 0); a.bt_lbl(mce); a.movb_st(0, 9); a.add_imm(1, 9); a.dt(12); a.bf_lbl(mc)
        a.deflabel(mce); a.mov_imm(0, 0); a.movb_st(0, 9); a.add_imm(1, 9)
        a.mov_rm_rn(10, 0); a.shll2(0); m.imm32(L.MSLOT(0), 1); a.add_rr(0, 1); a.mov_imm(k, 0); a.movl_st(0, 1)
        a.add_imm(1, 10)
        a.deflabel(nm); m.pool_jump()
    m.imm32(L.NMENU, 1); a.movl_st(10, 1)
    # L.DESC = {count orig + N, L.STRBUF, 0, 0x104}
    m.imm32(L.REG_ORIG_DESC, 1); a.movl_ld(1, 1); a.movl_ld(1, 12); a.add_rr(10, 12)
    m.imm32(L.DESC, 1); a.movl_st(12, 1); m.imm32(L.STRBUF, 0); a.movl_st_disp(0, 4, 1); a.mov_imm(0, 0); a.movl_st_disp(0, 8, 1); m.imm32(0x104, 0); a.movl_st_disp(0, 12, 1)
    # label j (j < L.NMENU): parent OTHER_PAGE, next, rect, flags, mask 1<<(8+j), vt, testo
    for j in range(NSLOTS):
        LB = L.LABELS + j*0x28; lj = m.newlabel('lj'); lje = m.newlabel('lje')
        m.imm32(LB, 1); a.mov_imm(0, 0)
        for d in (0, 8, 0xC, 0x18, 0x20): a.movl_st_disp(0, d, 1)
        m.imm32(OTHER_PAGE, 0); a.movl_st_disp(0, 4, 1); m.imm32(RECT_HINT, 0); a.movl_st_disp(0, 0x10, 1)
        m.imm32(0x90000000, 0); a.movl_st_disp(0, 0x14, 1); m.imm32(VT_LABEL, 0); a.movl_st_disp(0, 0x1C, 1); m.imm32(L.HINT, 0); a.movl_st_disp(0, 0x24, 1)
        a.mov_imm(j, 0); a.cmphs(10, 0); a.bt_lbl(lje)                       # j >= L.NMENU -> resta staccata
        m.imm32(0x100 << j, 0); a.movl_st_disp(0, 0x18, 1)
        if j + 1 < NSLOTS:
            a.mov_imm(j + 1, 0); a.cmphs(10, 0); a.bt_lbl(lje); m.imm32(LB + 0x28, 0); a.movl_st_disp(0, 8, 1)
        a.deflabel(lje); m.pool_jump()
    # aggancio / ripristino
    a.tst_rr(10, 10); non = m.newlabel('non'); a.bt_lbl(non)
    m.imm32(OTHER_LAST + 8, 1); m.imm32(L.LABELS, 0); a.movl_st(0, 1)
    m.imm32(OTHER_PAGE + 0x30, 1); m.imm32('owrap', 0); a.movl_st(0, 1)
    m.imm32(OTHER_MODEL + 0x20, 1); m.imm32(L.DESC, 0); a.movl_st(0, 1)
    a.bra_lbl('clamp'); a.nop()
    a.deflabel(non); m.imm32(OTHER_LAST + 8, 1); a.mov_imm(0, 0); a.movl_st(0, 1)
    m.imm32(L.REG_ORIG_HANDLER, 1); a.movl_ld(1, 0); m.imm32(OTHER_PAGE + 0x30, 1); a.movl_st(0, 1)
    m.imm32(L.REG_ORIG_DESC, 1); a.movl_ld(1, 0); m.imm32(OTHER_MODEL + 0x20, 1); a.movl_st(0, 1)
    a.deflabel('clamp'); m.pool_jump()
    m.imm32(OTHER_VAR, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.cmphs(12, 0); ncl = m.newlabel('ncl'); a.bf_lbl(ncl)
    a.mov_imm(0, 0); a.movb_st(0, 1)
    a.deflabel(ncl); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop(); m.pool_jump()

    # ================= wdog: controllo del codice a rotazione (WCHUNK byte per fire, uno slot alla volta) =================
    # Somma del codice immutabile dello slot WK; a fine giro diversa da SUMC -> INST = 2 (non piu' chiamato), voce OTHER via,
    # "MEM ERR <nome>" (riga 2), nome nella scatola nera. SHIFT+ERASE lo reinstalla pulito. Clobbera r0-r12. Salva PR.
    a.deflabel('wdog'); a.stsl_pr(); wnx = m.newlabel('wnx'); wrt = m.newlabel('wrt')
    m.imm32(L.WK, 1); a.movl_ld(1, 8); a.mov_imm(NSLOTS - 1, 0); a.and_rr(0, 8); a.mov_rm_rn(8, 9); a.shll2(9)
    m.imm32(L.INST(0), 1); a.add_rr(9, 1); a.movl_ld(1, 0); a.mov_imm(1, 2); a.cmpeq(2, 0); m.bf_far(wnx)
    m.imm32(L.TXT(0), 1); a.add_rr(9, 1); a.movl_ld(1, 10)                                   # r10 = byte da controllare
    m.imm32(L.WOFF, 1); a.movl_ld(1, 11)                                                      # r11 = fin qui
    m.imm32(L.BASE(0), 1); a.add_rr(9, 1); a.movl_ld(1, 5); a.add_rr(11, 5)
    a.mov_rm_rn(10, 6); a.sub(11, 6); m.imm32(WCHUNK, 0); a.cmphs(0, 6); wsm = m.newlabel('wsm'); a.bf_lbl(wsm); a.mov_rm_rn(0, 6)
    a.deflabel(wsm); a.shlr2(6)
    m.imm32(L.WSUM, 1); a.movl_ld(1, 7); wsd = m.newlabel('wsd'); a.tst_rr(6, 6); a.bt_lbl(wsd)
    wsl = m.newlabel('wsl'); a.deflabel(wsl); a.movl_ld_inc(5, 0); a.add_rr(0, 7); a.dt(6); a.bf_lbl(wsl)
    a.deflabel(wsd); m.imm32(L.WSUM, 1); a.movl_st(7, 1)
    m.imm32(WCHUNK, 0); a.add_rr(0, 11); m.imm32(L.WOFF, 1); a.movl_st(11, 1)
    a.cmphs(10, 11); m.bf_far(wrt)                                                            # slot non finito
    m.imm32(L.SUMC(0), 1); a.add_rr(9, 1); a.movl_ld(1, 0); a.cmpeq(7, 0); m.bt_far(wnx)     # uguale: sano
    m.pool_jump()
    m.imm32(L.INST(0), 1); a.add_rr(9, 1); a.mov_imm(2, 0); a.movl_st(0, 1)                    # CODICE CAMBIATO: fermo
    m.imm32(L.PAGE(0), 1); a.add_rr(9, 1); a.mov_imm(0, 0); a.movl_st(0, 1)
    m.imm32(L.BB_NERR, 1); a.movl_ld(1, 0); a.add_imm(1, 0); a.movl_st(0, 1)
    a.mov_rm_rn(8, 0); a.shll2(0); a.shll(0); m.imm32(L.BBNAME(0), 1); a.add_rr(0, 1); m.imm32(L.BB_ERRN, 2)
    a.movl_ld(1, 3); a.movl_st(3, 2); a.movl_ld_disp(1, 4, 3); a.movl_st_disp(3, 4, 2)
    m.imm32(L.MSGKIND, 1); a.mov_imm(4, 0); a.movl_st(0, 1); m.imm32(L.SHOW, 1); m.imm32(768, 0); a.movl_st(0, 1)
    m.imm32('rebuild_other', 2); a.jsr(2); a.nop()
    a.deflabel(wnx); m.pool_jump()
    m.imm32(L.WK, 1); a.movl_ld(1, 0); a.add_imm(1, 0); a.mov_imm(NSLOTS - 1, 2); a.and_rr(2, 0); a.movl_st(0, 1)
    a.mov_imm(0, 0); m.imm32(L.WOFF, 1); a.movl_st(0, 1); m.imm32(L.WSUM, 1); a.movl_st(0, 1)
    a.deflabel(wrt); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ================= owrap: handler OTHER (r4 pagina, r5 msg) =================
    a.deflabel('owrap')
    ow = m.newlabel('ow')
    a.movw_ld(5, 0); a.extu_w(0, 0); a.mov_imm(2, 2); a.cmpeq(2, 0); a.bf_lbl(ow)
    a.mov_rm_rn(5, 1); a.add_imm(2, 1); a.movw_ld(1, 0); a.extu_w(0, 0); a.mov_imm(EV_DOIT, 2); a.cmpeq(2, 0); a.bf_lbl(ow)
    m.imm32(OTHER_VAR, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.add_imm(-8, 0); a.cmppz(0); a.bf_lbl(ow)
    m.imm32(L.NMENU, 1); a.movl_ld(1, 2); a.cmphs(2, 0); a.bt_lbl(ow)        # j >= L.NMENU -> originale
    a.shll2(0); m.imm32(L.MSLOT(0), 1); a.add_rr(0, 1); a.movl_ld(1, 0)      # k
    a.shll2(0); m.imm32(L.PAGE(0), 1); a.add_rr(0, 1); a.movl_ld(1, 5)        # pagina
    a.stsl_pr(); m.imm32(EVQ, 4); a.mov_imm(0, 6); m.imm32(OS_PUSH_PAGE, 2); a.jsr(2); a.nop(); a.ldsl_pr()
    a.mov_imm(1, 0); a.rts(); a.nop()
    a.deflabel(ow); m.imm32(L.REG_ORIG_HANDLER, 2); a.movl_ld(2, 2); a.jmp(2); a.nop()
    m.pool_jump()

    # ================= splash: un fotogramma della scritta (r0-r14 liberi: salvati dal loader flash) =================
    a.deflabel('splash'); a.stsl_pr()
    m.imm32(L.SPL_F, 1); a.movl_ld(1, 8); a.add_imm(1, 8); a.movl_st(8, 1); a.shlr2(8); a.shlr2(8)   # r8 = fire / 16
    m.imm32(FRAMES, 2); a.movl_ld(2, 3); m.imm32(L.SPL_T0, 2); a.movl_ld(2, 2); a.sub(2, 3)            # r3 = frame passati
    sp1 = m.newlabel('sp1'); a.tst_rr(3, 3); a.bt_lbl(sp1)                                  # frame fermi: conta i fire
    m.imm32(FPC_MUL, 2); a.mul_l(2, 3); a.sts_macl(8); a.shlr16(8); a.shlr2(8); a.shlr2(8); a.shlr2(8)   # r8 = frame * 761 >> 22
    a.deflabel(sp1)
    m.imm32(L.SPL_POS, 1); a.movl_ld(1, 9); a.mov_imm(-1, 2); a.cmpeq(2, 9); sp2 = m.newlabel('sp2'); m.bf_far(sp2)
    # primo fotogramma: CGRAM dell'OS salvata, forme caricate, caratteri dell'OS deviati
    m.imm32(L.SPL_CGS, 11)
    for k in range(8):
        a.mov_imm(0x40 | k << 3, 1); a.bsr_lbl('spl_cmd'); a.nop()
        for i in range(8): a.bsr_lbl('spl_rd'); a.nop(); a.movb_st(0, 11); a.add_imm(1, 11)
    m.pool_jump()
    a.movl_pool('spl_glyph', 11); a.mov_imm(0x40, 1); a.bsr_lbl('spl_cmd'); a.nop(); a.mov_imm(64, 12)
    sgl = m.newlabel('sgl'); a.deflabel(sgl); a.movb_ldinc(11, 1); a.bsr_lbl('spl_dat'); a.nop(); a.dt(12); a.bf_lbl(sgl)
    m.imm32(LCD_DRV_DATA, 1); m.imm32(L.SPL_DUMMY, 0); a.movl_st(0, 1)
    m.imm32(L.LDR_SHOW, 1); a.mov_imm(0, 0); a.movl_st(0, 1)                               # loader v10: basta "LOADED hub"
    a.deflabel(sp2)
    m.imm32(SPL_STEPS, 2); a.cmphs(2, 8); spe = m.newlabel('spe'); m.bt_far(spe)            # tutta passata: fine
    a.cmpeq(8, 9); spx = m.newlabel('spx'); m.bt_far(spx)                                  # stessa colonna: niente
    m.imm32(L.SPL_POS, 1); a.movl_st(8, 1)
    a.mov_imm(0x0C, 1); a.bsr_lbl('spl_cmd'); a.nop()                                      # cursore spento
    for row, lbl in ((0x80, 'spl_top'), (0xC0, 'spl_bot')):
        a.mov_imm(row - 256, 1); a.bsr_lbl('spl_cmd'); a.nop()
        a.movl_pool(lbl, 10); a.mov_rm_rn(8, 12); a.add_imm(-16, 12); a.mov_imm(16, 11)     # r12 = colonna del testo
        scl = m.newlabel('scl'); scs = m.newlabel('scs'); a.deflabel(scl)
        a.mov_imm(0x20, 1); m.imm32(SPL_N, 3); a.cmphs(3, 12); a.bt_lbl(scs)               # fuori dal testo: spazio
        a.mov_rm_rn(10, 2); a.add_rr(12, 2); a.movb_ld(2, 1); a.extu_b(1, 1)
        a.deflabel(scs); a.bsr_lbl('spl_dat'); a.nop(); a.add_imm(1, 12); a.dt(11); a.bf_lbl(scl)
        m.pool_jump()
    a.bra_lbl(spx); a.nop()
    a.deflabel(spe)                                                                         # fine: CGRAM e schermo all'OS
    m.imm32(L.SPL_CGS, 11)
    for k in range(8):
        a.mov_imm(0x40 | k << 3, 1); a.bsr_lbl('spl_cmd'); a.nop()
        for i in range(8): a.movb_ldinc(11, 1); a.mov_rm_rn(1, 0); a.and_imm(0x1F); a.mov_rm_rn(0, 1); a.bsr_lbl('spl_dat'); a.nop()
    m.pool_jump()
    a.mov_imm(-128, 1); a.bsr_lbl('spl_cmd'); a.nop()                                      # di nuovo DDRAM
    m.imm32(LCD_DRV_DATA, 1); m.imm32(LCD_DATA, 0); a.movl_st(0, 1); m.os_redraw()
    m.imm32(L.SPL_ON, 1); a.mov_imm(0, 0); a.movl_st(0, 1)
    # la scritta copre i messaggi "LOADED <nome>" / "HUB v3 LOADED" arrivati mentre scorreva; gli errori restano
    m.imm32(L.MSGKIND, 1); a.movl_ld(1, 0); spk = m.newlabel('spk'); spc = m.newlabel('spc')
    a.tst_rr(0, 0); a.bt_lbl(spc); a.mov_imm(6, 2); a.cmpeq(2, 0); a.bf_lbl(spk)
    a.deflabel(spc); m.imm32(L.SHOW, 1); a.mov_imm(0, 0); a.movl_st(0, 1)
    a.deflabel(spk)
    a.deflabel(spx); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # spl_cmd / spl_dat: r1 al registro comandi / dati dopo il busy flag (bit 7 del registro comandi); spl_rd: r0 = dato.
    # Usano r0, r2, r3.
    for lbl, reg in (('spl_cmd', LCD_CMD), ('spl_dat', LCD_DATA)):
        a.deflabel(lbl); m.imm32(LCD_CMD, 2); wl = m.newlabel('wl'); a.deflabel(wl); a.movb_ld(2, 0); a.tst_imm(0x80); a.bf_lbl(wl)
        a.mov_imm(16, 3); wd = m.newlabel('wd'); a.deflabel(wd); a.dt(3); a.bf_lbl(wd)       # margine dopo il busy
        m.imm32(reg, 2); a.rts(); a.movb_st(1, 2)
    a.deflabel('spl_rd'); m.imm32(LCD_CMD, 2); wr_ = m.newlabel('wr'); a.deflabel(wr_); a.movb_ld(2, 0); a.tst_imm(0x80); a.bf_lbl(wr_)
    a.mov_imm(16, 3); wd2 = m.newlabel('wd'); a.deflabel(wd2); a.dt(3); a.bf_lbl(wd2)
    m.imm32(LCD_DATA, 2); a.rts(); a.movb_ld(2, 0)
    a.pool_here()
    for lbl, data in (('spl_top', SPL_TOP), ('spl_bot', SPL_BOT), ('spl_glyph', SPL_GLYPH)):
        while (len(a.words) * 2 + a.base) & 3: a.nop()
        a.deflabel(lbl); d = data + bytes(len(data) & 1)
        for i in range(0, len(d), 2): a._emit(d[i] | d[i + 1] << 8)
    while (len(a.words) * 2 + a.base) & 3: a.nop()

    # ================= showmsg (riga 2) =================
    a.deflabel('showmsg'); a.stsl_pr(); m.lcd_line2()
    m.imm32(L.MSGKIND, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); k1 = m.newlabel('k1'); a.bf_lbl(k1)
    m.lcd_str("LOADED ")
    m.imm32(L.SHOWSLOT, 1); a.movl_ld(1, 8); a.shll2(8); a.shll(8); m.imm32(L.NAME(0), 0); a.add_rr(0, 8); a.mov_imm(8, 12)
    nl = m.newlabel('nl'); a.deflabel(nl); a.movb_ldinc(8, 1); a.extu_b(1, 1); m.lcd_char_reg(1); a.dt(12); a.bf_lbl(nl)
    m.lcd_str(" "); a.bra_lbl('sm_end'); a.nop()
    a.deflabel(k1); m.pool_jump(); a.mov_imm(1, 2); a.cmpeq(2, 0); k2 = m.newlabel('k2'); a.bf_lbl(k2)
    m.lcd_str("BAD "); m.imm32(L.BADADDR, 1); a.movl_ld(1, 8); m.lcd_hex_reg(8, 8); m.lcd_str("    "); a.bra_lbl('sm_end'); a.nop()
    a.deflabel(k2); m.pool_jump(); a.mov_imm(2, 2); a.cmpeq(2, 0); k3 = m.newlabel('k3'); a.bf_lbl(k3)
    m.lcd_str("NO FREE SLOT    "); a.bra_lbl('sm_end'); a.nop()
    a.deflabel(k3); m.pool_jump(); a.mov_imm(3, 2); a.cmpeq(2, 0); k4 = m.newlabel('k4'); a.bf_lbl(k4)
    m.lcd_str("LOW MEM: ADDONS!"); a.bra_lbl('sm_end'); a.nop()
    a.deflabel(k4); m.pool_jump(); a.mov_imm(6, 2); a.cmpeq(2, 0); k6 = m.newlabel('k6'); a.bf_lbl(k6)
    m.lcd_str("HUB v3 LOADED   "); a.bra_lbl('sm_end'); a.nop()
    a.deflabel(k6); m.pool_jump(); a.mov_imm(4, 2); a.cmpeq(2, 0); k5 = m.newlabel('k5')
    a.bf_lbl(k5); m.lcd_str("MEM ERR "); m.imm32(L.BB_ERRN, 8); a.bra_lbl('sm_nm'); a.nop()
    a.deflabel(k5); m.lcd_str("CRASH:"); m.imm32(L.BB_CRASH, 8)
    a.deflabel('sm_nm'); a.mov_imm(8, 12)
    nl2 = m.newlabel('nl2'); a.deflabel(nl2); a.movb_ldinc(8, 1); a.extu_b(1, 1); m.lcd_char_reg(1); a.dt(12); a.bf_lbl(nl2)
    m.lcd_str("  ")
    a.deflabel('sm_end'); a.ldsl_pr(); a.rts(); a.nop(); m.pool_jump()
    # chkmagic(r2) -> T se @r2 c'e' MPCADDN3 (halfword). Clobbera r0, r3.
    a.deflabel('chkmagic'); fail = m.newlabel('mfail')
    for hw in MAGIC_HW:
        a.movw_ld(2, 0); a.extu_w(0, 0); m.imm32(hw, 3); a.cmpeq(3, 0); a.bf_lbl(fail); a.add_imm(2, 2)
    a.sett(); a.rts(); a.nop()
    a.deflabel(fail); a.clrt(); a.rts(); a.nop()
    a.deflabel(end)

def expected_code(code, rel, base, scr):
    """(test) codice atteso nello slot con l'HUB v3: ogni parola rilocata riceve il delta del codice (base - ADDON_BASE)
    o, se punta nella scratch di costruzione (offset >= SLOT_CODE_MAX), quello della scratch (scr - ADDON_BASE - 0x7000)"""
    out = bytearray(code); bc = ADDON_BASE & 0x1FFFFFFF
    for off in rel:
        v = struct.unpack('<I', out[off:off + 4])[0]
        d = (scr - ADDON_BASE - SLOT_CODE_MAX) if ((v & 0x1FFFFFFF) - bc) & 0xFFFFFFFF >= SLOT_CODE_MAX else (base - ADDON_BASE)
        out[off:off + 4] = struct.pack('<I', (v + d) & 0xffffffff)
    return bytes(out)

def runtime_addr(addr, base, scr):
    """(test) indirizzo di costruzione di un add-on (codice/oggetti o slot della scratch) -> indirizzo vero con l'HUB v3"""
    o = (addr & 0x1FFFFFFF) - (ADDON_BASE & 0x1FFFFFFF)
    return scr + (o - SLOT_CODE_MAX) if o >= SLOT_CODE_MAX else base + o

hub = Addon("hub")
hub.cdly = 0x1000
hub.background(body)
if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'HUB.WAV')
    hub.build_wav(out, hub=True)
