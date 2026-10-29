export function validateCareText(value) {
  const text = String(value ?? "").trim();
  if (!text) throw new Error("要钉的内容不能为空。");
  if ([...text].length > 60) throw new Error("牵挂最多 60 个字，请删减后再提交。");
  return text;
}

export class LifeBoard {
  constructor({ client, revision = () => 0, render = () => {},
    onError = error => { throw error; }, schedule = callback => setTimeout(callback, 2000) }) {
    this.client = client;
    this.revision = revision;
    this.render = render;
    this.onError = onError;
    this.schedule = schedule;
    this.latest = 0;
  }

  async load() {
    const epoch = this.client.epoch, revision = this.revision(), serial = ++this.latest;
    const current = () => epoch === this.client.epoch && revision === this.revision() && serial === this.latest;
    try {
      const data = await this.client.json("/api/life");
      if (!current()) return false;
      this.render(data);
      if (data.catchup?.running) this.schedule(() => current() ? this.load() : false);
      return true;
    } catch (error) {
      if (!current()) return false;
      this.onError(error);
      return false;
    }
  }
}
