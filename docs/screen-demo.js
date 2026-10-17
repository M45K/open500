// screen.html?demo — a fake MPC500 talking the real protocol (SysEx through link.feed, PEEK answered from fake memory),
// to preview the page without hardware. Loaded only with ?demo.
(function () {
  'use strict';
  var O = O5Link, OS = O.OS, mem = {};
  function w8(a, v) { mem[a] = v & 0xFF; }
  function w32(a, v) { for (var i = 0; i < 4; i++) w8(a + i, v >>> (8 * i)); }
  function ws(a, s) { for (var i = 0; i < s.length; i++) w8(a + i, s.charCodeAt(i)); }
  // program 1, bank A, pad names; sample list
  var prog = OS.PROG_BASE, NAMES = ['KICK 909', 'SNARE FAT', 'HAT CL', 'HAT OP', 'CLAP', 'RIM', 'BREAK 92 S1', 'BREAK 92 S2', 'BREAK 92 S3', 'BREAK 92 S4', 'VOX CHOP', 'SUB 808'];
  NAMES.forEach(function (n, p) { ws(prog + OS.PAD_OFF + p * OS.PAD_SIZE, n); });
  ['RHODES C3', 'RHODES E3', 'RHODES G3', 'STAB'].forEach(function (n, p) { ws(prog + OS.PAD_OFF + (12 + p) * OS.PAD_SIZE, n); });
  var head = 0x88680000, p = head, cur = 0;
  [['HUB', 4000], ['TRANSCHP', 15000], ['MASTERFX', 15000], ['KICK 909', 22050], ['SNARE FAT', 18000], ['HAT CL', 6000], ['HAT OP', 20000],
   ['CLAP', 15000], ['BREAK 92', 230000], ['VOX CHOP', 60000], ['SUB 808', 70000], ['RHODES C3', 120000]].forEach(function (s) {
    var size = (0x100 + 4 * s[1] + 3) & ~3; w32(p, size); ws(p + 4, s[0]); w32(p + 0x18, s[1]); if (s[0] === 'BREAK 92') cur = p; p += size;
  });
  w32(OS.MGR, head); w32(OS.MGR + 4, 0x8FF20000); w32(OS.MGR + 8, cur);
  var feed = function (m) { link.feed(Uint8Array.from(m)); };
  link.setSender(function (msg) {
    if (msg[2] !== 0x12) return;
    var a = O.num7(msg, 4, 5), n = msg[9], out = [0xF0, 0x7D, 0x13, msg[3], n];
    for (var i = 0; i < n; i++) { var b = mem[a + i] || 0; out.push(b >> 4, b & 15); }
    out.push(0xF7); setTimeout(function () { feed(out); }, 4);
  });
  function lcdmsg(t, c) { var m = [0xF0, 0x7D, 0x01, 0, c + 1]; for (var i = 0; i < 32; i++) m.push(t.charCodeAt(i) & 0x7F); m.push(0xF7); feed(m); }
  var vseq = 0;
  function view(v, data) {
    var pk = O.pack7(data), m = [0xF0, 0x7D, 0x30, v, 1, (vseq = (vseq + 1) & 0x7F), 0, 1].concat(pk); m.push(O.chk(m, 2, m.length)); m.push(0xF7); feed(m);
  }
  // waveform of a 2-bar break: 8 chunks of 32 peaks
  var LEN = 230000, sig = 7;
  for (var c = 0; c < 8; c++) {
    var m = [0xF0, 0x7D, 0x02, sig, c];
    for (var j = 0; j < 32; j++) { var k = c * 32 + j, ph = k % 32; m.push(Math.min(127, Math.round((ph < 2 ? 120 : 110 * Math.exp(-ph / 6)) * (k % 64 < 32 ? 1 : 0.75) + 8 * Math.abs(Math.sin(k * 1.7))))); }
    m.push(0xF7); feed(m);
  }
  var e7 = function (x, n) { return O.enc7(x, n); };
  feed([0xF0, 0x7D, 0x03, sig].concat(e7(LEN, 5), e7(2000, 5), e7(LEN - 3000, 5), [0xF7]));
  var cuts = []; for (var q = 0; q < 8; q++) cuts.push(2000 + Math.round(q * (LEN - 5000) / 8));
  var cm = [0xF0, 0x7D, 0x04, sig, 1, 0, 8, 8]; cuts.forEach(function (x) { cm = cm.concat(e7(x, 4)); }); cm.push(0xF7); feed(cm);
  // views: Pad Mute (bank A: 3 muted, 7 solo), Scale Pads (D dorian on bank B), Grid (bank A), Seq Tools
  var mute = [0]; for (var i = 0; i < 48; i++) mute.push(i === 2 ? 1 : i === 6 ? 2 : i < 12 && i !== 6 ? 4 : 0);
  var scale = [2, 2, 1, 0, 0]; [0, 2, 3, 5, 7, 9, 10, 12, 14, 15, 17, 19].forEach(function (x) { scale.push((x + 2) & 0xFF); });
  var steps = [0x1111, 0x0101 << 4, 0x5555, 0x0808, 0, 0, 0x8001, 0, 0, 0, 0x2020, 0x1001];
  function grid(bar, step) { var d = [8, 0, bar, step, 0, 0, 0, 0]; steps.forEach(function (x) { d.push(x & 0xFF, x >> 8); }); return d.concat([0, 0, 0, 0, 0, 0, 0, 0]); }
  view(4, mute); view(3, scale); view(2, grid(1, 1)); view(9, [8, 0, 116, 0]);
  // a little life: MAIN screen with bar counter, then Trans-Chop slices playing, then the grid moving
  var t = 0;
  setInterval(function () {
    t++; var bar = 1 + ((t / 8) | 0) % 8, ph = ((t / 40) | 0) % 3;
    if (ph === 0) lcdmsg(('MAIN  SEQ:01    BPM 92.0 BAR ' + bar + '      ').slice(0, 32), -1);
    if (ph === 1) {
      lcdmsg('TRANS-CHOP 8 slc Hit a pad     ', -1);
      if (t % 4 === 0) { var sl = (t / 4) % 8; feed([0xF0, 0x7D, 0x05, sig, t & 0x7F, 1, sl + 1].concat(e7(cuts[sl], 5), e7((sl < 7 ? cuts[sl + 1] : LEN - 3000) - 1, 5), [0xF7])); }
    }
    if (ph === 2) { lcdmsg(('GRID SEQ bar ' + bar + '/8 step ' + (1 + t % 16) + '         ').slice(0, 32), 9); view(2, grid(bar, 1 + t % 16)); }
  }, 250);
  document.getElementById('go').textContent = 'DEMO';
})();
