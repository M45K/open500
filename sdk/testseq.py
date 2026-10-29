#!/usr/bin/env python3
# Sequenza di PROVA generata (2026-10-16): sostituisce nei test la sequenza registrata dall'utente (non pubblicabile).
# Formato (RE Grid Seq, file MPC1000 SEQ 1.00 come in memoria): +0 u32 dimensione del blocco, +4 "MPC1000 SEQ 1.00",
#   +0x1C u16 battute, +0x30 tabella inizio battuta u32 (tick | 0x60 << 24 = 4/4) gia' riempita oltre la fine,
#   +0x1C10 eventi da 16 B {u32 tick, u8 traccia, u8 0x90, u8 nota, u8 velocity, u16 durata, 6 x 0} ordinati per tick,
#   chiusi da un evento con tick 0x7FFFFFFF.
# Contenuto: batteria di 8 battute in 4/4 sulla traccia 1 (indice 1 come nel file registrato), 96 tick per quarto:
#   kick (36) sui quarti, rullante (38) su 2 e 4, hat (47) sugli ottavi con alcuni colpi "suonati" fuori griglia
#   di qualche tick (come una registrazione vera), tom (55) sull'ultimo sedicesimo delle battute pari.
import struct

EV_OFF, BARS_OFF, BART_OFF, BAR_TICKS, END = 0x1C10, 0x1C, 0x30, 384, 0x7FFFFFFF
MAXBARS_TABLE = (EV_OFF - BART_OFF) // 4

def events(bars=8, track=1):
    ev = []
    for b in range(bars):
        t0 = b * BAR_TICKS
        for q in range(4): ev.append((t0 + q * 96, track, 36, 110, 48))
        for q in (1, 3): ev.append((t0 + q * 96, track, 38, 100, 48))
        for e8 in range(8):
            jitter = (-3, 2, 0, 4, -1, 0, 3, -2)[(b + e8) % 8]          # colpi fuori griglia (entro mezzo sedicesimo)
            ev.append((max(0, t0 + e8 * 48 + jitter), track, 47, 70 + 5 * (e8 % 3), 20))
        if b % 2: ev.append((t0 + 15 * 24, track, 55, 90, 18))
    ev.sort(key=lambda x: x[0])
    return ev

def make_seq(bars=8, track=1, evs=None):
    evs = events(bars, track) if evs is None else evs
    hdr = bytearray(EV_OFF)
    hdr[4:20] = b"MPC1000 SEQ 1.00"
    struct.pack_into('<H', hdr, BARS_OFF, bars)
    for i in range(MAXBARS_TABLE): struct.pack_into('<I', hdr, BART_OFF + 4 * i, (0x60 << 24) | (i * BAR_TICKS))
    body = b''.join(struct.pack('<IBBBBH6x', t, tr, 0x90, n, v, d) for t, tr, n, v, d in evs)
    body += struct.pack('<I12x', END)
    blk = hdr + body
    struct.pack_into('<I', blk, 0, len(blk))
    return bytes(blk)

if __name__ == '__main__':
    s = make_seq(); print(len(s), "byte,", len(events()), "eventi")
