// Capture at the context's actual rate, then average into 16 kHz mono PCM.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.sum = 0; this.weight = 0; this.remaining = sampleRate / 16000;
    this.ratio = this.remaining; this.chunk = new Int16Array(1600); this.used = 0;
    this.port.onmessage = ({data}) => {
      if (data !== 'flush') return;
      if (this.used) {
        const pcm = new ArrayBuffer(this.used * 2), view = new DataView(pcm);
        for (let i = 0; i < this.used; i++) view.setInt16(i * 2, this.chunk[i], true);
        this.port.postMessage({pcm, peak: 0}, [pcm]); this.used = 0;
      }
      this.port.postMessage({flushed: true});
    };
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    let peak = 0;
    for (const value of input) {
      peak = Math.max(peak, Math.abs(value));
      let left = 1;
      while (left > 1e-8) {
        const take = Math.min(left, this.remaining);
        this.sum += value * take; this.weight += take;
        this.remaining -= take; left -= take;
        if (this.remaining < 1e-8) {
          const v = Math.max(-1, Math.min(1, this.sum / this.weight));
          this.chunk[this.used++] = Math.round(v * (v < 0 ? 32768 : 32767));
          this.sum = this.weight = 0; this.remaining = this.ratio;
          if (this.used === this.chunk.length) {
            const pcm = new ArrayBuffer(3200), view = new DataView(pcm);
            for (let i = 0; i < 1600; i++) view.setInt16(i * 2, this.chunk[i], true);
            this.port.postMessage({pcm, peak}, [pcm]); this.used = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', CaptureProcessor);
