class CaptionPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(24000);
    this.offset = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let s = 0;
      for (const channel of channels) s += channel[i] || 0;
      s = Math.max(-1, Math.min(1, s / channels.length));
      this.samples[this.offset++] = s < 0 ? s * 32768 : s * 32767;
      if (this.offset === this.samples.length) {
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(24000);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor("caption-pcm", CaptionPCM);
