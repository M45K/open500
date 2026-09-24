// Checks the browser patcher against the files tested on the hardware:
//   original OS 1.31 + patch  ==  re_work/release/v4_topmem/mpc500_loader.bin  (byte for byte)
// and that wrong inputs are refused.   Run: node tools/test_patcher.js
'use strict';
var fs = require('fs'), path = require('path'), assert = require('assert');
var P = require('../docs/patcher.js'), PATCH = require('../docs/patch.js');
var RE = path.join(__dirname, '..', '..', 're_work');

var orig = fs.readFileSync(path.join(RE, 'mpc500.bin'));
var tested = fs.readFileSync(path.join(RE, 'release', 'v4_topmem', 'mpc500_loader.bin'));

var r = P.applyPatch(orig, PATCH);
assert.ok(r.ok, r.error);
assert.ok(Buffer.from(r.data).equals(tested), 'patched output differs from the hardware-tested firmware');
console.log('ok  OS 1.31 + patch == tested firmware (' + r.data.length + ' bytes, crc32 ' + P.crc32(r.data).toString(16) + ')');

var again = P.applyPatch(r.data, PATCH);
assert.ok(!again.ok && /already patched/.test(again.error)); console.log('ok  refuses an already patched file');

var bad = Buffer.from(orig); bad[0x20000] ^= 0xFF;
var rb = P.applyPatch(bad, PATCH);
assert.ok(!rb.ok && /not the original OS 1.31/.test(rb.error)); console.log('ok  refuses a modified OS');

var rs = P.applyPatch(orig.subarray(0, 1000), PATCH);
assert.ok(!rs.ok && /Wrong file size/.test(rs.error)); console.log('ok  refuses a file of the wrong size');

console.log('ALL PASSED');
