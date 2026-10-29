#!/usr/bin/env python3
# CONTROLLO STATICO dei WAV MPCADDN3 (2026-10-17), senza emulatore:
#  1) parole del codice che puntano all'area di COSTRUZIONE (0x88F00000.., anche alias P2) ma non sono nella tabella di
#     rilocazione: sull'MPC (B = 0x8FF20000) finirebbero nella memoria dei sample;
#  2) dove puntano le parole rilocate: codice / sotto la scratch / scratch / FUORI dai 32 KB dell'add-on, con alias.
# I buffer calcolati a runtime (scratch - offset) non si vedono qui: per quelli c'e' sdk/memguard.py.
#   python3 sdk/audit_static.py [WAV...]       (senza argomenti: tutti i *.WAV della radice)
import sys, os, struct, glob
RE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, RE)
from mpcmod import ADDON3_HDR, ADDON_BASE, SLOT_CODE_MAX, SLOT_SIZE, SCRATCH, RESERVE

def payload(path):
    w = open(path, 'rb').read(); i = w.find(b'data')
    if i < 0: return None
    p = w[i + 8:]
    if p[:8] not in (b'MPCADDN3', b'MPCHUB_3'): return None
    ln = struct.unpack('<I', p[8:12])[0]; code_len = struct.unpack('<I', p[24:28])[0]
    body = p[ADDON3_HDR:ADDON3_HDR + ln]; nrel = struct.unpack('<I', body[code_len:code_len + 4])[0]
    rel = struct.unpack('<%dI' % nrel, body[code_len + 4:code_len + 4 + 4 * nrel])
    return p[:8], p[16:24].decode().strip(), body[:code_len], rel, ln

def audit(path):
    r = payload(path)
    if not r: return None
    magic, name, code, rel, ln = r; rs = set(rel); bad = []
    for off in range(0, len(code) - 3, 4):
        v = struct.unpack('<I', code[off:off + 4])[0]
        if SCRATCH <= (v & 0xDFFFFFFF) < SCRATCH + RESERVE and off not in rs: bad.append((off, v))
    cats = {}
    if magic == b'MPCADDN3':
        for off in rel:
            v = struct.unpack('<I', code[off:off + 4])[0]; o = (v & 0xDFFFFFFF) - (ADDON_BASE & 0xDFFFFFFF)
            c = 'SOTTO LA BASE' if o < 0 else 'codice' if o < len(code) else 'sotto la scratch' if o < SLOT_CODE_MAX else 'scratch' if o < SLOT_SIZE else 'OLTRE 32K'
            lo, hi, n, al = cats.get(c, (o, o, 0, set()))
            al.add('P2' if v & 0x20000000 else 'P1'); cats[c] = (min(lo, o), max(hi, o), n + 1, al)
    return name, magic, len(code), ln, len(rel), bad, cats

if __name__ == '__main__':
    files = sys.argv[1:] or sorted(glob.glob(os.path.join(RE, '*.WAV')))
    problems = 0
    for f in files:
        r = audit(f)
        if not r: continue
        name, magic, cl, ln, nrel, bad, cats = r
        s = '  '.join('%s[%d %s %#x..%#x]' % (c, n, '/'.join(sorted(al)), lo, hi) for c, (lo, hi, n, al) in sorted(cats.items()))
        print('%-13s %-9s codice %#06x body %#06x reloc %3d  %s' % (os.path.basename(f), name, cl, ln, nrel, s))
        # l'HUB usa di proposito la base di costruzione degli add-on (ADDON_BASE+1, poi -1) per calcolare il delta
        bad = [(o, v) for o, v in bad if not (magic == b'MPCHUB_3' and v in (ADDON_BASE + 1, ADDON_BASE + SLOT_CODE_MAX + 1))]
        for o, v in bad: print('      NON RILOCATA @%#06x: %08x (area+%#x)' % (o, v, (v & 0xDFFFFFFF) - SCRATCH))
        problems += len(bad) + sum(n for c, (lo, hi, n, al) in cats.items() if c in ('SOTTO LA BASE', 'OLTRE 32K'))
    print('problemi: %d' % problems)
    sys.exit(1 if problems else 0)
