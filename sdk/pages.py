#!/usr/bin/env python3
"""
pages - PAGINE NATIVE dell'OS MPC500 per gli addon (strada A, verificata sul ferro 2026-09-18).

La pagina e i widget sono oggetti VERI del framework pagine dell'OS (stesse vtable/flag), costruiti qui sul Mac
dentro il payload del WAV con puntatori assoluti. L'OS li disegna e gestisce cursore/focus, rotella (con min/max),
DO IT/CANCEL, ridisegno. Il codice dell'addon e' solo: risposte GET/SET dei campi (automatiche, su slot di scratch)
+ l'azione di DO IT.

Uso:
    from pages import PageAddon
    pg = PageAddon("myaddon")
    pg.selector()                                       # riga 0: selettore sample dell'OS (rotella = scorre i sample)
    bpm = pg.value("B", x=0, y=1, fmt="  0.0", lo=300, hi=3000, default=1200)   # etichetta + campo numerico
    n   = pg.value(" N", x=6, y=1, fmt="  0", lo=1, hi=48, default=8)
    def doit(m):          # asm eseguito su DO IT (m = Addon; slot bpm/n leggibili con ld(m, bpm, r))
        ...
        pg.emit_message(m, "Done            ")           # opzionale: messaggio riga 0 per ~2 s
    pg.on_doit(doit)
    pg.build_wav("MYADDON.WAV")                          # SHIFT+STOP apre la pagina; CANCEL torna indietro

Campi: ogni value() alloca uno slot int32 nella LOAD_SCRATCH (ritornato) con valore iniziale 'default'.
Formato "picture": larghezza = len, decimali = cifre dopo '.', il passo della rotella agisce sull'ultima cifra.
"""
import sys, os, struct, zlib
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from mpcmod import Addon, LOAD_SCRATCH, ADDON_BASE, SLOT_CODE_MAX, EVQ, OS_PUSH_PAGE, pack_addon3, wav_from_pcm
from shasm import SHAsm

# --- framework pagine (indirizzi verificati) ---
VT_PAGE, VT_LABEL, VT_VALUE, VT_PMODEL, VT_SMODEL = 0x88507BA8, 0x88507C44, 0x88507C8C, 0x88507FD4, 0x885083DC
ROOT_PAGE   = 0x8849C924       # pagina radice comune (+0x2C di ogni pagina)
STR_BLANK16 = 0x88506A58       # "                " (formato del selettore)
VT_EMODEL, ENUM_FMT, VT_COMPOSITE = 0x885080D0, 0x88506BE4, 0x88507D1C   # modello ENUM + formato usato da tutti gli enum dell'OS
SEL_CB      = 0x884E3E38      # callback OS del selettore sample (rotella = prev/next)
INPUT_OBJ, ENABLE_DOIT = 0x88483FDC, 0x884E7372
# Tasti 12 LEVELS / FULL LEVEL: l'OS li trasforma in DO IT (0x2E) / CANCEL (0x2D) solo se nell'oggetto tasti sono accesi
# +0x9C (ENABLE_DOIT, LED 10) / +0x98 (ENABLE_CANCEL, LED 7) (FUN 0x884E8552 / 0x884E84CC); con +0x94 un DO IT o un CANCEL
# li spegne tutti e due. Prima del 2026-10-24 le pagine accendevano solo DO IT: CANCEL andava solo se +0x98 era rimasto
# acceso dalla pagina prima (HW: in Tap Chop CANCEL "non faceva niente" dopo il DO IT del menu di Chop Shop).
ENABLE_CANCEL = 0x884E73B4
CXSAVE = LOAD_SCRATCH + 0x24   # +0x98 com'era all'ingresso in una nostra pagina (+2 = salvato), rimesso all'uscita
SET_FOCUS   = 0x884E9B4E
GROUP       = 9                # gruppo parametri riservato agli addon
EV_ENTER, EV_DOIT, EV_CANCEL = 0x23, 0x2E, 0x2D
# La voce nel menu OTHER e' gestita dall'HUB (sdk/hub.py): l'addon dichiara solo nome voce + pagina nell'header MPCADDN3.

def rect(x, y, w, h=1): return ((x & 0x1f) << 11) | ((y & 3) << 9) | ((w & 0x1f) << 4) | ((h & 3) << 2)
def ld(m, slot, r):  m.imm32(slot, 1); m.a.movl_ld(1, r)
def st(m, r, slot):  m.imm32(slot, 1); m.a.movl_st(r, 1)
def sti(m, val, slot): m.imm32(slot, 1); m.imm32(val, 0); m.a.movl_st(0, 1)

class _Block:
    def __init__(self, base): self.base = base; self.buf = bytearray()
    def alloc(self, size):
        while len(self.buf) & 3: self.buf.append(0)
        a = self.base + len(self.buf); self.buf += bytes(size); return a
    def w32(self, addr, v): o = addr - self.base; self.buf[o:o+4] = struct.pack('<I', v & 0xffffffff)
    def w16(self, addr, v): o = addr - self.base; self.buf[o:o+2] = struct.pack('<H', v & 0xffff)
    def string(self, s):
        a = self.alloc(len(s) + 1); self.buf[a-self.base:a-self.base+len(s)] = s.encode(); return a

HOST = None    # add-on che ospita le pagine dei moduli importati dopo (es. wav_chopshop): vedi page_or_addon()

def page_or_addon(name, **kw):
    """Per i moduli che si costruiscono da soli O come pagina di un altro add-on: se pages.HOST e' impostato ritorna
    una pagina nuova di quell'add-on (HOST.add_page), altrimenti un PageAddon autonomo (come sempre)."""
    return HOST.add_page(name) if HOST is not None else PageAddon(name, **kw)

class Page:
    """Una pagina LCD nativa: widget, risposte GET/SET dei campi, eventi, DO IT, messaggi. PageAddon e' l'add-on con la
    sua prima pagina; PageAddon.add_page() aggiunge altre pagine allo STESSO add-on (stesso codice, stessa scratch; label
    con un prefisso proprio, contatore messaggi proprio). Per aprirle: OS_PUSH_PAGE(EVQ, p.page, 0); CANCEL torna indietro."""
    def _page_init(self, host, ns):
        self.host = host; self.ns = ns
        self.items = []          # ('label', text, x, y, w) | ('value', fid, label, x, y, fmt, lo, hi, slot) | ('selector',) | ('message',)
        self.slots = {}          # fid -> slot
        self.next_fid = 1
        self.doit_fn = None; self.open_press = None
        self.has_msg = False; self.has_sel = False
        self.other = None        # nome della voce nel menu OTHER (header MPCADDN3, gestita dall'HUB; solo la prima pagina)
        self.addr = {}           # riempito al build: page, first, msgw, msgt, values{fid: widget}
        self.fire_fn = None      # asm eseguito a ogni fire MENTRE la pagina e' quella attiva (coda+0x68)
        self.event_fns = {}      # tipo evento pagina -> fn(m, hret0, hret1) (es. 0x38 pad, 0x24 uscita)
        self.inactive_fn = None  # asm eseguito a ogni fire quando la pagina NON e' quella attiva
        self.keep_focus = False  # True: i messaggi spostano il focus solo se era sul selettore (nascosto), poi lo rimettono
        self.msg_fires = 0x180   # durata dei messaggi (fire)
        self.cancel_fn = None    # fn(m, hret1): asm su CANCEL prima dell'uscita (puo' consumarlo con bra hret1)
        self.far_hend = False    # True per handler > 4 KB (salto assoluto sopra l'handler)
        self.far_fire = False    # True per fire_fn > 4 KB (salto assoluto alla parte inattiva)
        self.init_fn = None      # fn(m): asm eseguito UNA volta dopo i valori iniziali (scratch appena azzerata)
        # timer del messaggio e "focus spostato": quelli della prima pagina restano a +0x1C/+0x20 della scratch
        if host is self: self.SHOWMSG, self.MOVEDF = LOAD_SCRATCH + 0x1C, LOAD_SCRATCH + 0x20
        else: self.SHOWMSG, self.MOVEDF = host.alloc_slot(), host.alloc_slot()
    @property
    def reserve(self): return self.host._reserve
    @reserve.setter
    def reserve(self, v): self.host._reserve = max(self.host._reserve, v)
    def alloc_slot(self, default=0): return self.host._alloc_slot(default)
    def alloc_buf(self, nbytes):
        """spazio nella scratch (azzerato all'installazione) senza valore iniziale per parola: tabelle, piccoli buffer"""
        return self.host._alloc_buf(nbytes)
    # ---- dichiarazione UI ----
    def label(self, text, x, y, w=None):
        self.items.append(('label', text, x, y, w or len(text)))
    def dlabel(self, x, y, w):
        """etichetta a testo variabile (w caratteri): ritorna l'indice; l'indirizzo del testo e' addr['dlabels'][i]"""
        n = sum(1 for it in self.items if it[0] == 'dlabel')
        self.items.append(('dlabel', n, x, y, w)); return n
    def value(self, label, x, y, fmt, lo, hi, default=0):
        fid = self.next_fid; self.next_fid += 1
        slot = self.alloc_slot(default); self.slots[fid] = slot
        self.items.append(('value', fid, label, x, y, fmt, lo, hi, slot)); return slot
    def choice(self, label, x, y, options, default=0):
        """campo a scelta con nomi (modello ENUM dell'OS, var byte diretta nello slot): la rotella scorre 'options'.
        Ritorna lo slot (il byte basso = indice della voce)."""
        fid = self.next_fid; self.next_fid += 1
        slot = self.alloc_slot(default); w = max(len(o) for o in options)
        self.items.append(('choice', fid, label, x, y, w, [o.ljust(w) for o in options], slot)); return slot
    def selector(self):
        self.has_sel = True; self.items.insert(0, ('selector',))
    def message(self):
        self.has_msg = True
    def on_doit(self, fn): self.doit_fn = fn
    def on_fire(self, fn):
        """fn(m): asm a ogni fire solo se la pagina e' attiva. Registri r0-r14 liberi (li salva il loader flash)."""
        self.fire_fn = fn
    def on_inactive(self, fn):
        """fn(m): asm a ogni fire quando la pagina NON e' attiva (coda+0x68 != pagina): per capire l'uscita vera.
        NB: l'evento pagina 0x24 NON e' un'uscita: e' la notifica di ridisegno (VT_PAGE+0x2C, a ogni EVQ+0x70)."""
        self.inactive_fn = fn
    def on_event(self, evtype, fn):
        """fn(m, hret0, hret1): asm per l'evento pagina 'evtype' (r8 = &evento: +0 s16 tipo, +2 u16 codice, +4 s32 dato).
        Deve finire con bra a hret1 (consumato) o hret0 (lascia fare all'OS). r9-r14 liberi (salvati dall'handler)."""
        self.event_fns[evtype] = fn
    def other_menu(self, name):
        """Voce 'name' nel menu OTHER dell'OS (aggiunta dall'HUB): selezionandola e premendo DO IT si apre la pagina."""
        self.other = name[:16]
    # ---- codice generato ----
    def _hcall(self, m, label):
        """chiamata a un helper comune (emessi in fondo): bsr, o jsr assoluto (r2) se l'addon e' grande (far_hend)"""
        if self.far_hend: m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()
        else: m.a.bsr_lbl(label); m.a.nop()
    def emit_message(self, m, text16):
        """asm: mette 'text16' (16 char) nel buffer messaggio e lo mostra per ~2 s (riga 0)."""
        a = m.a; assert self.has_msg
        m.imm32(self.addr['msgt'], 12)
        for ch in text16.ljust(16)[:16]: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_imm(0, 0); a.movb_st(0, 12)
        self._hcall(m, '__showmsg')
    def emit_message_dec(self, m, prefix, slot, suffix):
        """asm: prefix + 2 cifre decimali di *slot + suffix (totale <= 16 char)."""
        a = m.a; assert self.has_msg
        m.imm32(self.addr['msgt'], 12)
        for ch in prefix: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        ld(m, slot, 4); a.mov_imm(10, 5); self._hcall(m, '__udiv')
        a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
        for ch in suffix.ljust(16 - len(prefix) - 2)[:16 - len(prefix) - 2]: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_imm(0, 0); a.movb_st(0, 12)
        self._hcall(m, '__showmsg')
    def _build_objects(self, b, handler):
        page = b.alloc(0x38)
        b.w32(page+0x14, 0xF0000000); b.w32(page+0x1C, VT_PAGE); b.w32(page+0x28, 2); b.w32(page+0x2C, ROOT_PAGE); b.w32(page+0x30, handler)
        children = []; values = {}; first = None; msgw = msgt = 0; dlabels = {}
        for it in self.items:
            if it[0] == 'selector':
                sm = b.alloc(0x18); nf = b.alloc(0x28)
                b.w32(sm+0, 0x00300000); b.w32(sm+4, nf); b.w32(sm+8, 0xA0); b.w32(sm+0xC, 0x00050007); b.w32(sm+0x10, STR_BLANK16); b.w32(sm+0x14, VT_SMODEL)
                b.w32(nf+4, page); b.w16(nf+0x10, rect(0, 0, 16)); b.w32(nf+0x14, 0xD0000000); b.w32(nf+0x1C, VT_VALUE); b.w32(nf+0x20, SEL_CB); b.w32(nf+0x24, sm)
                children.append(nf); first = first or nf
            elif it[0] == 'dlabel':
                _, i_, x, y, w = it; o = b.alloc(0x28); t = b.alloc(w + 1)
                for k in range(w): b.buf[t - b.base + k] = 0x20
                b.w32(o+4, page); b.w16(o+0x10, rect(x, y, w)); b.w32(o+0x14, 0x90000000); b.w32(o+0x1C, VT_LABEL); b.w32(o+0x24, t)
                children.append(o); dlabels[i_] = t
            elif it[0] == 'label':
                _, text, x, y, w = it; o = b.alloc(0x28); s = b.string(text)
                b.w32(o+4, page); b.w16(o+0x10, rect(x, y, w)); b.w32(o+0x14, 0x90000000); b.w32(o+0x1C, VT_LABEL); b.w32(o+0x24, s)
                children.append(o)
            elif it[0] == 'value':
                _, fid, label, x, y, fmt, lo, hi, slot = it
                if label:
                    o = b.alloc(0x28); s = b.string(label)
                    b.w32(o+4, page); b.w16(o+0x10, rect(x, y, len(label))); b.w32(o+0x14, 0x90000000); b.w32(o+0x1C, VT_LABEL); b.w32(o+0x24, s)
                    children.append(o); x += len(label)
                fmts = b.string(fmt); mo = b.alloc(0x20); o = b.alloc(0x28)
                b.w32(mo+0, 0x00300000 | ((len(fmt) - 1) << 8)); b.w32(mo+4, o); b.w32(mo+8, 0x88); b.w32(mo+0xC, (GROUP << 16) | fid)
                b.w32(mo+0x10, fmts); b.w32(mo+0x14, VT_PMODEL); b.w32(mo+0x18, lo); b.w32(mo+0x1C, hi)
                b.w32(o+4, page); b.w16(o+0x10, rect(x, y, len(fmt))); b.w32(o+0x14, 0xD0000000); b.w32(o+0x1C, VT_VALUE); b.w32(o+0x20, handler); b.w32(o+0x24, mo)
                children.append(o); values[fid] = o; first = first or o
            elif it[0] == 'choice':
                # clone dei campi enum dell'OS (es. Other "Receive channel"): modello {0x00300000, widget, 1 = var byte
                # diretta, &var, formato 0x88506BE4, vt ENUM, 0, 0, desc {n, stringhe, 0, rect}}; widget = VALORE generico
                _, fid, label, x, y, w, opts, slot = it
                if label:
                    o = b.alloc(0x28); s = b.string(label)
                    b.w32(o+4, page); b.w16(o+0x10, rect(x, y, len(label))); b.w32(o+0x14, 0x90000000); b.w32(o+0x1C, VT_LABEL); b.w32(o+0x24, s)
                    children.append(o); x += len(label)
                strs = b.alloc(sum(len(t) + 1 for t in opts)); p = strs - b.base
                for t in opts: b.buf[p:p+len(t)] = t.encode(); p += len(t) + 1
                desc = b.alloc(16); mo = b.alloc(0x24); o = b.alloc(0x38)
                b.w32(desc, len(opts)); b.w32(desc+4, strs); b.w32(desc+0xC, rect(x, y, w))
                var = self.host.scratch + (slot - LOAD_SCRATCH)     # segue la base di costruzione (rilocazione)
                b.w32(mo+0, 0x00300000); b.w32(mo+4, o); b.w32(mo+8, 1); b.w32(mo+0xC, var); b.w32(mo+0x10, ENUM_FMT)
                b.w32(mo+0x14, VT_EMODEL); b.w32(mo+0x20, desc)
                # widget COMPOSITO come tutti gli enum dell'OS (0x38 B): +0x28 etichetta (0 = nessuna), +0x2E rect etichetta,
                # +0x30 rect valore, +0x35 = voci-1 (in tutti gli oggetti OS); il draw (FUN_a004480c) mette il cursore
                # sul campo quando ha il focus (il widget VALORE non lo fa con un modello enum: HW v9, cursore rimasto sul sample)
                b.w32(o+4, page); b.w16(o+0x10, rect(x, y, w)); b.w32(o+0x14, 0xD0000000); b.w32(o+0x1C, VT_COMPOSITE); b.w32(o+0x24, mo)
                b.w16(o+0x30, rect(x, y, w)); b.buf[o - b.base + 0x35] = len(opts) - 1
                children.append(o); values[fid] = o; first = first or o
        if self.has_msg:
            msgt = b.alloc(20); msgw = b.alloc(0x28)
            b.w32(msgw+4, page); b.w16(msgw+0x10, rect(0, 0, 16)); b.w32(msgw+0x14, 0x10000000); b.w32(msgw+0x1C, VT_LABEL); b.w32(msgw+0x24, msgt)
            children.append(msgw)
        b.w32(page+0xC, children[0])
        for a_, n_ in zip(children, children[1:]): b.w32(a_+8, n_)
        return dict(page=page, first=first, msgw=msgw, msgt=msgt, values=values, sel=children[0] if self.has_sel else 0, dlabels=dlabels)
    def _emit_page(self, m):
        """asm della pagina (dentro il body dell'add-on, con il prefisso di label della pagina attivo)"""
        a = m.a; A = self.addr
        ARMED, PREVK = LOAD_SCRATCH + 0x14, LOAD_SCRATCH + 0x18
        SHOWMSG, MOVEDF = self.SHOWMSG, self.MOVEDF   # MOVEDF (keep_focus): il messaggio ha spostato il focus dal selettore
        # timer messaggio
        if self.has_msg:
            ld(m, SHOWMSG, 13); a.tst_rr(13, 13); nomsg = m.newlabel('nomsg'); m.bt_far(nomsg)
            a.add_imm(-1, 13); st(m, 13, SHOWMSG); a.tst_rr(13, 13); m.bf_far(nomsg)
            m.imm32(A['msgw'] + 0x17, 1); a.movb_ld(1, 0); a.and_imm(0x7F); a.movb_st(0, 1)
            if self.has_sel: m.imm32(A['sel'] + 0x17, 1); a.movb_ld(1, 0); a.or_imm(0x80); a.movb_st(0, 1)
            if self.has_sel and self.keep_focus:        # il focus era sul selettore: rimettilo li'
                nmv = m.newlabel('nmv'); ld(m, MOVEDF, 0); a.tst_rr(0, 0); a.bt_lbl(nmv); sti(m, 0, MOVEDF)
                m.imm32(A['page'], 4); m.imm32(A['sel'], 5); m.imm32(SET_FOCUS, 2); a.jsr(2); a.nop(); a.deflabel(nmv)
            m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
            a.deflabel(nomsg); m.pool_jump()
        # combo di apertura (al rilascio)
        if self.open_press:      # scorciatoia tasti (open_press=None: solo dal menu OTHER)
            m.load_keys(10); m.keys_edge(8, PREVK)
            l = m.if_combo_edge(10, 8, hold=self.open_hold, press=self.open_press); m.imm32(ARMED, 1); a.mov_imm(1, 0); a.movl_st(0, 1); a.deflabel(l)
            no = m.newlabel('no')
            a.tst_rr(10, 10); m.bf_far(no)
            m.imm32(ARMED, 1); a.movl_ld(1, 13); a.tst_rr(13, 13); m.bt_far(no)
            a.mov_imm(0, 0); a.movl_st(0, 1)
            m.imm32(EVQ, 4); m.imm32(A['page'], 5); a.mov_imm(0, 6); m.imm32(OS_PUSH_PAGE, 2); a.jsr(2); a.nop()
            a.deflabel(no); m.pool_jump()
        if self.fire_fn or self.inactive_fn:
            nf = m.newlabel('nf'); fd_ = m.newlabel('fdone')
            m.imm32(EVQ + 0x68, 1); a.movl_ld(1, 2); m.imm32(A['page'], 3); a.cmpeq(3, 2)
            if self.far_fire:                       # fire_fn > 4 KB (es. USB MIDI v7): salto assoluto alla parte inattiva
                fy = m.newlabel('fy'); a.bt_lbl(fy); a.movl_pool(nf, 2); a.jmp(2); a.nop(); a.deflabel(fy)
            else: m.bf_far(nf)
            if self.fire_fn: self.fire_fn(m)
            m.a.movl_pool(fd_, 2); a.jmp(2); a.nop(); m.pool_jump()
            a.deflabel(nf)
            if self.inactive_fn: self.inactive_fn(m)
            a.deflabel(fd_); m.pool_jump()
        # ===== handler (pagina + callback widget): r4=obj r5=evt -> r0 =====
        hend = m.newlabel('hend')
        if self.far_hend: a.movl_pool(hend, 2); a.jmp(2); a.nop(); a.pool_here()   # handler > 4 KB: salto assoluto
        else: a.bra_lbl(hend); a.nop()
        a.deflabel('handler')
        for r in (8, 9, 10, 11, 12, 13, 14): a.movl_st_dec(r, 15)
        a.stsl_pr()
        a.mov_rm_rn(5, 8); a.movw_ld(8, 0); a.extu_w(0, 0); a.mov_rm_rn(0, 9)
        hret1 = m.newlabel('hret1'); hret0 = m.newlabel('hret0'); keys = m.newlabel('keys')
        a.mov_imm(0x10, 0); a.cmpeq(0, 9); isget = m.newlabel('isget'); a.bt_lbl(isget)
        a.mov_imm(0x11, 0); a.cmpeq(0, 9); isset = m.newlabel('isset'); a.bt_lbl(isset)
        a.mov_imm(0x12, 0); a.cmpeq(0, 9); ischg = m.newlabel('ischg'); a.bt_lbl(ischg)
        a.bra_lbl(keys); a.nop()
        def field_slot():
            a.movl_ld_disp(8, 12, 0); a.mov_rm_rn(0, 2); a.shlr16(2); a.extu_w(2, 2); a.mov_imm(GROUP, 3); a.cmpeq(3, 2); nm = m.newlabel('nm'); m.bf_far(nm)
            a.extu_b(0, 0); a.mov_imm(0, 10)
            for fid, slot in self.slots.items():
                nx = m.newlabel('fs'); a.mov_imm(fid, 3); a.cmpeq(3, 0); a.bf_lbl(nx); m.imm32(slot, 10); a.deflabel(nx)
            a.tst_rr(10, 10); m.bt_far(nm)
            return nm
        a.deflabel(isget); nm = field_slot(); a.movl_ld(10, 0); a.movl_st_disp(0, 4, 8); a.bra_lbl(hret1); a.nop(); a.deflabel(nm); a.bra_lbl(hret0); a.nop()
        m.pool_jump()
        a.deflabel(isset); nm = field_slot(); a.movl_ld_disp(8, 4, 0); a.movl_st(0, 10); a.bra_lbl(hret1); a.nop(); a.deflabel(nm); a.bra_lbl(hret0); a.nop()
        m.pool_jump()
        a.deflabel(ischg); nm = field_slot(); a.bra_lbl(hret1); a.nop(); a.deflabel(nm); a.bra_lbl(hret0); a.nop()
        m.pool_jump()
        a.deflabel(keys)
        m.imm32(A['page'], 2); a.cmpeq(2, 4); m.bf_far(hret0)                    # da qui: solo eventi alla PAGINA
        a.mov_imm(EV_ENTER, 0); a.cmpeq(0, 9); ne = m.newlabel('ne'); a.bf_lbl(ne)
        m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
        m.imm32(A['page'], 4); m.imm32(A['first'], 5); m.imm32(SET_FOCUS, 2); a.jsr(2); a.nop()
        a.bra_lbl(hret0); a.nop()
        a.deflabel(ne); m.pool_jump()
        for evtype, fn in self.event_fns.items():
            nx = m.newlabel('ev'); m.imm32(evtype, 0); a.cmpeq(0, 9); m.bf_far(nx); fn(m, hret0, hret1); a.deflabel(nx); m.pool_jump()
        a.mov_imm(2, 0); a.cmpeq(0, 9); m.bf_far(hret0)
        m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 0); a.extu_w(0, 0)
        a.mov_imm(EV_CANCEL, 3); a.cmpeq(3, 0); nc = m.newlabel('nc')
        if self.cancel_fn: m.bf_far(nc); self.cancel_fn(m, hret1)
        else: a.bf_lbl(nc)
        m.imm32(EVQ, 4); a.mov_imm(0, 5); a.mov_imm(2, 6); m.imm32(OS_PUSH_PAGE, 2); a.jsr(2); a.nop(); a.bra_lbl(hret1); a.nop()
        a.deflabel(nc); m.pool_jump()
        a.mov_imm(EV_DOIT, 3); a.cmpeq(3, 0); m.bf_far(hret0)
        if self.doit_fn and not self.far_hend: self.doit_fn(m)
        if self.doit_fn and self.far_hend: a.movl_pool('__doit', 2); a.jsr(2); a.nop()   # codice DO IT dopo l'epilogo
        a.deflabel(hret1); a.mov_imm(1, 0); a.bra_lbl('__hx'); a.nop()
        a.deflabel(hret0); a.mov_imm(0, 0)
        a.deflabel('__hx'); a.ldsl_pr()
        for r in (14, 13, 12, 11, 10, 9, 8): a.movl_ld_inc(15, r)
        a.rts(); a.nop()
        m.pool_jump()
        if self.doit_fn and self.far_hend:
            # handler grande: il codice di DO IT (e le sue subroutine) sta qui, chiamato come subroutine; gli eventi
            # e l'epilogo restano vicini (bra/bt +-4 KB)
            a.deflabel('__doit'); a.stsl_pr(); self.doit_fn(m); a.ldsl_pr(); a.rts(); a.nop()
            m.pool_jump()
        # ---- helper comuni ----
        a.deflabel('__udiv'); a.mov_imm(0, 0); a.mov_imm(0, 1); a.tst_rr(5, 5); dz = m.newlabel('dz'); a.bt_lbl(dz); a.mov_imm(32, 2)
        dl = m.newlabel('dl'); a.deflabel(dl); a.shll(4); a.rotcl(1); a.cmphs(5, 1); ds = m.newlabel('ds'); a.bf_lbl(ds); a.sub(5, 1); a.sett(); a.deflabel(ds); a.rotcl(0); a.dt(2); a.bf_lbl(dl)
        a.deflabel(dz); a.rts(); a.nop()
        if self.has_msg:
            a.deflabel('__showmsg'); a.stsl_pr()
            m.imm32(A['msgw'] + 0x17, 1); a.movb_ld(1, 0); a.or_imm(0x80); a.movb_st(0, 1)
            if self.has_sel:
                m.imm32(A['sel'] + 0x17, 1); a.movb_ld(1, 0); a.and_imm(0x7F); a.movb_st(0, 1)
                nxt = [w for f, w in sorted(A['values'].items())]
                if nxt and self.keep_focus:                 # sposta il focus solo se era sul selettore (che sparisce)
                    kf = m.newlabel('kf'); m.imm32(A['page'] + 0x24, 1); a.movl_ld(1, 0); m.imm32(A['sel'], 1); a.cmpeq(1, 0); a.bf_lbl(kf)
                    sti(m, 1, MOVEDF); m.imm32(A['page'], 4); m.imm32(nxt[0], 5); m.imm32(SET_FOCUS, 2); a.jsr(2); a.nop(); a.deflabel(kf)
                elif nxt: m.imm32(A['page'], 4); m.imm32(nxt[0], 5); m.imm32(SET_FOCUS, 2); a.jsr(2); a.nop()
            sti(m, self.msg_fires, SHOWMSG)
            m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
            a.ldsl_pr(); a.rts(); a.nop(); m.pool_jump()
        a.deflabel(hend)

class PageAddon(Page, Addon):
    """Add-on con pagine native: la prima pagina e' l'add-on stesso (pg.value(...), pg.on_doit(...)); altre con add_page()."""
    def __init__(self, name, open_hold=('SHIFT',), open_press=('STOP',)):
        Addon.__init__(self, name)
        self.cdly = 0x1000
        self.defaults = {}       # slot -> valore iniziale (di tutte le pagine)
        self.next_slot = LOAD_SCRATCH + 0x44      # slot valori dopo la firma di init_scratch (+0x40); controllo a +0x10..+0x20
        self._reserve = 0; self.subpages = []
        self._page_init(self, '')
        self.open_hold = open_hold; self.open_press = open_press
        self.background(self._page_body)
    def _alloc_slot(self, default=0):
        s = self.next_slot; self.next_slot += 4; self.defaults[s] = default; return s
    def _alloc_buf(self, nbytes):
        s = self.next_slot; self.next_slot += (nbytes + 3) & ~3; return s
    def add_page(self, name):
        """nuova pagina dello stesso add-on (stessa API di PageAddon per widget ed eventi); 'name' = prefisso delle sue label"""
        assert name and all(p.name != name for p in self.subpages), "nome pagina doppio: %r" % name
        p = Page(); p.name = name; p._page_init(self, name + ':'); self.subpages.append(p); return p
    def _pages(self): return [self] + self.subpages
    def _page_body(self, m):
        a = m.a
        assert self.next_slot - LOAD_SCRATCH <= 0x1000, "scratch oltre 4 KB: %#x" % (self.next_slot - LOAD_SCRATCH)
        m.init_scratch(self.next_slot - LOAD_SCRATCH)
        # valori iniziali (una volta: la scratch e' azzerata da init_scratch, INIT segnala)
        INIT = LOAD_SCRATCH + 0x10
        ld(m, INIT, 2); a.tst_rr(2, 2); di = m.newlabel('di'); m.bf_far(di)
        for slot, val in self.defaults.items(): sti(m, val, slot)
        for p in self._pages():
            if p.init_fn: a.ns = p.ns; p.init_fn(m); a.ns = ''
        sti(m, 1, INIT); a.deflabel(di)
        m.pool_jump()
        # DO IT e CANCEL accesi finche' e' aperta una nostra pagina; all'uscita CANCEL (+0x98) torna com'era (nelle pagine
        # dell'OS FULL LEVEL deve restare FULL LEVEL)
        ours = m.newlabel('ours'); notours = m.newlabel('notours'); kdone = m.newlabel('kdone'); have = m.newlabel('have')
        m.imm32(EVQ + 0x68, 1); a.movl_ld(1, 2)
        for p in self._pages(): m.imm32(p.addr['page'], 3); a.cmpeq(3, 2); m.bt_far(ours)
        a.bra_lbl(notours); a.nop()
        m.pool_jump()
        a.deflabel(ours)
        ld(m, CXSAVE, 0); a.tst_rr(0, 0); a.bf_lbl(have)
        m.imm32(INPUT_OBJ + 0x98, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); a.movt(0); a.mov_imm(3, 3); a.sub(0, 3); st(m, 3, CXSAVE)   # 2 + (acceso)
        a.deflabel(have)
        m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_CANCEL, 2); a.jsr(2); a.nop()
        m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
        a.bra_lbl(kdone); a.nop()
        m.pool_jump()
        a.deflabel(notours)
        ld(m, CXSAVE, 5); a.tst_rr(5, 5); a.bt_lbl(kdone)
        a.add_imm(-2, 5); m.imm32(INPUT_OBJ, 4); m.imm32(ENABLE_CANCEL, 2); a.jsr(2); a.nop(); sti(m, 0, CXSAVE)
        a.deflabel(kdone)
        m.pool_jump()
        for p in self._pages():
            a.ns = p.ns; p._emit_page(m); a.ns = ''
    def _asm(self, base):
        self.a = SHAsm(base); self.scratch = base + SLOT_CODE_MAX; self.magic = (zlib.crc32(self.name.encode()) & 0xffffffff) | 1
        self.a.stsl_pr(); self._body(self); self.a.ldsl_pr(); self.a.rts(); self.a.nop(); self._emit_helpers()
        return self.a.assemble()
    def _build_at(self, base):
        # passata 1: indirizzi dummy DISTINTI (stessa dimensione pool), poi oggetti a base+len codice, passata 2 reale
        pages = self._pages()
        for k, p in enumerate(pages):
            d = 0xDEAD0000 + (k << 12)
            p.addr = dict(page=d + 1, first=d + 2, msgw=d + 3, msgt=d + 4, sel=d + 5, values={it[1]: d + 0x10 + it[1] for it in p.items if it[0] in ('value', 'choice')},
                          dlabels={it[1]: d + 0x100 + it[1] for it in p.items if it[0] == 'dlabel'})
        code = self._asm(base); code_len = (len(code) + 3) & ~3; hs = [self.a.labels[p.ns + 'handler'] for p in pages]
        b = _Block(base + code_len)
        for p, h in zip(pages, hs): p.addr = p._build_objects(b, h)
        code2 = self._asm(base)
        assert len(code2) == len(code) and [self.a.labels[p.ns + 'handler'] for p in pages] == hs, "dimensione codice cambiata tra le passate"
        self._last = (code_len, len(b.buf), hs[0], self.addr)
        return code2 + bytes(code_len - len(code2)) + bytes(b.buf)
    def build_wav(self, out, name=None):
        """WAV rilocabile (MPCADDN3) per l'HUB: la voce OTHER (se other_menu) e la pagina vanno nell'header."""
        self._build_at(ADDON_BASE); code_len, objs_len, handler, addr = self._last     # indirizzi a ADDON_BASE (slot 0)
        built = [(p, p.addr, self.a.labels[p.ns + 'handler']) for p in self._pages()]
        pcm, nrel = pack_addon3(self.name, self._build_at, page_off=addr['page'] - ADDON_BASE if self.other else None, menu=self.other,
                                 reserve=getattr(self, 'reserve', 0))
        n = wav_from_pcm(out, pcm)
        print("%s [pagina nativa, HUB]: %d byte codice + %d byte oggetti, %d rilocazioni, pagina @%08x%s -> %s (%d byte)" % (
            self.name, code_len, objs_len, nrel, addr['page'], (" voce OTHER '%s'" % self.other) if self.other else "", out, n))
        for p, ad, hd in built:
            p.page = ad['page']; p.handler = hd; p.first = ad['first']; p.msgw = ad['msgw']; p.msgt = ad['msgt']; p.values = ad['values']; p.dlabels = ad['dlabels']
        return out
