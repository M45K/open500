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
 * Events: display, wave (full 256 peaks per sample), trim, chop (all positions), play, view, stats,
 *         mix / pads / mem (system views of MIDI Screen v11: sent only while subscribed, see subscribe()).
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
    } else if ((t >= 0x11 && t <= 0x19 && (t & 1)) || t === 0x41) {
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
    this.views[vw] = v; this.emit('view', v); this._sysView(v);
    return true;
  };
  /* system views (MIDI Screen v11): the MPC sends its state, the page no longer reads OS memory */
  var SYS = { PADS: 7, MIX: 10, MEM: 11 };
  Link.prototype._sysView = function (v) {
    var d = v.data, i;
    if (v.view === SYS.MIX && d.length >= 51) {
      this.sysAt = Date.now();
      this.emit('mix', { program: d[0] | d[1] << 8, bank: d[2] & 3, levels: d.slice(3, 51) });
    } else if (v.view === SYS.PADS && d.length >= 2 + 48 * 16) {
      var names = []; for (i = 0; i < 48; i++) names.push(cstr(d, 2 + 16 * i, 16));
      this.emit('pads', { program: d[0] | d[1] << 8, names: names });
    } else if (v.view === SYS.MEM && d.length >= 30) {
      this.emit('mem', { used: u32(d, 0), total: u32(d, 4), count: d[8] | d[9] << 8, frames: u32(d, 10), name: cstr(d, 14, 16) });
    }
  };
  /* true while the MPC is sending the system views (MIX seen in the last 5 s) */
  Link.prototype.hasSystemViews = function () { return !!this.sysAt && Date.now() - this.sysAt < 5000; };
  /* the system views are sent only while a screen/app asks CAPS now and then: call once after connecting */
  Link.prototype.subscribe = function (everyMs) {
    var self = this;
    if (this._sub) return;
    var tick = function () {
      if (!self.send) return;
      if (self.version === undefined && !self._pinging) {           // once: MIDI Screen version (>= 11: system views)
        self._pinging = true;
        self.ping().then(function (v) { self.version = v.version; self.emit('version', v.version); }, function () {})
                   .then(function () { self._pinging = false; });
      }
      self.caps().then(function (c) { self.capsInfo = c; self.emit('caps', c); }, function () {});
    };
    this._sub = setInterval(tick, everyMs || 4000); tick();
  };

  /* ---- requests (MPC answers one at a time: they are queued) ---- */
  Link.prototype._reply = function (d) {
    var p = this.pending[d[3]];
    if (!p || p.want !== d[2]) return true;                           // not ours / late: ignore
    if ((d[2] === 0x19 || d[2] === 0x41) && chk(d, 2, d.length - 2) !== d[d.length - 2]) return false;
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
      if (r.cmd === 0x18 || r.cmd === 0x40) msg.push(chk(msg, 2, msg.length));
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
  /* ACTION (PROTOCOL 4.3): result 0 ok, 1 unknown view/action, 2 busy, 3 refused */
  Link.prototype.action = function (view, action, arg) {
    return this._request(0x40, [view, action].concat(enc7(arg >>> 0, 5)), 0x41).then(function (d) { return d[4]; });
  };
  Link.prototype.caps = function () {
    return this._request(0x18, [], 0x19).then(function (d) {
      var views = []; for (var i = 0; i < d[5]; i++) views.push({ view: d[6 + 2 * i], ver: d[7 + 2 * i] });
      return { proto: d[4], views: views };
    });
  };

  /* ---- MPC500 OS 1.31 memory, READ ONLY through PEEK (addresses used by the official add-ons on hardware) ---- */
  var OS = { PROG_BASE: 0x8837B5AC, PROG_IDX: 0x8847C0E2, PROG_STRIDE: 0x1F98, PAD_OFF: 0x18, PAD_SIZE: 0xA4, LAYER: 0x18,
             BANK: 0x8849C98C, MGR: 0x8848ED30 };                        // MGR: [0] sample list, [1] limit, [2] current sample
  function u32(b, i) { return (b[i] | b[i + 1] << 8 | b[i + 2] << 16 | b[i + 3] << 24) >>> 0; }
  function cstr(b, i, n) {
    var s = ''; for (var k = i; k < i + n && b[k]; k++) s += b[k] >= 32 && b[k] < 127 ? String.fromCharCode(b[k]) : '?';
    return s.replace(/\s+$/, '');
  }
  /* program, bank and the sample name on each pad (first layer with a name). banks: e.g. [0] = only bank A */
  Link.prototype.readProgram = function (banks) {
    var self = this, out = { program: 0, bank: 0, pads: {} };
    return this.peek(OS.PROG_IDX, 2).then(function (b) {
      out.program = b[0] | b[1] << 8;
      return self.peek(OS.BANK, 1);
    }).then(function (b) {
      out.bank = b[0] & 3;
      var prog = OS.PROG_BASE + out.program * OS.PROG_STRIDE, chain = Promise.resolve();
      (banks || [out.bank]).forEach(function (bk) {
        for (var p = bk * 12; p < bk * 12 + 12; p++) (function (p) {
          var pad = prog + OS.PAD_OFF + p * OS.PAD_SIZE;                  // PEEK reads at most 32 bytes
          chain = chain.then(function () { return self.peek(pad, 16); }).then(function (b) {
            return (out.pads[p] = cstr(b, 0, 16)) || self.peek(pad + OS.LAYER, 16).then(function (b) { out.pads[p] = cstr(b, 0, 16); });
          });
        })(p);
      });
      return chain;
    }).then(function () { return out; });
  };
  /* mixer: level (0..100) of the 12 pads of a bank in the current program; the only thing we WRITE is that byte */
  OS.LEVEL = 0x8F;
  Link.prototype.readLevels = function (program, bank) {
    var self = this, prog = OS.PROG_BASE + program * OS.PROG_STRIDE, out = [], chain = Promise.resolve();
    for (var i = 0; i < 12; i++) (function (i) {
      chain = chain.then(function () { return self.peek(prog + OS.PAD_OFF + (bank * 12 + i) * OS.PAD_SIZE + OS.LEVEL, 1); })
                   .then(function (b) { out[i] = b[0]; });
    })(i);
    return chain.then(function () { return out; });
  };
  /* sets a pad level only if the MPC is still on that program (otherwise rejects: the page must re-read).
     MIDI Screen v11: typed action checked by the MPC itself; older MIDI Screen: PEEK of the program + POKE of the byte */
  Link.prototype.setLevel = function (program, pad, value) {
    var self = this, v = Math.max(0, Math.min(100, Math.round(value)));
    if (!(pad >= 0 && pad < 48)) return Promise.reject(new Error('bad pad'));
    if (this.hasSystemViews())
      return this.action(SYS.MIX, 0x10, v | pad << 7 | program << 13).then(function (r) {
        if (r === 3) throw new Error('program changed on the MPC');
        if (r !== 0) throw new Error('the MPC refused the level (' + r + ')');
        return true;
      });
    return this.peek(OS.PROG_IDX, 2).then(function (b) {
      if ((b[0] | b[1] << 8) !== program) throw new Error('program changed on the MPC');
      return self.poke(OS.PROG_BASE + program * OS.PROG_STRIDE + OS.PAD_OFF + pad * OS.PAD_SIZE + OS.LEVEL, [v]);
    });
  };
  /* sample memory: list of blocks {addr, name, frames, size}, current sample, bytes used / total */
  Link.prototype.readSamples = function () {
    var self = this, out = { list: [], current: 0, used: 0, total: 0 };
    return this.peek(OS.MGR, 12).then(function (b) {
      var head = u32(b, 0), limit = u32(b, 4); out.current = u32(b, 8);
      if (head < 0x88000000 || limit > 0x98000000 || head >= limit) throw new Error('sample list not found');
      out.total = limit - head;
      var walk = function (p) {
        if (p + 0x1C > limit || out.list.length >= 256) { out.used = p - head; return out; }
        return self.peek(p, 0x1C).then(function (b) {
          var size = u32(b, 0);
          if (!size) { out.used = p - head; return out; }
          if (size & 3 || size < 0x100 || p + size > limit) throw new Error('bad sample block @' + p.toString(16));
          out.list.push({ addr: p, name: cstr(b, 4, 16), frames: u32(b, 0x18), size: size });
          return walk(p + size);
        });
      };
      return walk(head);
    });
  };

  /* add-on map (diagnostics, on request): the HUB tables at B+0x100 (B = MGR[1]) and the HUB v3 black box at B+0x1000.
     Read only. Same layout as sdk/hub.py (Lay). */
  Link.prototype.readAddons = function () {
    var self = this, out = { B: 0, hub3: false, slots: [], bb: null };
    var rd = function (addr, n) {                                     // PEEK reads at most 32 bytes
      var parts = [], chain = Promise.resolve();
      for (var o = 0; o < n; o += 32) (function (o) {
        chain = chain.then(function () { return self.peek(addr + o, Math.min(32, n - o)); }).then(function (b) { parts = parts.concat(b); });
      })(o);
      return chain.then(function () { return parts; });
    };
    var T = {};
    return this.peek(OS.MGR + 4, 4).then(function (b) {
      out.B = u32(b, 0); var S = out.B + 0x100;
      return rd(S + 0x44, 64).then(function (x) { T.inst = x; return rd(S + 0x144, 64); })
        .then(function (x) { T.base = x; return rd(S + 0x184, 64); })
        .then(function (x) { T.len = x; return rd(S + 0x1C4, 128); })
        .then(function (x) { T.name = x; return rd(S + 0x384, 64); })
        .then(function (x) { T.scr = x; return rd(out.B + 0x1000, 0x20); })
        .then(function (bb) {
          out.hub3 = u32(bb, 0) === 0x42423554;                      // 'O5BB': only the HUB v3 writes it
          if (out.hub3) out.bb = { gen: u32(bb, 8), errors: u32(bb, 12), crash: cstr(bb, 0x10, 8), memErr: cstr(bb, 0x18, 8) };
          for (var k = 0; k < 16; k++) {
            var st = u32(T.inst, 4 * k); if (!st) continue;
            var base = u32(T.base, 4 * k), len = u32(T.len, 4 * k);
            out.slots.push({ slot: k, state: st === 1 ? 'ok' : st === 2 ? 'STOPPED (code changed)' : 'state ' + st,
                             name: cstr(T.name, 8 * k, 8), base: base, len: len, scratch: out.hub3 ? u32(T.scr, 4 * k) : base + 0x7000,
                             page: ((base & 0x1FFFFFFF) - ((out.B + 0x18000) & 0x1FFFFFFF)) >> 12 });
          }
          out.slots.sort(function (a, b) { return a.page - b.page; });
          return out;
        });
    });
  };

  /* ---- Web MIDI transport (Chrome desktop/Android) ---- */
  Link.prototype.connectWebMidi = function () {
    var self = this;
    if (typeof navigator === 'undefined' || !navigator.requestMIDIAccess)
      return Promise.reject(new Error('this browser has no Web MIDI (no iPhone/iPad browser has it)'));
    return navigator.requestMIDIAccess({ sysex: true }).then(function (access) {
      // requests go ONLY to the port the MPC talks on (same name as an input that delivered valid Open500 messages, or
      // a port named MPC500), never to every output: other apps/devices (Protokol, synths) must not get our traffic
      var mpc = {};
      function hook() {
        var names = [];
        access.inputs.forEach(function (i) {
          i.onmidimessage = function (e) {
            var ok = self.stats.ok; self.emit('raw', e.data.length); self.feed(e.data);
            if (self.stats.ok > ok && !mpc[i.name]) { mpc[i.name] = true; hook(); }
          };
          names.push(i.name);
        });
        var outs = []; access.outputs.forEach(function (o) { if (mpc[o.name] || /MPC500/i.test(o.name)) outs.push(o); });
        self.setSender(outs.length ? function (m) { outs.forEach(function (o) { o.send(m); }); } : null);
        var all = []; access.outputs.forEach(function (o) { all.push(o.name + (outs.indexOf(o) >= 0 ? ' (MPC)' : '')); });
        self.emit('ports', { inputs: names, outputs: all });
      }
      access.onstatechange = hook; hook(); return access;
    });
  };

  var api = { Link: Link, pack7: pack7, unpack7: unpack7, enc7: enc7, num7: num7, chk: chk, OS: OS, SYS: SYS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.O5Link = api;
})(this);
