#!/usr/bin/env python3
# PAD MUTE / SOLO (pagina nativa: SHIFT+TRACK MUTE o MODE -> OTHER -> "Pad Mute") -> PADMUTE.WAV (2026-10-16)
#   Richiesta piu' votata su YouTube (mattg505, 90zigbpm, KVNHDFK). Primo add-on con VISTA (docs/PROTOCOL.md, vista 4 MUTE).
#   riga 0: "PAD MUTE  BANK A"     riga 1: "M.S.-....... MUT" = stato dei 12 pad del banco + modo (MUT / SOL)
#     M = mutato, S = solo, - = zittito dal solo di un altro pad, . = suona
#   Pad colpito (pagina aperta) = mette/toglie il mute (o il solo, col modo SOL); il pad non suona. BANK A-D cambia banco.
#   DO IT = tutti i pad di nuovo attivi.  Uscendo dalla pagina mute e solo RESTANO (si suona dal MAIN coi pad zittiti).
# Come: il livello mixer del pad (blocco pad +0x8F, 0..100) va a 0 e il valore vero e' salvato nella scratch; quando il pad
#   torna attivo viene rimesso. Cambio di program: i livelli del program vecchio sono rimessi e il mute passa al nuovo.
#   ATTENZIONE (README): salvare il program con pad mutati salva livello 0.
#   2026-10-17 (PIANO.md 1.4): se un altro tool (Pad Tools, Trans-Chop, Auto Chop...) o la pagina PROGRAM cambia il livello
#   di un pad mutato, Pad Mute lo ricorda (a rotazione, 4 pad per fire) e lo rimette a 0: al ripristino torna il valore
#   NUOVO. Un livello che non e' piu' 0 non viene mai sovrascritto col valore vecchio.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ
from view import View, VIEW_MUTE

PROG_BASE, PROG_IDX, PROG_STRIDE, NPADS = 0x8837B5AC, 0x8847C0E2, 0x1F98, 48
PAD_OFF, PAD_SIZE, LEVEL = 0x18, 0xA4, 0x8F
BANKOBJ = 0x8849C98C
EV_PAD = 0x38
F_MUTE, F_SOLO, F_SILENT = 1, 2, 4                     # bit per pad nella vista (e nell'array STATE)

pg = PageAddon("padmute1", open_hold=('SHIFT',), open_press=('TRACK_MUTE',))
pg.other_menu("Pad Mute")
pg.far_hend = True
pg.label("PAD MUTE", 0, 0)
BANKL = pg.dlabel(10, 0, 6)
STL = pg.dlabel(0, 1, 12)
MODE = pg.choice(" ", 12, 1, ["MUT", "SOL"], default=0)
pg.message()
def arr(n): return [pg.alloc_slot() for _ in range((n + 3) // 4)][0]
MUTEA, SOLOA, APPL, SAVED = arr(NPADS), arr(NPADS), arr(NPADS), arr(NPADS)
APROG, DIRTY = pg.alloc_slot(), pg.alloc_slot()
SYNCP = pg.alloc_slot()                                # 2026-10-17: pad (a rotazione) di cui ricontrollare il livello
VBUF = arr(1 + NPADS)                                  # vista: banco + 48 byte di stato
OLDTXT = arr(20)
view = View(VIEW_MUTE, ver=1)

def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()
def far_bra(m, lbl): m.a.movl_pool(lbl, 2); m.a.jmp(2); m.a.nop()

def level_addr(m, r_prog, r_pad, r_out):
    """r_out = &livello del pad r_pad nel program r_prog. Usa r0-r3."""
    a = m.a
    m.imm32(PROG_STRIDE, 3); a.mov_rm_rn(r_prog, 0); a.mul_l(3, 0); a.sts_macl(1)
    m.imm32(PAD_SIZE, 3); a.mov_rm_rn(r_pad, 0); a.mul_l(3, 0); a.sts_macl(2); a.add_rr(2, 1)
    m.imm32(PROG_BASE + PAD_OFF + LEVEL, 3); a.add_rr(3, 1); a.mov_rm_rn(1, r_out)

def fire(m):
    a = m.a
    call(m, 'pm_maint')
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
    # riga 0: "BANK x"
    m.imm32(pg.addr['dlabels'][BANKL], 12)
    for ch in "BANK ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 9); a.extu_b(9, 9); a.mov_imm(3, 0); a.and_rr(0, 9)
    a.mov_rm_rn(9, 0); a.add_imm(ord('A'), 0); a.movb_st(0, 12)
    # riga 1: 12 caratteri di stato del banco
    a.mov_rm_rn(9, 0); a.mov_imm(12, 1); a.mul_l(1, 0); a.sts_macl(8)                    # r8 = primo pad
    m.imm32(pg.addr['dlabels'][STL], 12); a.mov_imm(12, 13)
    sl = m.newlabel('sl'); ch_ = m.newlabel('ch')
    a.deflabel(sl)
    m.imm32(SOLOA, 1); a.add_rr(8, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.mov_imm(ord('S'), 3); a.bf_lbl(ch_)
    m.imm32(MUTEA, 1); a.add_rr(8, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.mov_imm(ord('M'), 3); a.bf_lbl(ch_)
    m.imm32(APPL, 1); a.add_rr(8, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.mov_imm(ord('-'), 3); a.bf_lbl(ch_)
    a.mov_imm(ord('.'), 3)
    a.deflabel(ch_); a.movb_st(3, 12); a.add_imm(1, 12); a.add_imm(1, 8); a.dt(13); a.bf_lbl(sl)
    m.pool_jump()
    # ridisegno solo se il testo e' cambiato
    m.imm32(OLDTXT, 5); a.mov_imm(0, 3)
    for lbl, w in ((BANKL, 6), (STL, 12)):
        m.imm32(pg.addr['dlabels'][lbl], 4); a.mov_imm(w, 6)
        cl = m.newlabel('cl'); cs = m.newlabel('cs'); a.deflabel(cl)
        a.movb_ld(4, 0); a.movb_ld(5, 1); a.cmpeq(0, 1); a.bt_lbl(cs); a.mov_imm(1, 3); a.movb_st(0, 5)
        a.deflabel(cs); a.add_imm(1, 4); a.add_imm(1, 5); a.dt(6); a.bf_lbl(cl)
    a.tst_rr(3, 3); nr = m.newlabel('nr'); a.bt_lbl(nr)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel(nr)
    far_bra(m, 'pm_end')
    m.pool_jump()

    # ---- pm_maint (a ogni fire, pagina aperta o no): applica i cambi, segue il program, pubblica la vista ----
    a.deflabel('pm_maint'); a.stsl_pr()
    m.imm32(PROG_IDX, 1); a.movw_ld(1, 10); a.extu_w(10, 10)                             # r10 = program corrente
    # 0) livelli cambiati da fuori su pad mutati (program APROG): ricordati come valore vero, il pad resta a 0
    ld(m, APROG, 11); ld(m, SYNCP, 9); a.mov_imm(4, 13)
    syl = m.newlabel('syl'); syn = m.newlabel('syn'); a.deflabel(syl)
    m.imm32(APPL, 1); a.add_rr(9, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(syn)
    level_addr(m, 11, 9, 4); a.movb_ld(4, 0); a.extu_b(0, 0); a.tst_rr(0, 0); a.bt_lbl(syn)
    m.imm32(SAVED, 1); a.add_rr(9, 1); a.movb_st(0, 1); a.mov_imm(0, 0); a.movb_st(0, 4)
    a.deflabel(syn); a.add_imm(1, 9); a.mov_imm(NPADS, 0); a.cmpeq(0, 9); sy2 = m.newlabel('sy2'); a.bf_lbl(sy2); a.mov_imm(0, 9)
    a.deflabel(sy2); a.dt(13); a.bf_lbl(syl)
    st(m, 9, SYNCP)
    m.pool_jump()
    ld(m, DIRTY, 0); a.tst_rr(0, 0); go = m.newlabel('go'); a.bf_lbl(go)
    ld(m, APROG, 0); a.cmpeq(10, 0); pub = m.newlabel('pub'); m.bt_far(pub)
    a.deflabel(go)
    # 1) rimette i livelli salvati nel program dove erano stati applicati
    ld(m, APROG, 11); a.mov_imm(0, 9)
    rl = m.newlabel('rl'); rn = m.newlabel('rn'); a.deflabel(rl)
    m.imm32(APPL, 1); a.add_rr(9, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(rn)
    a.mov_imm(0, 0); a.movb_st(0, 1)
    level_addr(m, 11, 9, 4); a.movb_ld(4, 0); a.tst_rr(0, 0); a.bf_lbl(rn)            # non piu' 0: l'ha cambiato qualcun altro
    m.imm32(SAVED, 1); a.add_rr(9, 1); a.movb_ld(1, 0); a.movb_st(0, 4)
    a.deflabel(rn); a.add_imm(1, 9); a.mov_imm(NPADS, 0); a.cmpeq(0, 9); a.bf_lbl(rl)
    m.pool_jump()
    # 2) c'e' almeno un solo? (r12)
    a.mov_imm(0, 12); a.mov_imm(NPADS, 13); m.imm32(SOLOA, 1)
    so = m.newlabel('so'); a.deflabel(so); a.movb_ld(1, 0); a.or_rr(0, 12); a.add_imm(1, 1); a.dt(13); a.bf_lbl(so)
    # 3) applica al program corrente: zittito = mute || (qualche solo && !solo)
    a.mov_imm(0, 9)
    al = m.newlabel('al'); an = m.newlabel('an'); sil = m.newlabel('sil'); a.deflabel(al)
    m.imm32(MUTEA, 1); a.add_rr(9, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.bf_lbl(sil)
    a.tst_rr(12, 12); a.bt_lbl(an)
    m.imm32(SOLOA, 1); a.add_rr(9, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.bf_lbl(an)
    a.deflabel(sil)
    level_addr(m, 10, 9, 4); a.movb_ld(4, 0); m.imm32(SAVED, 1); a.add_rr(9, 1); a.movb_st(0, 1)
    a.mov_imm(0, 0); a.movb_st(0, 4)
    m.imm32(APPL, 1); a.add_rr(9, 1); a.mov_imm(1, 0); a.movb_st(0, 1)
    a.deflabel(an); a.add_imm(1, 9); a.mov_imm(NPADS, 0); a.cmpeq(0, 9); m.bf_far(al)
    st(m, 10, APROG); sti(m, 0, DIRTY)
    m.pool_jump()
    # 4) vista: banco + stato dei 48 pad
    a.deflabel(pub)
    m.imm32(VBUF, 8); m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.and_imm(3); a.movb_st(0, 8); a.add_imm(1, 8)
    a.mov_imm(0, 9)
    vl = m.newlabel('vl'); a.deflabel(vl); a.mov_imm(0, 2)
    for arr_, bit in ((MUTEA, F_MUTE), (SOLOA, F_SOLO), (APPL, F_SILENT)):
        nb = m.newlabel('nb'); m.imm32(arr_, 1); a.add_rr(9, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); a.bt_lbl(nb)
        a.mov_rm_rn(2, 0); a.or_imm(bit); a.mov_rm_rn(0, 2); a.deflabel(nb)
    a.movb_st(2, 8); a.add_imm(1, 8); a.add_imm(1, 9); a.mov_imm(NPADS, 0); a.cmpeq(0, 9); a.bf_lbl(vl)
    view.publish(m, VBUF, 1 + NPADS)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    view.emit_subs(m)
    a.deflabel('pm_end')
pg.on_fire(fire)
pg.on_inactive(lambda m: call(m, 'pm_maint'))

def ev_pad(m, hret0, hret1):
    """pagina aperta: il pad colpito cambia il suo mute/solo e non suona"""
    a = m.a
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); rel = m.newlabel('rel'); m.bt_far(rel)
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9); a.mov_imm(12, 0); a.cmphs(0, 9); m.bt_far(rel)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.and_imm(3); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(0, 9)
    m.imm32(MODE, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); m.imm32(MUTEA, 1); so = m.newlabel('so'); a.bt_lbl(so); m.imm32(SOLOA, 1)
    a.deflabel(so); a.add_rr(9, 1); a.movb_ld(1, 0)
    a.mov_imm(1, 2); a.xor_rr(2, 0); a.movb_st(0, 1)
    sti(m, 1, DIRTY)
    a.deflabel(rel); far_bra(m, hret1)
    m.pool_jump()
pg.on_event(EV_PAD, ev_pad)

def doit(m):
    a = m.a
    for arr_ in (MUTEA, SOLOA):
        m.imm32(arr_, 1); a.mov_imm(0, 0); a.mov_imm(NPADS // 4, 2)
        zl = m.newlabel('zl'); a.deflabel(zl); a.movl_st(0, 1); a.add_imm(4, 1); a.dt(2); a.bf_lbl(zl)
    sti(m, 1, DIRTY); pg.emit_message(m, "All pads on")
pg.on_doit(doit)


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'PADMUTE.WAV'); pg.build_wav(out)
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE, KEYS_ADDR, keymask, VBOX_OFF, VBOX_MAGIC, VSLOT, V_SEQ, V_LEN, V_VER, V_ALIVE, V_PAY
    from pages import SET_FOCUS
    from shasm import SHAsm
    MSG, LOG = 0x88E10000, 0x88E00000
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    stub(OS_PUSH_PAGE, lambda s: (s.movl_pool(LOG, 1), s.movl_st_disp(5, 4, 1), s.rts(), s.nop()))
    stub(ENABLE_DOIT, lambda s: (s.rts(), s.nop())); stub(SET_FOCUS, lambda s: (s.rts(), s.nop()))
    h.load_hub(); h.load_wav(out); h.fire(10); assert h.ram_r32(0x88F00144) == 1, "addon non installato"
    def call_(obj, type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 20_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    P = pg.page
    def pad(bank, k, vel=100):
        e.wr(BANKOBJ, bank, 1); r1 = call_(P, EV_PAD, code=k, val=vel); r2 = call_(P, EV_PAD, code=k, val=0); return r1, r2
    lvl = lambda prog, p: e.rd(PROG_BASE + prog * PROG_STRIDE + PAD_OFF + p * PAD_SIZE + LEVEL, 1)
    def fill(prog, base):
        for p in range(NPADS): e.wr(PROG_BASE + prog * PROG_STRIDE + PAD_OFF + p * PAD_SIZE + LEVEL, base + p, 1)
    txt = lambda k, w: bytes(e.rd(pg.dlabels[k] + i, 1) for i in range(w)).decode()
    fill(0, 40); fill(1, 10); h.ram_w16(PROG_IDX, 0)
    h.ram_w32(EVQ + 0x68, P); h.fire(3)
    assert txt(STL, 12) == '.' * 12 and txt(BANKL, 6) == 'BANK A'
    # SHIFT+TRACK MUTE apre la pagina
    h.ram_w32(EVQ + 0x68, 0); h.ram_w32(KEYS_ADDR, keymask('SHIFT', 'TRACK_MUTE')); h.fire(2); h.ram_w32(KEYS_ADDR, 0); h.fire(2)
    assert h.ram_r32(LOG + 4) == P, "SHIFT+TRACK MUTE"
    h.ram_w32(EVQ + 0x68, P)
    # mute A02 e A05: livello 0, gli altri intatti; il pad non suona (evento consumato)
    assert pad(0, 1) == (1, 1) and pad(0, 4) == (1, 1); h.fire(1)
    assert txt(STL, 12) == '.M..M.......', txt(STL, 12)
    assert lvl(0, 1) == 0 and lvl(0, 4) == 0 and lvl(0, 0) == 40 and lvl(0, 2) == 42
    pad(0, 1); h.fire(1); assert lvl(0, 1) == 41 and txt(STL, 12) == '....M.......', "unmute rimette il livello"
    print("  ok mute A02/A05 (livello 0), unmute A02 (livello 41 rimesso), pad consumati")
    # solo B03: tutti gli altri zittiti tranne lui; A05 resta mutato anche dopo
    e.wr(MODE, 1, 1); pad(1, 2); h.fire(1)
    assert lvl(0, 14) == 40 + 14 and all(lvl(0, p) == 0 for p in range(NPADS) if p != 14), [lvl(0, p) for p in range(NPADS)]
    assert txt(STL, 12) == '--S---------' and txt(BANKL, 6) == 'BANK B', txt(STL, 12)
    e.wr(BANKOBJ, 0, 1); h.fire(1); assert txt(STL, 12) == '----M-------', txt(STL, 12)
    pad(1, 2); h.fire(1); assert [lvl(0, p) for p in range(NPADS)] == [0 if p == 4 else 40 + p for p in range(NPADS)]
    print("  ok solo B03 zittisce gli altri 47; tolto il solo resta solo il mute di A05")
    # pagina chiusa: il mute resta; cambio program -> livelli del vecchio rimessi, mute applicato al nuovo
    h.ram_w32(EVQ + 0x68, 0x88123456); h.fire(3); assert lvl(0, 4) == 0
    h.ram_w16(PROG_IDX, 1); h.fire(1)
    assert lvl(0, 4) == 44 and lvl(1, 4) == 0 and lvl(1, 3) == 13, "il mute segue il program"
    print("  ok pagina chiusa: mute attivo; cambio program: vecchio ripristinato, nuovo mutato")
    # vista MUTE nella cassetta
    B = h.ram_r32(0x8848ED34); VB = B + VBOX_OFF; VS = VB + VIEW_MUTE * VSLOT
    assert h.ram_r32(VB) == VBOX_MAGIC and h.ram_r32(VS + V_LEN) == 49 and h.ram_r32(VS + V_VER) == 1
    pay = bytes(e.rd(VS + V_PAY + i, 1) for i in range(49))
    assert pay[1 + 4] == F_MUTE | F_SILENT and pay[1 + 0] == 0 and pay[0] == 1, pay          # banco B (ultimo pad colpito)
    s0 = h.ram_r32(VS + V_SEQ); a0 = h.ram_r32(VS + V_ALIVE); h.fire(3); assert h.ram_r32(VS + V_SEQ) == s0, "seq fermo se nulla cambia"
    e.wr(MODE, 0, 1); h.ram_w32(EVQ + 0x68, P); pad(0, 7); h.fire(1); assert h.ram_r32(VS + V_SEQ) == s0 + 1
    assert e.rd(VS + V_PAY + 1 + 7, 1) == F_MUTE | F_SILENT
    print("  ok vista MUTE: 'O5V1', 49 byte (banco + stato), seq cambia solo quando cambia lo stato")
    # DO IT: tutto attivo
    assert call_(P, 2, code=0x2E) == 1; h.fire(1)
    assert [lvl(1, p) for p in range(NPADS)] == [10 + p for p in range(NPADS)] and txt(STL, 12) == '.' * 12
    assert e.rd(VS + V_PAY + 1 + 7, 1) == 0
    print("  ok DO IT: tutti i pad di nuovo attivi, livelli originali")
    # un altro tool cambia il livello di un pad mutato: Pad Mute lo ricorda, il pad resta muto, al ripristino torna il nuovo
    h.ram_w32(EVQ + 0x68, P); h.ram_w16(PROG_IDX, 1); h.fire(1); e.wr(MODE, 0, 1); pad(0, 5); h.fire(1)
    assert lvl(1, 5) == 0
    e.wr(PROG_BASE + 1 * PROG_STRIDE + PAD_OFF + 5 * PAD_SIZE + LEVEL, 77, 1); h.fire(13)
    assert lvl(1, 5) == 0, "il pad mutato deve restare muto"
    pad(0, 5); h.fire(1); assert lvl(1, 5) == 77, "al ripristino il valore NUOVO (%d)" % lvl(1, 5)
    print("  ok livello cambiato da fuori su un pad mutato: resta muto, al ripristino torna il valore nuovo (77)")
    print("VALIDATO: Pad Mute -> %s" % os.path.basename(out))
