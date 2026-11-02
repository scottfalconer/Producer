// The selected source streams to the loopback helper; the helper owns Realtime.
export class RealtimeFeed {
  constructor(api, { status, fail, finished } = {}) {
    this.api = api;
    this.onStatus = status;
    this.onFail = fail;
    this.onFinished = finished;
    this.epoch = 0;
    this.pending = [];
    this.busy = false;
  }

  async start(stream, id, { rehearsal = false, nativeOBS = false, offset = 0, duration, video } = {}) {
    this.stop();
    const epoch = ++this.epoch;
    this.id = id;
    this.offset = offset;
    this.sequence = 0;
    this.pending = [];
    this.busy = false;
    this.video = video;
    this.rehearsal = rehearsal;
    this.nativeOBS = nativeOBS;
    this.failureReported = false;
    await this.api("/api/director/start", { id });
    if (epoch !== this.epoch) return;
    await this.poll(epoch);
    if (epoch !== this.epoch) return;
    // The helper accepts OBS's composed picture and audio directly. This page
    // only follows director state; it must not open browser capture or send PCM.
    if (nativeOBS) return;
    if (rehearsal) {
      if (!Number.isFinite(duration) || duration <= offset)
        throw Error("The selected recording has no remaining input.");
      this.recordingStarted = performance.now();
      this.duration = duration;
      this.recordingTick(epoch);
      return;
    }
    this.context = new AudioContext({ sampleRate: 24000 });
    if (this.context.sampleRate !== 24000)
      throw Error("This device cannot supply Producer's audio sample rate.");
    await this.context.audioWorklet.addModule("/director-worklet.js");
    if (epoch !== this.epoch) return;
    this.node = new AudioWorkletNode(this.context, "producer-director-pcm");
    this.source = this.context.createMediaStreamSource(stream);
    this.mute = this.context.createGain();
    this.mute.gain.value = 0;
    this.source.connect(this.node);
    this.node.connect(this.mute);
    this.mute.connect(this.context.destination);
    this.node.port.onmessage = event => {
      if (epoch !== this.epoch) return;
      const sequence = this.sequence++;
      const bytes = new Uint8Array(event.data);
      let binary = "";
      for (let i = 0; i < bytes.length; i += 4096)
        binary += String.fromCharCode(...bytes.subarray(i, i + 4096));
      try {
        this.enqueue({
          sequence,
          audio: btoa(binary),
          source_end: this.offset + (sequence + 1) * 0.5,
          ...this.sampleFrame(sequence),
        }, epoch);
      } catch {
        this.failure("Producer paused: the selected image could not be captured within its size limit.", epoch);
      }
    };
    await this.context.resume();
  }

  sampleFrame(sequence) {
    const video = this.video;
    if (sequence % 8 || !video?.videoWidth || !video.videoHeight) return {};
    const canvas = document.createElement("canvas");
    const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    for (const quality of [0.8, 0.65, 0.5, 0.35]) {
      const frame = canvas.toDataURL("image/jpeg", quality).split(",")[1];
      if (Math.floor(frame.length * 3 / 4) <= 350000) return { frame };
    }
    throw Error("The selected image exceeds Producer's frame-size limit.");
  }

  enqueue(packet, epoch) {
    if (epoch !== this.epoch) return;
    if (this.pending.length >= 6) {
      this.failure("Producer paused: the selected audio could not be delivered in time.", epoch);
      return;
    }
    this.pending.push(packet);
    void this.send(epoch);
  }

  async send(epoch) {
    if (this.busy || epoch !== this.epoch) return;
    this.busy = true;
    try {
      while (this.pending.length && epoch === this.epoch) {
        const packet = this.pending.shift();
        await this.api("/api/director/audio", { id: this.id, ...packet });
      }
    } catch {
      this.failure("Producer paused: the selected audio connection stopped.", epoch);
    } finally {
      if (epoch === this.epoch) this.busy = false;
    }
  }

  async poll(epoch) {
    if (epoch !== this.epoch) return;
    try {
      const state = await this.api("/api/director/status", { id: this.id });
      if (epoch !== this.epoch) return;
      if (state.status === "error" || state.status === "stopped") {
        this.failure(state.error || state.detail || "Producer's Realtime session stopped. Resume when ready.", epoch);
        return;
      }
      this.onStatus?.(state);
    } catch {
      this.failure("Producer paused: its Realtime connection is unavailable.", epoch);
      return;
    }
    if (epoch === this.epoch)
      this.statusTimer = setTimeout(() => void this.poll(epoch), 500);
  }

  recordingTick(epoch) {
    if (epoch !== this.epoch) return;
    const remaining = this.duration - this.offset;
    const available = (performance.now() - this.recordingStarted) / 1000;
    let packetEnd = Math.min(remaining, (this.sequence + 1) * 0.5);
    while (available >= packetEnd && epoch === this.epoch) {
      if (available - this.sequence * 0.5 > 3) {
        this.failure("Producer paused: recording input fell behind.", epoch);
        return;
      }
      this.enqueue({
        sequence: this.sequence++,
        source_end: this.offset + packetEnd,
      }, epoch);
      if (packetEnd >= remaining) {
        const finish = () => {
          if (epoch !== this.epoch) return;
          if (this.pending.length || this.busy)
            this.recordingTimer = setTimeout(finish, 100);
          else this.onFinished?.();
        };
        finish();
        return;
      }
      // Background tabs can receive timer callbacks less often than 500 ms.
      // Send every now-available interval in order rather than letting the
      // scheduling interval silently consume the recording's audio coverage.
      packetEnd = Math.min(remaining, (this.sequence + 1) * 0.5);
    }
    if (epoch === this.epoch)
      this.recordingTimer = setTimeout(() => this.recordingTick(epoch), 50);
  }

  failure(message, epoch) {
    if (epoch !== this.epoch || this.failureReported) return;
    this.failureReported = true;
    this.stop();
    this.onFail?.(message);
  }

  stop() {
    this.epoch++;
    clearTimeout(this.statusTimer);
    clearTimeout(this.recordingTimer);
    this.pending = [];
    this.busy = false;
    this.source?.disconnect();
    this.node?.disconnect();
    this.mute?.disconnect();
    if (this.node) this.node.port.onmessage = null;
    void this.context?.close();
    this.context = this.source = this.node = this.mute = null;
  }
}
