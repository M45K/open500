#!/usr/bin/env python3
# SEQ TOOLS (MODE -> OTHER -> "Seq Tools") -> SEQTOOLS.WAV (2026-10-16, issue #10 "Easy double or half seq")
#   riga 0: "SEQ TOOLS  8 BAR" (e messaggi)     riga 1: operazione  DOUBLE x2 / HALF /2
#   DO IT:  DOUBLE = la sequenza corrente diventa lunga il doppio e le battute 1..n sono copiate in n+1..2n (tutte le
#           tracce, tutti gli eventi): 2 -> 4 -> 8 battute in due DO IT.
#           HALF   = tiene la prima meta' (n/2 battute), gli eventi dopo la nuova fine sono tolti.
#   Vista 9 SEQ (docs/PROTOCOL.md): battute u16, eventi u16.
# Sequenza (stesso RE di Grid Seq, provato su HW): blocco = *(u32*)0x8847CBC8 nell'arena 0x881DB59C..+0x1A0000 (blocchi
#   [size], 0 = fine); +0x1C u16 battute; +0x30 tabella inizio battuta (tick & 0xFFFFFF, byte alto = metro), voce n = fine;
#   eventi da 16 B a +0x1C10 ordinati per tick, fine = tick 0x7FFFFFFF. La tabella e' gia' riempita oltre la fine (4/4
#   continuo): per DOUBLE le voci n+1..2n = voce (i-n) + lunghezza, col byte alto della battuta copiata.
#   Spostamenti dell'arena a interrupt bloccati; puntatore di riproduzione 0x8847CBD0 corretto come in Grid.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ
from view import View

CURSEQ, PLAYPTR = 0x8847CBC8, 0x8847CBD0
EV_OFF, BARS_OFF, BART_OFF = 0x1C10, 0x1C, 0x30
ARENA_LO, ARENA_HI = 0x881DB59C, 0x881DB59C + 0x1A0000
END, MAXBARS = 0x7FFFFFFF, 999
VIEW_SEQ = 9
OP_DOUBLE, OP_HALF = range(2)

pg = PageAddon("seqtool1", open_press=None)
pg.other_menu("Seq Tools")
pg.far_hend = True
pg.label("SEQ TOOLS", 0, 0)
BARL = pg.dlabel(10, 0, 6)
OP = pg.choice(None, 0, 1, ["DOUBLE x2", "HALF /2"], default=OP_DOUBLE)
pg.message()
VBUF, NEV, OLDB = pg.alloc_slot(), pg.alloc_slot(), pg.alloc_slot()
view = View(VIEW_SEQ, ver=1)

def call(m, lbl): m.a.movl_pool(lbl, 2); m.a.jsr(2); m.a.nop()
def far_bra(m, lbl): m.a.movl_pool(lbl, 2); m.a.jmp(2); m.a.nop()

def fire(m):
    a = m.a
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
    call(m, 'sq_maint')
    # "  8BAR" / "NO SEQ" a destra della riga 0, ridisegno solo se cambia
    ld(m, OLDB, 0); a.cmpeq(9, 0); same = m.newlabel('same'); m.bt_far(same)
    st(m, 9, OLDB); m.imm32(pg.addr['dlabels'][BARL], 12)
    a.tst_rr(8, 8); ok = m.newlabel('ok'); a.bf_lbl(ok)
    for ch in "NO SEQ": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    nd = m.newlabel('nd'); a.bra_lbl(nd); a.nop()
    a.deflabel(ok)
    a.mov_rm_rn(9, 4); a.mov_imm(100, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 11)     # r0 centinaia, r11 resto
    a.mov_rm_rn(0, 13)                                                                   # r13 = centinaia (0 = niente zeri)
    a.tst_rr(0, 0); a.mov_imm(ord(' '), 3); c1 = m.newlabel('c1'); a.bt_lbl(c1); a.add_imm(0x30, 0); a.mov_rm_rn(0, 3); a.deflabel(c1)
    a.movb_st(3, 12); a.add_imm(1, 12)
    a.mov_rm_rn(11, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
    a.mov_rm_rn(0, 2); a.or_rr(13, 2); a.add_imm(0x30, 0); c2 = m.newlabel('c2'); a.tst_rr(2, 2); a.bf_lbl(c2); a.mov_imm(ord(' '), 0); a.deflabel(c2)
    a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(0x30, 1); a.movb_st(1, 12); a.add_imm(1, 12)
    for ch in "BAR": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    a.deflabel(nd); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.deflabel(same)
    far_bra(m, 'sq_fend')
    m.pool_jump()

    # ---- sq_ctx: r8 = sequenza corrente (0 se non valida), r9 = battute (0 senza sequenza). r0-r1 ----
    a.deflabel('sq_ctx')
    m.imm32(CURSEQ, 1); a.movl_ld(1, 8); a.mov_imm(0, 9)
    m.imm32(ARENA_LO, 1); bad = m.newlabel('bad'); a.cmphs(1, 8); a.bf_lbl(bad); m.imm32(ARENA_HI, 1); a.cmphs(1, 8); a.bt_lbl(bad)
    a.mov_rm_rn(8, 1); a.add_imm(BARS_OFF, 1); a.movw_ld(1, 9); a.rts(); a.extu_w(9, 9)
    a.deflabel(bad); a.mov_imm(0, 8); a.rts(); a.nop()
    m.pool_jump()
    # ---- sq_nev: r8 = sequenza -> r12 = &marcatore di fine, r13 = numero di eventi. r0-r1 ----
    a.deflabel('sq_nev')
    m.imm32(EV_OFF, 12); a.add_rr(8, 12); a.mov_imm(0, 13); m.imm32(END, 1)
    nl = m.newlabel('nl'); nx = m.newlabel('nx'); a.deflabel(nl)
    a.movl_ld(12, 0); a.cmpeq(1, 0); a.bt_lbl(nx); a.add_imm(16, 12); a.add_imm(1, 13); a.bra_lbl(nl); a.nop()
    a.deflabel(nx); a.rts(); a.nop()
    m.pool_jump()
    # ---- sq_end: r10 = blocco di dimensione 0 che chiude l'arena; T = trovato. r0, r3 ----
    a.deflabel('sq_end')
    m.imm32(ARENA_LO, 10); m.imm32(ARENA_HI, 3)
    el = m.newlabel('el'); ee = m.newlabel('ee'); a.deflabel(el)
    a.movl_ld(10, 0); a.tst_rr(0, 0); a.bt_lbl(ee); a.add_rr(0, 10); a.cmphs(3, 10); a.bf_lbl(el)
    a.rts(); a.clrt()
    a.deflabel(ee); a.rts(); a.sett()
    m.pool_jump()
    # ---- sq_maint: contesto + vista (battute, eventi). Lascia r8 = sequenza, r9 = battute ----
    a.deflabel('sq_maint'); a.stsl_pr()
    a.bsr_lbl('sq_ctx'); a.nop(); a.mov_imm(0, 13); a.tst_rr(8, 8); nn = m.newlabel('nn'); a.bt_lbl(nn)
    a.bsr_lbl('sq_nev'); a.nop()
    a.deflabel(nn)
    m.imm32(VBUF, 1); a.movw_st(9, 1); a.add_imm(2, 1); a.movw_st(13, 1)
    a.movl_st_dec(8, 15); a.movl_st_dec(9, 15)
    view.publish(m, VBUF, 4)
    a.movl_ld_inc(15, 9); a.movl_ld_inc(15, 8)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    view.emit_subs(m)
    a.deflabel('sq_fend')
pg.on_fire(fire)
pg.on_inactive(lambda m: call(m, 'sq_maint'))


def doit(m):
    a = m.a
    def msg_bars(prefix):                                     # "<prefix>NNN bars" con r9 = battute
        m.imm32(pg.addr['msgt'], 12)
        for ch in prefix: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_rm_rn(9, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv'); a.mov_rm_rn(1, 11)  # r0 = n/10, r11 = unita'
        a.mov_rm_rn(0, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')                     # r0 centinaia, r1 decine
        hz = m.newlabel('hz'); hd = m.newlabel('hd')          # (prima 'hd' era una label fissa: emessa 2 volte, saltava nella 2a copia)
        a.tst_rr(0, 0); a.bt_lbl(hz); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.mov_imm(1, 7); a.bra_lbl(hd); a.nop()
        a.deflabel(hz); a.mov_imm(0, 7)
        a.deflabel(hd); a.mov_rm_rn(1, 0); a.or_rr(7, 0); dz = m.newlabel('dz'); a.tst_rr(0, 0); a.bt_lbl(dz)
        a.mov_rm_rn(1, 0); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.deflabel(dz); a.mov_rm_rn(11, 0); a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12)
        for ch in " bars": a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_imm(0, 0); a.movb_st(0, 12); pg._hcall(m, '__showmsg')
    a.bsr_lbl('sq_ctx'); a.nop(); a.tst_rr(8, 8); ok = m.newlabel('ok'); m.bf_far(ok)
    pg.emit_message(m, "No sequence"); far_bra(m, 'sq_dx')
    a.deflabel(ok)
    m.imm32(OP, 1); a.movb_ld(1, 0); a.tst_rr(0, 0); half = m.newlabel('half'); m.bf_far(half)
    m.pool_jump()

    # ===== DOUBLE =====
    a.mov_rm_rn(9, 0); a.add_rr(0, 0); m.imm32(MAXBARS, 1); a.cmpgt(1, 0); tm = m.newlabel('tm'); m.bf_far(tm)
    pg.emit_message(m, "Too many bars"); far_bra(m, 'sq_dx')
    a.deflabel(tm); a.tst_rr(9, 9); nz = m.newlabel('nz'); m.bf_far(nz)
    pg.emit_message(m, "Empty sequence"); far_bra(m, 'sq_dx')
    a.deflabel(nz)
    a.bsr_lbl('sq_nev'); a.nop()                                                       # r12 = pos (fine), r13 = N
    a.mov_rm_rn(13, 11); a.shll2(11); a.shll2(11)                                      # r11 = k = 16*N byte
    a.bsr_lbl('sq_end'); a.nop(); fu = m.newlabel('fu'); m.bf_far(fu)
    a.mov_rm_rn(10, 0); a.add_imm(4, 0); a.add_rr(11, 0); m.imm32(ARENA_HI, 1); a.cmphs(1, 0); nf = m.newlabel('nf'); m.bf_far(nf)
    a.deflabel(fu); pg.emit_message(m, "Memory full"); far_bra(m, 'sq_dx')
    a.deflabel(nf)
    m.pool_jump()
    # r14 = L = fine della sequenza in tick (voce n della tabella)
    a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(8, 0); a.add_imm(BART_OFF, 0); a.movl_ld(0, 14); m.imm32(0xFFFFFF, 1); a.and_rr(1, 14)
    a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0); a.movl_st_dec(7, 15)  # interrupt bloccati
    # sposta in su di k byte [pos, fine arena + 4)
    a.mov_rm_rn(10, 1); a.add_imm(4, 1); a.mov_rm_rn(1, 2); a.add_rr(11, 2)
    ml = m.newlabel('ml'); mx = m.newlabel('mx'); a.deflabel(ml)
    a.cmphs(1, 12); a.bt_lbl(mx); a.add_imm(-4, 1); a.add_imm(-4, 2); a.movl_ld(1, 0); a.movl_st(0, 2); a.bra_lbl(ml); a.nop()
    a.deflabel(mx)
    # copia gli N eventi dopo la fine con tick + L
    m.imm32(EV_OFF, 1); a.add_rr(8, 1); a.mov_rm_rn(12, 2); a.mov_rm_rn(13, 3)
    cz = m.newlabel('cz'); a.tst_rr(3, 3); a.bt_lbl(cz)
    cl = m.newlabel('cl'); a.deflabel(cl)
    a.movl_ld(1, 0); a.add_rr(14, 0); a.movl_st(0, 2)
    for d in (4, 8, 12): a.movl_ld_disp(1, d, 0); a.movl_st_disp(0, d, 2)
    a.add_imm(16, 1); a.add_imm(16, 2); a.dt(3); a.bf_lbl(cl)
    a.deflabel(cz)
    a.movl_ld(8, 0); a.add_rr(11, 0); a.movl_st(0, 8)                                  # dimensione del blocco
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 0); a.cmphs(12, 0); pp = m.newlabel('pp'); a.bf_lbl(pp); a.add_rr(11, 0); a.movl_st(0, 1); a.deflabel(pp)
    # tabella: voci n+1..2n = voce (i-n) + L, byte alto della battuta copiata
    a.mov_rm_rn(8, 1); a.add_imm(BART_OFF, 1); a.mov_rm_rn(9, 2); a.shll2(2); a.add_rr(1, 2); a.add_imm(4, 1); a.add_imm(4, 2)   # r1 = &t[1], r2 = &t[n+1]
    a.mov_rm_rn(9, 3); m.imm32(0xFFFFFF, 5)
    tl = m.newlabel('tl'); a.deflabel(tl)
    a.movl_ld(1, 0); a.mov_rm_rn(0, 4); a.and_rr(5, 4); a.add_rr(14, 4); a.and_rr(5, 4); a.not_rr(5, 6); a.and_rr(6, 0); a.or_rr(4, 0)
    a.movl_st(0, 2); a.add_imm(4, 1); a.add_imm(4, 2); a.dt(3); a.bf_lbl(tl)
    a.add_rr(9, 9); a.mov_rm_rn(8, 1); a.add_imm(BARS_OFF, 1); a.movw_st(9, 1)
    a.movl_ld_inc(15, 7); a.ldc_sr(7)
    msg_bars("Doubled: "); far_bra(m, 'sq_dx')
    m.pool_jump()

    # ===== HALF =====
    a.deflabel(half)
    a.mov_imm(2, 0); a.cmpge(0, 9); h2 = m.newlabel('h2'); m.bt_far(h2)
    pg.emit_message(m, "Only 1 bar"); far_bra(m, 'sq_dx')
    a.deflabel(h2)
    a.shlr(9)                                                                          # r9 = n/2
    a.mov_rm_rn(9, 0); a.shll2(0); a.add_rr(8, 0); a.add_imm(BART_OFF, 0); a.movl_ld(0, 14); m.imm32(0xFFFFFF, 1); a.and_rr(1, 14)   # r14 = nuova fine
    a.bsr_lbl('sq_nev'); a.nop()                                                       # r12 = &fine
    m.imm32(EV_OFF, 13); a.add_rr(8, 13)                                               # r13 = primo evento con tick >= r14
    fl = m.newlabel('fl'); fx = m.newlabel('fx'); a.deflabel(fl)
    a.cmpeq(12, 13); a.bt_lbl(fx); a.movl_ld(13, 0); a.cmphs(14, 0); a.bt_lbl(fx); a.add_imm(16, 13); a.bra_lbl(fl); a.nop()
    a.deflabel(fx)
    a.mov_rm_rn(12, 11); a.sub(13, 11)                                                 # r11 = k byte da togliere
    a.bsr_lbl('sq_end'); a.nop()                                                       # r10 = fine arena (c'e': la sequenza e' dentro)
    a.stc_sr(7); a.mov_rm_rn(7, 0); a.or_imm(0xF0); a.ldc_sr(0); a.movl_st_dec(7, 15)
    a.tst_rr(11, 11); hk = m.newlabel('hk'); m.bt_far(hk)
    a.mov_rm_rn(13, 2); a.mov_rm_rn(13, 1); a.add_rr(11, 1); a.mov_rm_rn(10, 3); a.add_imm(4, 3)
    rl = m.newlabel('rl'); rx = m.newlabel('rx'); a.deflabel(rl)
    a.cmphs(3, 1); a.bt_lbl(rx); a.movl_ld(1, 0); a.movl_st(0, 2); a.add_imm(4, 1); a.add_imm(4, 2); a.bra_lbl(rl); a.nop()
    a.deflabel(rx)
    a.movl_ld(8, 0); a.sub(11, 0); a.movl_st(0, 8)
    m.imm32(PLAYPTR, 1); a.movl_ld(1, 0); a.cmphi(13, 0); pq = m.newlabel('pq'); a.bf_lbl(pq)        # dopo l'inizio del taglio
    a.mov_rm_rn(13, 2); a.add_rr(11, 2); a.cmphs(2, 0); pin = m.newlabel('pin'); a.bf_lbl(pin); a.sub(11, 0); a.bra_lbl('__pst'); a.nop()
    a.deflabel(pin); a.mov_rm_rn(13, 0)                                                # era dentro il pezzo tolto: sulla fine
    a.deflabel('__pst'); a.movl_st(0, 1)
    a.deflabel(pq)
    a.deflabel(hk)
    a.mov_rm_rn(8, 1); a.add_imm(BARS_OFF, 1); a.movw_st(9, 1)
    a.movl_ld_inc(15, 7); a.ldc_sr(7)
    msg_bars("Halved: "); far_bra(m, 'sq_dx')
    m.pool_jump()
    a.deflabel('sq_dx')
pg.on_doit(doit)


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'SEQTOOLS.WAV'); pg.build_wav(out)
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE, VBOX_OFF, VSLOT, V_PAY, V_LEN
    from pages import SET_FOCUS
    from shasm import SHAsm
    MSG = 0x88E10000
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s_ = SHAsm(addr); fn(s_); blob = s_.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    for ad in (OS_PUSH_PAGE, ENABLE_DOIT, SET_FOCUS): stub(ad, lambda s_: (s_.rts(), s_.nop()))
    from testseq import make_seq
    seq = make_seq()                                          # sequenza di prova generata (8 battute)
    P = ARENA_LO; blk2 = struct.pack('<I', 64) + bytes(range(60))
    def load_arena(sq):
        arena = sq + blk2 + bytes(4)
        for i in range(0, len(arena), 4): h.ram_w32(P + i, struct.unpack('<I', arena[i:i+4])[0])
        return len(arena)
    n_ar = load_arena(seq); h.ram_w32(CURSEQ, P); PLAY0 = P + EV_OFF + 16 * 100; h.ram_w32(PLAYPTR, PLAY0)
    h.load_hub(); h.load_wav(out); h.fire(3); h.ram_w32(EVQ + 0x68, pg.page); h.fire(3)
    def hcall(type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG + 4, val & 0xffffffff); h.ram_w32(MSG + 8, 0); h.ram_w32(MSG + 12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = pg.page; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 50_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d" % i
        return e.r[0]
    msg = lambda: bytes(e.rd(pg.msgt + i, 1) for i in range(16)).split(b'\0')[0].decode()
    txt = lambda: bytes(e.rd(pg.dlabels[BARL] + i, 1) for i in range(6)).decode()
    def doit_(op): e.wr(OP, op, 1); assert hcall(2, 0x2E) == 1; h.fire(1); return msg().rstrip()   # (messaggi fissi: 16 car. con spazi)
    def rd(n): return bytes(e.rd(P + i, 1) for i in range(n))
    def events(base=P):
        out = []; o = base + EV_OFF
        while True:
            b = rd_at(o); t = struct.unpack('<I', b[:4])[0]
            if t == END: return out
            out.append(b); o += 16
    rd_at = lambda o: bytes(e.rd(o + i, 1) for i in range(16))
    bars = lambda: h.ram_r16(P + BARS_OFF) if hasattr(h, 'ram_r16') else e.rd(P + BARS_OFF, 2)
    tab = lambda i: h.ram_r32(P + BART_OFF + 4 * i)
    ev0 = events(); n0 = len(ev0); L = tab(8) & 0xFFFFFF
    assert bars() == 8 and txt() == '  8BAR', txt()
    # DOUBLE: 8 -> 16 battute, eventi copiati + L, blocco dopo intatto, puntatore spostato solo se dopo la fine
    r = doit_(OP_DOUBLE); assert r == 'Doubled: 16 bars', r
    ev = events(); assert bars() == 16 and len(ev) == 2 * n0
    for i in range(n0):
        assert ev[i] == ev0[i] and ev[n0 + i] == struct.pack('<I', struct.unpack('<I', ev0[i][:4])[0] + L) + ev0[i][4:], i
    assert [t for t in (struct.unpack('<I', x[:4])[0] for x in ev)] == sorted(struct.unpack('<I', x[:4])[0] for x in ev)
    sz = h.ram_r32(P); assert sz == len(seq) + 16 * n0 and rd_at(P + sz)[:16] == blk2[:16] and bytes(e.rd(P + sz + i, 1) for i in range(64)) == blk2
    assert h.ram_r32(P + sz + 64) == 0 and h.ram_r32(PLAYPTR) == PLAY0
    assert all((tab(i) & 0xFFFFFF) == 384 * i and tab(i) >> 24 == 0x60 for i in range(17)) and txt() == ' 16BAR', txt()
    print("  ok DOUBLE: 8 -> 16 battute, %d -> %d eventi (copie a +%d tick, in ordine), blocco dopo intatto, tabella 4/4" % (n0, 2 * n0, L))
    # HALF: torna identica all'originale (arena byte per byte)
    r = doit_(OP_HALF); assert r == 'Halved: 8 bars', r
    assert rd(n_ar) == seq + blk2 + bytes(4) and h.ram_r32(PLAYPTR) == PLAY0
    print("  ok HALF dopo DOUBLE: sequenza identica byte per byte all'originale")
    # HALF su 8: 4 battute, eventi oltre la fine tolti, puntatore dentro il pezzo tolto -> sulla nuova fine
    PLAYIN = P + EV_OFF + 16 * (n0 - 3); h.ram_w32(PLAYPTR, PLAYIN)
    r = doit_(OP_HALF); assert r == 'Halved: 4 bars', r
    ev = events(); L4 = tab(4) & 0xFFFFFF; keep = [x for x in ev0 if struct.unpack('<I', x[:4])[0] < L4]
    assert bars() == 4 and ev == keep and h.ram_r32(P) == len(seq) - 16 * (n0 - len(keep))
    assert bytes(e.rd(P + h.ram_r32(P) + i, 1) for i in range(64)) == blk2
    assert h.ram_r32(PLAYPTR) == P + EV_OFF + 16 * len(keep), "puntatore sulla nuova fine"
    print("  ok HALF 8 -> 4: %d eventi tenuti, %d tolti, blocco dopo spostato giu' intatto, puntatore sulla fine" % (len(keep), n0 - len(keep)))
    # 3 battute -> 1 (n/2 per difetto); 1 battuta -> HALF rifiutato
    e.wr(P + BARS_OFF, 3, 1); assert doit_(OP_HALF) == 'Halved: 1 bars'
    r = doit_(OP_HALF); assert r.rstrip() == 'Only 1 bar' and bars() == 1, (r, bars())
    # troppe battute, nessuna sequenza
    e.wr(P + BARS_OFF, 0xF4, 1); e.wr(P + BARS_OFF + 1, 1, 1); assert doit_(OP_DOUBLE) == 'Too many bars'   # 500
    h.ram_w32(CURSEQ, 0); assert doit_(OP_DOUBLE) == 'No sequence'; h.fire(1); assert txt() == 'NO SEQ'
    print("  ok 3 -> 1 battuta, HALF su 1 rifiutato, 500 battute -> 'Too many bars', nessuna sequenza")
    # vista 9: battute, eventi
    h.ram_w32(CURSEQ, P); e.wr(P + BARS_OFF, 1, 1); e.wr(P + BARS_OFF + 1, 0, 1); h.fire(1)
    VS = h.ram_r32(0x8848ED34) + VBOX_OFF + VIEW_SEQ * VSLOT
    assert h.ram_r32(VS + V_LEN) == 4 and struct.unpack('<HH', bytes(e.rd(VS + V_PAY + i, 1) for i in range(4))) == (1, len(events()))
    print("  ok vista SEQ (9): battute e numero di eventi")
    print("VALIDATO: Seq Tools -> %s" % os.path.basename(out))
