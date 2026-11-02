import { createPublicSurface, createPresenterSurface } from "./design-system/components.js";
import { GRAPHIC_TIMING, countWords, holdDuration } from "./design-system/timing.js";

const $ = id => document.getElementById(id);
const publicSamples = {
  question: ["What could a non-coder try with this tomorrow?", "Maya · simulated viewer"],
  reference: ["https://twitchcon-codex.devpost.com/", "Reference · spoken on stream"],
  takeaway: ["A useful AI workflow includes reviewing what changed.", "Takeaway · Producer"],
  result: ["The new build preserved the unfinished example.", "Reported on stream"],
  thread: ["Back to the example we parked.", "Returning to the thread"],
  remark: ["An API lets software components communicate.", "Producer"],
  title: ["Building a useful producer", "Office Hours"],
  context: ["AI for everyday work", "Turning an idea into one practical demonstration"],
  map: ["Canada", "Natural Earth · public domain"],
  captions: ["The useful part is knowing what changed, and whether that helped.", "Spoken captions"],
  quiet: ["", ""],
};
const presenterSamples = {
  editorial: ["Show one complete example.", "Editorial cue"],
  guidance: ["Next: Explore the project", "Producer reference"],
  audience: ["Maya: What could a non-coder try tomorrow?", "Audience cue"],
  recovery: ["Return to the example you parked.", "Return cue"],
  signal: ["Selected audio input ended.", "Measured input failure"],
  quiet: ["", "Quiet"],
};
let surface = null, expiry = null, playing = false, themeAccent = null;
const isPublic = () => $("surface").value === "public";
const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");

function stopPreview() {
  clearTimeout(expiry);
  playing = false;
  $("replay").textContent = "Play timing";
}
function chooseComponent() {
  const rows = isPublic() ? publicSamples : presenterSamples;
  const key = $("component").value;
  $("copy").value = rows[key][0];
  $("attribution").value = rows[key][1];
  $("copy").disabled = key === "quiet" || key === "map";
  render();
}
function chooseSurface() {
  stopPreview();
  const rows = isPublic() ? publicSamples : presenterSamples;
  $("component").replaceChildren(...Object.entries(rows).map(([key, value]) => {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = isPublic() ? ({ context:"Context label", captions:"Spoken captions", map:"Map · Canada", quiet:"Quiet" }[key] || GRAPHIC_TIMING[key].label) : value[1];
    return option;
  }));
  surface?.destroy?.();
  surface?.clear?.();
  $("surfaceRoot").replaceChildren();
  $("surfaceRoot").className = "";
  surface = isPublic()
    ? createPublicSurface($("surfaceRoot"), { align:$("align").value, showProgress:$("hairline").checked })
    : createPresenterSurface($("surfaceRoot"));
  $("publicOptions").hidden = !isPublic();
  $("presenterOptions").hidden = isPublic();
  $("themeEditor").hidden = !isPublic();
  document.querySelector(".background-switch").hidden = !isPublic();
  $("sampleScene").hidden = !isPublic();
  $("guides").disabled = !isPublic();
  $("safeGuides").hidden = !isPublic() || !$("guides").checked;
  $("canvas").dataset.surface = isPublic() ? "public" : "presenter";
  chooseComponent();
}
function render() {
  stopPreview();
  const kind = $("component").value;
  const line = $("copy").value.trim();
  const quiet = kind === "quiet";
  const words = countWords(line);
  $("canvas").style.setProperty("--producer-radius", $("radius").value + "px");
  for (const token of ["question", "reference", "thread", "note", "active"]) {
    if (themeAccent) $("canvas").style.setProperty("--producer-" + token, themeAccent);
    else $("canvas").style.removeProperty("--producer-" + token);
  }
  $("radiusValue").value = $("radius").value + " px";
  $("canvas").style.setProperty("--producer-card-font", $("typeSize").value);
  $("typeSizeValue").value = $("typeSize").value + " px";
  if (!isPublic()) {
    surface.render({ line: quiet ? "" : line, kind, presence:$("presence").value });
    $("componentSummary").textContent = "Presenter · one cue and a steady input indicator";
    $("textStatus").textContent = "";
    $("timingStatus").textContent = kind === "guidance"
      ? "A steady private reference chosen by Producer. It can replace or clear this as the show develops."
      : "The live cue policy owns timing and relevance.";
    $("usageCode").textContent = `import { createPresenterSurface } from './design-system/components.js';\n\nconst view = createPresenterSurface(mount);\nview.render(${JSON.stringify({line:quiet ? "" : line, kind, presence:$("presence").value},null,2)});\n// On stop or withdrawal:\nview.clear();`;
    return;
  }
  surface.setAlign($("align").value);
  const context = kind === "context"
    ? { subject:line, purpose:$("attribution").value }
    : $("withContext").checked ? { subject:"AI for everyday work", purpose:"Turning an idea into one practical demonstration" } : null;
  const caption = kind === "captions" ? {text:line} : $("withCaptions").checked ? {text:"Let’s try one small thing and check what actually happened."} : null;
  const graphic = ["context", "captions", "map", "quiet"].includes(kind) ? null : { kind, line, attribution:$("attribution").value, ...(kind === "question" ? {awaiting_response:true} : {}) };
  const tooLong = graphic && (line.length > 180 || words > 18);
  const badCaption = kind === "captions" && line.length > 170;
  const badContext = kind === "context" && (line.length > 80 || $("attribution").value.length > 120);
  const state = {
    context:quiet || badContext ? null : context,
    graphic:tooLong ? null : graphic,
    caption:quiet || badCaption ? null : caption,
    visual:kind === "map" ? {kind:"map", asset_id:"canada"} : null,
  };
  surface.render(state);
  const omitted = [surface.elements.card, surface.elements.context, surface.elements.captions].some(n => n.dataset.omitted);
  $("textStatus").textContent = tooLong ? "Use at most 18 words and 180 characters. The original is omitted rather than clipped." : badCaption ? "Use up to 170 characters for this caption preview." : badContext ? "Context uses up to 80 characters plus 120 characters of purpose." : omitted ? "This text does not fit this component at the chosen size; shorten it to display it." : quiet ? "The programme has the screen." : kind === "map" ? "A bundled country outline. The asset supplies its own title and attribution." : `${words} words · ${line.length} characters`;
  const timing = GRAPHIC_TIMING[kind];
  $("componentSummary").textContent = quiet ? "On air · quiet" : `On air · ${timing?.label || ({context:"Context",map:"Map · Canada",captions:"Captions"}[kind])} · ${$("resolution").selectedOptions[0].textContent}`;
  $("timingStatus").textContent = kind === "question" ? "Default preview holds the question. Producer may replace or clear it." : timing ? `${(holdDuration(kind,line)/1000).toFixed(1)} s hold · ${timing.enterMs} ms in · ${timing.exitMs} ms out · ${timing.gapMs/1000} s suggested gap` : "Clears with the supplied source state.";
  $("usageCode").textContent = `import { createPublicSurface } from './design-system/components.js';\n\nconst view = createPublicSurface(mount, { align: '${$("align").value}' });\nview.render(${JSON.stringify(state,null,2)});\n// Real expiry is provided by the producer, in epoch seconds.\n// On stop, deletion or connection failure:\nview.clear();`;
  surface.previewState = state;
}
function play() {
  if (playing) {
    stopPreview();
    surface.clear();
    $("timingStatus").textContent = "Preview stopped.";
    return;
  }
  render();
  const kind = $("component").value;
  if (isPublic() && kind === "question") {
    $("timingStatus").textContent = "Held in this preview. Clear simulates Producer withdrawing it.";
    return;
  }
  if (!isPublic() || !surface.previewState?.graphic) {
    $("timingStatus").textContent = "This state has no automatic preview expiry. Use Clear.";
    return;
  }
  // Recreate only the catalog mount to apply the optional progress visualization.
  const state = surface.previewState;
  surface.destroy();
  surface = createPublicSurface($("surfaceRoot"), { align:$("align").value, showProgress:$("hairline").checked });
  const duration = holdDuration(kind,$("copy").value) / 1000;
  surface.render({...state, graphic:{...state.graphic, id:"preview-"+Date.now(), duration, until:Date.now()/1000+duration}});
  playing = true;
  $("replay").textContent = "Stop preview";
  $("timingStatus").textContent = `Playing · ${duration.toFixed(1)} s preview hold`;
  expiry = setTimeout(() => {
    playing = false;
    $("replay").textContent = "Play timing";
    $("timingStatus").textContent = "Card cleared. Context and captions keep their own place.";
  }, duration*1000+50);
}
function size() {
  const width = Number($("resolution").value);
  const height = width*9/16;
  $("canvas").style.width = width+"px";
  $("canvas").style.height = height+"px";
  $("canvas").style.transform = `scale(${$("viewport").clientWidth/width})`;
}
async function copyValue(value) {
  try {
    await navigator.clipboard.writeText(value);
    $("copyStatus").textContent = "Copied.";
  } catch { $("copyStatus").textContent = "Clipboard unavailable. Select the example text to copy it."; }
}
$("surface").onchange = chooseSurface;
$("component").onchange = chooseComponent;
for (const name of ["copy", "attribution", "radius", "typeSize"]) $(name).oninput = render;
$("accent").oninput = () => {themeAccent = $("accent").value;render();};
for (const name of ["align", "withContext", "withCaptions", "presence"]) $(name).onchange = render;
$("hairline").onchange = render;
$("replay").onclick = play;
$("clear").onclick = () => {stopPreview();surface.clear();$("timingStatus").textContent="Cleared. Edit a field to restore, or play the timing preview.";};
$("guides").onchange = () => {$("safeGuides").hidden=!isPublic()||!$("guides").checked;};
$("resolution").onchange = () => {size();render();};
for (const [button, background] of [["darkScene","dark"],["lightScene","light"]]) $(button).onclick = () => {
  $("canvas").dataset.background = background;
  $("darkScene").setAttribute("aria-pressed",String(background==="dark"));
  $("lightScene").setAttribute("aria-pressed",String(background==="light"));
};
$("copyTheme").onclick = () => {
  const accents = themeAccent ? ["question", "reference", "thread", "note", "active"].map(token => `  --producer-${token}: ${themeAccent};\n`).join("") : "";
  void copyValue(`:root {\n${accents}  --producer-radius: ${$("radius").value}px;\n  --producer-card-font: ${$("typeSize").value};\n}`);
};
$("copyUsage").onclick = () => copyValue($("usageCode").textContent);
new ResizeObserver(size).observe($("viewport"));
function motionStatus() {$("motionStatus").textContent=motionPreference.matches ? "Reduced motion · no decorative animation" : "Uses your device’s motion preference";}
motionPreference.addEventListener("change",motionStatus);
motionStatus();size();chooseSurface();
