#!/usr/bin/env python3
"""
mpcmod - SDK di modding per Akai MPC500 (firmware SH3 little-endian).
Incapsula il ciclo PROVATO SUL FERRO: scrivi una funzione custom in ASM SH ->
la iniettiamo come hook nel main-loop dell'OS -> ricalcolo CRC -> .bin flashabile.

Modello:
  - L'OS gira da SDRAM. Nel main-task ci sono call indirette via literal pool: rimpiazziamo il puntatore con
    l'indirizzo di un TRAMPOLINO in area flash libera (0xA00103EC, ~1.3KB).
  - Il trampolino: chiama l'ORIG (cosi' l'OS continua a funzionare), poi esegue il TUO codice, poi ritorna.
  - HOOK disponibili (Addon(name, hook=...)):
      'loop' (DEFAULT) = pool @file 0x2B328 (ORIG 0x884BFE70, flash 0xA001B514), chiamata a OGNI iterazione del
               do{}while(true) del main task (FUN_a001af08) -> hook CONTINUO. Introdotto 2026-09-15.
      'init' = pool @file 0x2B2CC (ORIG 0x884F2B94), call-site 0xA001B0FC nella sequenza di init del main task
               -> parte UNA SOLA VOLTA al boot. (Era creduto continuo: il test "1024 fire" usava un contatore
               in scratch non azzerata.) Utile per patch/one-shot al boot.

Uso tipico:
    from mpcmod import Addon
    a = Addon("hello")
    def body(m):
        m.lcd_clear(); m.bigdelay()
        m.lcd_line1(); m.lcd_str("HELLO ADDON")
    a.background(body)          # gira ogni fire e ritorna all'OS
    a.build("mpc500_hello.bin") # scrive il .bin con CRC corretto

Install: copia il .bin come MPC500.bin sulla root della CF, alimentatore AC, tieni SHIFT, accendi, premi REC.
Recovery: riflasha mpc500.bin originale (bootloader intatto).
"""
import struct, zlib
from shasm import SHAsm

# --- costanti hardware/firmware (tutte confermate) ---
FW          = 'mpc500.bin'
HOOKS = {   # nome -> (file offset del literal pool da rimpiazzare, valore originale = funzione da richiamare)
    'loop': (0x2B328, 0x884BFE70),   # main-loop, continuo (default)
    'init': (0x2B2CC, 0x884F2B94),   # init del main task, one-shot al boot
}
POOL_FO, ORIG = HOOKS['loop']  # default (compat: usati da validate.py)
TRAMP_FO    = 0x103EC          # file offset area libera per il trampolino
TRAMP_FLASH = 0xA00103EC       # indirizzo runtime (flash P2) del trampolino
TRAMP_MAX   = 0x514            # spazio disponibile (byte)
LCD_CMD     = 0xB6000000       # HD44780 command reg (CS6)
LCD_DATA    = 0xB6000004       # HD44780 data reg
SCRATCH     = 0x88F00000       # RAM scratch libera per addon (contatori/stato/flag)
SCRATCH_MAGIC_OFF = 0x40       # dove init_scratch() mette la firma
SCRATCH_MAGIC     = 0x4D504301 # 'MPC\x01'
# mappa memoria utile agli addon:
SDRAM_BASE  = 0x88000000       # RAM di lavoro (16MB)
SAMPLE_BANK = 0xA1300000       # bank sample (linked-list: [0]=size, +4=nome16, +0x18=frame, +0x92=canali, +0x40=PCM)
SAMPLE_BANK_END = 0xA1800000
# --- INPUT a runtime (reverse 2026-09-15, drivers/KEYS.md; tabella CONFERMATA SU HW tasto per tasto) ---
KEYS_ADDR   = 0x881C0640       # u32 stato matrice scritto dall'ISR: bit=col*8+riga, 1=premuto (sola lettura)
WHEEL_ADDR  = 0x881C0670       # int32 accumulatore rotella (l'OS lo consuma: leggere il delta tra fire)
KEYBIT = {                     # nome -> bit in KEYS_ADDR
    'REC':0x00,'SHIFT':0x01,'SEQUENCE':0x02,'MODE':0x03,'TAP_TEMPO':0x04,'BANK_A':0x05,'CURSOR_LEFT':0x07,
    'OVERDUB':0x08,'STEP_BACK':0x09,'TRACK':0x0A,'TIMING_CORRECT':0x0B,'NOTE_REPEAT':0x0C,'BANK_B':0x0D,'CURSOR_DOWN':0x0F,
    'STOP':0x10,'STEP_FWD':0x11,'PARAMS':0x12,'METRONOME':0x13,'TRACK_MUTE':0x14,'BANK_C':0x15,'CURSOR_UP':0x17,
    'PLAY':0x18,'PLAY_START':0x19,'ERASE':0x1A,'NUMERIC':0x1B,'STATUS':0x1C,'BANK_D':0x1D,'CURSOR_RIGHT':0x1F,
    'QLINK_AFTER':0x06,'FULL_LEVEL':0x16,'LEVELS_12':0x1E,
    # bit 0x0E (col1 riga6, id 0): nessun tasto fisico trovato (31 tasti reali su 32 posizioni)
}
def keymask(*names):
    """maschera u32 per una combinazione di tasti, es. keymask('SHIFT','STOP')"""
    return sum(1 << KEYBIT[n] for n in names)

# --- ADDON CARICABILI DA CF (tappa 2, 2026-09-15) ---
BANK        = 0xA1300000       # sample bank (uncached): linked-list [0]=size,+4=nome16,+0x1C=ptr PCM?,+0x40=PCM
BANK_END    = 0xA1800000
EXEC        = 0xA8F10000       # dove il loader FLASH copia+esegue il codice caricato (alias P2 uncached di 0x88F10000)
HUB_SCRATCH = 0x88F00100       # scratch dell'addon caricato dal loader flash (= HUB; init_scratch usa la firma a +0x40)
ADDON_MAGIC = b'MPCADDN2'      # formato loader flash v6/v7 (obsoleto: v8 carica MPCHUB_3 rilocabile)
# --- ADDON MULTIPLI (2026-09-18): l'HUB (sdk/hub.py -> HUB.WAV, caricato dal loader flash in EXEC) gestisce NSLOTS slot
# per addon RILOCABILI (MPCADDN3): il WAV contiene codice+oggetti costruiti a ADDON_BASE piu' una tabella di rilocazione
# (offset delle parole a 32 bit che contengono indirizzi); l'HUB copia nello slot libero e somma il delta. La scratch di
# ogni addon sta DENTRO il suo slot (base+0x7000) cosi' si riloca con lo stesso delta.
# --- v8 (2026-09-18): area addon DINAMICA in cima alla memoria sample. Il loader flash legge MGR[1] (limite sample,
# 0x90000000 sul ferro con DIMM), riserva RESERVE byte in cima (B = MGR[1]-RESERVE), abbassa MGR[1] a B e usa
# B+0x0000 scratch loader, B+0x0100 scratch HUB, B+0x10000 HUB (uncached), B+0x18000+k*0x8000 slot. Gli indirizzi di
# COSTRUZIONE restano SCRATCH/EXEC/ADDON_BASE (B_build = 0x88F00000): a runtime tutto e' rilocato di B-0x88F00000.
RESERVE     = 0xE0000        # 2026-09-27 v10: 8 slot (fino a B+0x58000) + buffer FX 512 KB (B+0x60000..B+0xE0000)
FXBUF_OFF   = 0x60000        # buffer condiviso degli effetti (Master FX: delay, reverb, looper, downer), P1 = B + FXBUF_OFF
FXBUF_SIZE  = 0x80000
FXMARK_OFF  = 0x4C           # scratch del loader: *(B+0x4C) == RESERVE -> loader v10, buffer FX presente
MBOX_OFF    = 0x5F000        # 2026-10-02: "cassetta" schermo remoto in B+0x58000..0x60000 (libero fra slot e buffer FX):
#   i tool scrivono cosa mostrare (es. tagli di Trans-Chop), MIDI Screen lo spedisce. +0 'SCR1', +4 seq (scritto per
#   ULTIMO), +8 tipo (1 = tagli), +0xC blocco sample, +0x10 n, +0x14 attivo (pagina del tool aperta), +0x20 pos[48] u32
MBOX_MAGIC  = 0x31524353     # 'SCR1'
MBOX_MAXN   = 48
MBOX_PLAY   = 0xE0           # ascolto in corso (dopo pos[48]): +0xE0 on, +0xE4 fetta (0.. / -1 = sample intero), +0xE8 start,
#   +0xEC end, +0xF0 contatore (scritto per ULTIMO)
# 2026-10-16 CASSETTA DELLE VISTE (docs/PROTOCOL.md §6): B+VBOX_OFF, casella della vista N a +N*VSLOT (N = 1..VBOX_N-1)
VBOX_OFF    = 0x58000
VBOX_MAGIC  = 0x3156354F     # 'O5V1' (intestazione, casella 0)
VSLOT       = 0x400
VBOX_N      = 24
V_SEQ, V_LEN, V_VER, V_ALIVE, V_PAY, V_MAIL = 0x0, 0x4, 0x8, 0xC, 0x10, 0x3C0   # V_ALIVE = contatore frame dell'ultimo fire
V_PAYMAX    = V_MAIL - V_PAY
assert VBOX_OFF + VBOX_N * VSLOT <= MBOX_OFF
HUB_MAGIC   = b'MPCHUB_3'      # payload dell'HUB (MPCADDN3 con magic diversa: il loader flash carica SOLO questo)
NSLOTS      = 16             # 2026-10-16 HUB v2: 16 addon, allocati a pagine da 4 KB negli stessi 256 KB (8 x SLOT_SIZE)
SLOT_SIZE   = 0x8000         # estensione di un addon: codice da +0, scratch a +SLOT_CODE_MAX
SLOT_CODE_MAX = 0x7000         # codice+oggetti+tabella reloc; da +0x7000 la scratch dell'addon
ADDON_BASE  = EXEC + SLOT_SIZE # base di costruzione (= slot 0 dell'HUB: 0xA8F18000); slot k = ADDON_BASE + k*SLOT_SIZE
LOAD_SCRATCH= ADDON_BASE + SLOT_CODE_MAX   # scratch degli addon caricabili (rilocata insieme al codice)
ADDON3_MAGIC= b'MPCADDN3'
ADDON3_HDR  = 48               # magic8, u32 len(body), u32 sum16(body), nome8, u32 code_len, u32 page_off(-1=nessuna), menu16
                               # body = codice (code_len, multiplo di 4) + u32 nreloc + u32 off[nreloc]
def pack_addon3(name, build, page_off=None, menu=None, base=None, magic=ADDON3_MAGIC):
    """build(base) -> bytes (codice+oggetti assemblati con base 'base'). Costruisce a ADDON_BASE e ADDON_BASE+0x1000 e
    ricava la tabella di rilocazione dalle parole (allineate a 4) che differiscono esattamente di 0x1000."""
    base = ADDON_BASE if base is None else base
    c0 = build(base); c1 = build(base + 0x1000)
    assert len(c0) == len(c1), "dimensione diversa tra le due costruzioni"
    c0 += bytes((-len(c0)) & 3); c1 += bytes((-len(c1)) & 3)
    relocs = []
    for off in range(0, len(c0), 4):
        w0 = int.from_bytes(c0[off:off+4], 'little'); w1 = int.from_bytes(c1[off:off+4], 'little')
        if w0 != w1:
            assert w1 - w0 == 0x1000, "parola @%#x non rilocabile: %08x vs %08x" % (off, w0, w1)
            relocs.append(off)
    body = c0 + struct.pack('<I', len(relocs)) + b''.join(struct.pack('<I', o) for o in relocs)
    assert len(body) <= SLOT_CODE_MAX, "addon troppo grande per uno slot: %d > %d" % (len(body), SLOT_CODE_MAX)
    sm = sum(struct.unpack('<%dH' % (len(body)//2), body)) & 0xffffffff
    nm = name.encode()[:8].ljust(8, b' ')
    mn = (menu or '').encode()[:16].ljust(16, b'\0')
    hdr = magic + struct.pack('<II', len(body), sm) + nm + struct.pack('<II', len(c0), 0xFFFFFFFF if page_off is None else page_off) + mn
    assert len(hdr) == ADDON3_HDR
    return hdr + body, len(relocs)
def wav_from_pcm(out, pcm):
    pcm += b'\0' * max(0, WAV_MIN - len(pcm))
    if len(pcm) & 1: pcm += b'\0'
    open(out, 'wb').write(wav_wrap(pcm)); return len(pcm) + 44
WAV_MIN     = 0x2000           # padding minimo del chunk data (silenzio) per un WAV "normale"

# --- API EVENTI dell'OS (reverse 2026-09-15: FUN_a0043872 key_id->evento, FUN_a0043d7a rotella, post @0xA0042496) ---
EVQ           = 0x88483EE4     # coda eventi UI (get_target() la ritorna costante): +4 wr, +8 rd, +0xC mask, +0x10 buf, entry 8B
OS_POST_EVENT = 0x884E6DF2     # post_event(r4=coda, r5=&evt{s16 type, u16 code, s32 data}) — copia l'evento nella coda
                               # tasti: {2|3, code, 0}. Rotella: {6, 0, delta} (il delta e' nel campo data, NON in code)
EV_PRESS, EV_RELEASE, EV_WHEEL = 2, 3, 6
OS_GOTO_PAGE  = 0x884E6EF4     # goto_page(r4=coda EVQ, r5=pagina) (flash 0xA0042598): SOLO globale + evento 0x23 (la pagina si
                               # disegna ma coda+0x68 resta la vecchia: gli eventi NON arrivano). Usare OS_PUSH_PAGE.
OS_PUSH_PAGE  = 0x884E718E     # push_page(r4=coda, r5=pagina, r6=modo) (flash 0xA0042832 = FUN_a004283a): stack pagine
                               # coda+0x44[], coda+0x68=pagina, layout, eventi 0x1D/0x13/0x23. modo 2 + pagina 0 = INDIETRO.
PAGE_DIAGNOSTICS = 0x88491274  # pagina "Diagnostics" (menu nascosto: LCD/LED/Switch/Pad/Slider/Jog/CF/Audio/MIDI/USB test)
PAGE_TRIM_ASSIGN = 0x8849C2E4  # pagina "Assign to pad:" del TRIM
PAGE_TRIM_STRETCH = 0x8849C324
OS_REDRAW_FLAG = EVQ + 0x70    # =1 -> il dispatcher (0xA00423DA) ridisegna la pagina corrente al prossimo giro
LCD_OBJ        = 0x88483E74    # oggetto LCD dell'OS (ctor 0xA0025CD6); +0x28 = SHADOW 32B del display: il flush
LCD_SHADOW     = LCD_OBJ + 0x28  # (FUN_a0025d30) manda all'LCD solo i char diversi dalla shadow -> azzerarla = riscrittura totale
LCD_ROWS       = LCD_OBJ + 9     # byte: righe del flush (2)
LCD_DRV        = LCD_OBJ + 0x10  # driver HD44780 dell'OS: [0]=indirizzo registro comandi, [1]=indirizzo registro dati
LCD_DRV_DATA   = LCD_DRV + 4     # (la busy-wait legge *[0]: NON deviare i comandi; i caratteri passano da [1])
LCD_DUMMY      = 0x88F000F0      # dove finiscono i caratteri dell'OS mentre lo schermo e' dell'addon
EV_SHIFT      = 0x40           # modificatore OR-ato al code quando SHIFT e' tenuto (obj+0xB4)
EVCODE = {                     # nome tasto -> code evento (NON e' l'id matrice)
    'CURSOR_DOWN':0x01,'CURSOR_LEFT':0x02,'CURSOR_RIGHT':0x03,'CURSOR_UP':0x04,'FULL_LEVEL':0x05,'LEVELS_12':0x06,
    'BANK_A':0x07,'BANK_B':0x08,'BANK_C':0x09,'BANK_D':0x0A,'TAP_TEMPO':0x0B,'NOTE_REPEAT':0x0C,'TRACK_MUTE':0x0D,
    'STATUS':0x0E,'MODE':0x0F,'METRONOME':0x10,'TIMING_CORRECT':0x11,'NUMERIC':0x12,'SEQUENCE':0x13,'TRACK':0x14,
    'PARAMS':0x15,'ERASE':0x16,'STEP_BACK':0x17,'STEP_FWD':0x18,'PLAY_START':0x19,'REC':0x1A,'OVERDUB':0x1B,
    'STOP':0x1C,'PLAY':0x1D,'REC_PLAY':0x1E,'OVERDUB_PLAY':0x1F,'QLINK_AFTER':0x20,'SHIFT':0x40,
}

CDLY_DEF    = 0x8000           # delay breve (settling LCD)
SDLY_DEF    = 0x700000         # delay lungo (schermata leggibile)

class Addon:
    def __init__(self, name, hook='loop'):
        self.name = name
        self.hook = hook
        self.pool_fo, self.orig = HOOKS[hook]
        self.a = SHAsm(TRAMP_FLASH)
        self._body = None
        self._mode = None
        self._save = (0,1,8,9,10,11,12,13)
        self.cdly = CDLY_DEF
        self.sdly = SDLY_DEF
        self.scratch = SCRATCH      # base scratch usata da init_scratch (LOAD_SCRATCH per i caricabili)
        self.magic = SCRATCH_MAGIC  # firma di init_scratch (per-addon nei caricabili: stato non condiviso)
        self._lbl = 0
    # ---- modi ----
    def background(self, body, save=None):
        """body(self) gira a OGNI fire e poi ritorna all'OS (task di sfondo, no takeover LCD)."""
        self._mode='bg'; self._body=body
        if save is not None: self._save=tuple(save)
    def takeover_after(self, body, threshold=0x4000, save=None):
        """Ritorna finche' counter<threshold, poi esegue body(self) prendendo l'LCD (NON ritorna). Per i viewer."""
        self._mode='takeover'; self._body=body; self._threshold=threshold
        if save is not None: self._save=tuple(save)
    # ---- primitive ASM (emettono nel trampolino) ----
    def call_orig(self):
        a=self.a; a.stsl_pr(); a.movl_pool(self.orig,2); a.jsr(2); a.nop()
    def push(self):
        for r in self._save: self.a.movl_st_dec(r,15)
    def pop_ret(self):
        for r in reversed(self._save): self.a.movl_ld_inc(15,r)
        self.a.ldsl_pr(); self.a.rts(); self.a.nop()
    def lcd_clear(self):   self._lc_imm(1)
    def lcd_line1(self):   self._lc_imm(-128)   # 0x80
    def lcd_line2(self):   self._lc_imm(-64)    # 0xC0
    def _lc_imm(self,v):   self.a.mov_imm(v,1); self.a.bsr_lbl('lc'); self.a.nop()
    def lcd_char_reg(self,r):
        if r!=1: self.a.mov_rm_rn(r,1)
        self.a.bsr_lbl('ld'); self.a.nop()
    def lcd_str(self,s):
        for ch in s: self.a.mov_imm(ord(ch),1); self.a.bsr_lbl('ld'); self.a.nop()
    def lcd_hex_reg(self,r,nib=8):
        """stampa i 'nib' nibble bassi di Rr in hex (high->low). usa r come scratch (copiato)."""
        a=self.a; a.mov_rm_rn(r,11); a.mov_imm(nib,12)
        self._lbl+=1; lbl='__hx%d'%self._lbl; a.deflabel(lbl)
        for _ in range(4): a.rotl(11)
        a.mov_rm_rn(11,0); a.and_imm(0xf); a.mov_rm_rn(0,1); a.bsr_lbl('ph'); a.nop(); a.dt(12); a.bf_lbl(lbl)
    def lcd_hexbyte_reg(self,r):
        if r!=1: self.a.mov_rm_rn(r,1)
        self.a.bsr_lbl('pby'); self.a.nop()
    def bigdelay(self): self.a.bsr_lbl('bigd'); self.a.nop()
    def init_scratch(self, nbytes=SCRATCH_MAGIC_OFF):
        """IMPORTANTE: la scratch RAM NON e' azzerata al boot (contiene spazzatura).
        Chiama questo come PRIMA cosa nel body: azzera nbytes di scratch una sola volta
        (rilevato via firma) cosi' contatori/flag partono davvero da 0."""
        a=self.a; skip=self.newlabel('initskip'); S=self.scratch
        self.imm32(S+SCRATCH_MAGIC_OFF,1); a.movl_ld(1,0)
        self.imm32(self.magic,2); a.cmpeq(2,0); a.bt_lbl(skip)
        self.imm32(S,1); a.mov_imm(0,0); self.imm32(max(1,nbytes//4),12)
        lp=self.newlabel('initz'); a.deflabel(lp)
        a.movl_st(0,1); a.add_imm(4,1); a.dt(12); a.bf_lbl(lp)
        self.imm32(S+SCRATCH_MAGIC_OFF,1); self.imm32(self.magic,0); a.movl_st(0,1)
        a.deflabel(skip)
    def pool(self):
        """Literal pool intermedia (chiamare subito dopo bra/rts). Necessaria per body > ~1KB."""
        self.a.pool_here()
    def pool_jump(self):
        """Literal pool in un punto qualsiasi del flusso: la salta con un bra."""
        a=self.a; l=self.newlabel('pj'); a.bra_lbl(l); a.nop(); a.pool_here(); a.deflabel(l)
    def bt_far(self,lbl):
        """bt a lunga gittata (bt/bf nativi arrivano a +-256 byte; bra a +-4KB)"""
        a=self.a; s=self.newlabel('nf'); a.bf_lbl(s); a.bra_lbl(lbl); a.nop(); a.deflabel(s)
    def bf_far(self,lbl):
        a=self.a; s=self.newlabel('nt'); a.bt_lbl(s); a.bra_lbl(lbl); a.nop(); a.deflabel(s)
    def newlabel(self, base='L'):
        """restituisce un nome di label univoco (usalo per i salti nel tuo addon)"""
        self._lbl+=1; return '__%s%d'%(base,self._lbl)
    # ---- input ----
    def load_keys(self, dst, tmp=9):
        """dst = stato tasti live (u32, 1=premuto). tmp = registro scratch per l'indirizzo."""
        self.imm32(KEYS_ADDR, tmp); self.a.movl_ld(tmp, dst)
    def keys_edge(self, dst, prev_slot, tmp=9, tmp2=12):
        """dst = tasti PREMUTI in questo fire (fronte di salita): now & ~prev. prev_slot = indirizzo scratch
        (4 byte, azzerato da init_scratch) che tiene lo stato del fire precedente. Clobbera tmp e tmp2."""
        a=self.a
        self.load_keys(dst, tmp)
        self.imm32(prev_slot, tmp); a.movl_ld(tmp, tmp2); a.movl_st(dst, tmp)   # tmp2=prev ; prev=now
        a.not_rr(tmp2, tmp2); a.and_rr(tmp2, dst)                               # dst = now & ~prev
    def load_wheel(self, dst, tmp=9):
        """dst = accumulatore rotella (int32). L'OS lo azzera consumandolo: e' un DELTA transitorio."""
        self.imm32(WHEEL_ADDR, tmp); self.a.movl_ld(tmp, dst)
    def if_combo(self, reg, *names):
        """Salta a una label (restituita) se in reg NON sono premuti TUTTI i tasti indicati. Clobbera r0,r1.
        Uso:  skip = m.if_combo(r, 'SHIFT','STOP'); ...codice combo...; m.a.deflabel(skip)"""
        a=self.a; lbl=self.newlabel('nocombo'); mask=keymask(*names)
        self.imm32(mask, 1); a.mov_rm_rn(reg, 0); a.and_rr(1, 0); a.cmpeq(1, 0); a.bf_lbl(lbl)
        return lbl
    def if_combo_edge(self, now_reg, edge_reg, hold, press):
        """Salta (label ritornata) se NON (tutti i tasti 'hold' premuti in now_reg E almeno uno di 'press' appena
        premuto in edge_reg). now_reg da load_keys, edge_reg da keys_edge. Clobbera r0,r1."""
        a=self.a; lbl=self.newlabel('nocombo')
        hm=keymask(*hold); pm=keymask(*press)
        if hm:
            self.imm32(hm,1); a.mov_rm_rn(now_reg,0); a.and_rr(1,0); a.cmpeq(1,0); a.bf_lbl(lbl)
        self.imm32(pm,1); a.mov_rm_rn(edge_reg,0); a.and_rr(1,0); a.tst_rr(0,0); a.bt_lbl(lbl)
        return lbl
    # ---- eventi verso l'OS ----
    def post_event(self, type_, code, data=0):
        """Inietta un evento {type, code, data} nella coda UI dell'OS (come se venisse dal pannello).
        Clobbera r0-r7 e PR (PR e' salvato dal trampolino/prologo). Usa 8 byte di stack."""
        a=self.a
        a.add_imm(-8,15)
        self.imm32((type_&0xffff)|((code&0xffff)<<16),0); a.movl_st(0,15)
        self.imm32(data&0xffffffff,0); a.movl_st_disp(0,4,15)
        self.imm32(EVQ,4); a.mov_rm_rn(15,5); self.imm32(OS_POST_EVENT,2); a.jsr(2); a.nop()
        a.add_imm(8,15)
    def os_lcd_off(self):
        """Devia i CARATTERI del driver LCD dell'OS su un byte di RAM: lo schermo e' dell'addon finche' non chiami
        os_lcd_on(). (I comandi dell'OS - posizione cursore - continuano ad arrivare all'LCD: innocui.)"""
        a=self.a; self.imm32(LCD_DRV_DATA,1); self.imm32(LCD_DUMMY,0); a.movl_st(0,1)
    def os_lcd_on(self):
        """Ripristina il registro dati vero e forza il ridisegno completo (os_redraw)."""
        a=self.a; self.imm32(LCD_DRV_DATA,1); self.imm32(LCD_DATA,0); a.movl_st(0,1); self.os_redraw()
    def lcd_cursor(self, pos, on=True):
        """Cursore hardware HD44780 alla posizione pos (0-15 riga1, 0x40+ riga2). on=False lo spegne."""
        v=0x80|pos; self._lc_imm(v-256 if v>127 else v); self._lc_imm(0x0E if on else 0x0C)
    def keys_clear(self, *names):
        """Toglie i bit dei tasti indicati da KEYS_ADDR: l'OS non vedra' la pressione (l'ISR li rimette al prossimo
        scan; chiamare ogni fire finche' serve). Clobbera r0,r1,r2."""
        a=self.a; self.imm32(KEYS_ADDR,1); a.movl_ld(1,0); self.imm32((~keymask(*names))&0xffffffff,2); a.and_rr(2,0); a.movl_st(0,1)
    def os_redraw(self):
        """Restituisce lo schermo all'OS dopo che l'addon ha scritto sull'LCD: azzera la shadow del driver
        (altrimenti l'OS crede che il display sia gia' aggiornato e non riscrive) e alza il flag di redraw."""
        a=self.a
        self.imm32(LCD_SHADOW,1); a.mov_imm(0,0)
        for i in range(0,0x20,4): a.movl_st_disp(0,i,1)
        self.imm32(OS_REDRAW_FLAG,1); a.mov_imm(1,0); a.movl_st(0,1)
    def goto_page(self, page):
        """Apre la pagina (oggetto pagina) con la push_page dell'OS: diventa la pagina corrente a tutti gli effetti
        (eventi, focus, stack). Clobbera r0-r7, PR."""
        a=self.a; self.imm32(EVQ,4); self.imm32(page,5); a.mov_imm(0,6); self.imm32(OS_PUSH_PAGE,2); a.jsr(2); a.nop()
    def page_back(self):
        """Torna alla pagina precedente dello stack (push_page(coda, 0, 2))."""
        a=self.a; self.imm32(EVQ,4); a.mov_imm(0,5); a.mov_imm(2,6); self.imm32(OS_PUSH_PAGE,2); a.jsr(2); a.nop()
    def press_key(self, name, shift=False):
        """Simula pressione+rilascio di un tasto (per nome EVCODE), opzionalmente con SHIFT."""
        code=EVCODE[name]|(EV_SHIFT if shift else 0)
        self.post_event(EV_PRESS,code); self.post_event(EV_RELEASE,code)
    def wheel(self, delta):
        """Simula 'delta' scatti di rotella (negativo = indietro)."""
        self.post_event(EV_WHEEL, 0, delta)
    # ---- iniezione "hardware": scrive dove l'ISR mette tasti/rotella (l'OS non distingue dal pannello) ----
    def inject_wheel(self, delta):
        """Somma 'delta' scatti all'accumulatore rotella dell'ISR (2 impulsi/scatto): l'OS lo consuma come rotella vera."""
        a=self.a; self.imm32(WHEEL_ADDR,1); a.movl_ld(1,0); self.imm32(delta*2,2); a.add_rr(2,0); a.movl_st(0,1)
    def key_hold_start(self, hold_slot, fires):
        """Programma una pressione lunga 'fires' fire (vedi key_hold_tick)."""
        a=self.a; self.imm32(hold_slot,1); self.imm32(fires,0); a.movl_st(0,1)
    def key_hold_tick(self, name, hold_slot):
        """Da chiamare OGNI fire: finche' il contatore in hold_slot > 0 tiene 'premuto' il tasto scrivendo il suo bit
        nel byte-colonna di KEYS (l'ISR lo riscrive ogni scan, noi lo rimettiamo ogni fire; a contatore 0 = rilascio)."""
        a=self.a; bit=KEYBIT[name]; col=bit>>3; row=bit&7; skip=self.newlabel('hold')
        self.imm32(hold_slot,1); a.movl_ld(1,0); a.tst_rr(0,0); a.bt_lbl(skip)
        a.add_imm(-1,0); a.movl_st(0,1)
        self.imm32(KEYS_ADDR+col,1); a.movb_ld(1,0); a.or_imm(1<<row); a.movb_st(0,1)
        a.deflabel(skip)
    # accesso memoria (helper minimi; per il resto usa self.a direttamente)
    def load32(self,addr_reg,dst): self.a.movl_ld(addr_reg,dst)
    def store32(self,src,addr_reg): self.a.movl_st(src,addr_reg)
    def imm32(self,val,rn):
        # indirizzi nella LOAD_SCRATCH (slot valori, flag degli addon caricabili) seguono la base di costruzione:
        # cosi' pack_addon3 li vede cambiare tra le due costruzioni e li mette nella tabella di rilocazione
        if isinstance(val,int) and LOAD_SCRATCH<=val<LOAD_SCRATCH+0x1000 and self.scratch!=LOAD_SCRATCH and self.scratch!=SCRATCH:
            val=self.scratch+(val-LOAD_SCRATCH)
        self.a.movl_pool(val,rn)
    # ---- helper condivisi (emessi una volta in fondo) ----
    def _emit_helpers(self):
        a=self.a
        a.deflabel('lc'); a.movl_pool(LCD_CMD,0); a.movb_st(1,0); a.movl_pool(self.cdly,3); a.deflabel('c1'); a.dt(3); a.bf_lbl('c1'); a.rts(); a.nop()
        a.deflabel('ld'); a.movl_pool(LCD_DATA,0); a.movb_st(1,0); a.movl_pool(self.cdly,3); a.deflabel('d1'); a.dt(3); a.bf_lbl('d1'); a.rts(); a.nop()
        a.deflabel('ph'); a.stsl_pr(); a.mov_rm_rn(1,0); a.add_imm(0x30,0); a.mov_imm(0x39,2); a.cmpgt(2,0); a.bf_lbl('phs'); a.add_imm(7,0)
        a.deflabel('phs'); a.mov_rm_rn(0,1); a.bsr_lbl('ld'); a.nop(); a.ldsl_pr(); a.rts(); a.nop()
        a.deflabel('pby'); a.stsl_pr(); a.movl_st_dec(1,15)
        a.mov_rm_rn(1,0); a.shlr2(0); a.shlr2(0); a.and_imm(0xf); a.mov_rm_rn(0,1); a.bsr_lbl('ph'); a.nop()
        a.movl_ld_inc(15,1); a.mov_rm_rn(1,0); a.and_imm(0xf); a.mov_rm_rn(0,1); a.bsr_lbl('ph'); a.nop(); a.ldsl_pr(); a.rts(); a.nop()
        a.deflabel('bigd'); a.movl_pool(self.sdly,3); a.deflabel('bd'); a.dt(3); a.bf_lbl('bd'); a.rts(); a.nop()
    # ---- assemblaggio + iniezione + CRC ----
    def _assemble(self):
        a=self.a
        assert self._mode, "chiama background(...) o takeover_after(...) prima di build()"
        self.call_orig()
        self.push()
        if self._mode=='bg':
            self._body(self)
            self.pop_ret()
        else:  # takeover
            a.movl_pool(SCRATCH,1); a.movl_ld(1,0); a.add_imm(1,0); a.movl_st(0,1); a.mov_rm_rn(0,13)
            a.movl_pool(self._threshold,1); a.cmphs(1,13); a.bt_lbl('__go')
            self.pop_ret()
            a.deflabel('__go')
            self._body(self)   # non ritorna (il body deve fare un loop infinito)
        self._emit_helpers()
        return a.assemble()
    def build(self, out):
        blob=self._assemble()
        assert len(blob)<=TRAMP_MAX, "addon troppo grande: %d > %d"%(len(blob),TRAMP_MAX)
        buf=bytearray(open(FW,'rb').read())
        buf[TRAMP_FO:TRAMP_FO+len(blob)]=blob
        assert struct.unpack('<I',buf[self.pool_fo:self.pool_fo+4])[0]==self.orig, "pool inatteso: firmware base diverso?"
        buf[self.pool_fo:self.pool_fo+4]=struct.pack('<I',TRAMP_FLASH)
        out_b=_fix_crc(bytes(buf)); assert len(out_b)==514732
        open(out,'wb').write(out_b)
        crc=struct.unpack('<I',out_b[0x10010:0x10014])[0]
        print("%s [hook=%s]: %d byte payload -> %s (CRC=0x%08x)"%(self.name,self.hook,len(blob),out,crc))
        return out

    # ---- addon CARICABILE (WAV su CF, eseguito dal loader mpc500_loader.bin) ----
    def build_wav(self, out, name=None, hub=False):
        """Impacchetta il body in un WAV mono 16 bit da caricare sull'MPC con LOAD (Sample). Non serve riflashare.
        hub=False (default): addon RILOCABILE (MPCADDN3) caricato dall'HUB in uno slot libero; scratch = LOAD_SCRATCH
        (dentro lo slot). hub=True: formato del loader flash (MPCADDN2, base EXEC, scratch HUB_SCRATCH): e' l'HUB stesso
        (o un addon singolo vecchio stile, che pero' sostituisce l'HUB)."""
        assert self._mode=='bg', "gli addon caricabili sono solo 'background' (devono ritornare)"
        nm = name or self.name
        self.magic = (zlib.crc32(nm.encode()) & 0xffffffff) | 1   # firma per-addon
        def build(base):
            self.a = SHAsm(base); self.scratch = (HUB_SCRATCH + base - EXEC) if hub else base + SLOT_CODE_MAX; a=self.a
            a.stsl_pr(); self._body(self); a.ldsl_pr(); a.rts(); a.nop(); self._emit_helpers()
            code = a.assemble()
            return code + (b'\0' if len(code) & 1 else b'')
        if hub:
            pcm, nrel = pack_addon3(nm, build, base=EXEC, magic=HUB_MAGIC)
            n = wav_from_pcm(out, pcm)
            print("%s [HUB, loader flash]: %d byte, %d rilocazioni -> %s (%d byte)"%(self.name,len(pcm)-ADDON3_HDR,nrel,out,n))
        else:
            pcm, nrel = pack_addon3(nm, build)
            n = wav_from_pcm(out, pcm)
            print("%s [caricabile, HUB]: %d byte, %d rilocazioni -> %s (%d byte)"%(self.name,len(pcm)-ADDON3_HDR,nrel,out,n))
        return out

def wav_wrap(pcm, rate=44100, ch=1, bits=16):
    fmt=struct.pack('<HHIIHH',1,ch,rate,rate*ch*bits//8,ch*bits//8,bits)
    return (b'RIFF'+struct.pack('<I',4+8+len(fmt)+8+len(pcm))+b'WAVE'+b'fmt '+struct.pack('<I',len(fmt))+fmt
            +b'data'+struct.pack('<I',len(pcm))+pcm)

def _fix_crc(buf):
    size=struct.unpack('<I',buf[0x1000c:0x10010])[0]; b=bytearray(buf); b[0x10010:0x10014]=b'\0\0\0\0'
    b[0x10010:0x10014]=struct.pack('<I',zlib.crc32(bytes(b[0x10000:0x10000+size]))&0xffffffff); return bytes(b)
