#!/usr/bin/env python3
# SCALE PADS (pagina nativa, MODE -> OTHER -> "Scale Pads") -> SCALEPAD.WAV (2026-10-16, issue #8)
#   "12 Levels in scala": un suono su tutti i 12 pad di un banco, accordati su una scala invece che cromatici.
#   riga 0: "SCALE PADS" (e messaggi)      riga 1: "MINOR   C  >B" = scala, tonalita', banco di destinazione
#   DO IT -> "Hit source pad" -> pad -> "A01>bank B DOIT?" -> DO IT -> i 12 pad del banco = copia del blocco pad della
#   sorgente (sample, inviluppi, filtro, mixer...), tune di ogni layer = tune della sorgente + nota della scala.
#   Pad 1 = tonica, poi i gradi della scala in salita (oltre l'ottava si riparte un'ottava sopra).
#   Tonalita': il sample della sorgente e' considerato in DO; C#..F# = +1..+6 semitoni, G..B = -5..-1 (stiramento minimo).
#   Tune clampato a +-3600 cent (limite dell'OS). Le note MIDI dei pad non cambiano. CANCEL annulla.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ, LOAD_SCRATCH
from view import View, VIEW_SCALE

PROG_BASE, PROG_IDX, PROG_STRIDE, NPADS = 0x8837B5AC, 0x8847C0E2, 0x1F98, 48
PAD_OFF, PAD_SIZE = 0x18, 0xA4
LAYER, NLAYERS, TUNE_OFF, TUNE_MAX = 0x18, 4, 0x14, 3600
BANKOBJ = 0x8849C98C
EV_PAD, LEAVE_FIRES = 0x38, 16
SHOWMSG = LOAD_SCRATCH + 0x1C
ST_IDLE, ST_SRC, ST_CONF = range(3)

SCALES = [("MAJOR",   [0, 2, 4, 5, 7, 9, 11]),
          ("MINOR",   [0, 2, 3, 5, 7, 8, 10]),
          ("DORIAN",  [0, 2, 3, 5, 7, 9, 10]),
          ("PHRYG",   [0, 1, 3, 5, 7, 8, 10]),
          ("LYDIAN",  [0, 2, 4, 6, 7, 9, 11]),
          ("MIXOLYD", [0, 2, 4, 5, 7, 9, 10]),
          ("HARMMIN", [0, 2, 3, 5, 7, 8, 11]),
          ("PENTMAJ", [0, 2, 4, 7, 9]),
          ("PENTMIN", [0, 3, 5, 7, 10]),
          ("BLUES",   [0, 3, 5, 6, 7, 10]),
          ("CHROMA",  list(range(12)))]
KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
def semis(scale, i):
    """semitoni dalla tonica del pad i (0..11) nella scala"""
    iv = SCALES[scale][1]; return iv[i % len(iv)] + 12 * (i // len(iv))
def keyoff(k): return k if k <= 6 else k - 12

pg = PageAddon("scalpad1", open_press=None)
pg.other_menu("Scale Pads")
pg.msg_fires = 0xC00
pg.keep_focus = True
pg.far_hend = True
pg.label("SCALE PADS", 0, 0)
SCALE = pg.choice(None, 0, 1, [n for n, _ in SCALES], default=1)
KEY = pg.choice(" ", 7, 1, KEYS, default=0)
BANK = pg.choice(" >", 10, 1, ["A", "B", "C", "D"], default=1)
pg.message()
ACTIVE, OUTCNT, STEP, P1 = [pg.alloc_slot() for _ in range(4)]
TMP = [pg.alloc_slot() for _ in range(PAD_SIZE // 4)][0]      # copia del blocco della sorgente (puo' stare nel banco)
VBUF = [pg.alloc_slot() for _ in range(5)][0]                 # vista 3 SCALE (17 byte)
for _s in range(VBUF, VBUF + 20, 4): del pg.defaults[_s]
view = View(VIEW_SCALE, ver=1)
# vista 3 SCALE v1: scala, tonalita', banco, stato (0 fermo, 1 aspetta il pad, 2 conferma), pad sorgente (0xFF = nessuno),
#   12 semitoni s8 (pad 1..12 del banco, tonalita' compresa) -> il telefono disegna la tastiera con le note della scala


def far_call(m, label):
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def persist(m):
    m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)

def fire(m):
    a = m.a
    sti(m, 0, OUTCNT); sti(m, 1, ACTIVE)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
    far_call(m, 'sp_vpub')
pg.on_fire(fire)

def inactive(m):
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    sti(m, 0, ACTIVE); sti(m, 0, OUTCNT); sti(m, ST_IDLE, STEP)
    a.deflabel(ix)
    far_call(m, 'sp_vpub')
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """in attesa della sorgente il pad colpito e' l'argomento; altrimenti lo suona l'OS"""
    a = m.a
    ld(m, STEP, 0); a.tst_rr(0, 0); sel = m.newlabel('sel'); m.bf_far(sel)
    far_bra(m, hret0)
    a.deflabel(sel)
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on)
    far_bra(m, hret1)
    a.deflabel(on)
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(9, 0)
    a.mov_rm_rn(0, 4); far_call(m, 'onpad'); far_bra(m, hret1)
pg.on_event(EV_PAD, ev_pad)

def on_cancel(m, hret1):
    a = m.a
    ld(m, STEP, 0); a.tst_rr(0, 0); cx = m.newlabel('cx'); m.bt_far(cx)
    sti(m, ST_IDLE, STEP); sti(m, 1, SHOWMSG); far_bra(m, hret1)
    a.deflabel(cx)
pg.cancel_fn = on_cancel


def doit(m):
    a = m.a
    MT = pg.addr['msgt']

    def text(s_):
        for ch in s_: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    def padname(r_pad):                 # "A01" a r12; usa r0-r5
        a.mov_rm_rn(r_pad, 4); a.mov_imm(12, 5); pg._hcall(m, '__udiv')
        a.add_imm(ord('A'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_rm_rn(1, 4); a.add_imm(1, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
        a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(0x30, 1); a.movb_st(1, 12); a.add_imm(1, 12)
    def msg(parts, keep):               # parts: stringhe, ('pad', slot), ('bank',)
        m.imm32(MT, 12)
        for p_ in parts:
            if isinstance(p_, str): text(p_)
            elif p_[0] == 'pad': ld(m, p_[1], 9); padname(9)
            else: m.imm32(BANK, 1); a.movb_ld(1, 0); a.add_imm(ord('A'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_imm(0, 0); a.movb_st(0, 12); pg._hcall(m, '__showmsg')
        if keep: persist(m)
    def branch_eq(r, val, lbl):
        nx = m.newlabel('be'); a.mov_imm(val, 3); a.cmpeq(3, r); a.bf_lbl(nx); far_bra(m, lbl); a.deflabel(nx)

    # ---- DO IT ----
    ld(m, STEP, 8)
    branch_eq(8, ST_IDLE, '__start')
    branch_eq(8, ST_CONF, '__go')
    far_bra(m, '__dx')                                                                # (aspetta un pad)
    m.pool_jump()
    a.deflabel('__start'); sti(m, ST_SRC, STEP); msg(["Hit source pad"], True); far_bra(m, '__dx')
    a.deflabel('__go'); far_call(m, 'doscale'); sti(m, ST_IDLE, STEP)
    msg([('pad', P1), ">bank ", ('bank',), ": done"], False)
    a.deflabel('__dx'); far_bra(m, 'spret')
    m.pool_jump()

    # ---- onpad: r4 = pad 0..47 -> sorgente, chiede conferma ----
    a.deflabel('onpad'); a.stsl_pr()
    st(m, 4, P1); sti(m, ST_CONF, STEP)
    msg([('pad', P1), ">bank ", ('bank',), " DOIT?"], True)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- blk: r0 = pad -> r0 = blocco pad nel program corrente (usa r1-r3) ----
    a.deflabel('blk')
    m.imm32(PAD_SIZE, 3); a.mul_l(3, 0); a.sts_macl(1)
    m.imm32(PROG_IDX, 3); a.movw_ld(3, 2); a.extu_w(2, 2); m.imm32(PROG_STRIDE, 3); a.mul_l(3, 2); a.sts_macl(2)
    m.imm32(PROG_BASE + PAD_OFF, 3); a.add_rr(3, 2); a.add_rr(1, 2); a.mov_rm_rn(2, 0)
    a.rts(); a.nop()
    m.pool_jump()

    # ---- doscale: 12 pad del banco BANK = sorgente P1 accordata sulla scala ----
    a.deflabel('doscale'); a.stsl_pr()
    for r in (8, 9, 10, 11, 12, 13, 14): a.movl_st_dec(r, 15)
    ld(m, P1, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 11)                       # sorgente -> TMP
    m.imm32(TMP, 12); a.mov_imm(PAD_SIZE // 4, 13)
    c0 = m.newlabel('c0'); a.deflabel(c0); a.movl_ld(11, 0); a.movl_st(0, 12); a.add_imm(4, 11); a.add_imm(4, 12); a.dt(13); a.bf_lbl(c0)
    m.imm32(SCALE, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0)
    m.imm32('scaletab', 9); a.add_rr(0, 9)                                             # r9 = riga della scala (12 byte)
    m.imm32(KEY, 1); a.movb_ld(1, 10); a.extu_b(10, 10)                                 # r10 = spostamento della tonalita'
    a.mov_imm(6, 0); a.cmpgt(0, 10); kk = m.newlabel('kk'); a.bf_lbl(kk); a.add_imm(-12, 10); a.deflabel(kk)
    m.imm32(BANK, 1); a.movb_ld(1, 8); a.extu_b(8, 8); a.mov_imm(12, 2); a.mul_l(2, 8); a.sts_macl(8)   # r8 = primo pad
    a.mov_imm(12, 14)
    m.pool_jump()
    pl = m.newlabel('pl'); a.deflabel(pl)
    a.mov_rm_rn(8, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 12)                   # r12 = blocco destinazione
    m.imm32(TMP, 11); a.mov_rm_rn(12, 1); a.mov_imm(PAD_SIZE // 4, 13)
    c1 = m.newlabel('c1'); a.deflabel(c1); a.movl_ld(11, 0); a.movl_st(0, 1); a.add_imm(4, 11); a.add_imm(4, 1); a.dt(13); a.bf_lbl(c1)
    a.movb_ld(9, 6); a.add_rr(10, 6); a.mov_imm(100, 0); a.mul_l(0, 6); a.sts_macl(6)  # r6 = cent
    a.mov_rm_rn(12, 1); a.add_imm(TUNE_OFF, 1); a.mov_imm(NLAYERS, 13)
    tl = m.newlabel('tl'); a.deflabel(tl)
    a.movw_ld(1, 0); a.add_rr(6, 0)                                                    # tune s16 + cent
    m.imm32(TUNE_MAX, 2); a.cmpgt(2, 0); t1 = m.newlabel('t1'); a.bf_lbl(t1); a.mov_rm_rn(2, 0); a.deflabel(t1)
    m.imm32(-TUNE_MAX, 2); a.cmpgt(0, 2); t2 = m.newlabel('t2'); a.bf_lbl(t2); a.mov_rm_rn(2, 0); a.deflabel(t2)
    a.movw_st(0, 1); a.add_imm(LAYER, 1); a.dt(13); a.bf_lbl(tl)
    a.add_imm(1, 9); a.add_imm(1, 8); a.dt(14); m.bf_far(pl)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    for r in (14, 13, 12, 11, 10, 9, 8): a.movl_ld_inc(15, r)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    # ---- sp_vpub: vista 3 (r0-r10) ----
    a.deflabel('sp_vpub'); a.stsl_pr()
    m.imm32(VBUF, 8)
    for src in (SCALE, KEY, BANK): m.imm32(src, 1); a.movb_ld(1, 0); a.movb_st(0, 8); a.add_imm(1, 8)
    ld(m, STEP, 0); a.movb_st(0, 8); a.add_imm(1, 8)
    a.mov_imm(-1, 0); ld(m, STEP, 1); a.tst_rr(1, 1); vs = m.newlabel('vs'); a.bt_lbl(vs); ld(m, P1, 0); a.deflabel(vs)
    a.movb_st(0, 8); a.add_imm(1, 8)
    m.imm32(SCALE, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0)
    m.imm32('scaletab', 9); a.add_rr(0, 9)
    m.imm32(KEY, 1); a.movb_ld(1, 10); a.extu_b(10, 10)
    a.mov_imm(6, 0); a.cmpgt(0, 10); vk = m.newlabel('vk'); a.bf_lbl(vk); a.add_imm(-12, 10); a.deflabel(vk)
    a.mov_imm(12, 3)
    vl = m.newlabel('vl'); a.deflabel(vl); a.movb_ld(9, 0); a.add_rr(10, 0); a.movb_st(0, 8); a.add_imm(1, 8); a.add_imm(1, 9); a.dt(3); a.bf_lbl(vl)
    view.publish(m, VBUF, 17)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    view.emit_subs(m)
    # tabella: per ogni scala 12 byte = semitoni dalla tonica dei pad 1..12
    a.deflabel('scaletab')
    tab = bytes(semis(s, i) for s in range(len(SCALES)) for i in range(12))
    for i in range(0, len(tab), 2): a._emit(tab[i] | tab[i + 1] << 8)
    a.deflabel('spret')
pg.on_doit(doit)


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'SCALEPAD.WAV'); pg.build_wav(out)
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE
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
    def call(obj, type_, code=0, val=0):
        h.ram_w32(MSG, (type_ & 0xffff) | (code << 16)); h.ram_w32(MSG+4, val & 0xffffffff); h.ram_w32(MSG+8, 0); h.ram_w32(MSG+12, 0)
        regs = [0x22220000 + i for i in range(16)]; regs[15] = 0x88400000 - 256; regs[4] = obj; regs[5] = MSG
        e.r = regs[:]; e.pr = 0xDEADBEEE; e.pc = pg.handler; e.maxsteps = 20_000_000
        assert e.run(stop_pc=0xDEADBEEE), "handler non ritorna"
        for i in range(8, 16): assert e.r[i] == regs[i], "r%d corrotto" % i
        return e.r[0]
    msg = lambda: bytes(e.rd(pg.msgt + i, 1) for i in range(16)).split(b'\0')[0].decode()
    P = pg.page; DOIT, CANCEL = 0x2E, 0x2D
    def doit_(): assert call(P, 2, code=DOIT) == 1
    def pad(bank, k, vel=100): e.wr(BANKOBJ, bank, 1); r = call(P, EV_PAD, code=k, val=vel); call(P, EV_PAD, code=k, val=0); return r
    h.ram_w16(PROG_IDX, 2); prog = PROG_BASE + 2 * PROG_STRIDE
    blk = lambda p: prog + PAD_OFF + p * PAD_SIZE
    rdb = lambda p: bytes(e.rd(blk(p) + i, 1) for i in range(PAD_SIZE))
    tune = lambda p, l: struct.unpack('<h', bytes(e.rd(blk(p) + l * LAYER + TUNE_OFF + i, 1) for i in range(2)))[0]
    def fill():
        for p in range(NPADS):
            for i in range(PAD_SIZE): e.wr(blk(p) + i, (p * 7 + i) & 0xFF, 1)
    def wtune(p, l, v): e.wr(blk(p) + l * LAYER + TUNE_OFF, v & 0xFF, 1); e.wr(blk(p) + l * LAYER + TUNE_OFF + 1, (v >> 8) & 0xFF, 1)
    def setf(scale, key, bank): e.wr(SCALE, scale, 1); e.wr(KEY, key, 1); e.wr(BANK, bank, 1)
    h.ram_w32(EVQ + 0x68, P); h.fire(3)
    assert pad(0, 0) == 0, "fuori scelta il pad lo suona l'OS"
    def run(src_bank, src_pad, scale, key, bank, tunes=(0, 0, 0, 0)):
        fill(); src = src_bank * 12 + src_pad
        for l, v in enumerate(tunes): wtune(src, l, v)
        srcb = rdb(src); setf(scale, key, bank)
        doit_(); assert msg() == 'Hit source pad', msg()
        pad(src_bank, src_pad); nm = "%s%02d" % ("ABCD"[src_bank], src_pad + 1)
        assert msg() == '%s>bank %s DOIT?' % (nm, "ABCD"[bank]), msg()
        others = {p: rdb(p) for p in range(NPADS) if p // 12 != bank}
        doit_(); assert msg() == '%s>bank %s: done' % (nm, "ABCD"[bank]), msg()
        for i in range(12):
            p = bank * 12 + i; b = rdb(p); exp_cents = (semis(scale, i) + keyoff(key)) * 100
            for l in range(4):
                assert tune(p, l) == max(-TUNE_MAX, min(TUNE_MAX, tunes[l] + exp_cents)), (SCALES[scale][0], i, l, tune(p, l))
                o = l * LAYER + TUNE_OFF; b = b[:o] + srcb[o:o+2] + b[o+2:]
            assert b == srcb, "pad %d: blocco diverso dalla sorgente (a parte il tune)" % p
        assert all(rdb(p) == v for p, v in others.items()), "altri banchi toccati"
        assert h.ram_r32(STEP) == ST_IDLE
    run(0, 0, 1, 0, 1)                                                          # A01 -> banco B, C minor
    print("  ok C MINOR: B01..B12 = A01 con tune", [semis(1, i) * 100 for i in range(12)])
    run(0, 4, 8, 9, 2, tunes=(-200, 300, 0, 3500))                              # A minor pentatonica (A = -3), tune sorgente
    print("  ok A PENTMIN su banco C da A05, tune della sorgente sommato e clampato a +-3600")
    run(1, 3, 10, 6, 1)                                                         # sorgente DENTRO il banco di destinazione
    print("  ok sorgente nel banco di destinazione (copiata prima), CHROMA F#")
    # CANCEL annulla la conferma: niente scritto
    fill(); before = [rdb(p) for p in range(NPADS)]; setf(0, 0, 3)
    doit_(); pad(0, 1); assert msg() == 'A02>bank D DOIT?'
    assert call(P, 2, code=CANCEL) == 1 and h.ram_r32(STEP) == ST_IDLE and [rdb(p) for p in range(NPADS)] == before
    doit_(); pad(0, 1); pad(0, 2); assert msg() == 'A03>bank D DOIT?', "nuovo pad = nuova sorgente"
    assert call(P, 2, code=CANCEL) == 1
    print("  ok CANCEL non scrive nulla; un altro pad cambia la sorgente")
    # vista 3: A PENTMIN (A = -3), banco C
    from mpcmod import VBOX_OFF, VSLOT, V_LEN, V_VER, V_PAY
    setf(8, 9, 2); h.fire(2); VS = h.ram_r32(0x8848ED34) + VBOX_OFF + VIEW_SCALE * VSLOT
    v = bytes(e.rd(VS + V_PAY + i, 1) for i in range(17))
    assert h.ram_r32(VS + V_LEN) == 17 and h.ram_r32(VS + V_VER) == 1 and v[:5] == bytes([8, 9, 2, 0, 0xFF])
    assert [x - 256 if x > 127 else x for x in v[5:]] == [semis(8, i) - 3 for i in range(12)], list(v)
    doit_(); pad(0, 6); h.fire(1); v = bytes(e.rd(VS + V_PAY + i, 1) for i in range(5)); assert v == bytes([8, 9, 2, 2, 6]), v
    assert call(P, 2, code=CANCEL) == 1
    print("  ok vista SCALE: scala/tonalita'/banco, stato e pad sorgente, 12 semitoni (A = -3)")
    print("VALIDATO: Scale Pads -> %s" % os.path.basename(out))
