// AudioWorklet — tourne dans un thread audio dédié.
// Reçoit des blocs de 128 samples, les accumule en chunks de 2400 samples (150 ms @ 16 kHz)
// et les envoie au thread principal via postMessage.
class AudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = [];
    this._chunkSamples = 2400; // 150 ms @ 16 kHz
  }

  process(inputs) {
    const ch = inputs[0]?.[0];
    if (!ch) return true;

    for (let i = 0; i < ch.length; i++) this._buf.push(ch[i]);

    while (this._buf.length >= this._chunkSamples) {
      const chunk = new Float32Array(this._buf.splice(0, this._chunkSamples));
      this.port.postMessage(chunk.buffer, [chunk.buffer]);
    }

    return true;
  }
}

registerProcessor("audio-processor", AudioProcessor);
