// Open500 patcher: applies the Open500 loader to an original Akai MPC500 OS 1.31 file, entirely in the browser.
// Nothing is uploaded. Works in the page (window.Open500Patcher) and in Node (module.exports) for testing.
(function (root) {
  'use strict';

  var CRC_TABLE = (function () {
    var t = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[n] = c >>> 0;
    }
    return t;
  })();

  // standard CRC32 (zlib), same algorithm the MPC500 bootloader uses to validate an OS update
  function crc32(u8, start, end) {
    var c = 0xFFFFFFFF;
    for (var i = start || 0, e = (end === undefined ? u8.length : end); i < e; i++) c = CRC_TABLE[(c ^ u8[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function hexToBytes(hex) {
    var out = new Uint8Array(hex.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
    return out;
  }

  function u32le(u8, off) { return (u8[off] | (u8[off + 1] << 8) | (u8[off + 2] << 16) | (u8[off + 3] << 24)) >>> 0; }

  // returns { ok: true, data: Uint8Array } or { ok: false, error: '...' }
  function applyPatch(input, patch) {
    var src = new Uint8Array(input);
    if (src.length !== patch.size)
      return { ok: false, error: 'Wrong file size (' + src.length + ' bytes). Expected the MPC500 OS 1.31 file "mpc500.bin" (' + patch.size + ' bytes).' };
    var hdr = String.fromCharCode.apply(null, src.subarray(patch.os_header, patch.os_header + 6));
    if (hdr !== 'MPC500')
      return { ok: false, error: 'This is not an MPC500 OS file.' };
    var base = crc32(src);
    if (base === patch.result_crc32)
      return { ok: false, error: 'This file is already patched with Open500. Use it as it is.' };
    if (base !== patch.base_crc32)
      return { ok: false, error: 'This is not the original OS 1.31 (version ' + src[patch.os_header + 8] + '.' + src[patch.os_header + 9] +
        ', CRC32 ' + base.toString(16) + '). Open500 only supports the unmodified Akai OS 1.31.' };

    var out = new Uint8Array(src);
    for (var i = 0; i < patch.patches.length; i++) {
      var p = patch.patches[i], orig = hexToBytes(p.orig), data = hexToBytes(p.data);
      for (var j = 0; j < orig.length; j++)
        if (out[p.off + j] !== orig[j]) return { ok: false, error: 'Unexpected content at offset 0x' + (p.off + j).toString(16) + '.' };
      out.set(data, p.off);
    }
    // OS checksum: CRC32 of the OS image with the CRC field set to zero, stored little-endian in the header
    var c = patch.crc_off, osSize = u32le(out, patch.os_header + 0xC);
    out[c] = out[c + 1] = out[c + 2] = out[c + 3] = 0;
    var crc = crc32(out, patch.os_header, patch.os_header + osSize);
    out[c] = crc & 0xFF; out[c + 1] = (crc >>> 8) & 0xFF; out[c + 2] = (crc >>> 16) & 0xFF; out[c + 3] = (crc >>> 24) & 0xFF;

    if (crc32(out) !== patch.result_crc32)
      return { ok: false, error: 'Internal check failed: the result does not match the tested Open500 firmware.' };
    return { ok: true, data: out };
  }

  var api = { crc32: crc32, applyPatch: applyPatch };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Open500Patcher = api;
})(this);
