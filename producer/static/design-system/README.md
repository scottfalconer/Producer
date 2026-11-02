# Producer component library

Open the local [catalog](http://127.0.0.1:8786/design). It uses the same rendering code as the on-air overlay and private presenter monitor. It runs on authored examples with no capture, API calls or Twitch connection.

## Change a component

1. Choose **On air** or **Presenter**, then the component/state.
2. Edit its text. On-air previews support alignment, captions/context, light/dark programme backgrounds, safe guides and 1080p/720p reference canvases.
3. **Play timing** demonstrates expiry; **Clear** withdraws everything immediately. The optional hairline is off by default.
4. Under **Theme tokens**, adjust the public card's accent, radius and type size. **Copy theme CSS** returns overrides; preview edits are not silently applied to a live show. Apply overrides in `tokens.css` to use them throughout Producer.
5. **Use this component** provides a copyable JavaScript example.

The presenter preview has one cue plus a small steady input indicator. The active Realtime director chooses the private line directly, including an opening from the notes, a useful question, a return anchor or a deliberate quiet state. Local rendering does not substitute a question or notes fallback for that choice. The current prompt favors one concrete, directly usable thought, usually roughly 3–8 words, with longer accurate questions/details when needed. Stable wording and natural opportunities for change are editorial defaults; they are not local word limits, dwell timers or speech locks. Quiet has only the indicator. The catalog's **Producer reference** is an authored `kind: 'guidance'` example in the same slot; it does not imply a separate live rail. Editing controls live outside this surface. No checklist, completion marks, tangent timer, rundown, acknowledgements or added presenter navigation are part of the library's monitor.

Questions with `awaiting_response: true` remain until the authoritative public state changes, unless `editorial_timing: true` supplies an explicit model-chosen `until` deadline. Timing out does not claim the question was answered. Producer may keep, replace or clear a question using its judgment; waiting for an answer is a behavioral guideline, not a renderer lock. The catalog uses the held-question default; **Clear** simulates Producer withdrawing it. Subtitles are off until explicitly enabled. A `visual: {kind: 'map', asset_id: 'canada'}` uses the bundled country outline in its own slot; arbitrary source URLs are rejected.

The active passive showrunner uses `visual: {kind: 'asset', asset_id: 'img-0123456789abcdef', src: '/show-assets/img-0123456789abcdef', title: 'Workflow', alt: 'Workflow', until: unixSeconds}`. The exact hash route must match; arbitrary paths and remote URLs are rejected. The server supplies these values from its pinned show-folder catalog. Images occupy their own slot above the question, expire at the server deadline, and clear on Stop or load failure. Filenames remain accessibility metadata, without an extra on-air caption. The bundled-map example above is retained for historical fixtures; spoken commands are shelved.

## Source map

| File | Owns |
| --- | --- |
| `tokens.css` | Semantic colors, type, spacing, safe insets, card dimensions and motion defaults |
| `components.css` | Scoped public and presenter components, responsive geometry and reduced motion |
| `components.js` | Text-safe rendering, in-place binding, omission of oversized originals, authoritative expiry and immediate withdrawal |
| `timing.js` | Editable preview duration/entrance/exit/gap rules and `holdDuration` |

There are no external fonts, packages or image assets. The renderer is plain browser JavaScript and CSS. Setup/review styling remains in `room.css`; this is a focused library for live surfaces, not a general-purpose UI framework.

## Public surface

The viewer overlay has two independent layers: a steady newcomer recap and one occasional editorial card. In the active Realtime path the recap stays until Producer changes/clears it, output is disabled, or the show/source stops. Card expiry does not remove the recap. Older compatibility sessions retain their timed context policy. Context text wraps into up to two lines each for its headline and summary; overflow is omitted rather than silently clipped. Producer's prompt chooses the wording and when another card earns attention.

At 1080p the topic headline is 30px, summary 24px, card text 30px and attribution 18px; the 720p equivalents are 20/16/20/12px. These are editable reference-frame defaults. The context occupies the upper-left band, an optional image uses a separate middle region, and the card occupies the lower left with space for captions and the authored lower-right camera area. The existing alignment option still supports moving text to the other side. The layout cannot know every streamer's custom camera/scene placement.

Useful background, definitions, takeaways, references, reported outcomes and available audience questions use the existing card types. Model recall is not a verification service. Unsupported trivia can stay off screen; no forced fact cadence or ticker was added. Private production instructions and research status stay backstage unless a correction itself matters to viewers. Subtitles remain a separate explicit choice.

Load `tokens.css` followed by `components.css` once on the page. The mount needs an explicit size; on-air canvases use a 16:9 frame.

```html
<link rel="stylesheet" href="/design-system/tokens.css" />
<link rel="stylesheet" href="/design-system/components.css" />
```

```js
import { createPublicSurface } from './design-system/components.js';

const view = createPublicSurface(mount, {
  align: 'left',       // or right; the same slot is reused
  motion: true,       // operating-system reduced motion still wins
  showProgress: false,
});

view.render({
  context: { subject: 'Office Hours', purpose: 'A practical AI example' },
  graphic: {
    kind: 'question',
    line: 'What could a non-coder try tomorrow?',
    attribution: 'Maya · simulated viewer',
  },
  caption: { text: 'Let’s try one small thing and check what changed.' },
});
view.clear();        // immediate; use for Stop, moderation or connection loss
view.destroy();      // also releases resize observation and timers
```

Kinds: `question`, `reference`, `takeaway`, `result`, `thread`, `remark`, `title`, `poll`. One graphic occupies the card slot. Context and spoken captions occupy their own areas. A reported result keeps an appropriate attribution; the renderer does not verify claims or create successful-action receipts.

An explicit `graphic.until` is epoch seconds; `duration` is seconds. Those values may drive normal exit before the deadline and an optional hairline. Without real expiry metadata the live renderer invents no countdown. The catalog supplies clearly fictional timing. Null/withdrawal clears immediately, including while animation is running. A failed helper poll clears the on-air surface.

The component accepts validated display data. Oversized original card text is omitted rather than silently truncated. The catalog explains its word/character limit and layout omission. Private plans, model prompts and action payloads do not belong in public props. Use source validation, attribution and the existing moderation/lifecycle policy before rendering.

## Presenter surface

The reading column stays in the upper part of the monitor, centered as a column with left-aligned text. Its width is capped at `40rem`, with `1.5rem` side clearance in a small window. The existing steady input indicator aligns above that same column, including during quiet. Paused input keeps its square shape and accessible label; active input uses a filled circle. The presenter has no added controls, badges, decorative motion or secondary content rail.

Typography follows the **presenter container**, not the whole browser. At the default 16px root font, the current sizes are:

| Monitor width | Cue type | Line height |
| --- | --- | --- |
| 390px | 28px | 35px |
| 640px | 32px | 40px |
| 1280px | 44px | 55px |
| 1920px | 52px | 65px |

These are adjustable product defaults for a glance monitor, not physiological thresholds. The installed sans-serif stack, semibold text, off-white foreground and matte dark canvas are retained. Full questions and the welcome wrap naturally; the component does not truncate, shrink by content, or move the reading anchor for a longer cue. REM-based sizes respect user text enlargement. Geometry and contrast checks cover representative content and sizes, not complete accessibility conformance or readability at an unmeasured viewing distance.

Modify `--producer-host-*` in `tokens.css` for the reading width, type tiers, leading, gutters, top inset and indicator spacing. The named container breakpoints live in `components.css`. Both the live monitor and the catalog/replay component use these same rules. Model decisions, display duration and Stop remain outside the styles.

```js
import { createPresenterSurface } from './design-system/components.js';

const view = createPresenterSurface(mount);
view.render({ line: 'Return to the example you parked.', kind: 'recovery', presence: 'active' });
view.render({ line: 'Start with: Explain applied AI', kind: 'guidance', presence: 'active' });
view.render({ line: 'Next: Explore the project', kind: 'guidance', presence: 'active' });
view.render({ line: '', presence: 'active' }); // quiet
view.clear();
```

Both helpers can mount into an empty container or bind the existing production IDs. The presenter helper preserves the existing hard-stop and passive on-air-awareness nodes. It does not acquire input, choose cues, decide whether a warning is warranted or add an interaction.

## Timing and spatial policy

The attachment is design input, not a source of established broadcast or human-factors thresholds. The preview formula `clamp(2000 + words × 350, minHoldMs, maxHoldMs)` and per-kind gaps are editable product defaults. The live server's existing eligibility, queue and expiry policy remains authoritative; this pass does not replace its scheduling with the preview table. No speech-pause or tangent detector was invented.

EBU R095 specifies graphics-safe margins of 5% on each edge. At 1920 × 1080 that is 96 px horizontally and 54 px vertically. Producer's extra top and lower-third clearance is a product choice. Captions keep at least the graphics-safe bottom inset, while the card moves upward to avoid them. [EBU R095](https://tech.ebu.ch/publications/r095)

Test both reference sizes, long original text, caption/card coexistence, alignment, expiry, Stop/deletion, bright/dark backgrounds and reduced motion. Contrast and motion checks support evaluation; they are not a claim of complete accessibility conformance. [W3C contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html), [motion guidance](https://www.w3.org/WAI/WCAG21/Understanding/animation-from-interactions)

## Run the checks

```sh
node producer/design-components-browser-check.cjs
node producer/design-browser-check.cjs
node producer/on-air-browser-check.cjs
node producer/room-browser-check.cjs
node producer/presenter-design-check.cjs --output .local/producer/presenter-design/check
node producer/viewer-design-check.cjs --output .local/producer/viewer-design/check
```

The standalone component check intercepts every fixture request; catalog/workflow checks require the local helper at port 8786. These browser regressions use authored data and mocked inference. They do not prove real viewer usefulness or live broadcast quality.
