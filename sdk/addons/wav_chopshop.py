#!/usr/bin/env python3
# CHOP SHOP -> CHOPSHOP.WAV: tutti i tagli in un add-on solo, una voce in OTHER ("Chop Shop")
#   pagina del menu: riga 0 "CHOP SHOP", riga 1 "Mode: Transient" (rotella: BPM / Transient / Tap / Warp); DO IT apre la
#   pagina del modo, CANCEL da li' torna al menu; il modo scelto resta (scratch dell'add-on).
#   Pagine dei modi = quelle degli add-on singoli, ospitate (pages.HOST + page_or_addon): BPM = Auto Chop
#   (wav_autochop2), Transient = Trans-Chop (wav_transchop), Tap = lazy chop (wav_tapchop), Warp = Auto Warp
#   (wav_autowarp). Stesso codice e stessi test: selftest() di ogni modulo gira sul WAV di Chop Shop.
#   Un blocco solo di 28 KB (riserva di Trans-Chop): il buffer di lavoro di Trans-Chop sta a +0x4000, quindi codice,
#   oggetti e tabella di rilocazione di tutte le pagine devono finire prima (controllato nel test).
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pages
from pages import PageAddon, ld
from mpcmod import EVQ, OS_PUSH_PAGE, SLOT_CODE_MAX

cs = PageAddon("chopshop", open_press=None)
cs.other_menu("Chop Shop")
cs.label("CHOP SHOP", 0, 0)
MODES = ["BPM", "Transient", "Tap", "Warp"]
MODE = cs.choice("Mode: ", 0, 1, MODES, default=1)
pages.HOST = cs
import wav_autochop2 as AC, wav_transchop as TC, wav_tapchop as TP, wav_autowarp as AW
pages.HOST = None
MODULES = [AC, TC, TP, AW]          # nell'ordine di MODES
assert all(mod.pg.host is cs for mod in MODULES), "un modulo era gia' importato da solo: importa wav_chopshop prima"

def doit(m):
    """DO IT = apre la pagina del modo scelto (CANCEL li' torna qui: pila delle pagine dell'OS)"""
    a = m.a
    ld(m, MODE, 9); a.extu_b(9, 9)
    for k, mod in enumerate(MODULES):
        nx = m.newlabel('md'); a.mov_imm(k, 0); a.cmpeq(0, 9); a.bf_lbl(nx)
        m.imm32(EVQ, 4); m.imm32(mod.pg.addr['page'], 5); a.mov_imm(0, 6); m.imm32(OS_PUSH_PAGE, 2); a.jsr(2); a.nop()
        a.deflabel(nx)
cs.on_doit(doit)


def selftest(out):
    from validate import Harness
    from pages import SET_FOCUS, ENABLE_DOIT
    from shasm import SHAsm
    import audit_static
    # un blocco: codice e oggetti prima del buffer di lavoro di Trans-Chop (la tabella di rilocazione puo' finirci sotto:
    # l'HUB la legge solo all'installazione, dalla copia nel blocco, e poi non la usa piu')
    magic, name, code, rel, ln = audit_static.payload(out)
    end = len(code); limit = SLOT_CODE_MAX - TC.WORK_OFF; rend = end + 4 + 4 * len(rel)
    assert TC.HOSTED and limit + TC.POS_OFF + 4 * TC.MAXSL == SLOT_CODE_MAX
    assert end <= limit, "Chop Shop: codice+oggetti fino a %#x, il buffer di Trans-Chop parte da %#x" % (end, limit)
    assert rend <= SLOT_CODE_MAX
    hdr = open(out, 'rb').read(); hdr = hdr[hdr.find(b'data') + 8:]
    assert hdr[:8] == b'MPCADDN3' and hdr[32:48].rstrip(b'\0') == b'Chop Shop' and ln == SLOT_CODE_MAX
    print("  ok un blocco da %d KB: codice+oggetti fino a %#x, buffer di Trans-Chop da %#x (margine %d byte), rilocazioni fino a %#x"
          % (ln // 1024, end, limit, limit - end, rend))
    # menu dei modi
    LOG, MSG = 0x88E00000, 0x88E10000
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    stub(OS_PUSH_PAGE, lambda s: (s.movl_pool(LOG, 1), s.movl_st(4, 1), s.movl_st_disp(5, 4, 1), s.movl_st_disp(6, 8, 1), s.rts(), s.nop()))
    stub(ENABLE_DOIT, lambda s: (s.movl_pool(LOG + 0x10, 1), s.movl_st(5, 1), s.rts(), s.nop()))
    stub(SET_FOCUS, lambda s: (s.movl_pool(LOG + 0x20, 1), s.movl_st(4, 1), s.movl_st_disp(5, 4, 1), s.rts(), s.nop()))
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato"
    def call(obj, type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = cs.handler; e.maxsteps = 5_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    h.ram_w32(EVQ + 0x68, cs.page); h.fire(3)
    assert e.rd(MODE, 1) == 1, "modo di partenza: Transient"
    assert call(cs.page, 0x23) == 0 and h.ram_r32(LOG + 0x10) == 1 and h.ram_r32(LOG + 0x24) == cs.first, "enter: DO IT on + focus sul modo"
    pg_addrs = [mod.pg.page for mod in MODULES]
    assert len(set(pg_addrs + [cs.page])) == 5 and all(cs.page < p < cs.page + 0x7000 for p in pg_addrs)
    for k, nm in enumerate(MODES):
        e.wr(MODE, k, 1); h.ram_w32(LOG + 4, 0)
        assert call(cs.page, 2, code=0x2E) == 1 and h.ram_r32(LOG) == EVQ and h.ram_r32(LOG + 4) == pg_addrs[k] and h.ram_r32(LOG + 8) == 0, nm
    print("  ok menu: 'Mode:' parte da Transient, DO IT apre la pagina giusta per %s" % ' / '.join(MODES))
    # tasti DO IT / CANCEL dell'OS (FULL LEVEL = CANCEL solo con INPUT_OBJ+0x98): accesi nelle nostre pagine, CANCEL rimesso
    # com'era all'uscita (ENABLE_CANCEL = modello dell'harness: scrive il flag)
    from pages import INPUT_OBJ, CXSAVE
    CF = INPUT_OBJ + 0x98; OSPAGE = 0x88123456
    for before in (0, 1):
        h.ram_w32(EVQ + 0x68, OSPAGE); h.fire(2); h.ram_w32(CF, before); h.ram_w32(LOG + 0x10, 0)
        h.ram_w32(EVQ + 0x68, cs.page); h.fire(1)
        assert h.ram_r32(CF) == 1 and h.ram_r32(LOG + 0x10) == 1 and h.ram_r32(CXSAVE) == 2 + before, (before, h.ram_r32(CXSAVE))
        h.ram_w32(EVQ + 0x68, TP.pg.page); h.ram_w32(CF, 0); h.ram_w32(LOG + 0x10, 0); h.fire(1)   # DO IT/CANCEL "una volta" li spegne
        assert h.ram_r32(CF) == 1 and h.ram_r32(LOG + 0x10) == 1, "riaccesi nella pagina Tap"
        h.ram_w32(EVQ + 0x68, OSPAGE); h.fire(1)
        assert h.ram_r32(CF) == before and h.ram_r32(CXSAVE) == 0, "all'uscita CANCEL torna com'era"
    print("  ok DO IT e CANCEL accesi in tutte le pagine di Chop Shop (anche se l'OS li spegne), CANCEL rimesso com'era all'uscita")
    # le pagine dei modi: i test di ciascun add-on, sul WAV di Chop Shop
    for nm, mod in zip(MODES, MODULES):
        print("== modo %s (%s)" % (nm, mod.__name__)); mod.selftest(out)
    print("VALIDATO: Chop Shop (%s in un add-on, una voce OTHER) -> %s" % (' / '.join(MODES), os.path.basename(out)))


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'CHOPSHOP.WAV'); cs.build_wav(out); selftest(out)
