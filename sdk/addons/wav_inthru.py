#!/usr/bin/env python3
# INPUT THRU (MODE -> OTHER -> "Input Thru") -> INTHRU.WAV
# Passthrough ANALOGICO dell'ingresso linea verso le uscite (bypass del codec WM8731, zero CPU, zero latenza).
#   riga 1: "INPUT THRU ON"   rotella: OFF / ON
#   riga 2: "LEVEL +0.0dB"    rotella: volume ingresso linea, 32 passi da 1.5 dB (-34.5 .. +12.0 dB, reg 0/1 LINVOL)
# Resta attivo su TUTTE le pagine (il codice gira a ogni fire, pagina aperta o no). Durante il campionamento (flag OS
# 0x881C297C != 0: ADC acceso dall'OS) il bypass si mette in pausa e il codec resta all'OS; finito, si riattiva da solo.
# Ogni REFRESH fire riscrive i registri (l'OS riscrive reg 4 / volume linea in alcuni momenti).
# Codec (verificato su HW con la sonda, 2026-09-27): scrittura 0x884C6722(reg, val); 0x884C672E(ombra) = volume linea
# dell'OS (muto se non registra); OS normale: reg4 = 0x12 (DACSEL|MUTEMIC); bypass: reg4 = 0x1A (+ BYPASS).
import sys, os, struct
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from pages import PageAddon, ld, st, sti

CODEC_W, CODEC_GAIN, GAIN_SHADOW, REC_FLAG = 0x884C6722, 0x884C672E, 0x881C2980, 0x881C297C
REG4_OS, REG4_THRU = 0x12, 0x1A
LVL_0DB = 23          # LINVOL 0b10111 = 0 dB
REFRESH = 256         # fire tra due riscritture di sicurezza
LEVELS = ["%+.1fdB" % ((v - LVL_0DB) * 1.5) for v in range(32)]

pg = PageAddon("inthru1", open_press=None)
pg.other_menu("Input Thru")
ONS = pg.choice("INPUT THRU ", 0, 0, ["OFF", "ON"], default=0)
LVL = pg.choice("LEVEL ", 0, 1, LEVELS, default=LVL_0DB)
APPLIED, ALV, CNT = [pg.alloc_slot() for _ in range(3)]

def codec(m, reg, val_reg=None, val=None):
    a = m.a; a.mov_imm(reg, 4)
    if val_reg is not None: a.mov_rm_rn(val_reg, 5)
    else: m.imm32(val, 5)
    m.imm32(CODEC_W, 2); a.jsr(2); a.nop()

def maint_call(m):
    m.a.movl_pool('it_maint', 2); m.a.jsr(2); m.a.nop()

def fire(m):
    a = m.a
    maint_call(m)
    a.bra_lbl('it_end'); a.nop()
    m.pool_jump()
    # ---- it_maint: stato voluto -> codec (a ogni fire, pagina aperta o no); usa r0-r7, r8-r10 ----
    a.deflabel('it_maint'); a.stsl_pr()
    m.imm32(ONS, 1); a.movb_ld(1, 8); a.extu_b(8, 8)                          # r8 = ON
    m.imm32(LVL, 1); a.movb_ld(1, 9); a.extu_b(9, 9)                          # r9 = livello 0..31
    m.imm32(REC_FLAG, 1); a.movl_ld(1, 10)                                    # r10 = l'OS sta campionando
    off = m.newlabel('off'); a.tst_rr(8, 8); m.bt_far(off); a.tst_rr(10, 10); m.bf_far(off)
    # voluto ON: applica se non applicato, livello cambiato o refresh
    ld(m, APPLIED, 0); a.tst_rr(0, 0); ap = m.newlabel('ap'); m.bt_far(ap)
    ld(m, ALV, 0); a.cmpeq(9, 0); m.bf_far(ap)
    ld(m, CNT, 0); a.add_imm(1, 0); st(m, 0, CNT); m.imm32(REFRESH, 1); a.cmphs(1, 0); m.bt_far(ap)
    a.bra_lbl('it_mx'); a.nop()
    a.deflabel(ap)
    sti(m, 0, CNT); st(m, 9, ALV); sti(m, 1, APPLIED)
    codec(m, 0, val_reg=9); codec(m, 1, val_reg=9); codec(m, 4, val=REG4_THRU)
    a.bra_lbl('it_mx'); a.nop()
    m.pool_jump()
    # voluto OFF (o campionamento in corso): se applicato, rimetti il codec dell'OS
    a.deflabel(off)
    ld(m, APPLIED, 0); a.tst_rr(0, 0); m.bt_far('it_mx')
    sti(m, 0, APPLIED); codec(m, 4, val=REG4_OS)
    m.imm32(REC_FLAG, 1); a.movl_ld(1, 0); a.tst_rr(0, 0); m.bf_far('it_mx')   # campionamento: il volume e' dell'OS
    m.imm32(GAIN_SHADOW, 1); a.movw_ld(1, 4); m.imm32(CODEC_GAIN, 2); a.jsr(2); a.nop()
    a.deflabel('it_mx'); a.ldsl_pr(); a.rts(); a.nop()
    m.pool_jump()
    a.deflabel('it_end')
pg.on_fire(fire)
pg.on_inactive(maint_call)

if __name__ == '__main__':
    out = os.path.join(os.path.dirname(__file__), '..', 'INTHRU.WAV'); pg.build_wav(out)
    from validate import Harness
    from mpcmod import EVQ
    from shasm import SHAsm
    h = Harness(os.path.join(os.path.dirname(__file__), '..', 'mpc500_loader.bin')); h.fill_scratch(); e = h.emu
    CLOG = 0x88E20000; h.ram_w32(CLOG, 0)
    def stub(addr, fn):
        s = SHAsm(addr); fn(s); blob = s.assemble()
        for i in range(0, len(blob), 2): e.wr(addr + i, struct.unpack('<H', blob[i:i+2])[0], 2)
    def logw(s, fixed_reg=None):            # log (reg, val); volume OS: (0xFF, ombra)
        s.movl_pool(CLOG, 1); s.movl_ld(1, 0); s.add_imm(1, 0); s.movl_st(0, 1); s.shll2(0); s.shll(0); s.add_rr(0, 1)
        if fixed_reg is not None: s.mov_imm(fixed_reg - 256, 4); s.extu_b(4, 4)
        s.movl_st(4, 1); s.movl_st_disp(5, 4, 1)
        for r in range(0, 8): s.mov_imm(0x55, r) if r not in (1,) else None       # sporca r0-r7 come una funzione C
        s.rts(); s.nop()
    stub(0x88E21000, logw); stub(0x88E21100, lambda s: (s.mov_rm_rn(4, 5), logw(s, 0xFF)))
    stub(CODEC_W, lambda s: (s.movl_pool(0x88E21000, 0), s.jmp(0), s.nop()))
    stub(CODEC_GAIN, lambda s: (s.movl_pool(0x88E21100, 0), s.jmp(0), s.nop()))
    def clog():
        n = h.ram_r32(CLOG); r = [(h.ram_r32(CLOG + 8 * i + 8) & 0xff, h.ram_r32(CLOG + 8 * i + 12)) for i in range(n)]
        h.ram_w32(CLOG, 0); return r
    h.ram_w16(GAIN_SHADOW, 0x1234); h.ram_w32(REC_FLAG, 0)
    h.load_hub(); h.load_wav(out); h.fire(10); clog()
    OTHER = 0x88123456
    assert e.rd(ONS, 1) == 0 and e.rd(LVL, 1) == LVL_0DB
    h.ram_w32(EVQ + 0x68, OTHER); h.fire(REFRESH + 10); assert clog() == [], "OFF: il codec non si tocca"
    print("  ok caricato: OFF, livello proposto %s, nessuna scrittura" % LEVELS[LVL_0DB])
    # pagina aperta, ON
    h.ram_w32(EVQ + 0x68, pg.page); h.fire(2); assert clog() == []
    h.ram_w8(ONS, 1); h.fire(1); assert clog() == [(0, 23), (1, 23), (4, REG4_THRU)]
    h.fire(5); assert clog() == []
    # livello
    h.ram_w8(LVL, 31); h.fire(1); assert clog() == [(0, 31), (1, 31), (4, REG4_THRU)]
    print("  ok ON: linea al livello scelto + bypass; livello cambiato -> riscritto")
    # fuori dalla pagina: resta attivo; refresh periodico
    h.ram_w32(EVQ + 0x68, OTHER); h.fire(REFRESH - 3); assert clog() == [], clog()
    h.fire(4); assert clog() == [(0, 31), (1, 31), (4, REG4_THRU)], "refresh"
    h.fire(REFRESH - 2); assert clog() == []   # 1 fire gia' passato dopo il refresh
    print("  ok su altre pagine resta ON; riscrittura di sicurezza ogni %d fire" % REFRESH)
    # campionamento: pausa (solo reg4, il volume e' dell'OS), poi ripresa
    h.ram_w32(REC_FLAG, 1); h.fire(1); assert clog() == [(4, REG4_OS)]
    h.fire(REFRESH * 2); assert clog() == []
    h.ram_w32(REC_FLAG, 0); h.fire(1); assert clog() == [(0, 31), (1, 31), (4, REG4_THRU)]
    print("  ok campionamento: bypass in pausa (codec all'OS), poi riattivato")
    # OFF: codec come l'OS
    h.ram_w32(EVQ + 0x68, pg.page); h.ram_w8(ONS, 0); h.fire(1); assert clog() == [(4, REG4_OS), (0xFF, 0x1234)]
    h.ram_w32(EVQ + 0x68, OTHER); h.fire(REFRESH + 5); assert clog() == []
    print("  ok OFF: reg4 e volume linea dell'OS rimessi, poi piu' nulla")
    print("VALIDATO: Input Thru -> INTHRU.WAV")
