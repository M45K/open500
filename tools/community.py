#!/usr/bin/env python3
"""Community tools: from a submission (issue form or pull request) to the website catalog.

    python3 tools/community.py from-issue EVENT.json   issue form -> community/<id>/tool.py + tool.json (prints <id>)
    python3 tools/community.py build <id>|--all       RUNS tool.py: builds docs/community/<id>/<WAV>, checks it, catalog
    python3 tools/community.py check-wav <id>          checks the built WAV (data only, no tool code is run)
    python3 tools/community.py catalog                 docs/community/catalog.json from tool.json + WAVs (data only)
    python3 tools/community.py verify                  rebuilds every tool and fails if docs/community is different

Layout: community/<id>/tool.py (source, defines `pg`, a PageAddon), community/<id>/tool.json (name, author, ...);
docs/community/<id>/<WAV> + docs/community/catalog.json are generated and served by the website.
Only `build` and `verify` execute submitted code: the GitHub workflows run them in a job without write permissions
or secrets, and only after the repository owner labels the submission `approved`.
"""
import sys, os, re, json, struct, hashlib, subprocess, shutil, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK, SRC, OUT = os.path.join(ROOT, 'sdk'), os.path.join(ROOT, 'community'), os.path.join(ROOT, 'docs', 'community')
OFFICIAL = os.path.join(ROOT, 'docs', 'addons')
sys.path.insert(0, SDK)
from mpcmod import SLOT_CODE_MAX, ADDON3_HDR

ID_RE, WAV_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,23}$'), re.compile(r'^[A-Z0-9]{1,8}\.WAV$')
MAX_SOURCE = 60000


class Bad(Exception): pass


def wav_info(path):
    """(addon name, payload length) of an add-on WAV; Bad if it is not a clean Open500 add-on WAV"""
    d = open(path, 'rb').read()
    if d[:4] != b'RIFF' or d[8:12] != b'WAVE': raise Bad('%s: not a WAV file' % os.path.basename(path))
    i, chunks, data = 12, [], None
    while i + 8 <= len(d):
        cid, n = d[i:i + 4], struct.unpack('<I', d[i + 4:i + 8])[0]; chunks.append(cid)
        if cid == b'data': data = d[i + 8:i + 8 + n]
        i += 8 + n + (n & 1)
    if set(chunks) - {b'fmt ', b'data'}: raise Bad('%s: extra WAV chunks %s' % (os.path.basename(path), chunks))
    if not data or data[:8] != b'MPCADDN3': raise Bad('%s: not an Open500 add-on (MPCADDN3)' % os.path.basename(path))
    ln = struct.unpack('<I', data[8:12])[0]
    return data[16:24].rstrip(b'\0 ').decode('latin-1'), ln


def meta(tid):
    p = os.path.join(SRC, tid, 'tool.json')
    m = json.load(open(p, encoding='utf-8'))
    for k in ('name', 'wav', 'author', 'description'):
        if not isinstance(m.get(k), str) or not m[k].strip(): raise Bad('%s: "%s" missing' % (tid, k))
    if not WAV_RE.match(m['wav']): raise Bad('%s: WAV name must be 1-8 capital letters/digits + .WAV' % tid)
    if len(m['name']) > 32: raise Bad('%s: name too long' % tid)
    if m.get('video') and not re.match(r'^https://', m['video']): raise Bad('%s: video must be an https link' % tid)
    lcd = m.get('lcd') or []
    if len(lcd) > 2 or any(len(r) > 16 for r in lcd): raise Bad('%s: lcd = up to 2 rows of 16 characters' % tid)
    return m


def tool_ids():
    return sorted(t for t in os.listdir(SRC) if os.path.isdir(os.path.join(SRC, t))) if os.path.isdir(SRC) else []


def check_wav(tid):
    """data-only checks of docs/community/<id>/<WAV>: format, size, relocation audit, unique names"""
    if not ID_RE.match(tid): raise Bad('bad tool id %r' % tid)
    m = meta(tid); path = os.path.join(OUT, tid, m['wav'])
    if not os.path.exists(path): raise Bad('%s: %s not built' % (tid, m['wav']))
    name, ln = wav_info(path)
    if ln > SLOT_CODE_MAX: raise Bad('%s: add-on too big (%d > %d bytes)' % (tid, ln, SLOT_CODE_MAX))
    import audit_static
    r = audit_static.audit(path)
    if r is None: raise Bad('%s: unreadable payload' % tid)
    _, _, _, _, _, bad, cats = r
    if bad: raise Bad('%s: %d addresses into the add-on area are not relocated (first at +%#x)' % (tid, len(bad), bad[0][0]))
    if any(c in cats for c in ('SOTTO LA BASE', 'OLTRE 32K')): raise Bad('%s: relocated addresses outside the add-on' % tid)
    taken = {}
    for f in sorted(os.listdir(OFFICIAL)):
        if f.endswith('.WAV'):
            try: taken[wav_info(os.path.join(OFFICIAL, f))[0]] = 'official ' + f
            except Bad: pass
            taken.setdefault('file:' + f, 'official ' + f)
    for other in tool_ids():
        if other == tid: continue
        try:
            om = meta(other); taken.setdefault('file:' + om['wav'], 'community ' + other)
            op = os.path.join(OUT, other, om['wav'])
            if os.path.exists(op): taken.setdefault(wav_info(op)[0], 'community ' + other)
        except (Bad, ValueError, OSError): pass
    if name in taken: raise Bad('%s: internal name %r already used by %s (change PageAddon("...") )' % (tid, name, taken[name]))
    if 'file:' + m['wav'] in taken: raise Bad('%s: %s already used by %s' % (tid, m['wav'], taken['file:' + m['wav']]))
    return name, ln


def build(tid):
    """RUNS community/<id>/tool.py in a separate Python process and writes docs/community/<id>/<WAV>"""
    if not ID_RE.match(tid): raise Bad('bad tool id %r' % tid)
    m = meta(tid); src = os.path.join(SRC, tid, 'tool.py')
    if not os.path.exists(src): raise Bad('%s: tool.py missing' % tid)
    os.makedirs(os.path.join(OUT, tid), exist_ok=True)
    out = os.path.join(OUT, tid, m['wav'])
    tmp = tempfile.mkdtemp(prefix='o5c_')
    try:
        code = ("import sys, runpy; sys.path[:0] = [%r, %r]; g = runpy.run_path(%r, run_name='open500_community');"
                "pg = g.get('pg'); assert pg is not None and hasattr(pg, 'build_wav'), 'tool.py must define pg (a PageAddon)';"
                "pg.build_wav(%r)" % (os.path.join(SRC, tid), SDK, src, os.path.join(tmp, m['wav'])))
        p = subprocess.run([sys.executable, '-c', code], cwd=tmp, capture_output=True, text=True, timeout=300)
        if p.returncode != 0: raise Bad('%s: build failed:\n%s' % (tid, (p.stderr or p.stdout)[-2000:]))
        shutil.copyfile(os.path.join(tmp, m['wav']), out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for f in os.listdir(os.path.join(OUT, tid)):                 # only the current WAV stays
        if f != m['wav']: os.remove(os.path.join(OUT, tid, f))
    return check_wav(tid)


def catalog():
    """docs/community/catalog.json from tool.json + built WAVs (no tool code is run)"""
    tools = []
    for tid in tool_ids():
        m = meta(tid); path = os.path.join(OUT, tid, m['wav'])
        if not os.path.exists(path): continue
        name, ln = wav_info(path)
        tools.append(dict(id=tid, name=m['name'], wav=m['wav'], author=m['author'], description=m['description'],
                          how_to_use=m.get('how_to_use') or [], lcd=m.get('lcd') or [], video=m.get('video') or '',
                          hardware_tested=bool(m.get('hardware_tested')), version=str(m.get('version', '1')),
                          issue=m.get('issue'), size=os.path.getsize(path),
                          sha256=hashlib.sha256(open(path, 'rb').read()).hexdigest(),
                          download='community/%s/%s' % (tid, m['wav']), source='community/%s/tool.py' % tid))
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'catalog.json'), 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'tools': tools}, f, indent=1, ensure_ascii=False); f.write('\n')
    return tools


# ---------------- issue form ----------------
FIELDS = {'Tool name': 'name', 'WAV file name': 'wav', 'What it does': 'description', 'How to use': 'how_to_use',
          'Display (optional)': 'lcd', 'Video (optional)': 'video', 'Source (.py)': 'source',
          'Emulator test output': 'test', 'Checks': 'checks'}


def parse_issue(body):
    """issue form body ('### Label' sections) -> dict of fields"""
    out, cur, buf = {}, None, []
    for line in (body or '').replace('\r\n', '\n').split('\n'):
        if line.startswith('### ') and line[4:].strip() in FIELDS:
            if cur: out[FIELDS[cur]] = '\n'.join(buf).strip()
            cur, buf = line[4:].strip(), []
        elif cur: buf.append(line)
    if cur: out[FIELDS[cur]] = '\n'.join(buf).strip()
    return {k: ('' if v == '_No response_' else v) for k, v in out.items()}


def unfence(s):
    m = re.match(r'^```[a-zA-Z0-9]*\n(.*?)\n```\s*$', s.strip(), re.S)
    return m.group(1) if m else s


def from_issue(event_path):
    ev = json.load(open(event_path, encoding='utf-8')); issue = ev['issue']
    f = parse_issue(issue.get('body'))
    name, wav = f.get('name', '').strip(), f.get('wav', '').strip().upper()
    if not wav.endswith('.WAV'): wav += '.WAV'
    if not WAV_RE.match(wav): raise Bad('WAV file name must be 1-8 capital letters/digits (got %r)' % f.get('wav'))
    tid = wav[:-4].lower()
    if not ID_RE.match(tid): raise Bad('bad tool id %r' % tid)
    if tid in tool_ids(): raise Bad('a community tool called %s already exists' % tid)
    src = unfence(f.get('source', ''))
    if 'PageAddon' not in src or len(src) > MAX_SOURCE: raise Bad('source missing, not a PageAddon, or too long')
    if '[X] I publish this tool under the GPL-3.0' not in f.get('checks', '') and '[x] I publish this tool under the GPL-3.0' not in f.get('checks', ''):
        raise Bad('the GPL-3.0 box is not ticked')
    lcd = [r for r in unfence(f.get('lcd', '')).split('\n') if r.strip()][:2]
    how = [re.sub(r'^\s*(?:[-*]|\d+[.)])\s*', '', r).strip() for r in f.get('how_to_use', '').split('\n') if r.strip()]
    m = dict(name=name or tid, wav=wav, author=issue['user']['login'], description=f.get('description', '').strip(),
             how_to_use=how, lcd=[r[:16] for r in lcd], video=f.get('video', '').strip(),
             hardware_tested='[X] I tested it on my MPC500' in f.get('checks', '') or '[x] I tested it on my MPC500' in f.get('checks', ''),
             version='1', issue=issue['number'])
    os.makedirs(os.path.join(SRC, tid), exist_ok=True)
    open(os.path.join(SRC, tid, 'tool.py'), 'w', encoding='utf-8').write(src.rstrip() + '\n')
    with open(os.path.join(SRC, tid, 'tool.json'), 'w', encoding='utf-8') as fp:
        json.dump(m, fp, indent=1, ensure_ascii=False); fp.write('\n')
    meta(tid)
    return tid


def verify():
    """rebuild every tool in a copy and compare with docs/community (pull request check)"""
    before = {}
    for dp, _, fs in os.walk(OUT):
        for f in fs: before[os.path.relpath(os.path.join(dp, f), OUT)] = open(os.path.join(dp, f), 'rb').read()
    for tid in tool_ids(): build(tid)
    catalog()
    after = {}
    for dp, _, fs in os.walk(OUT):
        for f in fs: after[os.path.relpath(os.path.join(dp, f), OUT)] = open(os.path.join(dp, f), 'rb').read()
    diff = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if diff: raise Bad('docs/community is not what the sources build (run tools/community.py build --all): %s' % diff)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == 'from-issue': print(from_issue(argv[2]))
    elif cmd == 'build':
        ids = tool_ids() if argv[2] == '--all' else [argv[2]]
        for tid in ids: name, ln = build(tid); print('built %s: %s, %d bytes, checks ok' % (tid, name, ln))
        catalog()
    elif cmd == 'check-wav': name, ln = check_wav(argv[2]); print('ok %s: %s, %d bytes' % (argv[2], name, ln))
    elif cmd == 'catalog': print('%d tools in the catalog' % len(catalog()))
    elif cmd == 'verify': verify(); print('ok: docs/community matches the sources')
    else: print(__doc__); return 2
    return 0


if __name__ == '__main__':
    try: sys.exit(main(sys.argv))
    except Bad as e: print('ERROR: %s' % e, file=sys.stderr); sys.exit(1)
