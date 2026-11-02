export class LiveCaptions {
  constructor(api, fail) {
    this.api = api;
    this.fail = fail;
    this.context = null;
    this.epoch = 0;
    this.pending = [];
    this.busy = false;
    this.sequence = 0;
  }
  async start(stream, id) {
    const epoch = ++this.epoch;
    this.id = id;
    this.sequence = 0;
    await this.api("/api/captions/start", { id });
    if (epoch !== this.epoch) return;
    this.context = new AudioContext({ sampleRate: 24000 });
    if (this.context.sampleRate !== 24000)
      throw Error("This device cannot supply the caption sample rate.");
    await this.context.audioWorklet.addModule("/caption-worklet.js");
    if (epoch !== this.epoch) return;
    this.node = new AudioWorkletNode(this.context, "caption-pcm");
    this.source = this.context.createMediaStreamSource(stream);
    const mute = this.context.createGain();
    mute.gain.value = 0;
    this.source.connect(this.node);
    this.node.connect(mute);
    mute.connect(this.context.destination);
    this.node.port.onmessage = (event) => {
      if (epoch !== this.epoch) return;
      if (this.pending.length >= 2) {
        this.stop();
        this.fail("Captions paused: input queue filled.");
        return;
      }
      const bytes = new Uint8Array(event.data);
      let binary = "";
      for (let i = 0; i < bytes.length; i += 4096)
        binary += String.fromCharCode(...bytes.subarray(i, i + 4096));
      this.pending.push({ audio: btoa(binary), sequence: this.sequence++ });
      void this.send(epoch);
    };
    await this.context.resume();
  }
  async send(epoch) {
    if (this.busy) return;
    this.busy = true;
    try {
      while (this.pending.length && epoch === this.epoch)
        await this.api("/api/captions/audio", {
          id: this.id,
          ...this.pending.shift(),
        });
    } catch {
      if (epoch === this.epoch) {
        this.stop();
        this.fail("Captions stopped. The private monitor continues.");
      }
    } finally {
      if (epoch === this.epoch) this.busy = false;
    }
  }
  async startRehearsal(id, seconds) {
    const epoch = ++this.epoch;
    this.id = id;
    this.sequence = 0;
    const started = performance.now();
    await this.api("/api/captions/start", { id });
    const tick = async () => {
      if (epoch !== this.epoch || this.sequence >= Math.floor(seconds)) return;
      const available = (performance.now() - started) / 1000;
      try {
        if (available - this.sequence > 3)
          throw Error("Caption feed fell behind.");
        if (available >= this.sequence + 1) {
          await this.api("/api/captions/audio", { id, sequence: this.sequence++ });
        }
      } catch {
        if (epoch === this.epoch) {
          this.stop();
          this.fail("Captions stopped. The private monitor continues.");
        }
        return;
      }
      if (epoch === this.epoch) this.timer = setTimeout(tick, 100);
    };
    void tick();
  }
  stop() {
    this.epoch++;
    clearTimeout(this.timer);
    this.pending = [];
    this.busy = false;
    this.source?.disconnect();
    this.node?.disconnect();
    void this.context?.close();
    this.context = null;
  }
}
