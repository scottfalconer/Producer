class DirectorPCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(12000);
    this.offset = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] || 0;
      value = Math.max(-1, Math.min(1, value / channels.length));
      this.samples[this.offset++] = value < 0 ? value * 32768 : value * 32767;
      if (this.offset === this.samples.length) {
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(12000);
        this.offset = 0;
      }
    }
    return true;
  }
}
registerProcessor("producer-director-pcm", DirectorPCM);
