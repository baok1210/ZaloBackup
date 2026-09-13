#!/usr/bin/env node
/**
 * Smoke test for the ZaloBackup viewer.
 *
 * Opens every master in the live WebUI (default http://localhost:8320/viewer)
 * and asserts, per master:
 *   1. No message text is a raw JSON dump (no row starts with '{').
 *   2. No '?' senders (every row has a non-empty sender).
 *   3. At most ONE sender is flagged as "me" in Zalo-style view, and that
 *      sender owns every .zrow.me bubble (side-guessing is consistent).
 *   4. Sticker/audio flags (st/vo) only appear on rows that actually render
 *      inline media (no broken players: audio elements have readyState>0 or no error).
 *
 * Usage:
 *   node smoke_test.mjs [--url http://localhost:8320] [--max-per-master 400]
 * Exit code 0 = all pass, 1 = failures (they are printed).
 */
import { chromium } from 'playwright';

const args = process.argv.slice(2);
const arg = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : def;
};
const BASE = arg('--url', 'http://localhost:8320');
const MAX_PER_MASTER = parseInt(arg('--max-per-master', '400'), 10);

// The viewer page keeps its state in top-level `let` variables (DATA, ME_GUESS,
// shown, drawMsgs...). page.evaluate runs in its own scope, so reach the page's
// global scope through indirect eval.
const G = expr => page.evaluate(expr);            // shorthand
const gv = async expr => page.evaluate(`(function(){ return ${expr}; })()`);

const failures = [];
const check = (master, id, ok, detail) => {
  if (!ok) failures.push(`[${master}] ${id}: ${detail}`);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
page.on('pageerror', e => failures.push(`[page] JS error: ${e.message}`));

await page.goto(BASE + '/viewer', { waitUntil: 'domcontentloaded' });

// wait for the master list
await page.waitForFunction(
  () => document.querySelectorAll('#mlist .mcard').length > 0, null, { timeout: 20000 });
const masters = await page.evaluate(() =>
  [...document.querySelectorAll('#mlist .mcard .nm')].map(e => e.textContent.trim()));
console.log(`Found ${masters.length} masters: ${masters.join(', ')}`);

for (const name of masters) {
  console.log(`\n=== ${name} ===`);

  // open the master (back to list first if a previous one is open)
  await page.evaluate(() => { if (typeof backToList === 'function') backToList(); });
  await page.waitForTimeout(300);
  await page.evaluate(nm => {
    const cards = [...document.querySelectorAll('#mlist .mcard')]
      .find(c => c.querySelector('.nm')?.textContent.trim() === nm);
    if (cards) cards.click();
  }, name);
  await page.waitForFunction(
    () => document.querySelector('#crumb')?.textContent.trim().length > 1,
    null, { timeout: 60000 });
  await page.waitForTimeout(800);

  const info = await gv(`({total: (typeof DATA!=='undefined'&&DATA)?DATA.total:-1, msgs:(typeof DATA!=='undefined'&&DATA)?DATA.messages.length:-1})`);
  console.log(`  total: ${typeof info.total === 'number' && info.total >= 0 ? info.total.toLocaleString('vi-VN') : '?'}`);

  // ---- invariant checks over the FULL master via the API (fast + complete)
  const uid = await gv(`(typeof CUR!=='undefined'&&CUR)?CUR.uid:''`);
  const api = await page.evaluate(async u => {
    const r = await fetch('/api/master?uid=' + encodeURIComponent(u) + '&from=&to=');
    return r.json();
  }, uid);
  const msgs = api.messages || [];

  // 1. no JSON dumps
  const jsonRows = msgs.filter(x => (x.text || '').trim().startsWith('{'));
  check(name, 'no-json-dumps', jsonRows.length === 0,
    `${jsonRows.length} rows start with '{' — e.g. ${(jsonRows[0] || {}).text ? String(jsonRows[0].text).slice(0, 80) : ''}`);

  // 2. no '?' senders
  const qRows = msgs.filter(x => (x.sender || '').trim() === '?' || !x.sender);
  check(name, 'no-missing-senders', qRows.length === 0,
    `${qRows.length} rows have empty or '?' sender`);

  // senders sanity: fewer than 5 distinct senders for a 1-1/self chat
  const senders = [...new Set(msgs.map(x => x.sender).filter(Boolean))];
  check(name, 'sender-count-sane', senders.length <= 4,
    `${senders.length} distinct senders: ${senders.slice(0, 6).join(' | ')}`);

  // 3. Zalo-style bubble sides: exactly one "me" sender, all .me bubbles match
  await page.evaluate(() => document.getElementById('tzalo').click());
  await page.waitForTimeout(1200);
  const meOwners = await page.evaluate(() => {
    const out = {};
    document.querySelectorAll('#content .zrow.me').forEach(r => {
      const t = (r.querySelector('.zbubble')?.getAttribute('title') || '').split(' · ')[0];
      out[t] = (out[t] || 0) + 1;
    });
    return out;
  });
  const meNames = Object.keys(meOwners);
  check(name, 'one-me-sender', meNames.length <= 1,
    `multiple senders on the "me" side: ${JSON.stringify(meOwners)}`);
  if (meNames.length === 1) {
    const guessed = await gv(`(typeof ME_GUESS!=='undefined')?ME_GUESS:null`);
    check(name, 'me-guess-matches-bubbles', guessed === meNames[0],
      `ME_GUESS="${guessed}" but bubbles belong to "${meNames[0]}"`);
  }

  // 4. media flags render inline (sample the first flagged rows in list view)
  await page.evaluate(() => document.getElementById('tmsg').click());
  await page.waitForTimeout(600);
  const flagCheck = await gv(`(function(){
    if(typeof DATA==='undefined'||!DATA) return {hasSt:false,hasVo:false};
    const st=DATA.messages.find(x=>x.st), vo=DATA.messages.find(x=>x.vo);
    return {hasSt:!!st, hasVo:!!vo};
  })()`);
  if (flagCheck.hasVo) {
    const audioOk = await page.evaluate(async () => {
      return (function(){ return new Promise(res => {
        const v = DATA.messages.find(x => x.vo);
        const idx = DATA.messages.indexOf(v);
        shown = Math.min(idx + 30, DATA.total); drawMsgs();
        setTimeout(async () => {
          const a = document.querySelector('#content audio.vaud');
          if (!a) return res('no <audio> rendered for a vo row');
          a.load();
          await new Promise(r => setTimeout(r, 3500));
          if (a.error) return res('audio error code ' + a.error.code);
          if (a.readyState === 0) return res('audio never loaded');
          res(true);
        }, 500);
      }); })();
    });
    check(name, 'voice-plays', audioOk === true, String(audioOk));
  }
  if (flagCheck.hasSt) {
    const stOk = await page.evaluate(async () => {
      return (function(){ return new Promise(res => {
        const s = DATA.messages.find(x => x.st);
        const idx = DATA.messages.indexOf(s);
        shown = Math.min(idx + 30, DATA.total); drawMsgs();
        setTimeout(async () => {
          const img = [...document.querySelectorAll('#content img.stk')][0];
          if (!img) return res('no sticker <img> rendered');
          img.loading = 'eager'; img.scrollIntoView({ block: 'center' });
          await new Promise(r => setTimeout(r, 3500));
          if (img.complete && img.naturalWidth === 0) return res('sticker image failed to load');
          res(true);
        }, 500);
      }); })();
    });
    check(name, 'sticker-renders', stOk === true, String(stOk));
  }
  console.log(`  senders: ${senders.join(' | ')}${meNames.length ? `  ·  "me" = ${meNames[0]}` : ''}`);
}

await browser.close();

console.log('\n========================================');
if (failures.length) {
  console.log(`FAIL — ${failures.length} problem(s):`);
  failures.forEach(f => console.log('  ✗ ' + f));
  process.exit(1);
} else {
  console.log('PASS — all masters clean ✔');
  process.exit(0);
}
