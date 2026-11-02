const video = document.querySelector("#program");
async function sync() {
  try {
    const response = await fetch("/api/rehearsal", {
      cache: "no-store",
      signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) throw Error();
    const state = await response.json();
    if (video.readyState && Math.abs(video.currentTime - state.elapsed) > 1)
      video.currentTime = state.elapsed;
    if (state.running && state.elapsed < state.duration) {
      if (video.paused) await video.play();
    } else video.pause();
  } catch {
    video.pause();
  }
  setTimeout(sync, 500);
}
void sync();
