export class AudioPlayer {
  constructor(onBlocked = () => {}) {
    this.onBlocked = onBlocked;
    this.stop();
  }

  stop() {
    this.generation = (this.generation || 0) + 1;
    this.audio?.pause();
    if (this.url) URL.revokeObjectURL(this.url);
    this.audio = null;
    this.url = null;
    this.source = null;
    this.buffer = null;
    this.queue = [];
    this.chunks = [];
    this.ended = false;
    this.streaming = false;
    this.warned = false;
  }

  start() {
    this.stop();
    this.streaming = Boolean(globalThis.MediaSource?.isTypeSupported("audio/mpeg"));
    if (!this.streaming) return;
    const generation = this.generation;
    try {
      this.source = new MediaSource();
      this.url = URL.createObjectURL(this.source);
      this.audio = new Audio(this.url);
      this.source.addEventListener("sourceopen", () => {
        if (generation !== this.generation) return;
        try {
          this.buffer = this.source.addSourceBuffer("audio/mpeg");
          this.buffer.addEventListener("updateend", () => {
            if (generation === this.generation) this.pump();
          });
          this.pump();
        } catch { this.fallback(); }
      }, { once: true });
    } catch { this.fallback(); }
  }

  push(base64) {
    const binary = atob(base64);
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
    this.chunks.push(bytes);
    if (this.streaming) {
      this.queue.push(bytes);
      this.pump();
    }
  }

  pump() {
    if (!this.streaming || !this.buffer || this.buffer.updating) return;
    try {
      if (this.queue.length) {
        this.buffer.appendBuffer(this.queue.shift());
        this.play();
      } else if (this.ended && this.source.readyState === "open") {
        this.source.endOfStream();
      }
    } catch { this.fallback(); }
  }

  end() {
    this.ended = true;
    if (this.streaming) this.pump();
    else this.playBlob();
  }

  fallback() {
    this.streaming = false;
    this.audio?.pause();
    if (this.url) URL.revokeObjectURL(this.url);
    this.audio = null;
    this.url = null;
    this.queue = [];
    // Keep every received chunk so a mid-stream MSE failure loses no audio.
    if (this.ended) this.playBlob();
  }

  playBlob() {
    if (!this.chunks.length) return;
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = URL.createObjectURL(new Blob(this.chunks, { type: "audio/mpeg" }));
    this.audio = new Audio(this.url);
    this.play();
  }

  play() {
    const generation = this.generation;
    this.audio?.play()?.catch(() => {
      if (generation !== this.generation || this.warned) return;
      this.warned = true;
      this.onBlocked(() => { this.warned = false; this.play(); });
    });
  }
}
