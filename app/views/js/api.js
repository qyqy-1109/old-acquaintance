export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export class ApiClient {
  constructor({ fetchImpl = globalThis.fetch, storage = globalThis.localStorage,
    onUnauthorized = () => {} } = {}) {
    this.fetchImpl = fetchImpl.bind(globalThis);
    this.storage = storage;
    this.onUnauthorized = onUnauthorized;
    this.identity = { mode: "", value: "" };
    this.username = "";
    this.epoch = 0;
    this.pending = new Set();
  }

  setIdentity(mode, value) {
    this.cancelPending();
    this.epoch++;
    this.identity = { mode, value: value.trim() };
    this.username = "";
  }

  restoreIdentity() {
    const token = this.storage?.getItem("auth_token");
    const nickname = this.storage?.getItem("user_id");
    if (token) {
      this.setIdentity("token", token);
      this.username = this.storage?.getItem("auth_username") || "";
    }
    else if (nickname) this.setIdentity("nickname", nickname);
    return Boolean(this.identity.value);
  }

  persistIdentity() {
    const { mode, value } = this.identity;
    if (!value) return;
    this.storage?.setItem(mode === "token" ? "auth_token" : "user_id", value);
    this.storage?.removeItem(mode === "token" ? "user_id" : "auth_token");
    if (mode === "token" && this.username) this.storage?.setItem("auth_username", this.username);
    else this.storage?.removeItem("auth_username");
  }

  clearIdentity() {
    this.cancelPending();
    this.epoch++;
    this.identity = { mode: "", value: "" };
    this.username = "";
    this.storage?.removeItem("auth_token");
    this.storage?.removeItem("user_id");
    this.storage?.removeItem("auth_username");
  }

  cancelPending() {
    for (const controller of this.pending) controller.abort();
    this.pending.clear();
  }

  async request(path, { anonymous = false, suppressAuth = false, signal, ...options } = {}) {
    const headers = new Headers(options.headers);
    const epoch = this.epoch;
    if (!anonymous && this.identity.value) {
      if (this.identity.mode === "token") {
        headers.set("Authorization", "Bearer " + this.identity.value);
      } else {
        headers.set("X-User-Id", encodeURIComponent(this.identity.value));
        headers.set("X-User-Id-Encoding", "percent-encoded");
      }
    }
    const controller = new AbortController();
    const transportSignal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
    this.pending.add(controller);
    try {
      const response = await this.fetchImpl(path, { ...options, headers, signal: transportSignal });
      if (response.status === 401) {
        if (!anonymous && epoch === this.epoch) {
          this.clearIdentity();
          if (!suppressAuth) this.onUnauthorized();
        }
        let message = "登录状态已失效，请重新登录。";
        if (anonymous) {
          try {
            const data = await response.json();
            if (typeof data.detail === "string") message = data.detail;
          } catch { /* Use the generic authentication error for a non-JSON response. */ }
        }
        throw new ApiError(message, 401);
      }
      if (!response.ok) {
        let message = "请求失败（HTTP " + response.status + "）";
        try {
          const data = await response.json();
          if (typeof data.detail === "string") message = data.detail;
          else if (typeof data.message === "string") message = data.message;
        } catch { /* Preserve the HTTP status when the error body is not JSON. */ }
        throw new ApiError(message, response.status);
      }
      return response;
    } catch (error) {
      if (error instanceof ApiError || error.name === "AbortError") throw error;
      throw new ApiError("连接服务失败，请确认后端已启动后重试。");
    } finally {
      this.pending.delete(controller);
    }
  }

  async json(path, options = {}) {
    const request = { ...options };
    if (request.body !== undefined) {
      request.headers = new Headers(request.headers);
      request.headers.set("Content-Type", "application/json");
      request.body = JSON.stringify(request.body);
    }
    const response = await this.request(path, request);
    return response.json();
  }

  async login(username, password, { setup = false, inheritDefault = false } = {}) {
    const epoch = this.epoch;
    const body = { username, password };
    if (setup) body.inherit_default = inheritDefault;
    const data = await this.json(setup ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST", anonymous: true, suppressAuth: true, body,
    });
    if (epoch !== this.epoch) throw new DOMException("Identity changed", "AbortError");
    if (typeof data.access_token !== "string" || !data.access_token || typeof data.username !== "string") {
      throw new ApiError("登录响应不完整，请重新连接服务。");
    }
    this.setIdentity("token", data.access_token);
    this.username = data.username;
    this.persistIdentity();
    return data;
  }

  async me() {
    const epoch = this.epoch;
    const data = await this.json("/api/auth/me");
    if (epoch !== this.epoch) throw new DOMException("Identity changed", "AbortError");
    this.username = data.username;
    this.persistIdentity();
    return data;
  }

  async settings() {
    return this.json("/api/settings");
  }

  async saveSettings(values, clear = []) {
    return this.json("/api/settings", { method: "POST", body: { values, clear } });
  }

  async testSettings(values) {
    return this.json("/api/settings/test", { method: "POST", body: { values } });
  }

  async personaPreview(filename, content = "", contentBase64 = null) {
    return this.json("/api/persona/preview", {
      method: "POST",
      body: { filename, content, content_base64: contentBase64 },
    });
  }

  async personaImport(filename, content = "", contentBase64 = null, includeWorld = true) {
    return this.json("/api/persona/import", {
      method: "POST",
      body: {
        filename, content, content_base64: contentBase64, include_world: includeWorld,
      },
    });
  }

  async sessions() {
    return this.json("/api/sessions");
  }

  async createSession(title = "") {
    return this.json("/api/sessions", { method: "POST", body: { title } });
  }

  async selectSession(sessionId) {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/select`, {
      method: "POST", body: {},
    });
  }

  async renameSession(sessionId, title) {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH", body: { title },
    });
  }

  async deleteSession(sessionId) {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  }

  async sessionMessages(sessionId) {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
  }

  async editMessage(sessionId, seq, content) {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/messages/${seq}`, {
      method: "PATCH", body: { content },
    });
  }

  async deleteMessage(sessionId, seq) {
    return this.json(`/api/sessions/${encodeURIComponent(sessionId)}/messages/${seq}`, {
      method: "DELETE",
    });
  }

  async regenerateMessage(sessionId, seq) {
    return this.json(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages/${seq}/regenerate`,
      { method: "POST", body: {} },
    );
  }

  async search(query) {
    return this.json("/api/search?q=" + encodeURIComponent(query));
  }

  async claimLegacy() {
    return this.json("/api/auth/legacy", { method: "POST" });
  }

  async logout() {
    const epoch = this.epoch;
    try {
      if (this.identity.mode === "token" && this.identity.value) {
        await this.json("/api/auth/logout", { method: "POST", body: {}, suppressAuth: true });
      }
    } finally {
      if (epoch === this.epoch) this.clearIdentity();
    }
  }
}
