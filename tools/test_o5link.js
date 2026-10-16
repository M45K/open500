// node tools/test_o5link.js — o5link.js against REAL bytes produced by MIDI Screen v8 in the emulator
// (fixtures/midiscreen_v8.json, regenerate with O5_FIXTURE=... python3 sdk/wav_midiscreen.py in the dev repo).
'use strict';
const assert = require('assert');
const path = require('path');
const O5 = require(path.join(__dirname, '..', 'docs', 'o5link.js'));
const P = require(path.join(__dirname, 'fixtures', 'midiscreen_v8.json'));

// pack7 / unpack7 round trip
for (const n of [0, 1, 6, 7, 8, 40, 300]) {
  const b = Array.from({ length: n }, (_, i) => (i * 101 + 7) & 0xFF);
  assert.deepStrictEqual(O5.unpack7(O5.pack7(b)), b, 'pack7 ' + n);
}

function run(feedFn) {
  const L = new O5.Link(), seen = { display: [], wave: [], trim: [], chop: [], play: [], view: [] };
  for (const k of Object.keys(seen)) L.on(k, d => seen[k].push(d));
  feedFn(L);
  return { L, seen };
}
// 1) one message at a time (Web MIDI)
const a = run(L => P.forEach(p => L.feed(Uint8Array.from(p))));
// 2) the same bytes as one raw stream with MIDI clock bytes sprinkled in (native bridges)
const a2 = run(L => {
  const stream = []; P.forEach((p, i) => { stream.push(...p); if (i % 3 === 0) stream.push(0xF8); });
  for (let i = 0; i < stream.length; i += 17) L.feedStream(stream.slice(i, i + 17));
});
for (const r of [a, a2]) {
  const { L, seen } = r;
  assert.strictEqual(L.stats.bad, 0, 'no bad packets');
  assert.ok(seen.display.length > 10 && seen.display[0].text.length === 32);
  assert.strictEqual(String.fromCharCode(...seen.display[0].text), 'TRIM st:  1000  end: 25000      ');
  assert.ok(seen.display.some(d => d.cursor === 20), 'cursor 21 -> index 20');
  const t0 = seen.trim[0]; assert.deepStrictEqual([t0.len, t0.start, t0.end], [30000, 1000, 25000]);
  assert.ok(seen.wave.some(w => w.complete), 'full waveform');
  const POS = Array.from({ length: 20 }, (_, i) => i * 300 + (i * i) % 17);
  assert.ok(seen.chop.some(c => JSON.stringify(c.positions) === JSON.stringify(POS)), 'Trans-Chop cuts (20, 2 parts)');
  assert.ok(seen.play.some(p => p.on && p.slice === 1 && p.start === 1000 && p.end === 1999), 'play slice 2');
  const mute = seen.view.filter(v => v.view === 4), big = seen.view.filter(v => v.view === 9);
  const pay = [1].concat(Array.from({ length: 48 }, (_, i) => (i * 37) & 0xFF));
  assert.ok(mute.some(v => JSON.stringify(v.data) === JSON.stringify(pay)), 'view 4 (49 bytes)');
  const bigp = Array.from({ length: 300 }, (_, i) => (i * 101 + 7) & 0xFF);
  assert.ok(big.some(v => v.ver === 3 && JSON.stringify(v.data) === JSON.stringify(bigp)), 'view 9 (300 bytes, 8 parts)');
  assert.ok(big.every(v => v.data.length === 300 && (v.data.every(x => x === 0) || JSON.stringify(v.data) === JSON.stringify(bigp))), 'never mixed parts');
}
console.log('ok: %d packets decoded, same result per message and as a byte stream with MIDI clock', P.length);

// 3) requests: the MPC side is simulated by replaying the recorded replies with the id we send
(async () => {
  const L = new O5.Link(), replies = {}; P.filter(p => p[2] >= 0x11 && p[2] <= 0x19 && (p[2] & 1)).forEach(p => replies[p[2]] = p);
  L.setSender(msg => {
    const r = replies[msg[2] + 1]; if (!r) return;
    const out = r.slice(); out[3] = msg[3];
    if (out[2] === 0x19) out[out.length - 2] = O5.chk(out, 2, out.length - 2);
    setTimeout(() => L.feed(Uint8Array.from(out)), 5);
  });
  assert.deepStrictEqual(await L.ping(), { version: 8 });
  assert.strictEqual(String.fromCharCode(...await L.peek(0x88483E9C, 16)), 'HELLO PEEK TEST!');
  assert.strictEqual(await L.poke(0x88E30000, [0xDE, 0xAD]), true);
  assert.strictEqual(await L.event(2, 0x1D, 0), true);
  const c = await L.caps(); assert.deepStrictEqual(c, { proto: 1, views: [{ view: 4, ver: 1 }, { view: 9, ver: 1 }] });
  const L2 = new O5.Link(); L2.setSender(() => {});                    // silent MPC: timeout after retries
  await assert.rejects(L2._request(0x10, [], 0x11, { tries: 2, timeout: 20 }), /did not answer/);
  console.log('ok: ping, peek, poke, event, caps (queued, ids), timeout with retries');
})().catch(e => { console.error(e); process.exit(1); });
