import { createParser } from "../assets/eventsource-parser.js";

export async function readEvents(response, onEvent, signal) {
  if (!response.body) throw new Error("浏览器未提供流式回复，请刷新后重试。");
  let complete = false;
  let failure = "";
  const parser = createParser({ onEvent(message) {
    let event;
    try { event = JSON.parse(message.data); }
    catch { throw new Error("回复数据无法解析，请重试。"); }
    if (event.type === "done") complete = true;
    if (event.type === "error") failure = event.message || "回复生成失败，请稍后重试。";
    onEvent(event);
  } });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException("Request aborted", "AbortError");
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    if (failure) throw new Error(failure);
    if (!complete) throw new Error("回复连接已中断，已收到的内容保留在对话中。");
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
