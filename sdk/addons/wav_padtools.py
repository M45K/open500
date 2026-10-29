#!/usr/bin/env python3
# PAD TOOLS (pagina nativa, SHIFT+STOP o menu OTHER) -> PADTOOLS.WAV: scambia / copia / svuota pad del program corrente
#   riga 0: "PAD TOOLS" (e messaggi)      riga 1: "SWAP  PAD" = operazione + cosa
#     cosa: SAMPLE   = solo i 4 layer sample (nome, livello, range, tune, modo: blocco pad +0x00..+0x5F)
#           PAD      = tutto il blocco pad 0xA4 (sample + proprieta': filtri, inviluppi, mono/mute, mixer...)
#           PAD+NOTE = come PAD e in piu' la nota MIDI (solo SWAP: le sequenze continuano a suonare gli stessi suoni)
#   SWAP : DO IT -> "Hit first pad" -> pad -> "A01: hit 2nd pad" -> pad -> "Swapped A01>C05" (CANCEL al 2o pad = di nuovo
#          "Hit first pad": CANCEL = un passo indietro, 2026-10-24)
#   COPY : DO IT -> "Hit source pad" -> pad -> ogni pad colpito riceve la copia ("A01>C05 copied"); pad di destinazione
#          PIENO -> "C05 full DOIT=ok": DO IT (o lo stesso pad di nuovo) sovrascrive, CANCEL lo salta, un ALTRO pad lo
#          salta e va avanti con quello (issue #15, 2026-10-27: prima, con la domanda aperta, gli altri pad erano
#          ignorati e con destinazioni piene non si potevano scegliere piu' pad). DO IT/CANCEL chiudono ("Copied NN pads").
#          Le note MIDI non si copiano mai (due pad con la stessa nota andrebbero in conflitto).
#   CLEAR: DO IT -> colpisci i pad da svuotare (segnati) -> DO IT -> "Clear NN pads?" -> DO IT: pad ai valori
#          predefiniti dell'OS (FUN_a0010974, nomi azzerati), nota MIDI tenuta. CANCEL sulla domanda = di nuovo a
#          segnare i pad (quelli segnati restano); CANCEL mentre segni = annulla.
#   Pad = banco*12 + pad fisico (banco corrente: byte 0 di 0x8849C98C). Fuori da una scelta i pad suonano il program.
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti, INPUT_OBJ, ENABLE_DOIT
from mpcmod import EVQ, LOAD_SCRATCH

PROG_BASE, PROG_IDX, PROG_STRIDE, NPADS = 0x8837B5AC, 0x8847C0E2, 0x1F98, 48
PAD_OFF, PAD_SIZE, LAYERS_SIZE, NOTE_A, NOTE_B = 0x18, 0xA4, 0x60, 0x1ED8, 0x1F08
PAD_INIT = 0x884B52D0                  # FUN_a0010974(this, r5 = blocco pad): valori predefiniti (ingresso verificato)
BANKOBJ = 0x8849C98C
EV_PAD, LEAVE_FIRES = 0x38, 16
SHOWMSG = LOAD_SCRATCH + 0x1C
OP_SWAP, OP_COPY, OP_CLEAR = range(3)
W_SAMPLE, W_PAD, W_NOTE = range(3)
# STEP: 0 fermo, 1 primo pad (SWAP/COPY), 2 secondo pad (SWAP), 3 destinazioni (COPY), 4 conferma pad pieno (COPY),
#       5 segna pad (CLEAR), 6 conferma CLEAR
ST_IDLE, ST_FIRST, ST_SECOND, ST_DEST, ST_FULL, ST_MARK, ST_CLRQ = range(7)

pg = PageAddon("ptools1")              # SHIFT+STOP (tolto ad Auto Chop) + voce OTHER
pg.other_menu("Pad Tools")
pg.msg_fires = 0xC00
pg.keep_focus = True
pg.far_hend = True
pg.label("PAD TOOLS", 0, 0)
OP = pg.choice(None, 0, 1, ["SWAP", "COPY", "CLEAR"], default=OP_SWAP)
WHAT = pg.choice(" ", 5, 1, ["SAMPLE", "PAD", "PAD+NOTE"], default=W_PAD)
pg.message()
ACTIVE, OUTCNT, STEP, P1, P2, NDONE, MASK0, MASK1 = [pg.alloc_slot() for _ in range(8)]


def far_call(m, label):
    m.a.movl_pool(label, 2); m.a.jsr(2); m.a.nop()

def far_bra(m, label):
    m.a.movl_pool(label, 2); m.a.jmp(2); m.a.nop()

def persist(m):
    """il messaggio appena mostrato resta finche' non cambia lo stato"""
    m.imm32(0x7FFFFFFF, 0); st(m, 0, SHOWMSG)

def fire(m):
    a = m.a
    sti(m, 0, OUTCNT); sti(m, 1, ACTIVE)
    m.imm32(INPUT_OBJ, 4); a.mov_imm(1, 5); m.imm32(ENABLE_DOIT, 2); a.jsr(2); a.nop()
pg.on_fire(fire)

def inactive(m):
    a = m.a
    ld(m, ACTIVE, 0); a.tst_rr(0, 0); ix = m.newlabel('ix'); m.bt_far(ix)
    ld(m, OUTCNT, 0); a.add_imm(1, 0); st(m, 0, OUTCNT); a.mov_imm(LEAVE_FIRES, 3); a.cmpge(3, 0); m.bf_far(ix)
    sti(m, 0, ACTIVE); sti(m, 0, OUTCNT); sti(m, ST_IDLE, STEP)
    a.deflabel(ix)
pg.on_inactive(inactive)

def ev_pad(m, hret0, hret1):
    """in una scelta il pad colpito e' un argomento; altrimenti lo suona l'OS (program)"""
    a = m.a
    ld(m, STEP, 0); a.tst_rr(0, 0); sel = m.newlabel('sel'); m.bf_far(sel)
    far_bra(m, hret0)
    a.deflabel(sel)
    a.movl_ld_disp(8, 4, 0); a.tst_rr(0, 0); on = m.newlabel('on'); a.bf_lbl(on)
    far_bra(m, hret1)                                                                  # rilascio: consumato
    a.deflabel(on)
    m.imm32(2, 1); a.add_rr(8, 1); a.movw_ld(1, 9); a.extu_w(9, 9)
    m.imm32(BANKOBJ, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.mov_imm(12, 2); a.mul_l(2, 0); a.sts_macl(0); a.add_rr(9, 0)
    a.mov_rm_rn(0, 4); far_call(m, 'onpad'); far_bra(m, hret1)
pg.on_event(EV_PAD, ev_pad)

def on_cancel(m, hret1):
    a = m.a
    ld(m, STEP, 0); a.tst_rr(0, 0); cx = m.newlabel('cx'); m.bt_far(cx)
    far_call(m, 'oncancel'); far_bra(m, hret1)
    a.deflabel(cx)
pg.cancel_fn = on_cancel


def doit(m):
    a = m.a
    MT = pg.addr['msgt']

    def text(s_):                       # asm: testo a r12 (r12 avanza)
        for ch in s_: a.mov_imm(ord(ch), 0); a.movb_st(0, 12); a.add_imm(1, 12)
    def endmsg(keep):                   # chiude il testo e lo mostra
        a.mov_imm(0, 0); a.movb_st(0, 12); pg._hcall(m, '__showmsg')
        if keep: persist(m)
    def padname(r_pad):                 # asm: "A01" per il pad r_pad a r12; usa r0-r5
        a.mov_rm_rn(r_pad, 4); a.mov_imm(12, 5); pg._hcall(m, '__udiv')
        a.add_imm(ord('A'), 0); a.movb_st(0, 12); a.add_imm(1, 12)
        a.mov_rm_rn(1, 4); a.add_imm(1, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
        a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(0x30, 1); a.movb_st(1, 12); a.add_imm(1, 12)
    def dec2(slot):                     # asm: 2 cifre di *slot a r12
        ld(m, slot, 4); a.mov_imm(10, 5); pg._hcall(m, '__udiv')
        a.add_imm(0x30, 0); a.movb_st(0, 12); a.add_imm(1, 12); a.add_imm(0x30, 1); a.movb_st(1, 12); a.add_imm(1, 12)
    def msg(parts, keep):               # parts: stringhe o ('pad', slot) o ('n', slot)
        m.imm32(MT, 12)
        for p_ in parts:
            if isinstance(p_, str): text(p_)
            elif p_[0] == 'pad': ld(m, p_[1], 9); padname(9)
            else: dec2(p_[1])
        endmsg(keep)
    def op(r):                          # r = OP (0..2)
        m.imm32(OP, 1); a.movb_ld(1, r); a.extu_b(r, r)
    def what(r):
        m.imm32(WHAT, 1); a.movb_ld(1, r); a.extu_b(r, r)
    def branch_eq(r, val, lbl):         # if r == val: bra_far lbl
        nx = m.newlabel('be'); a.mov_imm(val, 3); a.cmpeq(3, r); a.bf_lbl(nx); far_bra(m, lbl); a.deflabel(nx)

    # ---- DO IT ----
    ld(m, STEP, 8)
    branch_eq(8, ST_IDLE, '__start')
    branch_eq(8, ST_DEST, '__cpend')
    branch_eq(8, ST_FULL, '__cpyes')
    branch_eq(8, ST_MARK, '__clrq')
    branch_eq(8, ST_CLRQ, '__clrdo')
    far_bra(m, '__dx')                                                                # (1/2: aspetta un pad)
    m.pool_jump()
    a.deflabel('__start'); sti(m, 0, NDONE); sti(m, 0, MASK0); sti(m, 0, MASK1)
    op(8)
    branch_eq(8, OP_CLEAR, '__stclr')
    sti(m, ST_FIRST, STEP)
    branch_eq(8, OP_COPY, '__stcpy')
    msg(["Hit first pad"], True); far_bra(m, '__dx')
    a.deflabel('__stcpy'); msg(["Hit source pad"], True); far_bra(m, '__dx')
    a.deflabel('__stclr'); sti(m, ST_MARK, STEP); msg(["Hit pads to clr"], True); far_bra(m, '__dx')
    m.pool_jump()
    a.deflabel('__cpend')                                                             # fine COPY
    sti(m, ST_IDLE, STEP); msg(["Copied ", ('n', NDONE), " pads"], False); far_bra(m, '__dx')
    a.deflabel('__cpyes')                                                             # pad pieno: sovrascrivi
    a.bsr_lbl('docopy'); a.nop(); sti(m, ST_DEST, STEP); far_bra(m, '__dx')
    a.deflabel('__clrq')                                                              # CLEAR: domanda
    ld(m, NDONE, 0); a.tst_rr(0, 0); cq = m.newlabel('cq'); a.bf_lbl(cq)
    sti(m, ST_IDLE, STEP); msg(["No pads marked"], False); far_bra(m, '__dx')
    a.deflabel(cq); sti(m, ST_CLRQ, STEP); msg(["Clear ", ('n', NDONE), " pads?"], True); far_bra(m, '__dx')
    a.deflabel('__clrdo'); a.bsr_lbl('doclear'); a.nop(); sti(m, ST_IDLE, STEP)
    msg(["Cleared ", ('n', NDONE), " pads"], False)
    a.deflabel('__dx'); far_bra(m, 'ptret')
    m.pool_jump()

    # ---- oncancel ----
    a.deflabel('oncancel'); a.stsl_pr()
    ld(m, STEP, 8)
    nf = m.newlabel('nf'); a.mov_imm(ST_FULL, 3); a.cmpeq(3, 8); a.bf_lbl(nf)
    sti(m, ST_DEST, STEP); msg(["Skipped ", ('pad', P2)], False); a.bra_lbl('__ocx'); a.nop()          # salta il pad pieno
    a.deflabel(nf)
    nd = m.newlabel('nd'); a.mov_imm(ST_DEST, 3); a.cmpeq(3, 8); a.bf_lbl(nd)
    sti(m, ST_IDLE, STEP); msg(["Copied ", ('n', NDONE), " pads"], False); a.bra_lbl('__ocx'); a.nop()
    a.deflabel(nd)
    n2 = m.newlabel('n2'); a.mov_imm(ST_SECOND, 3); a.cmpeq(3, 8); a.bf_lbl(n2)                          # SWAP: di nuovo il 1o pad
    sti(m, ST_FIRST, STEP); msg(["Hit first pad"], True); a.bra_lbl('__ocx'); a.nop()
    a.deflabel(n2)
    nq = m.newlabel('nq'); a.mov_imm(ST_CLRQ, 3); a.cmpeq(3, 8); a.bf_lbl(nq)                            # CLEAR: di nuovo a segnare
    sti(m, ST_MARK, STEP); msg(["Hit pads to clr"], True); a.bra_lbl('__ocx'); a.nop()
    a.deflabel(nq); sti(m, ST_IDLE, STEP); sti(m, 1, SHOWMSG)
    a.deflabel('__ocx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- onpad: r4 = pad 0..47 ----
    a.deflabel('onpad'); a.stsl_pr()
    ld(m, STEP, 8)
    branch_eq(8, ST_FIRST, '__p1')
    branch_eq(8, ST_SECOND, '__p2')
    branch_eq(8, ST_DEST, '__pd')
    branch_eq(8, ST_MARK, '__pm')
    branch_eq(8, ST_FULL, '__pf')
    far_bra(m, '__px')
    m.pool_jump()
    a.deflabel('__p1'); st(m, 4, P1)
    op(8); nc = m.newlabel('nc'); a.mov_imm(OP_COPY, 3); a.cmpeq(3, 8); a.bf_lbl(nc)
    sti(m, ST_DEST, STEP); msg([("pad", P1), " > hit pads"], True); far_bra(m, '__px')
    a.deflabel(nc); sti(m, ST_SECOND, STEP); msg([('pad', P1), ": hit 2nd pad"], True); far_bra(m, '__px')
    m.pool_jump()
    a.deflabel('__p2'); st(m, 4, P2); sti(m, ST_IDLE, STEP)
    ld(m, P1, 0); a.cmpeq(4, 0); ns = m.newlabel('ns'); a.bf_lbl(ns); msg(["Same pad"], False); far_bra(m, '__px')
    a.deflabel(ns); a.bsr_lbl('doswap'); a.nop()
    msg(["Swapped ", ('pad', P1), ">", ('pad', P2)], False); far_bra(m, '__px')
    m.pool_jump()
    a.deflabel('__pd'); st(m, 4, P2)
    ld(m, P1, 0); a.cmpeq(4, 0); m.bt_far('__px')                                     # la sorgente stessa: niente
    a.mov_rm_rn(4, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 11)                  # r11 = blocco destinazione
    a.mov_imm(4, 13); fu = m.newlabel('fu'); fl = m.newlabel('fl'); a.deflabel(fl)    # pieno se un layer ha un nome
    a.movb_ld(11, 0); a.tst_rr(0, 0); a.bf_lbl(fu); a.add_imm(0x18, 11); a.dt(13); a.bf_lbl(fl)
    a.bsr_lbl('docopy'); a.nop(); far_bra(m, '__px')
    a.deflabel(fu); sti(m, ST_FULL, STEP); msg([('pad', P2), " full DOIT=ok"], True); far_bra(m, '__px')
    m.pool_jump()
    a.deflabel('__pf')                                                                # COPY, pad pieno in attesa:
    ld(m, P2, 0); a.cmpeq(4, 0); pfo = m.newlabel('pfo'); a.bf_lbl(pfo)
    a.bsr_lbl('docopy'); a.nop(); sti(m, ST_DEST, STEP); far_bra(m, '__px')           # lo stesso di nuovo = sovrascrivi
    a.deflabel(pfo); sti(m, ST_DEST, STEP); far_bra(m, '__pd')                        # un altro: quello e' saltato
    m.pool_jump()
    a.deflabel('__pm')                                                                # CLEAR: segna il pad
    a.mov_imm(1, 5); a.mov_rm_rn(4, 0); a.mov_imm(31, 3); a.cmpgt(3, 0); mk1 = m.newlabel('mk1'); a.bt_lbl(mk1)
    a.shad(0, 5); ld(m, MASK0, 0); a.mov_rm_rn(0, 6); a.bra_lbl('__mk'); a.nop()
    a.deflabel(mk1); a.add_imm(-32, 0); a.shad(0, 5); ld(m, MASK1, 6)
    a.deflabel('__mk')
    a.mov_rm_rn(6, 0); a.and_rr(5, 0); a.tst_rr(0, 0); m.bf_far('__px')               # gia' segnato
    a.or_rr(5, 6); a.mov_imm(31, 3); a.cmpgt(3, 4); mk2 = m.newlabel('mk2'); a.bt_lbl(mk2)
    st(m, 6, MASK0); a.bra_lbl('__mk2'); a.nop()
    a.deflabel(mk2); st(m, 6, MASK1)
    a.deflabel('__mk2'); st(m, 4, P2); ld(m, NDONE, 0); a.add_imm(1, 0); st(m, 0, NDONE)
    msg([('pad', P2), " marked (", ('n', NDONE), ")"], True)
    a.deflabel('__px'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- blk: r0 = pad -> r0 = blocco pad nel program corrente (usa r1-r3) ----
    a.deflabel('blk')
    m.imm32(PAD_SIZE, 3); a.mul_l(3, 0); a.sts_macl(1)
    m.imm32(PROG_IDX, 3); a.movw_ld(3, 2); a.extu_w(2, 2); m.imm32(PROG_STRIDE, 3); a.mul_l(3, 2); a.sts_macl(2)
    m.imm32(PROG_BASE + PAD_OFF, 3); a.add_rr(3, 2); a.add_rr(1, 2); a.mov_rm_rn(2, 0)
    a.rts(); a.nop()
    m.pool_jump()

    # ---- nbytes: r13 = parole da trattare per WHAT (SAMPLE: 0x60, altrimenti 0xA4) ----
    a.deflabel('nbytes')
    m.imm32(WHAT, 1); a.movb_ld(1, 0); a.extu_b(0, 0); a.tst_rr(0, 0); a.mov_imm(PAD_SIZE // 4, 13)
    nb = m.newlabel('nb'); a.bf_lbl(nb); a.mov_imm(LAYERS_SIZE // 4, 13); a.deflabel(nb)
    a.rts(); a.nop()

    # ---- doswap: P1 <-> P2 (+ note se PAD+NOTE) ----
    a.deflabel('doswap'); a.stsl_pr()
    ld(m, P1, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 11)
    ld(m, P2, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 12)
    a.bsr_lbl('nbytes'); a.nop()
    sl = m.newlabel('sl'); a.deflabel(sl)
    a.movl_ld(11, 0); a.movl_ld(12, 1); a.movl_st(1, 11); a.movl_st(0, 12); a.add_imm(4, 11); a.add_imm(4, 12); a.dt(13); a.bf_lbl(sl)
    what(0); a.mov_imm(W_NOTE, 3); a.cmpeq(3, 0); nn = m.newlabel('nn'); m.bf_far(nn)
    m.imm32(PROG_IDX, 3); a.movw_ld(3, 8); a.extu_w(8, 8); m.imm32(PROG_STRIDE, 3); a.mul_l(3, 8); a.sts_macl(8); m.imm32(PROG_BASE, 3); a.add_rr(3, 8)
    ld(m, P1, 9); ld(m, P2, 10)
    m.imm32(NOTE_A, 3); a.mov_rm_rn(8, 11); a.add_rr(3, 11)
    a.mov_rm_rn(11, 1); a.add_rr(9, 1); a.movb_ld(1, 4); a.extu_b(4, 4)
    a.mov_rm_rn(11, 2); a.add_rr(10, 2); a.movb_ld(2, 5); a.extu_b(5, 5)
    a.movb_st(5, 1); a.movb_st(4, 2)
    m.imm32(NOTE_B, 3); a.mov_rm_rn(8, 12); a.add_rr(3, 12); a.mov_imm(0x7F, 3)
    for r_n, r_p in ((5, 9), (4, 10)):
        sk = m.newlabel('sk'); a.mov_rm_rn(r_n, 0); a.cmphs(0, 3); a.bf_lbl(sk)
        a.mov_rm_rn(12, 1); a.add_rr(0, 1); a.movb_st(r_p, 1); a.deflabel(sk)
    a.deflabel(nn); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- docopy: P1 -> P2 (SAMPLE o PAD; mai le note), messaggio ----
    a.deflabel('docopy'); a.stsl_pr()
    ld(m, P1, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 11)
    ld(m, P2, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 12)
    a.bsr_lbl('nbytes'); a.nop()
    cl = m.newlabel('cl'); a.deflabel(cl)
    a.movl_ld(11, 0); a.movl_st(0, 12); a.add_imm(4, 11); a.add_imm(4, 12); a.dt(13); a.bf_lbl(cl)
    ld(m, NDONE, 0); a.add_imm(1, 0); st(m, 0, NDONE)
    msg([('pad', P1), ">", ('pad', P2), " copied"], True)
    m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()

    # ---- doclear: pad segnati -> valori predefiniti dell'OS, nomi azzerati; nota tenuta ----
    a.deflabel('doclear'); a.stsl_pr()
    a.mov_imm(0, 9)
    ql = m.newlabel('ql'); qn = m.newlabel('qn'); qx = m.newlabel('qx'); a.deflabel(ql)
    a.mov_imm(NPADS, 0); a.cmpge(0, 9); m.bt_far(qx)
    a.mov_imm(1, 5); a.mov_rm_rn(9, 0); a.mov_imm(31, 3); a.cmpgt(3, 0); q1 = m.newlabel('q1'); a.bt_lbl(q1)
    a.shad(0, 5); ld(m, MASK0, 6); a.bra_lbl('__qm'); a.nop()
    a.deflabel(q1); a.add_imm(-32, 0); a.shad(0, 5); ld(m, MASK1, 6)
    a.deflabel('__qm'); a.and_rr(5, 6); a.tst_rr(6, 6); m.bt_far(qn)
    a.mov_rm_rn(9, 0); a.bsr_lbl('blk'); a.nop(); a.mov_rm_rn(0, 10)
    a.mov_rm_rn(10, 5); m.imm32(PAD_INIT, 2); a.jsr(2); a.nop()                      # OS: pad ai default (r5 = blocco)
    a.mov_imm(4, 13); a.mov_rm_rn(10, 11)                                              # nomi dei 4 layer tutti a 0
    zl = m.newlabel('zl'); a.deflabel(zl); a.mov_imm(0, 0); a.mov_imm(4, 1); a.mov_rm_rn(11, 2)
    zw = m.newlabel('zw'); a.deflabel(zw); a.movl_st(0, 2); a.add_imm(4, 2); a.dt(1); a.bf_lbl(zw)
    a.add_imm(0x18, 11); a.dt(13); a.bf_lbl(zl)
    a.deflabel(qn); a.add_imm(1, 9); far_bra(m, ql)
    m.pool_jump()
    a.deflabel(qx); m.imm32(EVQ + 0x70, 1); a.mov_imm(1, 0); a.movl_st(0, 1)
    a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('ptret')
pg.on_doit(doit)


if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'PADTOOLS.WAV'); pg.build_wav(out)
    from validate import Harness
    from mpcmod import OS_PUSH_PAGE, KEYS_ADDR, keymask
    from pages import SET_FOCUS
    from shasm import SHAsm
    MSG, LOG = 0x88E10000, 0x88E00000
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): h.ram_w16(addr + i, struct.unpack('<H', blob[i:i+2])[0])
    stub(OS_PUSH_PAGE, lambda s: (s.movl_pool(LOG, 1), s.movl_st_disp(5, 4, 1), s.rts(), s.nop()))
    stub(ENABLE_DOIT, lambda s: (s.rts(), s.nop())); stub(SET_FOCUS, lambda s: (s.rts(), s.nop()))
    # PAD_INIT come l'OS (sottoinsieme verificabile): layer {nome[0]=0, +0x11 0x46, +0x12 0, +0x13 0x7F, tune 0, modo 0}, +0x62/63 0, +0x8F 100, +0x90 50
    def pinit(s):
        s.mov_rm_rn(5, 2); s.mov_imm(4, 3)
        s.deflabel('l'); s.mov_imm(0, 0); s.movb_st(0, 2); s.mov_imm(0x11, 0); s.add_rr(2, 0); s.mov_imm(0x46, 1); s.movb_st(1, 0)
        s.add_imm(1, 0); s.mov_imm(0, 1); s.movb_st(1, 0); s.add_imm(1, 0); s.mov_imm(0x7F, 1); s.movb_st(1, 0)
        s.add_imm(1, 0); s.mov_imm(0, 1); s.movb_st(1, 0); s.add_imm(1, 0); s.movb_st(1, 0); s.add_imm(1, 0); s.movb_st(1, 0)
        s.add_imm(0x18, 2); s.dt(3); s.bf_lbl('l')
        s.mov_rm_rn(5, 2); s.add_imm(0x62, 2); s.mov_imm(0, 0); s.movb_st(0, 2); s.add_imm(1, 2); s.movb_st(0, 2)
        s.mov_rm_rn(5, 2); s.add_imm(0x7F, 2); s.add_imm(0x10, 2); s.mov_imm(100, 0); s.movb_st(0, 2); s.add_imm(1, 2); s.mov_imm(50, 0); s.movb_st(0, 2)
        s.rts(); s.nop()
    stub(PAD_INIT, pinit)
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
    def cancel(): assert call(P, 2, code=CANCEL) == 1
    def pad(bank, k, vel=100): e.wr(BANKOBJ, bank, 1); r = call(P, EV_PAD, code=k, val=vel); call(P, EV_PAD, code=k, val=0); return r
    h.ram_w16(PROG_IDX, 3); prog = PROG_BASE + 3 * PROG_STRIDE
    def fill():
        for p in range(NPADS):
            for i in range(PAD_SIZE): e.wr(prog + PAD_OFF + p * PAD_SIZE + i, (p + 1) & 0xFF, 1)
        for n in range(128): e.wr(prog + NOTE_B + n, 0x30, 1)
        for p in range(NPADS): e.wr(prog + NOTE_A + p, 37 + p, 1); e.wr(prog + NOTE_B + 37 + p, p, 1)
    rdb = lambda p: bytes(e.rd(prog + PAD_OFF + p * PAD_SIZE + i, 1) for i in range(PAD_SIZE))
    orig = lambda p: bytes([(p + 1) & 0xFF]) * PAD_SIZE
    A = lambda p: e.rd(prog + NOTE_A + p, 1); B = lambda n: e.rd(prog + NOTE_B + n, 1)
    def setop(o, w): e.wr(OP, o, 1); e.wr(WHAT, w, 1)
    fill(); h.ram_w32(EVQ + 0x68, P); h.fire(3)
    # SHIFT+STOP apre la pagina
    h.ram_w32(KEYS_ADDR, keymask('SHIFT', 'STOP')); h.fire(2); h.ram_w32(KEYS_ADDR, 0); h.fire(2); assert h.ram_r32(LOG + 4) == P, "SHIFT+STOP"
    assert pad(0, 0) == 0, "fuori scelta il pad lo suona l'OS"
    # SWAP SAMPLE: solo i primi 0x60 byte
    setop(OP_SWAP, W_SAMPLE); doit_(); assert msg() == 'Hit first pad'; pad(0, 0); assert msg() == 'A01: hit 2nd pad'; pad(0, 1)
    assert rdb(0) == orig(1)[:0x60] + orig(0)[0x60:] and rdb(1) == orig(0)[:0x60] + orig(1)[0x60:] and msg() == 'Swapped A01>A02'
    fill(); setop(OP_SWAP, W_PAD); doit_(); pad(0, 0); pad(2, 4)
    assert rdb(0) == orig(28) and rdb(28) == orig(0) and (A(0), A(28)) == (37, 65)
    fill(); setop(OP_SWAP, W_NOTE); doit_(); pad(0, 0); pad(0, 1)
    assert rdb(0) == orig(1) and (A(0), A(1), B(37), B(38)) == (38, 37, 1, 0)
    print("  ok SWAP SAMPLE (solo layer) / PAD (blocco) / PAD+NOTE (blocco + note)")
    fill(); setop(OP_SWAP, W_PAD); doit_(); pad(0, 0); assert msg() == 'A01: hit 2nd pad'
    cancel(); assert msg() == 'Hit first pad' and h.ram_r32(STEP) == ST_FIRST and rdb(0) == orig(0)
    pad(0, 2); pad(0, 3); assert msg() == 'Swapped A03>A04' and rdb(2) == orig(3) and rdb(0) == orig(0)
    doit_(); cancel(); assert h.ram_r32(STEP) == ST_IDLE, "CANCEL al 1o pad = annulla"
    print("  ok SWAP: CANCEL al 2o pad = di nuovo 'Hit first pad', CANCEL al 1o = annulla")
    # COPY PAD: A01 -> A03 (vuoto), A04 (pieno: salta con CANCEL), A05 (pieno: DO IT = ok), fine con DO IT
    fill()
    for p in (2,):
        for l in range(4): e.wr(prog + PAD_OFF + p * PAD_SIZE + l * 0x18, 0, 1)      # A03 vuoto (nessun nome)
    setop(OP_COPY, W_PAD); doit_(); assert msg() == 'Hit source pad'; pad(0, 0); assert msg() == "A01 > hit pads"
    pad(0, 2); assert rdb(2) == orig(0) and msg() == 'A01>A03 copied'
    pad(0, 3); assert msg() == 'A04 full DOIT=ok' and rdb(3) == orig(3); cancel(); assert rdb(3) == orig(3) and msg() == 'Skipped A04'
    pad(0, 4); assert msg() == 'A05 full DOIT=ok'; doit_(); assert rdb(4) == orig(0)
    pad(0, 0); assert rdb(0) == orig(0)                                                 # sorgente su se stessa: niente
    doit_(); assert msg() == 'Copied 02 pads' and h.ram_r32(STEP) == 0 and (A(2), A(4)) == (39, 41), "le note non si copiano"
    fill(); setop(OP_COPY, W_SAMPLE); doit_(); pad(0, 5); pad(0, 6); assert msg() == 'A07 full DOIT=ok'; doit_(); doit_()   # conferma, poi fine
    assert h.ram_r32(STEP) == 0 and msg() == 'Copied 01 pads', msg()
    assert rdb(6) == orig(5)[:0x60] + orig(6)[0x60:]
    print("  ok COPY PAD/SAMPLE: vuoto copiato subito, pieno = conferma (CANCEL salta, DO IT sovrascrive), note mai copiate")
    # COPY su piu' pad pieni e vuoti (#15): con la domanda aperta un altro pad la salta e va avanti, lo stesso conferma
    for w in (W_PAD, W_NOTE):
        fill()
        for p in (2, 5):
            for l in range(4): e.wr(prog + PAD_OFF + p * PAD_SIZE + l * 0x18, 0, 1)  # A03, A06 vuoti
        empty = {p: rdb(p) for p in (2, 5)}
        setop(OP_COPY, w); doit_(); pad(0, 0)
        pad(0, 2); assert msg() == 'A01>A03 copied'
        pad(0, 3); assert msg() == 'A04 full DOIT=ok'
        pad(0, 5); assert msg() == 'A01>A06 copied' and rdb(3) == orig(3) and rdb(5) == orig(0), "un altro pad salta quello pieno"
        pad(0, 4); assert msg() == 'A05 full DOIT=ok'
        pad(0, 4); assert msg() == 'A01>A05 copied' and rdb(4) == orig(0), "lo stesso pad di nuovo = sovrascrivi"
        pad(0, 6); pad(1, 1); assert msg() == 'B02 full DOIT=ok' and rdb(6) == orig(6)
        doit_(); assert rdb(13) == orig(0) and msg() == 'A01>B02 copied'
        pad(0, 0); assert h.ram_r32(STEP) == ST_DEST                                   # la sorgente: niente
        doit_(); assert msg() == 'Copied 04 pads' and h.ram_r32(STEP) == ST_IDLE, msg()
        assert (A(2), A(4), A(5), A(13)) == (39, 41, 42, 50), "le note non si copiano (anche con PAD+NOTE)"
    print("  ok COPY su piu' pad pieni e vuoti (#15): un altro pad salta la domanda e prosegue, lo stesso pad conferma (PAD e PAD+NOTE)")
    # CLEAR: segna A02, B01 (pad 12), A02 di nuovo (gia' segnato), DO IT domanda, DO IT esegue
    fill(); setop(OP_CLEAR, W_PAD); doit_(); assert msg() == 'Hit pads to clr'
    pad(0, 1); assert msg() == 'A02 marked (01)'; pad(1, 0); pad(0, 1); assert msg() == 'B01 marked (02)'
    doit_(); assert msg() == 'Clear 02 pads?' and rdb(1) == orig(1)
    doit_(); assert msg() == 'Cleared 02 pads'
    for p in (1, 12):
        b = rdb(p); assert b[0] == 0 and b[0x11] == 0x46 and b[0x13] == 0x7F and b[0x8F] == 100 and b[0x90] == 50 and b[:16] == bytes(16) and b[0x18:0x28] == bytes(16)
    assert rdb(0) == orig(0) and rdb(2) == orig(2) and (A(1), A(12)) == (38, 49), "altri pad e note intatti"
    fill(); doit_(); pad(0, 7); doit_(); assert msg() == 'Clear 01 pads?'
    cancel(); assert rdb(7) == orig(7) and h.ram_r32(STEP) == ST_MARK and msg() == 'Hit pads to clr', "CANCEL sulla domanda = di nuovo a segnare"
    doit_(); assert msg() == 'Clear 01 pads?', "i pad segnati restano"
    cancel(); cancel(); assert h.ram_r32(STEP) == ST_IDLE and rdb(7) == orig(7)
    doit_(); doit_(); assert msg() == 'No pads marked'
    print("  ok CLEAR: pad segnati -> valori OS (nomi azzerati), note tenute, domanda di conferma; CANCEL un passo indietro")
    print("VALIDATO: Pad Tools -> %s" % os.path.basename(out))
