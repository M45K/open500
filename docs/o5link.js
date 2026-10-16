/* o5link.js — Open500 Link: the MPC500 <-> screen/app protocol (docs/PROTOCOL.md in the dev repo), transport-agnostic.
 * Used by screen.html (Web MIDI) and by the companion app (native MIDI bridge). No dependencies.
 *
 *   var link = new O5Link.Link();
 *   link.on('display', function (d) { ... d.text (32 char codes), d.cursor (0..31 or -1) });
 *   link.on('view', function (v) { ... v.view, v.ver, v.data (bytes) });
 *   link.feed(sysexBytes)          // one complete F0..F7 message (Web MIDI gives these)
 *   link.feedStream(bytes)         // or a raw byte stream (native bridges): messages are assembled here
 *   link.setSender(function (bytes) { output.send(bytes); });
 *   link.caps().then(function (c) { ... c.views = [{view, ver}] });
 *
 * Events: display, wave (full 256 peaks per sample), trim, chop (all positions), play, view, stats.
 */
(function (root) {
  'use strict';
  var MFR = 0x7D;

  function num7(d, i, n) { var v = 0; for (var k = n - 1; k >= 0; k--) v = v * 128 + d[i + k]; return v; }
  function enc7(x, n) { var out = []; for (var k = 0; k < n; k++) { out.push(x % 128); x = Math.floor(x / 128); } return out; }
  function unpack7(b) {
    var out = [];
    for (var i = 0; i < b.length; i += 8) {
      var hi = b[i];
      for (var j = 1; j < 8 && i + j < b.length; j++) out.push(b[i + j] | ((hi >> (j - 1)) & 1 ? 0x80 : 0));
    }
    return out;
  }
  function pack7(b) {
    var out = [];
    for (var i = 0; i < b.length; i += 7) {
      var g = b.slice(i, i + 7), hi = 0;
      for (var j = 0; j < g.length; j++) if (g[j] & 0x80) hi |= 1 << j;
      out.push(hi); for (j = 0; j < g.length; j++) out.push(g[j] & 0x7F);
    }
    return out;
  }
  function chk(d, from, to) { var x = 0; for (var i = from; i < to; i++) x ^= d[i]; return x & 0x7F; }

  function Link() {
    this.handlers = {};
    this.stats = { ok: 0, bad: 0, unknown: 0 };
    this.sample = { sig: -1, peaks: new Array(256).fill(0), have: new Array(8).fill(false) };
    this.chopIn = null; this.vparts = {}; this.views = {};
    this.pending = {}; this.nextId = 0; this.send = null;
    this.stream = null;
  }
  Link.prototype.on = function (ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); return this; };
  Link.prototype.emit = function (ev, data) { (this.handlers[ev] || []).forEach(function (fn) { fn(data); }); };
  Link.prototype.setSender = function (fn) { this.send = fn; };

  /* raw byte stream (native bridges): assemble F0..F7, skip real-time bytes inside SysEx (MIDI clock) */
  Link.prototype.feedStream = function (bytes) {
    for (var i = 0; i < bytes.length; i++) {
      var b = bytes[i];
      if (b >= 0xF8) continue;
      if (b === 0xF0) { this.stream = [0xF0]; continue; }
      if (this.stream === null) continue;
      if (b === 0xF7) { this.stream.push(0xF7); var m = this.stream; this.stream = null; this.feed(m); continue; }
      if (b & 0x80) { this.stream = null; continue; }               // another status byte: SysEx interrupted
      this.stream.push(b);
    }
  };

  Link.prototype._newSample = function (sig) {
    if (sig === this.sample.sig) return;
    this.sample = { sig: sig, peaks: new Array(256).fill(0), have: new Array(8).fill(false) };
    this.chopIn = null;
  };

  /* one complete SysEx message */
  Link.prototype.feed = function (d) {
    if (d[0] !== 0xF0 || d[1] !== MFR || d[d.length - 1] !== 0xF7) { this.stats.unknown++; return; }
    var t = d[2], n = d.length, ok = true;
    if (t === 0x01 && n === 38) {
      var text = []; for (var i = 0; i < 32; i++) text.push(d[5 + i]);
      this.emit('display', { seq: d[3], cursor: d[4] - 1, text: text });
    } else if (t === 0x02 && n === 38) {
      this._newSample(d[3]); var c = d[4];
      for (var j = 0; j < 32; j++) this.sample.peaks[c * 32 + j] = d[5 + j];
      this.sample.have[c] = true;
      this.emit('wave', { sig: d[3], peaks: this.sample.peaks, have: this.sample.have, complete: this.sample.have.every(Boolean) });
    } else if (t === 0x03 && n === 20) {
      this._newSample(d[3]);
      this.emit('trim', { sig: d[3], len: num7(d, 4, 5), start: num7(d, 9, 5), end: num7(d, 14, 5) });
    } else if (t === 0x04 && n >= 9 && n === 9 + 4 * d[7]) {
      this._newSample(d[3]);
      var ms = d[4], part = d[5], nn = d[6], k = d[7];
      if (!this.chopIn || this.chopIn.mseq !== ms) this.chopIn = { mseq: ms, n: nn, pos: new Array(nn), got: 0 };
      for (var z = 0; z < k; z++) this.chopIn.pos[part * 12 + z] = num7(d, 8 + 4 * z, 4);
      this.chopIn.got++;
      if (this.chopIn.got >= Math.max(1, Math.ceil(nn / 12))) {
        this.emit('chop', { sig: d[3], positions: this.chopIn.pos.slice() }); this.chopIn = null;
      }
    } else if (t === 0x05 && n === 18) {
      this._newSample(d[3]);
      this.emit('play', { sig: d[3], on: !!d[5], slice: d[6] - 1, start: num7(d, 7, 5), end: num7(d, 12, 5) });
    } else if (t === 0x30 && n >= 10) {
      ok = this._view(d);
    } else if (t >= 0x11 && t <= 0x19 && (t & 1)) {
      ok = this._reply(d);
    } else { this.stats.unknown++; return; }
    if (ok) this.stats.ok++; else this.stats.bad++;
    this.emit('stats', this.stats);
  };

  Link.prototype._view = function (d) {
    if (chk(d, 2, d.length - 2) !== d[d.length - 2]) return false;
    var vw = d[3], ver = d[4], sq = d[5], part = d[6], np = d[7], key = vw + ':' + sq;
    var e = this.vparts[key] || (this.vparts[key] = { n: np, parts: {} });
    e.parts[part] = unpack7(Array.prototype.slice.call(d, 8, d.length - 2));
    if (Object.keys(e.parts).length < e.n) return true;
    var data = []; for (var k = 0; k < e.n; k++) data = data.concat(e.parts[k]);
    for (var kk in this.vparts) if (kk.split(':')[0] == vw) delete this.vparts[kk];
    var v = { view: vw, ver: ver, seq: sq, data: data };
    this.views[vw] = v; this.emit('view', v);
    return true;
  };

  /* ---- requests (MPC answers one at a time: they are queued) ---- */
  Link.prototype._reply = function (d) {
    var p = this.pending[d[3]];
    if (!p || p.want !== d[2]) return true;                           // not ours / late: ignore
    if (d[2] === 0x19 && chk(d, 2, d.length - 2) !== d[d.length - 2]) return false;
    clearTimeout(p.timer); delete this.pending[d[3]];
    p.resolve(Array.prototype.slice.call(d)); this._pump();
    return true;
  };
  Link.prototype._request = function (cmd, body, want, opts) {
    var self = this; opts = opts || {};
    return new Promise(function (resolve, reject) {
      (self.queue = self.queue || []).push({ cmd: cmd, body: body, want: want, resolve: resolve, reject: reject,
                                            tries: opts.tries || 3, timeout: opts.timeout || 1000 });
      self._pump();
    });
  };
  Link.prototype._pump = function () {
    var self = this;
    if (!this.queue || !this.queue.length || Object.keys(this.pending).length) return;
    if (!this.send) { var q = this.queue.shift(); q.reject(new Error('no MIDI output')); return; }
    var r = this.queue[0];
    var go = function () {
      self.nextId = (self.nextId + 1) & 0x7F; var id = self.nextId;
      var msg = [0xF0, MFR, r.cmd, id].concat(r.body);
      if (r.cmd === 0x18) msg.push(chk(msg, 2, msg.length));
      msg.push(0xF7);
      self.pending[id] = { want: r.want, resolve: function (d) { self.queue.shift(); r.resolve(d); },
        timer: setTimeout(function () {
          delete self.pending[id];
          if (--r.tries > 0) go(); else { self.queue.shift(); r.reject(new Error('MPC did not answer (cmd 0x' + r.cmd.toString(16) + ')')); self._pump(); }
        }, r.timeout) };
      self.send(msg);
    };
    go();
  };
  Link.prototype.ping = function () { return this._request(0x10, [], 0x11).then(function (d) { return { version: d[4] }; }); };
  Link.prototype.peek = function (addr, n) {
    return this._request(0x12, enc7(addr, 5).concat([n]), 0x13).then(function (d) {
      var out = []; for (var i = 0; i < d[4]; i++) out.push((d[5 + 2 * i] << 4) | d[6 + 2 * i]); return out;
    });
  };
  Link.prototype.poke = function (addr, bytes) {
    var body = enc7(addr, 5).concat([bytes.length]);
    bytes.forEach(function (b) { body.push(b >> 4, b & 15); });
    return this._request(0x14, body, 0x15).then(function () { return true; });
  };
  Link.prototype.event = function (type, code, data) {
    return this._request(0x16, enc7(type, 2).concat(enc7(code, 2), enc7(data >>> 0, 5)), 0x17).then(function () { return true; });
  };
  Link.prototype.caps = function () {
    return this._request(0x18, [], 0x19).then(function (d) {
      var views = []; for (var i = 0; i < d[5]; i++) views.push({ view: d[6 + 2 * i], ver: d[7 + 2 * i] });
      return { proto: d[4], views: views };
    });
  };

  /* ---- Web MIDI transport (Chrome desktop/Android) ---- */
  Link.prototype.connectWebMidi = function () {
    var self = this;
    if (typeof navigator === 'undefined' || !navigator.requestMIDIAccess)
      return Promise.reject(new Error('this browser has no Web MIDI (no iPhone/iPad browser has it)'));
    return navigator.requestMIDIAccess({ sysex: true }).then(function (access) {
      function hook() {
        var names = [];
        access.inputs.forEach(function (i) { i.onmidimessage = function (e) { self.feed(e.data); }; names.push(i.name); });
        var outs = []; access.outputs.forEach(function (o) { outs.push(o); });
        self.setSender(outs.length ? function (m) { outs.forEach(function (o) { o.send(m); }); } : null);
        self.emit('ports', { inputs: names, outputs: outs.map(function (o) { return o.name; }) });
      }
      access.onstatechange = hook; hook(); return access;
    });
  };

  var api = { Link: Link, pack7: pack7, unpack7: unpack7, enc7: enc7, num7: num7, chk: chk };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.O5Link = api;
})(this);
