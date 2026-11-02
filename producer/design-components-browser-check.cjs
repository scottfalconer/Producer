const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
 const browser = await chromium.launch({channel:'chrome', headless:true});
 const page = await browser.newPage(); const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://producer.test/**',r=>{
  const p = new URL(r.request().url()).pathname;
  if (p === '/') return r.fulfill({contentType:'text/html',body:'<!doctype html><html><head><link rel="stylesheet" href="/design-system/tokens.css"><link rel="stylesheet" href="/design-system/components.css"><style>html,body{margin:0;width:100%;height:100%}#surface{width:100%;height:100%}</style></head><body><div id="surface"></div></body></html>'});
  const file=path.join(process.cwd(),'producer/static',p); return r.fulfill({body:fs.readFileSync(file),contentType:p.endsWith('.js')?'text/javascript':'text/css'});
 });
 const state={context:{subject:'Building Producer',purpose:'Helping the audience follow the conversation'},graphic:{id:'q1',kind:'question',line:'How can I tell whether the agent did the work or only described it?',attribution:'Maya · simulated viewer'},caption:{text:'The point is to make it easier for the presenter to stay with the audience while the producer handles the details.'}};
 const geometry=[];
 try {
  for(const [width,height] of [[1920,1080],[1280,720]]) {
   await page.setViewportSize({width,height}); await page.goto('http://producer.test/');
   await page.evaluate(async state=>{const{createPublicSurface}=await import('/design-system/components.js');window.surface=createPublicSurface(document.querySelector('#surface'),{motion:false,showProgress:true});window.surface.render(state);},state);
   await page.waitForTimeout(50);
   let g=await page.evaluate(()=>{const e=window.surface.elements;return{width:innerWidth,height:innerHeight,hidden:[e.card.hidden,e.context.hidden,e.captions.hidden],card:e.card.getBoundingClientRect().toJSON(),context:e.context.getBoundingClientRect().toJSON(),captions:e.captions.getBoundingClientRect().toJSON(),progress:e.progress.hidden};});
   assert.deepEqual(g.hidden,[false,false,false]); assert(g.card.bottom<g.captions.top); assert(g.card.top>=height*.70); assert(g.context.bottom<=height*.24); assert(g.progress); assert(g.captions.bottom <= height*.95+1); geometry.push(g);
   await page.evaluate(s=>{window.surface.setAlign('right');window.surface.render(s);},state);
   let right=await page.evaluate(()=>window.surface.elements.card.getBoundingClientRect().toJSON());assert(Math.abs(right.right-width*.95)<1);
   await page.evaluate(()=>window.surface.clear());assert(await page.evaluate(()=>Object.values(window.surface.elements).filter(e=>e.classList?.contains('producer-card')||e.classList?.contains('producer-context')||e.classList?.contains('producer-captions')).every(e=>e.hidden)));
  }
  await page.evaluate(s=>{window.surface.destroy();const root=document.querySelector('#surface');root.replaceChildren();window.surface=null;},state);
  await page.evaluate(async()=>{const{createPublicSurface}=await import('/design-system/components.js');window.surface=createPublicSurface(document.querySelector('#surface'),{motion:true,showProgress:true});window.surface.render({graphic:{id:'expiry',kind:'question',line:'Why use this approach?',duration:.7,until:Date.now()/1000+.7}});});
  assert.equal(await page.evaluate(()=>window.surface.elements.progress.hidden),false);
  await page.waitForTimeout(450); assert.equal(await page.evaluate(()=>window.surface.elements.card.dataset.exit),'true');
  await page.evaluate(()=>window.surface.clear());assert.equal(await page.evaluate(()=>window.surface.elements.card.hidden),true);
  await page.evaluate(()=>{
   window.renewalGraphic={id:'renewal',kind:'takeaway',line:'Keep a useful idea visible.',attribution:'Producer',duration:1,until:Date.now()/1000+1};
   window.surface.render({graphic:window.renewalGraphic});
  });
  await page.waitForTimeout(150);
  await page.evaluate(()=>{
   window.renewalChanges=[];
   window.renewalObserver=new MutationObserver(rows=>window.renewalChanges.push(...rows.filter(r=>r.attributeName==='data-enter').map(r=>r.attributeName)));
   window.renewalObserver.observe(window.surface.elements.card,{attributes:true});
   window.surface.render({graphic:{...window.renewalGraphic,duration:3,until:Date.now()/1000+3}});
  });
  await page.waitForTimeout(1100);
  assert.equal(await page.evaluate(()=>window.surface.elements.card.hidden),false,'A renewed deadline must replace the original expiry');
  assert.deepEqual(await page.evaluate(()=>window.renewalChanges),[],'Renewing the same card must not restart its entrance');
  await page.evaluate(()=>{window.renewalObserver.disconnect();window.surface.render({graphic:null});});
  assert.equal(await page.locator('.producer-card').isVisible(),false,'A model clear takes effect without a minimum reading gate');
  await page.evaluate(()=>{
   window.timedQuestion={id:'timed-question',kind:'question',line:'Still unanswered, but time to move on.',awaiting_response:true,editorial_timing:true,duration:.3,until:Date.now()/1000+.3};
   window.surface.render({graphic:window.timedQuestion});
  });
  await page.waitForTimeout(400);
  assert.equal(await page.locator('.producer-card').isVisible(),false,'Explicit model timing can retire an unanswered question');
  assert.equal(await page.evaluate(()=>window.timedQuestion.awaiting_response),true,'Expiry must not claim the question was answered');
  await page.evaluate(()=>window.surface.render({graphic:{id:'legacy-question',kind:'question',line:'A question held by default.',awaiting_response:true,duration:.1,until:Date.now()/1000+.1}}));
  await page.waitForTimeout(200);
  assert.equal(await page.locator('.producer-card').isVisible(),true,'Legacy held questions without explicit editorial timing remain untimed');
  await page.evaluate(()=>window.surface.clear());
  await page.evaluate(()=>window.surface.render({graphic:{kind:'question',line:'<img src=x onerror=alert(1)>'}}));
  assert.equal(await page.locator('.producer-card img').count(),0);assert.equal(await page.locator('.producer-card__line').textContent(),'<img src=x onerror=alert(1)>');
  await page.evaluate(()=>window.surface.render({graphic:{kind:'question',line:'x'.repeat(181)},caption:{text:'x'.repeat(241)}}));assert.equal(await page.evaluate(()=>window.surface.elements.card.hidden&&window.surface.elements.captions.hidden),true);
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.evaluate(()=>window.surface.render({graphic:{id:'reduce',kind:'question',line:'What changes?',duration:2,until:Date.now()/1000+2}}));
  assert.equal(await page.locator('.producer-card').evaluate(e=>getComputedStyle(e).animationName),'none');assert.equal(await page.locator('.producer-card__progress').evaluate(e=>getComputedStyle(e).display),'none');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({geometry,immediate_clear:true,explicit_expiry_exit:true,renewal_reschedules_without_entry_animation:true,explicit_model_timing_retires_unanswered_question:true,untimed_legacy_question_supported:true,no_invented_progress:true,source_text_not_html:true,oversized_source_omitted:true,reduced_motion:true,page_errors:errors},null,2));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
