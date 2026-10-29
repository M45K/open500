#!/usr/bin/env python3
# Harness di validazione generico per addon costruiti con mpcmod (OBBLIGATORIO prima del flash).
# Carica il .bin, mappa RAM/MMIO come l'MPC, stubba la funzione ORIG (rts) e invoca il trampolino
# N volte come farebbe l'OS. Verifica: ritorno pulito al chiamante, registri r8-r15 preservati,
# nessun crash; raccoglie i caratteri scritti sull'LCD per ispezione.
#   from validate import Harness
#   h=Harness('mpc500_keys.bin'); h.ram_w32(0x881c0640, 0x00000002); h.fire(3); print(h.lcd_text())
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import shemu
from mpcmod import RESERVE
from mpcmod import TRAMP_FLASH, HOOKS, SCRATCH

SENTINEL = 0xDEADBEEE

# SDK pubblico: il firmware e' il TUO MPC500.bin patchato (sito di Open500), mai quello di Akai nel repository.
# I test degli add-on ufficiali nominano il firmware di sviluppo ('mpc500_loader.bin'): se non c'e', si usa questo.
FIRMWARE = os.environ.get('OPEN500_FIRMWARE', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'MPC500.bin'))

class Harness:
    def __init__(self, binpath, ram_size=0x1200000, os_sdram=False, limit=None):
        if not os.path.exists(binpath):
            binpath = FIRMWARE
            if not os.path.exists(binpath):
                raise SystemExit("patched firmware not found: %s\nCreate it with the Open500 website installer and set "
                                 "OPEN500_FIRMWARE=/path/to/MPC500.bin" % binpath)
        self.emu = shemu.SH()
        if os.environ.get('O5_LOADER') == '11' and binpath.endswith('mpc500_loader.bin'):   # prova del loader v11 (non flashato)
            binpath = binpath[:-len('mpc500_loader.bin')] + 'mpc500_loader_v11.bin'
        self.loader_v11 = binpath.endswith('mpc500_loader_v11.bin')
        fw = open(binpath, 'rb').read()
        hooks = [h for h,(fo,orig) in HOOKS.items() if struct.unpack('<I', fw[fo:fo+4])[0] == TRAMP_FLASH]
        assert len(hooks) == 1, "il .bin non contiene esattamente un hook: %s" % hooks
        self.hook = hooks[0]; ORIG = HOOKS[self.hook][1]
        self.emu.map_flash(fw, 0xA0000000)
        self.emu.map_ram(0x88000000, ram_size)          # SDRAM (include SCRATCH 0x88F00000)
        if os_sdram:                                     # immagine SDRAM post-boot (codice+dati OS rilocati)
            img = open(os.path.join(os.path.dirname(__file__), '..', 'os_sdram.bin'), 'rb').read()
            buf, off = self.emu._region(0x88000000); buf[off:off+len(img)] = img
        if not os_sdram:                                 # senza immagine OS: ENABLE_DOIT / ENABLE_CANCEL = scrivono il flag
            from pages import INPUT_OBJ, ENABLE_DOIT, ENABLE_CANCEL      # (+0x9C / +0x98 dell'oggetto tasti) e tornano
            for fn, off in ((ENABLE_DOIT, 0x9C), (ENABLE_CANCEL, 0x98)):
                for i, w in enumerate((0xE000 | off, 0x600C, 0x0456, 0x000B, 0x0009)):   # mov #off,r0; extu.b r0,r0; mov.l r5,@(r0,r4); rts; nop
                    self.emu.wr(fn + 2 * i, w, 2)
        self.emu.mmio_r[0xA4000138] = lambda a: 0xFF     # matrice tasti: nessun tasto
        for p in range(0xB6000000, 0xB6000010, 4):       # LCD mai busy
            self.emu.mmio_r[p] = lambda a: 0x00
        # stub ORIG: rts; nop  (opcode LE)
        self.emu.wr(ORIG, 0x000b, 2); self.emu.wr(ORIG+2, 0x0009, 2)
        self.emu.logrange = (0xB6000000, 0xB6000008)
        # lista sample come sull'MPC: MGR[0]=inizio (0x88680000), MGR[1]=limite; blocchi {+0 size, +4 name16, +0x100 PCM}
        # contigui, terminatore size 0. Stub di FUN_a0020684 (LIST_END) con la stessa logica dell'OS.
        from shasm import SHAsm
        self.MGR, self.LIST_HEAD, self.LIST_LIMIT = 0x8848ED30, 0x88680000, 0x88F00000 + RESERVE   # limite - RESERVE = 0x88F00000 = SCRATCH: delta di rilocazione 0
        if limit: self.LIST_LIMIT = limit
        self.emu.wr(self.MGR, self.LIST_HEAD, 4); self.emu.wr(self.MGR + 4, self.LIST_LIMIT, 4); self.emu.wr(self.LIST_HEAD, 0, 4)
        LIST_END = 0x884C4FE0
        s = SHAsm(LIST_END); s.movl_ld(4, 1); s.movl_ld_disp(4, 4, 3); s.deflabel('l'); s.cmphs(3, 1); s.bt_lbl('z')
        s.movl_ld(1, 0); s.tst_rr(0, 0); s.bt_lbl('f'); s.add_rr(0, 1); s.bra_lbl('l'); s.nop()
        s.deflabel('z'); s.mov_imm(0, 1); s.deflabel('f'); s.mov_rm_rn(1, 0); s.rts(); s.nop(); blob = s.assemble()
        for i in range(0, len(blob), 2): self.emu.wr(LIST_END + i, struct.unpack('<H', blob[i:i+2])[0], 2)
        self.fires = 0
    def list_end(self):
        """fine della lista sample: primo blocco con dimensione 0 o non plausibile (gli stub dei test non sempre scrivono il
        terminatore: senza questo controllo si camminava nella memoria oltre la lista)"""
        p = self.LIST_HEAD
        while p < self.LIST_LIMIT:
            sz = self.emu.rd(p, 4)
            if sz < 0x100 or sz & 3 or p + sz > self.LIST_LIMIT: break
            p += sz
        return p
    def add_sample(self, pcm, name=b'SAMPLE'):
        """appende un blocco sample alla lista (come fa LOAD sull'MPC). Ritorna l'indirizzo del blocco."""
        blk = self.list_end(); size = (0x100 + len(pcm) + 3) & ~3
        self.emu.wr(blk, size, 4)
        for i, c in enumerate(name[:16].ljust(16, b'\0')): self.emu.wr(blk + 4 + i, c, 1)
        self.emu.wr(blk + 0x18, len(pcm) // 2, 4)
        for k in range(0, len(pcm), 2): self.emu.wr(blk + 0x100 + k, struct.unpack('<H', pcm[k:k+2].ljust(2, b'\0'))[0], 2)
        self.emu.wr(blk + size, 0, 4)
        return blk
    def load_wav(self, wavpath, corrupt=False):
        """simula il LOAD di un WAV (il PCM finisce in un nuovo blocco della lista sample)"""
        w = open(wavpath, 'rb').read(); i = w.index(b'data'); n = struct.unpack('<I', w[i+4:i+8])[0]; pcm = bytearray(w[i+8:i+8+n])
        if corrupt: pcm[200] ^= 0x55   # nel corpo (oltre gli header MPCADDN2/3)
        return self.add_sample(bytes(pcm), os.path.basename(wavpath).split('.')[0].encode())
    def load_window(self):
        """simula il LOAD di un sample normale (senza addon)"""
        return self.add_sample(b'\0' * 0x200)
    def remove_sample(self, blk):
        """simula la cancellazione di un sample: compatta la lista (i blocchi successivi scendono)"""
        size = self.emu.rd(blk, 4); end = self.list_end() + 4
        buf, off = self.emu._region(blk); n = end - blk                    # copia diretta nella RAM emulata (veloce)
        if buf is not None and 0 < size <= n and off + n <= len(buf):
            buf[off:off + n - size] = buf[off + size:off + n]; buf[off + n - size:off + n] = bytes(size); return
        for p in range(blk, end - size, 4): self.emu.wr(p, self.emu.rd(p + size, 4), 4)
        for p in range(end - size, end, 4): self.emu.wr(p, 0, 4)
    OTHER_PAGE, OTHER_MODEL, OTHER_VAR, OTHER_LAST, OTHER_DESC, OTHER_STR, ORIG_HANDLER = 0x88490640, 0x88490684, 0x88490680, 0x884909DC, 0x881C2C30, 0x88502B54, 0x884CBED6
    OTHER_ITEMS = ['Master Level', 'Sus.pedal record', 'Truncate dura.', 'Tap average', 'Pad Sensitivity', 'Low Bat. Warning', "Owner's Name", 'Version']
    def setup_other(self):
        """pagina OTHER come sull'MPC: handler originale = stub che ritorna 0x77, tabella enum a 8 voci con le stringhe vere"""
        from shasm import SHAsm
        s = SHAsm(self.ORIG_HANDLER); s.mov_imm(0x77, 0); s.rts(); s.nop(); blob = s.assemble()
        for i in range(0, len(blob), 2): self.emu.wr(self.ORIG_HANDLER + i, struct.unpack('<H', blob[i:i+2])[0], 2)
        data = b''.join(x.encode() + b'\0' for x in self.OTHER_ITEMS)
        for i, c in enumerate(data): self.emu.wr(self.OTHER_STR + i, c, 1)
        for i, v in enumerate((8, self.OTHER_STR, 0, 0x104)): self.emu.wr(self.OTHER_DESC + 4*i, v, 4)
        self.emu.wr(self.OTHER_PAGE + 0x30, self.ORIG_HANDLER, 4); self.emu.wr(self.OTHER_MODEL + 0x20, self.OTHER_DESC, 4)
        self.emu.wr(self.OTHER_LAST + 8, 0, 4); self.emu.wr(self.OTHER_VAR, 0, 1)
    def load_hub(self, splash=False):
        """carica l'HUB (HUB.WAV) tramite il loader flash: dopo, gli addon MPCADDN3 caricati con load_wav vengono installati
        dall'HUB nel fire successivo al LOAD. splash=False: come dopo una ricarica (scatola nera gia' valida), niente
        scritta iniziale "OPEN 500" (2026-10-26: compare solo al primo avvio dopo l'accensione; la prova e' in test_hub)"""
        self.setup_other(); self.fire(2)
        if not splash:
            import hub
            L = self.hub_lay()
            for o in range(0, hub.BB_LEN, 4): self.ram_w32(L.BB + o, 0)
            self.ram_w32(L.BB_MAGIC, hub.BB_MAGIC_V); self.ram_w32(L.SPL_ON, 0)
        blk = self.load_wav(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'docs', 'addons', 'HUB.WAV')); self.fire(3)   # HUB pubblicato
        assert self.ram_r32(self.B() + 0x20) == 1, "HUB non installato dal loader flash"
        self.lcd_text(); return blk
    def other_menu(self):
        """voci correnti del menu OTHER (dalla tabella enum installata)"""
        d = self.ram_r32(self.OTHER_MODEL + 0x20); n = self.ram_r32(d); p = self.ram_r32(d + 4); out = []
        for _ in range(n):
            s = b''
            while self.emu.rd(p, 1): s += bytes([self.emu.rd(p, 1)]); p += 1
            p += 1; out.append(s.decode())
        return out
    # --- RAM helpers ---
    def ram_w32(self, addr, v): self.emu.wr(addr, v, 4)
    def ram_w16(self, addr, v): self.emu.wr(addr, v, 2)
    def ram_w8(self, addr, v):  self.emu.wr(addr, v, 1)
    def ram_r32(self, addr):    return self.emu.rd(addr, 4)
    def fill_scratch(self, pattern=0xAA88AFA9, n=0x100):
        """simula la scratch NON azzerata al boot (dove il loader mettera' l'area: limite - RESERVE)"""
        for i in range(0, n, 4): self.emu.wr(self.LIST_LIMIT - RESERVE + i, pattern, 4)
    def B(self):
        """base dell'area riservata a runtime (= SCRATCH di costruzione salvo Harness(limit=...)): MGR[1] dopo il loader"""
        b = self.ram_r32(self.MGR + 4)
        return b if self.ram_r32(b + 0x40) == 0x4D504301 else self.LIST_LIMIT - RESERVE
    def hub_lay(self):
        """indirizzi dell'HUB (tabelle slot) all'area vera"""
        import hub
        return hub.Lay(self.B() - SCRATCH)
    # --- esecuzione ---
    def fire(self, n=1, maxsteps=5_000_000):
        """invoca il trampolino n volte come l'OS. Ritorna lista dei passi per fire. Solleva su errore."""
        out = []
        for _ in range(n):
            e = self.emu
            regs = [0x11110000 + i for i in range(16)]
            regs[15] = 0x88400000 - 64
            e.r = regs[:]; e.pr = SENTINEL; e.pc = TRAMP_FLASH; e.maxsteps = maxsteps
            ok = e.run(stop_pc=SENTINEL)
            assert ok, "fire %d: NON e' tornato al chiamante (pc=0x%08x dopo %d step)" % (self.fires, e.pc, e.steps)
            for i in range(8, 16):
                assert e.r[i] == regs[i], "fire %d: r%d corrotto 0x%08x != 0x%08x" % (self.fires, i, e.r[i], regs[i])
            out.append(e.steps); self.fires += 1
            if self.scratch_mirror: self._mirror()
        return out
    scratch_mirror = True
    def _mirror(self):
        """HUB v3: la scratch di un add-on sta subito dopo il suo codice, non a +0x7000 come nella costruzione. I test di UN
        add-on (area non spostata, slot 0) leggono gli slot all'indirizzo di costruzione LOAD_SCRATCH: con un solo add-on
        installato quell'indirizzo diventa uno specchio della scratch vera. Con due o piu' add-on, niente specchio."""
        from mpcmod import LOAD_SCRATCH, ADDON_BASE
        e = self.emu; e.alias = None
        if self.B() != SCRATCH: return
        L = self.hub_lay(); ks = [k for k in range(16) if self.ram_r32(L.INST(k))]
        if len(ks) != 1 or self.ram_r32(L.BASE(ks[0])) != ADDON_BASE: return
        scr = self.ram_r32(L.SCR(ks[0])) & 0xDFFFFFFF; lo = LOAD_SCRATCH & 0xDFFFFFFF
        if scr and scr != lo: e.alias = (lo, lo + 0x1000, scr)
    # --- LCD ---
    def lcd_text(self, clear=True):
        """testo scritto al data port dall'ultimo reset del log (line1/line2 separate da '|')"""
        s = ''
        for pc, a, v, sz in self.emu.wlog:
            if a == 0xB6000004: s += chr(v & 0xff) if 32 <= (v & 0xff) < 127 else '.'
            elif a == 0xB6000000 and (v & 0xff) == 0xC0: s += '|'
            elif a == 0xB6000000 and (v & 0xff) == 0x80: s += '/'
        if clear: self.emu.wlog.clear()
        return s

if __name__ == '__main__':
    h = Harness(sys.argv[1])
    steps = h.fire(3)
    print("hook =", h.hook, "| OK: 3 fire, ritorno pulito, r8-r15 preservati. step/fire =", steps)
    print("LCD:", repr(h.lcd_text()))
