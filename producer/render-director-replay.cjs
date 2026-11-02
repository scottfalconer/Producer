#!/usr/bin/env node
/* API-free playback of captured Producer screen snapshots using production components. */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const os = require('node:os');
const { spawn } = require('node:child_process');
const { chromium } = require('playwright');

const STATIC = path.join(__dirname, 'static');
const WIDTH = 2560, HEIGHT = 760, PANEL_WIDTH = 1280, HEADER = 40;
function readBundle(directory, options = {}) {
  directory = path.resolve(directory);
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  const duration = Number(manifest.duration_s);
  const epoch = Number(manifest.wall_epoch_s);
  if (!(duration > 0 && duration <= 3600) || !Number.isFinite(epoch)) throw Error('Expected duration_s and wall_epoch_s in manifest.json');
  const source = manifest.source || {};
  const sourcePath = path.resolve(directory, source.path || '');
  if (!source.path || !fs.statSync(sourcePath).isFile()) throw Error('Missing source.path media file');
  const scenesPath = path.resolve(directory, options.scenes || manifest.scenes_file || 'scenes.jsonl');
  const rows = fs.readFileSync(scenesPath, 'utf8').split('\n').filter(Boolean).map(JSON.parse);
  let previous = -1;
  const scenes = rows.map(row => {
    const wall = Number(row.wall_s);
    if (!Number.isFinite(wall) || wall < previous || wall < 0 || wall > duration + 2) throw Error('Scene timestamps must be ordered nonnegative wall_s');
    previous = wall;
    const value = row.scene || row;
    return { wall_s: wall, source_s: Number(row.source_s), confidence: value.confidence || null,
      public: { graphic: value.graphic || null, context: value.context || null, visual: value.visual || null, caption: value.caption || null },
      presence: value.presence || 'active' };
  });
  const assets = {};
  for (const [id, value] of Object.entries(manifest.assets || {})) {
    if (!/^img-[0-9a-f]{16}$/.test(id)) throw Error('Invalid asset ID');
    const file = typeof value === 'string' ? value : value.path;
    assets[id] = path.resolve(directory, file);
    if (!fs.statSync(assets[id]).isFile()) throw Error('Missing selected image');
  }
  let chatEventsPath = null, chatStyle = null;
  if (manifest.audience_input === 'screen') {
    // Historical optical tests predate style metadata. Their source images
    // used the original fixed panel, so never repaint them with today's default.
    chatStyle = manifest.chat_overlay?.style === undefined ? 'panel-v1' : manifest.chat_overlay.style;
    if (!['panel-v1', 'compact-v2', 'stream-v3'].includes(chatStyle)) throw Error('Unknown recorded chat-overlay style');
    const selected = manifest.chat_events;
    const file = typeof selected === 'string' ? selected : selected?.snapshot || selected?.path || 'mock-events.json';
    chatEventsPath = path.resolve(directory, file);
    if (!fs.statSync(chatEventsPath).isFile()) throw Error('Missing simulated-chat timeline');
  }
  return { directory, manifest, duration, epoch, sourcePath, sourceStart: Number(source.start_s || 0), scenesPath, scenes, assets, chatEventsPath, chatStyle };
}
const HTML = `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/design-system/tokens.css"><link rel="stylesheet" href="/design-system/components.css"><style>
html,body{margin:0;width:2560px;height:760px;overflow:hidden;background:transparent}
header{position:absolute;top:0;height:40px;background:#10151d;color:#a6b4c5;font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:.06em;display:flex;align-items:center;box-sizing:border-box;padding:0 22px;width:1280px}
#viewerHeader{left:0}#presenterHeader{left:1280px;border-left:1px solid #293342}header strong{color:#f1f5f9;font-size:13px}header span{margin-left:auto;letter-spacing:0}
#public,#presenter{position:absolute;top:40px;left:0;width:1280px;height:720px;overflow:hidden;box-sizing:border-box}
#presenter{left:1280px;min-height:0;border-left:1px solid #293342}
#simulatedChat{position:absolute;left:0;top:40px;width:1280px;height:720px;pointer-events:none}
</style></head><body><header id="viewerHeader"><strong>AUDIENCE</strong><span>Recorded rehearsal · simulated chat</span></header><header id="presenterHeader"><strong>PRESENTER · confidence monitor</strong><span id="clock">00:00</span></header><!--SIMULATED_CHAT--><div id="public"></div><div id="presenter"></div><script type="module">
import {createPublicSurface,createPresenterSurface} from '/design-system/components.js';
window.publicSurface=createPublicSurface(document.querySelector('#public'));
window.presenterSurface=createPresenterSurface(document.querySelector('#presenter'));
window.renderScene=async function(scene){
  publicSurface.render(scene.public||{});
  presenterSurface.render({...scene.confidence,presence:scene.presence||'active'});
  await Promise.all([...document.images].map(i=>i.complete?Promise.resolve():new Promise(r=>{i.addEventListener('load',r,{once:true});i.addEventListener('error',r,{once:true})})));
};
const animationStarts=new WeakMap();
const chatTimeline=__CHAT_TIMELINE__;
let chatIndex=-1;
async function renderChat(wall){
  const image=document.querySelector('#simulatedChat');
  if(!image)return;
  const sourceTime=wall+__CHAT_SOURCE_START__;
  let selected=0;
  for(let i=0;i<chatTimeline.length&&chatTimeline[i]<=sourceTime;i++)selected=i;
  if(selected===chatIndex)return;
  await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(Error('Simulated chat image failed'));image.src='/simulated-chat.png?index='+selected;});
  chatIndex=selected;
}
window.sampleScene=async function(wall){
  await renderChat(wall);
  document.querySelector('#clock').textContent=String(Math.floor(wall/60)).padStart(2,'0')+':'+String(Math.floor(wall%60)).padStart(2,'0');
  const animationKeys=[];
  for(const a of document.getAnimations()){
    if(!animationStarts.has(a)){animationStarts.set(a,wall*1000);a.pause();}
    const elapsed=Math.max(0,wall*1000-animationStarts.get(a));
    const timing=a.effect.getComputedTiming();
    const end=timing.endTime;
    a.currentTime=Math.min(elapsed,Number.isFinite(end)?end:elapsed);
    animationKeys.push(a.animationName+':'+Math.min(elapsed,Number.isFinite(end)?end:elapsed));
  }
  return document.body.innerHTML+'\\n'+animationKeys.join('|');
};
window.ready=true;
</script></body></html>`;
function mime(file) { return ({'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'})[path.extname(file)] || 'application/octet-stream'; }
async function startReplayServer(bundle) {
  let chat = null;
  if (bundle.chatEventsPath) {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'producer-chat-render-'));
    const output = path.join(temporary, 'frames');
    try {
      await command('python3', [path.join(__dirname, 'screen_chat_overlay.py'), '--events', bundle.chatEventsPath,
        '--output', output, '--duration', String(bundle.sourceStart + bundle.duration), '--style', bundle.chatStyle], path.join(temporary, 'prepare.log'));
      chat = { temporary, output, index: JSON.parse(fs.readFileSync(path.join(output, 'index.json'), 'utf8')) };
    } catch (error) { fs.rmSync(temporary, { recursive: true, force: true }); throw error; }
  }
  const html = HTML.replace('<!--SIMULATED_CHAT-->', chat ? '<img id="simulatedChat" alt="Simulated chat" src="/simulated-chat.png?index=0">' : '')
    .replace('__CHAT_TIMELINE__', JSON.stringify(chat ? chat.index.map(row => row.at) : []))
    .replace('__CHAT_SOURCE_START__', JSON.stringify(bundle.sourceStart));
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/') { res.setHeader('Content-Type','text/html'); res.end(html); return; }
    if (url.pathname === '/simulated-chat.png' && chat) {
      const index = Number(url.searchParams.get('index'));
      const frame = Number.isInteger(index) && index >= 0 ? chat.index[index] : null;
      if (!frame) { res.writeHead(404); res.end(); return; }
      res.setHeader('Content-Type','image/png');
      fs.createReadStream(path.join(chat.output, frame.file)).pipe(res); return;
    }
    let file;
    if (url.pathname.startsWith('/show-assets/')) file = bundle.assets[url.pathname.slice(13)];
    else if (/^\/(design-system|visuals)\/[a-zA-Z0-9_.-]+$/.test(url.pathname)) file = path.join(STATIC,url.pathname);
    if (!file || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type',mime(file)); fs.createReadStream(file).pipe(res);
  });
  if (chat) server.once('close', () => fs.rmSync(chat.temporary, { recursive: true, force: true }));
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  return { server, url:`http://127.0.0.1:${server.address().port}` };
}
async function command(binary,args,logFile) {
  return new Promise((resolve,reject)=>{
    const log=fs.openSync(logFile,'w',0o600);
    const p=spawn(binary,args,{stdio:['ignore',log,log]});
    p.once('error',e=>{fs.closeSync(log);reject(e)});
    p.once('exit',code=>{fs.closeSync(log);code===0?resolve():reject(Error(`${binary} exited ${code}; see ${logFile}`))});
  });
}
async function renderBundle(directory,options={}) {
  const bundle=readBundle(directory, options), fps=Number(options.fps||10);
  if (!Number.isInteger(fps)||fps<1||fps>30) throw Error('fps must be an integer from 1 to 30');
  const output=path.resolve(options.output||path.join(bundle.directory,'side-by-side.mp4'));
  if(fs.existsSync(output)) throw Error('Output exists; choose a new path to preserve the previous render');
  const frames=path.join(bundle.directory,'render-frames-'+Date.now());fs.mkdirSync(frames,{mode:0o700});
  const {server,url}=await startReplayServer(bundle);
  const browser=await chromium.launch({channel:'chrome',headless:true});
  const page=await browser.newPage({viewport:{width:WIDTH,height:HEIGHT},deviceScaleFactor:1});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let captured=0,sceneIndex=0,previousSignature=null,previousFrame=null;
  const count=Math.ceil(bundle.duration*fps), renderStarted=Date.now();
  try {
    // Freeze before loading the components. Rendering/encoding wall time must
    // never consume a card's captured on-air lifetime.
    await page.clock.install({time:new Date(bundle.epoch*1000-1000)});
    await page.clock.pauseAt(new Date(bundle.epoch*1000));
    await page.goto(url);await page.waitForFunction(()=>window.ready);
    await page.evaluate(()=>window.renderScene({}));
    for(let index=0;index<count;index++){
      const wall=index/fps;
      if(index) await page.clock.fastForward(1000/fps);
      while(sceneIndex<bundle.scenes.length&&bundle.scenes[sceneIndex].wall_s<=wall+1e-8){
        await page.evaluate(scene=>window.renderScene(scene),bundle.scenes[sceneIndex++]);
      }
      const signature=await page.evaluate(wall=>window.sampleScene(wall),wall);
      const frame=path.join(frames,String(index).padStart(6,'0')+'.png');
      if(signature===previousSignature){fs.linkSync(previousFrame,frame);}
      else{await page.screenshot({path:frame,omitBackground:true,animations:'allow'});captured++;previousFrame=frame;previousSignature=signature;}
      if(index===0||index% (fps*30)===0) console.log(JSON.stringify({render_s:wall,total_s:bundle.duration,screenshots:captured}));
    }
    if(errors.length) throw Error('Replay page errors: '+errors.join('; '));
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
  const ffmpegLog=path.join(bundle.directory,'render-ffmpeg.log');
  await command('ffmpeg',['-hide_banner','-nostdin','-n','-ss',String(bundle.sourceStart),'-t',String(bundle.duration),'-i',bundle.sourcePath,'-framerate',String(fps),'-i',path.join(frames,'%06d.png'),'-filter_complex',
    `[0:v]setpts=PTS-STARTPTS,scale=${PANEL_WIDTH}:720:force_original_aspect_ratio=decrease,pad=${PANEL_WIDTH}:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1[show];color=c=black:s=${WIDTH}x${HEIGHT}:r=30:d=${bundle.duration}[canvas];[canvas][show]overlay=x=0:y=${HEADER}:shortest=1[under];[under][1:v]overlay=x=0:y=0:shortest=0:eof_action=repeat,format=yuv420p[out]`,
    '-map','[out]','-map','0:a:0?','-t',String(bundle.duration),'-r','30','-c:v','libx264','-preset','veryfast','-crf','19','-c:a','aac','-b:a','160k','-movflags','+faststart',output],ffmpegLog);
  const report={output,seconds:bundle.duration,source:bundle.sourcePath,source_start_s:bundle.sourceStart,scenes_file:bundle.scenesPath,width:WIDTH,height:HEIGHT,scene_snapshots:bundle.scenes.length,sampled_frames:count,distinct_screenshots:captured,render_fps:fps,video_fps:30,frames,seconds_to_render:(Date.now()-renderStarted)/1000,page_errors:errors,api_calls:0,renderer:'Production createPublicSurface/createPresenterSurface with captured states; source wall-clock playback, sampled animation at render_fps'};
  if(bundle.chatEventsPath) report.simulated_chat={enabled:true,events_file:bundle.chatEventsPath,painter:'screen_chat_overlay.py',style:bundle.chatStyle,source_clock:true};
  fs.writeFileSync(path.join(bundle.directory,'render-report.json'),JSON.stringify(report,null,2)+'\n',{mode:0o600});
  console.log(JSON.stringify(report,null,2));return report;
}
if(require.main===module){
  const args=process.argv.slice(2),options={};
  for(let i=0;i<args.length;i+=2){if(!['--bundle','--output','--fps','--scenes'].includes(args[i])||!args[i+1])throw Error('Usage: node producer/render-director-replay.cjs --bundle /run [--scenes /replayed-scenes.jsonl] [--output /video.mp4] [--fps 10]');options[args[i].slice(2)]=args[i+1];}
  if(!options.bundle)throw Error('--bundle is required');
  renderBundle(options.bundle,options).catch(e=>{console.error(e);process.exitCode=1;});
}
module.exports={readBundle,startReplayServer,renderBundle,WIDTH,HEIGHT};
