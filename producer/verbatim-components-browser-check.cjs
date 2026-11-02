/* Actual shared surfaces: whole source comments or omission, and optional topic. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({channel: 'chrome', headless: true});
  const page = await browser.newPage({viewport: {width: 1280, height: 720}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://producer.test') return route.abort();
    if (url.pathname === '/') return route.fulfill({contentType:'text/html', body:`
      <!doctype html><html><head>
      <link rel="stylesheet" href="/design-system/tokens.css">
      <link rel="stylesheet" href="/design-system/components.css">
      <style>html,body{margin:0;width:100%;height:100%}#surface{width:100%;height:100%}</style>
      </head><body><div id="surface"></div></body></html>`});
    if (!/^\/design-system\/(components\.js|timing\.js|tokens\.css|components\.css)$/.test(url.pathname)) return route.abort();
    return route.fulfill({body: fs.readFileSync(path.join(__dirname, 'static', url.pathname)),
      contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css'});
  });
  const comment = "  How'd you attach it?\nCan it come off again? 🎃  ";
  const tall = Array(70).fill('a').join('\n');
  try {
    await page.goto('http://producer.test/');
    await page.evaluate(async comment => {
      const {createPublicSurface} = await import('/design-system/components.js');
      window.surface = createPublicSurface(document.getElementById('surface'), {motion:false});
      window.surface.render({graphic:{id:'original',kind:'question',line:comment,attribution:'Maya · simulated viewer'}});
    }, comment);
    assert.equal(await page.locator('.producer-card__line').textContent(), comment);
    assert.equal(await page.locator('.producer-card__line').evaluate(e => getComputedStyle(e).whiteSpace), 'pre-wrap');
    assert.equal(await page.locator('.producer-card').isVisible(), true);
    await page.evaluate(() => window.surface.render({graphic:{kind:'question',line:'<b>Source words</b>'}}));
    assert.equal(await page.locator('.producer-card b').count(), 0);
    assert.equal(await page.locator('.producer-card__line').textContent(), '<b>Source words</b>');
    await page.evaluate(tall => window.surface.render({graphic:{kind:'question',line:tall}}), tall);
    assert.equal(await page.locator('.producer-card').isVisible(), false, 'Omit a source quote that overflows instead of clipping its lines');
    assert.equal(await page.locator('.producer-card__line').textContent(), tall);
    await page.evaluate(() => window.surface.render({graphic:{kind:'question',line:'x'.repeat(181)}}));
    assert.equal(await page.locator('.producer-card').isVisible(), false);

    await page.evaluate(() => window.surface.render({context:{subject:'Making Halloween decorations',purpose:''}}));
    assert.equal(await page.locator('.producer-context').isVisible(), true);
    await page.evaluate(() => window.surface.render({context:null,graphic:{kind:'remark',line:'A useful topical explanation.'}}));
    assert.equal(await page.locator('.producer-context').isVisible(), false);
    assert.equal(await page.locator('.producer-card').isVisible(), true);
    await page.evaluate(() => window.surface.render({context:{subject:'Testing the cauldron lighting'},graphic:null}));
    assert.equal(await page.locator('.producer-context').isVisible(), true);
    assert.equal(await page.locator('.producer-context__subject').textContent(), 'Testing the cauldron lighting');
    assert.equal(await page.locator('.producer-card').isVisible(), false);

    await page.evaluate(async comment => {
      window.surface.destroy();
      const root = document.getElementById('surface'); root.replaceChildren();
      const {createPresenterSurface} = await import('/design-system/components.js');
      window.surface = createPresenterSurface(root);
      window.surface.render({line:comment,kind:'question',presence:'active'});
    }, comment);
    assert.equal(await page.locator('.producer-cue__line').textContent(), comment);
    assert.equal(await page.locator('.producer-cue__line').evaluate(e => getComputedStyle(e).whiteSpace), 'pre-wrap');
    assert.equal(await page.locator('.producer-cue').isVisible(), true);
    await page.evaluate(tall => window.surface.render({line:tall,kind:'question',presence:'active'}), tall);
    assert.equal(await page.locator('.producer-cue').isVisible(), false, 'Presenter also omits a quote that cannot fit');
    assert.equal(await page.locator('.producer-cue__line').textContent(), tall);
    await page.evaluate(() => window.surface.render({line:'x'.repeat(181),kind:'question',presence:'active'}));
    assert.equal(await page.locator('.producer-cue').isVisible(), false);
    await page.evaluate(comment => window.surface.render({line:comment,kind:'question',presence:'active'}), comment);
    assert.equal(await page.locator('.producer-cue').isVisible(), true, 'A fitting later comment is not stuck hidden');
    await page.evaluate(() => window.surface.destroy());
    assert.equal(await page.locator('.producer-cue').isVisible(), false);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({verbatim_public_and_presenter:true,whitespace_and_punctuation_preserved:true,
      source_text_not_html:true,whole_overflow_omitted:true,topic_can_clear_and_return:true,
      later_fitting_comment_recovers:true,page_errors:errors,api_calls:0},null,2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
