export function promisePayload(text, dueInput = "") {
  const clean = String(text || "").trim();
  if (!clean) throw new Error("请先写下她答应过的事。");
  const dueAt = dueInput ? new Date(dueInput).getTime() / 1000 : null;
  if (dueAt !== null && !Number.isFinite(dueAt)) throw new Error("请填写有效的约定时间。");
  return { text: clean, due_at: dueAt };
}

export class RelationshipBoard {
  constructor({ client, revision = () => 0, render = () => {}, onError = error => { throw error; } }) {
    this.client = client;
    this.revision = revision;
    this.render = render;
    this.onError = onError;
    this.latest = 0;
  }

  async load() {
    const epoch = this.client.epoch;
    const revision = this.revision();
    const serial = ++this.latest;
    const current = () => epoch === this.client.epoch && revision === this.revision() && serial === this.latest;
    try {
      const data = await this.client.json("/api/relationship");
      if (!current()) return false;
      this.render(data);
      return true;
    } catch (error) {
      if (!current()) return false;
      this.onError(error);
      return false;
    }
  }

  async add(text, dueInput = "") {
    return this.client.json("/api/relationship/promises", {
      method: "POST", body: promisePayload(text, dueInput),
    });
  }

  async complete(promiseId, note = "") {
    return this.client.json(`/api/relationship/promises/${encodeURIComponent(promiseId)}/complete`, {
      method: "POST", body: { note: String(note || "").trim() },
    });
  }

  async miss(promiseId, reason) {
    const clean = String(reason || "").trim();
    if (!clean) throw new Error("请写下这次没做到的原因。");
    return this.client.json(`/api/relationship/promises/${encodeURIComponent(promiseId)}/miss`, {
      method: "POST", body: { reason: clean },
    });
  }
}
