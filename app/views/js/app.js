import { ApiClient, ApiError } from "./api.js";
import { readEvents } from "./sse.js";
import { AudioPlayer } from "./audio.js";
import { BubbleSplitter } from "./bubble.js";
import { RelationshipBoard } from "./relationship.js";
import { LifeBoard, validateCareText } from "./life.js";

const $ = id => document.getElementById(id);
const log = $("log");
const input = $("message-input");
const client = new ApiClient({ onUnauthorized: () => {
  resetSession();
  showEntry("登录状态已失效，请重新登录。");
} });
let health = null;
let state = null;
let settings = null;
let settingsInitial = {};
let pendingPersonaContent = "";
let pendingPersonaBase64 = "";
let personaFilename = "";
let avatarUrl = "";
let wizardActive = false;
let wizardStep = 1;
let wizardNeedsPersona = localStorage.getItem("wizard_persona_pending") === "1";
let wizardKeyReady = false;
let wizardEmbedReady = false;
let wizardPersonaImported = false;
let demoMode = false;
let setupMode = false;
let loginBusy = false;
let busy = false;
let greetingBusy = false;
let currentView = "chat";
let viewRevision = 0;
let memoryCheckPromise = null;
let memoryCheckController = null;
let memoryCheckState = "idle";
const MEMORY_CHECK_TIMEOUT_MS = 45000;
let streamController = null;
let followLatest = true;
let toastTimer;
let memoryTimers = [];
let ttsOn = localStorage.getItem("tts") !== "off";
let voiceRevision = 0;
const audio = new AudioPlayer(retry => toast("声音暂未播放", retry));
const titles = { chat: "对话", world: "世界书", slots: "状态", timeline: "时间轴",
  life: "她的生活", relationship: "我们的关系", profile: "画像与人设", care: "记忆体检" };
const relationshipBoard = new RelationshipBoard({
  client, revision: () => viewRevision, render: renderRelationship,
  onError: error => {
    if (!isQuiet(error)) {
      $("relationship-stage").textContent = "暂时未读取到关系记录";
      setError("memory-error", error.message);
    }
  },
});
let missPromiseId = "";
const lifeBoard = new LifeBoard({
  client, revision: () => viewRevision, render: renderLifeBoard,
  onError: error => {
    if (!isQuiet(error)) $("life-state").textContent = error.message;
  },
});

function element(tag, className = "", text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function displayMemoryText(value) {
  return String(value ?? "").replaceAll("@@@", "\n");
}

function icon(name) {
  const node = element("i");
  node.dataset.icon = name;
  node.setAttribute("aria-hidden", "true");
  return node;
}

function iconButton(name, label) {
  const button = element("button", "icon-button small");
  button.type = "button";
  button.dataset.tooltip = label;
  button.setAttribute("aria-label", label);
  button.append(icon(name));
  return button;
}

function setError(id, message = "") {
  $(id).textContent = message;
  $(id).hidden = !message;
}

function toast(message, action) {
  clearTimeout(toastTimer);
  $("toast-text").textContent = message;
  $("toast").hidden = false;
  const composerRect = $("composer").getBoundingClientRect();
  $("toast").style.bottom = currentView === "chat" && !$("app-shell").hidden
    ? Math.max(24, innerHeight - composerRect.top + 12) + "px" : "24px";
  $("toast-action").hidden = !action;
  $("toast-action").onclick = () => { action?.(); $("toast").hidden = true; };
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, action ? 12000 : 4500);
}

function setButtonBusy(button, active) {
  if (active) {
    button.dataset.original = button.innerHTML;
    button.dataset.originalMinWidth = button.style.minWidth;
    button.style.minWidth = button.getBoundingClientRect().width + "px";
    if (button.classList.contains("icon-button")) button.replaceChildren(icon("loader-circle"));
    else button.replaceChildren(icon("loader-circle"), document.createTextNode("处理中"));
  } else if (button.dataset.original !== undefined) {
    button.innerHTML = button.dataset.original;
    button.style.minWidth = button.dataset.originalMinWidth;
    delete button.dataset.original;
    delete button.dataset.originalMinWidth;
  }
  button.disabled = active;
  button.setAttribute("aria-busy", String(active));
}

function isQuiet(error) { return error.name === "AbortError" || error.status === 401; }

async function action(button, task, errorId = "memory-error") {
  if (button.disabled) return;
  const epoch = client.epoch;
  setButtonBusy(button, true);
  setError(errorId);
  try { await task(epoch); }
  catch (error) {
    if (epoch === client.epoch && !isQuiet(error)) {
      if (errorId === "memory-error" && currentView === "chat") toast(error.message);
      else setError(errorId, error.message);
    }
  } finally { setButtonBusy(button, false); }
}

function applyAccountMode() {
  setupMode = Boolean(health?.account_setup_required);
  $("password-confirm-wrap").hidden = !setupMode;
  $("inherit-default-wrap").hidden = !setupMode || !health?.legacy_data_available;
  $("password-input").autocomplete = setupMode ? "new-password" : "current-password";
  if (!loginBusy) setLoginBusy(false);
}

function setWizardStep(step) {
  wizardStep = step;
  $("wizard-steps").hidden = !wizardActive;
  for (let i = 1; i <= 3; i++) {
    $("wizard-step-" + i).className = i === step ? "font-medium text-ink"
      : i < step ? "text-brand" : "text-faint";
  }
  $("wizard-model").hidden = !wizardActive || step !== 1;
  $("login-form").hidden = wizardActive && step !== 2;
  $("wizard-persona").hidden = !wizardActive || step !== 3;
  if (step === 3 && !wizardPersonaImported) {
    $("wizard-persona-import").hidden = false;
    $("wizard-persona-start").hidden = true;
    $("wizard-persona-note").textContent = "";
  }
  applyAccountMode();
  if (step === 1) loadWizardSettings();
}

function configureEntry() {
  const keysReady = Boolean(health?.keys_ready);
  const needsPersona = wizardNeedsPersona && client.identity.mode === "token";
  wizardActive = Boolean(health?.first_run) || needsPersona;
  $("demo-open").hidden = keysReady || Boolean(health?.auth_required);
  if (health?.password_login) $("entry-status").querySelector("span:last-child").textContent =
    !keysReady ? "先配置模型" : needsPersona ? "最后一步" : health.account_setup_required ? "首次创建账号" : "身份验证已开启";
  if (!wizardActive) {
    $("wizard-steps").hidden = true;
    $("wizard-model").hidden = true;
    $("wizard-persona").hidden = true;
    $("login-form").hidden = false;
    applyAccountMode();
    return;
  }
  if (!keysReady) setWizardStep(1);
  else if (health?.account_setup_required) setWizardStep(2);
  else if (needsPersona) setWizardStep(3);
  else setWizardStep(2);
}

function setLoginBusy(active) {
  loginBusy = active;
  $("login-submit").disabled = active || !health?.password_login || !health?.keys_ready;
  $("login-submit").querySelector("span").textContent = active ? "正在验证"
    : !health?.password_login ? "服务未连接"
      : !health?.keys_ready ? "先配置模型"
        : setupMode ? "创建账号并进入" : "登录";
  $("login-submit").setAttribute("aria-busy", String(active));
  for (const id of ["username-input", "password-input", "password-confirm", "inherit-default", "toggle-password"]) $(id).disabled = active;
}

const settingsFields = {
  "settings-base": ["WB_BASE_URL", false],
  "settings-api-key": ["WB_API_KEY", true],
  "settings-chat-base": ["WB_CHAT_BASE_URL", false],
  "settings-chat-key": ["WB_CHAT_API_KEY", true],
  "settings-embed-base": ["WB_EMBED_BASE_URL", false],
  "settings-embed-key": ["WB_EMBED_API_KEY", true],
  "settings-model": ["WB_MODEL", false],
  "settings-embed-model": ["WB_EMBED_MODEL", false],
  "settings-embed-dimensions": ["WB_EMBED_DIMENSIONS", false],
  "settings-extract-model": ["WB_EXTRACT_MODEL", false],
  "settings-amap-key": ["WB_AMAP_KEY", true],
  "settings-emotion-key": ["WB_EMOTION_API_KEY", true],
  "settings-tts-key": ["WB_TTS_API_KEY", true],
};

function sourceText(source) {
  return { env: "环境变量", settings: "页面设置", envfile: ".env", default: "默认值", missing: "未配置" }[source] || "未配置";
}

function renderSettings(data) {
  settings = data;
  settingsInitial = {};
  const managed = new Set(data.managed_by_env || []);
  for (const [id, [key, secret]] of Object.entries(settingsFields)) {
    const input = $(id);
    const item = data.values?.[key] || {};
    input.disabled = managed.has(key);
    if (secret) {
      input.value = "";
      input.placeholder = item.set ? `已设置：${item.masked}` : "未设置";
    } else {
      input.value = item.value || "";
      input.placeholder = key === "settings-chat-base" ? "留空复用主 API 地址"
        : key === "settings-extract-model" ? "留空与对话模型相同"
          : data.defaults?.[key] || "";
      settingsInitial[key] = input.value;
    }
  }
  const chat = data.chat_ready ? "已配置" : "未配置";
  const bg = data.bg_ready ? "已配置" : "未配置";
  const embed = data.embed_ready ? "已配置" : "未配置";
  $("settings-summary").textContent =
    `主对话：${chat}（${sourceText(data.chat_source)}） · 后台：${bg}（${sourceText(data.bg_source)}）`
    + ` · Embedding：${embed}（${sourceText(data.embed_source)}）`
    + (managed.size ? `\n以下项由部署环境变量管理，页面只读：${[...managed].join("、")}` : "");
}

async function loadWizardSettings() {
  try {
    const data = await client.settings();
    wizardKeyReady = Boolean(data.bg_ready);
    wizardEmbedReady = Boolean(data.embed_ready);
    const base = data.values?.WB_BASE_URL || {};
    const embedBase = data.values?.WB_EMBED_BASE_URL || {};
    $("wizard-base").value = base.value || "";
    $("wizard-base").placeholder = data.defaults?.WB_BASE_URL || "https://api.example.com/v1";
    $("wizard-embed-base").value = embedBase.value || "";
    $("wizard-embed-base").placeholder = data.defaults?.WB_EMBED_BASE_URL || "https://api.example.com/v1";
    const apiKey = data.values?.WB_API_KEY || {};
    $("wizard-api-key").value = "";
    $("wizard-api-key").placeholder = apiKey.set ? `已设置：${apiKey.masked}` : "sk-...";
    const embedKey = data.values?.WB_EMBED_API_KEY || {};
    $("wizard-embed-key").value = "";
    $("wizard-embed-key").placeholder = embedKey.set ? `已设置：${embedKey.masked}` : "sk-...";
  } catch (error) {
    setError("wizard-model-error", error.message);
  }
}

function wizardValues() {
  const values = {};
  const base = $("wizard-base").value.trim();
  const key = $("wizard-api-key").value.trim();
  const embedBase = $("wizard-embed-base").value.trim();
  const embedKey = $("wizard-embed-key").value.trim();
  if (base) values.WB_BASE_URL = base;
  if (key) values.WB_API_KEY = key;
  if (embedBase) values.WB_EMBED_BASE_URL = embedBase;
  if (embedKey) values.WB_EMBED_API_KEY = embedKey;
  return values;
}

async function saveWizardModel(event) {
  event.preventDefault();
  const values = wizardValues();
  if (!$("wizard-api-key").value.trim() && !wizardKeyReady) {
    setError("wizard-model-error", "请填写主 API Key。");
    return;
  }
  if (!$("wizard-embed-key").value.trim() && !wizardEmbedReady) {
    setError("wizard-model-error", "请填写 Embedding Key。");
    return;
  }
  setError("wizard-model-error");
  $("wizard-model-note").hidden = true;
  setButtonBusy($("wizard-save"), true);
  try {
    const result = await client.saveSettings(values);
    health = await client.json("/api/healthz", { anonymous: true, suppressAuth: true });
    if (result.applied && health.keys_ready) {
      wizardActive = true;
      configureEntry();
      toast("模型配置已生效");
    } else {
      $("wizard-model-note").hidden = false;
      $("wizard-model-note").textContent = result.message || "配置已保存，但需要重启服务后生效。";
    }
  } catch (error) {
    setError("wizard-model-error", error.message);
  } finally {
    setButtonBusy($("wizard-save"), false);
  }
}

async function testWizardModel() {
  setError("wizard-model-error");
  $("wizard-model-note").hidden = true;
  setButtonBusy($("wizard-test"), true);
  try {
    const result = await client.testSettings(wizardValues());
    $("wizard-model-note").hidden = false;
    $("wizard-model-note").textContent =
      `主对话：${result.chat.ok ? "可用" : "失败"} — ${result.chat.message}\n`
      + `Embedding：${result.embedding.ok ? "可用" : "失败"} — ${result.embedding.message}`;
  } catch (error) {
    setError("wizard-model-error", error.message);
  } finally {
    setButtonBusy($("wizard-test"), false);
  }
}

async function finishWizard() {
  localStorage.removeItem("wizard_persona_pending");
  wizardNeedsPersona = false;
  wizardActive = false;
  wizardPersonaImported = false;
  $("wizard-steps").hidden = true;
  $("wizard-model").hidden = true;
  $("wizard-persona").hidden = true;
  $("login-form").hidden = false;
  $("wizard-persona-import").hidden = false;
  $("wizard-persona-start").hidden = true;
  $("wizard-persona-note").textContent = "";
  try {
    await enterApp(await client.json("/api/state"));
  } catch (error) {
    showEntry(error.message);
  }
}

async function openDemo() {
  try {
    const data = await client.json("/api/state", { anonymous: true });
    demoMode = true;
    await enterApp(data, { demo: true });
  } catch (error) {
    demoMode = false;
    setError("entry-error", error.message);
  }
}

function exitDemo() {
  demoMode = false;
  $("demo-banner").hidden = true;
  for (const button of document.querySelectorAll("[data-view]")) button.disabled = false;
  showEntry();
}

function renderDemoConversation() {
  log.replaceChildren();
  message("assistant", "这是演示模式。配置好模型后，我会真正记住你说过的事、共同的世界和我们的约定。");
  message("user", "这里能看到什么？");
  message("assistant", "左侧可以看世界书、状态、时间轴、她的生活、关系和画像；导入人格卡片后，角色也会跟着变。");
  systemMessage("演示模式只读。点上方「配置模型」后开始真实对话。");
}

function collectSettings() {
  const values = {};
  for (const [id, [key, secret]] of Object.entries(settingsFields)) {
    const value = $(id).value.trim();
    if (secret) {
      if (value) values[key] = value;
    } else if (value !== (settingsInitial[key] ?? "")) {
      values[key] = value;
    }
  }
  return values;
}

async function openSettings() {
  setError("settings-error");
  $("settings-note").hidden = true;
  $("settings-summary").textContent = "正在读取配置…";
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
  try {
    renderSettings(await client.settings());
  } catch (error) {
    $("settings-summary").textContent = "配置暂时无法读取。";
    setError("settings-error", error.message);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const values = collectSettings();
  if (!Object.keys(values).length) {
    setError("settings-error", "没有改动。填写或修改一项后再保存。");
    return;
  }
  setError("settings-error");
  $("settings-note").hidden = true;
  setButtonBusy($("settings-save"), true);
  try {
    const result = await client.saveSettings(values);
    renderSettings(result.keys);
    health = await client.json("/api/healthz", { anonymous: true, suppressAuth: true });
    configureEntry();
    updateComposer();
    $("settings-note").hidden = false;
    $("settings-note").textContent = result.message || "配置已保存。";
    if (result.applied && health.keys_ready) {
      toast("模型配置已生效");
      if (demoMode) {
        $("settings-dialog").close();
        demoMode = false;
        $("demo-banner").hidden = true;
        showEntry();
        return;
      }
      if (wizardActive) {
        $("settings-dialog").close();
        configureEntry();
        return;
      }
      if (client.identity.mode === "token") {
        $("settings-dialog").close();
        await enterApp(await client.json("/api/state"));
      } else {
        $("settings-dialog").close();
        $("username-input").focus();
      }
    }
  } catch (error) {
    setError("settings-error", error.message);
  } finally {
    setButtonBusy($("settings-save"), false);
  }
}

async function testSettings() {
  setError("settings-error");
  $("settings-note").hidden = true;
  setButtonBusy($("settings-test"), true);
  try {
    const result = await client.testSettings(collectSettings());
    $("settings-note").hidden = false;
    $("settings-note").textContent =
      `主对话：${result.chat.ok ? "可用" : "失败"} — ${result.chat.message}\n`
      + `Embedding：${result.embedding.ok ? "可用" : "失败"} — ${result.embedding.message}`;
  } catch (error) {
    setError("settings-error", error.message);
  } finally {
    setButtonBusy($("settings-test"), false);
  }
}

function fillPreviewList(id, items) {
  $(id).replaceChildren(...(items?.length ? items : ["（无）"]).map(text =>
    element("li", "whitespace-pre-wrap", "· " + text)));
}

function showPersonaPreview(preview, content, base64, filename) {
  pendingPersonaContent = content;
  pendingPersonaBase64 = base64;
  personaFilename = filename;
  $("persona-preview-name").textContent = preview.name || "未命名角色";
  $("persona-preview-current").textContent =
    `导入会替换当前「${preview.current_name}」v${preview.current_version}；旧版本会留在历史里，可回滚。`;
  $("persona-preview-detail").textContent =
    `${preview.format} · ${preview.traits.length} 条特质 · ${preview.greetings.length} 条开场白`
    + ` · ${preview.world_count} 条世界书` + (preview.avatar ? " · 含头像" : "");
  $("persona-preview-identity").textContent = preview.identity || "（无）";
  fillPreviewList("persona-preview-traits", preview.traits);
  fillPreviewList("persona-preview-greetings", preview.greetings);
  fillPreviewList("persona-preview-behavior", preview.behavior);
  fillPreviewList("persona-preview-world", preview.world);
  $("persona-preview-world-title").textContent = `世界书（${preview.world_count} 条）`;
  $("persona-preview-world-toggle").checked = preview.world_count > 0;
  $("persona-preview-world-toggle").disabled = preview.world_count === 0;
  $("persona-preview-warnings").replaceChildren(...(preview.warnings || []).map(text =>
    element("li", "whitespace-pre-wrap", "· " + text)));
  setError("persona-preview-error");
  $("persona-preview-dialog").showModal();
}

function toBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

async function handlePersonaFile(file) {
  if (demoMode) { toast("演示模式只读，配置模型后再导入"); return; }
  if (!file) return;
  if (file.size > 4_000_000) { toast("人格卡片太大（上限 4 MB）"); return; }
  try {
    const isPng = file.type === "image/png" || /\.png$/i.test(file.name);
    let content = "", base64 = null;
    if (isPng) {
      base64 = toBase64(new Uint8Array(await file.arrayBuffer()));
    } else {
      content = await file.text();
    }
    showPersonaPreview(await client.personaPreview(file.name, content, base64), content, base64, file.name);
  } catch (error) {
    toast(error.message);
  }
}

async function confirmPersonaImport(event) {
  event.preventDefault();
  setError("persona-preview-error");
  setButtonBusy($("persona-confirm"), true);
  try {
    const includeWorld = $("persona-preview-world-toggle").checked;
    const result = await client.personaImport(
      personaFilename, pendingPersonaContent, pendingPersonaBase64, includeWorld);
    $("persona-preview-dialog").close();
    await loadAvatar();
    await refreshState();
    const summary = [result.summary, result.world, result.avatar].filter(Boolean).join(" · ");
    if (wizardActive && wizardStep === 3) {
      wizardPersonaImported = true;
      $("wizard-persona-note").textContent = summary + "\n可以点「开始聊天」进入。";
      $("wizard-persona-import").hidden = true;
      $("wizard-persona-start").hidden = false;
      return;
    }
    toast(summary);
  } catch (error) {
    setError("persona-preview-error", error.message);
  } finally {
    setButtonBusy($("persona-confirm"), false);
  }
}

function resetAvatar() {
  if (avatarUrl) URL.revokeObjectURL(avatarUrl);
  avatarUrl = "";
  for (const id of ["header-avatar", "entry-avatar", "profile-avatar"]) {
    const img = $(id);
    if (img) img.src = "/static/assets/avatar-placeholder.png";
  }
}

async function loadAvatar() {
  try {
    const response = await client.request("/api/persona/avatar");
    const blob = await response.blob();
    if (avatarUrl) URL.revokeObjectURL(avatarUrl);
    avatarUrl = URL.createObjectURL(blob);
    for (const id of ["header-avatar", "entry-avatar", "profile-avatar"]) {
      const img = $(id);
      if (img) img.src = avatarUrl;
    }
  } catch (error) {
    if (error.status !== 404) console.warn("头像读取失败：", error.message);
    resetAvatar();
  }
}

async function downloadPersona(format) {
  try {
    const response = await client.request(
      "/api/persona/export?format=" + encodeURIComponent(format));
    const blob = await response.blob();
    const name = String(state?.name || "personality").replace(/[\\/:*?"<>|]/g, "_");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${name}-${format === "v2" ? "card-v2" : "personality"}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    $("persona-export-dialog").close();
    toast("已导出");
  } catch (error) {
    toast(error.message);
  }
}

function showEntry(message = "") {
  $("entry-screen").hidden = false;
  $("app-shell").hidden = true;
  configureEntry();
  setLoginBusy(false);
  if (wizardActive && wizardStep === 3) {
    $("wizard-persona-note").textContent = message;
  } else if (wizardActive && wizardStep === 1) {
    $("wizard-model-note").hidden = !message;
    $("wizard-model-note").textContent = message;
  } else {
    setError("entry-error", message);
  }
}

function resetSession() {
  streamController?.abort();
  streamController = null;
  audio.stop();
  for (const timer of memoryTimers) clearTimeout(timer);
  memoryTimers = [];
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  closeNav();
  state = null;
  busy = false;
  greetingBusy = false;
  viewRevision++;
  memoryCheckController?.abort();
  memoryCheckController = null;
  memoryCheckPromise = null;
  memoryCheckState = "idle";
  setButtonBusy($("memory-check"), false);
  log.replaceChildren();
  input.value = "";
  input.style.height = "";
  $("world-input").value = "";
  $("feedback-note").value = "";
  $("relationship-input").value = "";
  $("relationship-due").value = "";
  $("relationship-miss-reason").value = "";
  missPromiseId = "";
  $("life-pin-input").value = "";
  for (const id of ["slots-output", "timeline-output", "care-output", "profile-summary", "profile-text", "personality-summary", "personality-text", "world-list", "life-events", "life-said", "life-exp", "relationship-stage", "relationship-call", "relationship-detail", "relationship-why", "relationship-promises", "relationship-events", "relationship-miss-promise"]) $(id).replaceChildren();
  for (const id of ["life-state", "life-script"]) $(id).textContent = "";
  // `#119`：换账号必须清掉"见过哪些待发表"——`pending_say.id` 是**每用户各自的自增
  // rowid**，不清的话第二个账号的主动消息会被上一个账号残留的 id 挡掉（`T89`）。
  seenSay.clear();
  setUnread(0);
  $("feedback-open").disabled = true;
  $("stop").hidden = true;
  $("send").hidden = false;
  $("toast").hidden = true;
  demoMode = false;
  $("demo-banner").hidden = true;
  for (const button of document.querySelectorAll("[data-view]")) button.disabled = false;
  pendingPersonaContent = "";
  pendingPersonaBase64 = "";
  personaFilename = "";
  resetAvatar();
  setButtonBusy($("legacy-claim-confirm"), false);
  setError("legacy-claim-error");
  updateComposer();
}

async function boot() {
  const url = new URL(location.href);
  const token = url.searchParams.get("token");
  if (token) {
    localStorage.setItem("auth_token", token);
    url.searchParams.delete("token");
    history.replaceState(null, "", url.pathname + url.search + url.hash);
  }
  $("entry-retry").hidden = true;
  $("login-submit").disabled = true;
  try {
    health = await client.json("/api/healthz", { anonymous: true, suppressAuth: true });
    $("entry-status").querySelector("span:last-child").textContent = health.password_login ? health.account_setup_required ? "首次创建账号" : "身份验证已开启" : "服务需要更新";
    $("entry-status").querySelector(".status-dot").classList.remove("offline");
    if (!health.password_login) {
      showEntry("后端需要加载账号密码登录，请重启服务后重新连接。");
      $("entry-retry").hidden = false;
      return;
    }
    configureEntry();
    $("inherit-default").checked = Boolean(health.legacy_data_available);
    $("username-input").value = localStorage.getItem("auth_username") || localStorage.getItem("user_id") || "";
    if (client.restoreIdentity()) {
      if (client.identity.mode === "token") {
        try {
          const user = await client.me();
          $("username-input").value = user.username;
          configureEntry();
          if (!setupMode) {
            if (!health.keys_ready) {
              showEntry("请先配置模型 API Key，再开始聊天。");
              return;
            }
            if (wizardNeedsPersona) {
              showEntry("最后一步：导入人格卡片，或者直接开始。");
              return;
            }
            await enterApp(await client.json("/api/state"));
            return;
          }
        } catch (error) {
          if (error.status !== 401) throw error;
          showEntry("登录状态已失效，请重新登录。");
          return;
        }
      } else client.clearIdentity();
    }
    showEntry();
  } catch (error) {
    health = null;
    showEntry(error.message);
    $("entry-status").querySelector("span:last-child").textContent = "服务未连接";
    $("entry-status").querySelector(".status-dot").classList.add("offline");
    $("entry-retry").hidden = false;
  }
}

async function login() {
  if (loginBusy || !health) return;
  if (!health.keys_ready) {
    setError("entry-error", "请先配置模型 API Key，再创建账号或登录。");
    if (wizardActive) setWizardStep(1);
    else openSettings();
    return;
  }
  const username = $("username-input").value.trim();
  const password = $("password-input").value;
  const wasSetup = setupMode;
  if (!username || !password) { setError("entry-error", "请填写账号和密码。"); return; }
  if (setupMode && (password.length < 8 || password.length > 128)) { setError("entry-error", "密码需为 8 至 128 个字符。"); return; }
  if (setupMode && password !== $("password-confirm").value) { setError("entry-error", "两次密码不一致。"); return; }
  setError("entry-error");
  setLoginBusy(true);
  try {
    await client.login(username, password, { setup: setupMode, inheritDefault: $("inherit-default").checked });
    $("password-input").value = "";
    $("password-confirm").value = "";
    health.account_setup_required = false;
    health.auth_required = true;
    if (wasSetup || wizardNeedsPersona) {
      wizardActive = true;
      wizardNeedsPersona = true;
      localStorage.setItem("wizard_persona_pending", "1");
      configureEntry();
      return;
    }
    wizardActive = false;
    configureEntry();
    await enterApp(await client.json("/api/state"));
  } catch (error) {
    if (error.name === "AbortError") return;
    if (error.status === 409 && setupMode) {
      try {
        health = await client.json("/api/healthz", { anonymous: true, suppressAuth: true });
        configureEntry();
      } catch { $("entry-retry").hidden = false; }
    }
    setError("entry-error", error.message);
  } finally { setLoginBusy(false); }
}

async function loadSessionMessages(sessionId) {
  try {
    const data = await client.sessionMessages(sessionId);
    log.replaceChildren();
    for (const item of data.messages || []) {
      message(item.role === "user" ? "user" : "assistant", item.content || "", item.ts);
    }
    return (data.messages || []).length;
  } catch (error) {
    if (!isQuiet(error)) systemMessage("历史消息暂时无法加载：" + error.message);
    return 0;
  }
}

async function showGreeting(epoch = client.epoch) {
  greetingBusy = true;
  updateComposer();
  const greeting = message("assistant", "正在打个招呼…");
  try {
    const data = await client.json("/api/greeting");
    if (epoch !== client.epoch) return;
    if (data.greeting) renderAssistantMessages(data.greeting, greeting);
    else greeting.root.remove();
  } catch (error) {
    if (epoch !== client.epoch || isQuiet(error)) return;
    greeting.root.remove();
    systemMessage("开场白暂未加载，仍然可以继续聊天。");
  } finally {
    if (epoch === client.epoch) {
      greetingBusy = false;
      updateComposer();
      input.focus();
      checkLegacyClaim(epoch);
    }
  }
}

function sessionTime(ts) {
  if (!ts) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(Number(ts) * 1000));
}

function renderSessions(data) {
  const list = $("sessions-list");
  list.replaceChildren();
  const items = data.items || [];
  if (!items.length) {
    list.append(element("p", "py-6 text-sm text-muted", "还没有会话。"));
    return;
  }
  for (const item of items) {
    const row = element("div", "flex items-center gap-3 py-3");
    const select = element("button", "min-w-0 flex-1 text-left");
    select.type = "button";
    select.append(
      element("p", "truncate font-medium " + (item.active ? "text-brand" : ""),
              item.title || "新对话"),
      element("p", "mt-1 text-xs text-muted",
              `${item.message_count} 条消息${item.updated_at ? " · " + sessionTime(item.updated_at) : ""}`));
    select.addEventListener("click", () => switchSession(item.id));
    const rename = element("button", "text-xs text-muted", "改名");
    rename.type = "button";
    rename.addEventListener("click", () => renameSession(item));
    const remove = iconButton("trash-2", "删除会话");
    remove.addEventListener("click", () => removeSession(item));
    row.append(select, rename, remove);
    list.append(row);
  }
}

async function refreshSessions() {
  try {
    const data = await client.sessions();
    renderSessions(data);
    return data;
  } catch (error) {
    setError("sessions-error", error.message);
    return null;
  }
}

async function openSessions() {
  setError("sessions-error");
  if (!$("sessions-dialog").open) $("sessions-dialog").showModal();
  await refreshSessions();
}

async function switchSession(sessionId) {
  try {
    await client.selectSession(sessionId);
    const data = await client.json("/api/state");
    renderState(data);
    const count = await loadSessionMessages(sessionId);
    $("sessions-dialog").close();
    if (!count) await showGreeting();
    else { updateComposer(); input.focus(); }
    toast("已切换会话");
  } catch (error) {
    setError("sessions-error", error.message);
  }
}

async function newSession() {
  try {
    await client.createSession();
    const data = await client.json("/api/state");
    renderState(data);
    log.replaceChildren();
    $("sessions-dialog").close();
    await showGreeting();
    toast("已新建会话");
  } catch (error) {
    setError("sessions-error", error.message);
  }
}

async function renameSession(item) {
  const title = prompt("会话名称", item.title || "新对话");
  if (title === null) return;
  if (!title.trim()) { setError("sessions-error", "会话名称不能为空。"); return; }
  try {
    await client.renameSession(item.id, title.trim());
    await refreshSessions();
  } catch (error) {
    setError("sessions-error", error.message);
  }
}

async function removeSession(item) {
  if (!confirm(`删除「${item.title || "新对话"}」及其消息？长期记忆不会被删除。`)) return;
  try {
    await client.deleteSession(item.id);
    const data = await client.json("/api/state");
    renderState(data);
    const count = await loadSessionMessages(data.session);
    await refreshSessions();
    if (!count) await showGreeting();
  } catch (error) {
    setError("sessions-error", error.message);
  }
}

async function downloadAllData() {
  try {
    const response = await client.request("/api/export");
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "jiushi-export.zip";
    link.click();
    URL.revokeObjectURL(link.href);
    toast("已导出全部数据");
  } catch (error) {
    toast(error.message);
  }
}

async function enterApp(data, { demo = false } = {}) {
  demoMode = demo;
  const epoch = client.epoch;
  $("entry-screen").hidden = true;
  $("app-shell").hidden = false;
  $("demo-banner").hidden = !demo;
  for (const button of document.querySelectorAll("[data-view]")) {
    button.disabled = demo && !["chat", "profile"].includes(button.dataset.view);
  }
  $("persona-import").disabled = demo;
  $("persona-export").disabled = demo;
  $("export-all").disabled = demo;
  $("sessions-open").disabled = demo;
  $("command-open").disabled = demo;
  $("feedback-open").disabled = demo;
  log.replaceChildren();
  followLatest = true;
  renderState(data);
  $("current-user").textContent = demo ? "演示模式" : client.username || "已登录";
  $("current-user").title = $("current-user").textContent;
  $("user-avatar").textContent = demo ? "演" : Array.from(client.username)[0] || "你";
  $("identity-kind").textContent = demo ? "只读预览" : "账号登录";
  navigate("chat");
  if (demo) {
    resetAvatar();
    renderDemoConversation();
    updateComposer();
    return;
  }
  loadAvatar();
  const historyCount = await loadSessionMessages(data.session || "");
  if (historyCount) {
    greetingBusy = false;
    updateComposer();
    input.focus();
    checkLegacyClaim(epoch);
  } else {
    await showGreeting(epoch);
  }
}

async function checkLegacyClaim(epoch) {
  try {
    const status = await client.json("/api/auth/legacy");
    if (epoch !== client.epoch || !status?.available) return;
    const dialog = $("legacy-claim-dialog");
    $("legacy-claim-message").textContent =
      "这里有一套还没有绑定账号的旧世界书、时间轴和画像。\n认领后，这个账号会继续使用那段历史；当前账号目录会保留，不会覆盖。";
    setError("legacy-claim-error");
    if (!dialog.open) dialog.showModal();
  } catch (error) {
    if (epoch !== client.epoch || isQuiet(error) || error.status === 404) return;
    console.warn("旧记忆认领状态暂时无法读取：", error.message);
  }
}

async function claimLegacy() {
  let epoch = client.epoch;
  const button = $("legacy-claim-confirm");
  if (button.disabled) return;
  setButtonBusy(button, true);
  setError("legacy-claim-error");
  try {
    await client.claimLegacy();
    if (epoch !== client.epoch) return;
    const identity = { ...client.identity }, username = client.username;
    client.setIdentity(identity.mode, identity.value);
    client.username = username;
    client.persistIdentity();
    epoch = client.epoch;
    resetSession();
    await refreshState();
    if (epoch === client.epoch) toast("旧记忆已接回这个账号");
  } catch (error) {
    if (epoch === client.epoch && !isQuiet(error)) setError("legacy-claim-error", error.message);
  } finally {
    if (epoch === client.epoch) setButtonBusy(button, false);
  }
}

function renderState(data) {
  state = data;
  const herName = String(data.name || "头像");
  for (const id of ["entry-avatar", "header-avatar"]) {
    const img = $(id);
    if (img) img.alt = herName;
  }
  $("world-badge").textContent = Number(data.world_count || 0);
  $("world-summary").textContent = Number(data.world_count || 0) + " 条设定";
  $("profile-summary").textContent = displayMemoryText(data.profile || "还没有记录。");
  $("profile-text").textContent = displayMemoryText(data.profile_text || "还没有记录。");
  $("personality-summary").textContent = displayMemoryText(data.personality || "还没有记录。");
  $("personality-text").textContent = displayMemoryText(data.personality_text || "还没有记录。");
  const issues = Array.isArray(data.memory_issues) ? data.memory_issues : [];
  const memoryCheckError = String(data.memory_check_error || "").trim();
  $("issue-badge").hidden = !issues.length && !memoryCheckError;
  $("care-summary").textContent = memoryCheckError || (issues.length ? `${issues.length} 处需要留意` : "目前没有待处理的启动检查问题");
  $("memory-issues").replaceChildren(...issues.map(issue => element("li", "error-message",
    displayMemoryText(typeof issue === "string" ? issue : JSON.stringify(issue)))));
  $("model-details").replaceChildren();
  for (const [label, value] of [["对话模型", data.model], ["对话服务", data.chat_provider], ["后台模型", data.bg_model], ["记忆", data.memory]]) {
    const row = element("div", "flex gap-5 py-3 text-xs");
    row.append(element("dt", "w-20 shrink-0 text-muted", label), element("dd", "min-w-0 break-all", String(value || "未提供")));
    $("model-details").append(row);
  }
  renderWorld(data.world || []);
}

async function refreshState() {
  const epoch = client.epoch;
  const data = await client.json("/api/state");
  if (epoch === client.epoch) renderState(data);
}

function renderWorld(entries) {
  $("world-list").replaceChildren();
  if (!entries.length) {
    $("world-list").append(element("p", "py-10 text-sm text-muted", "还没有共同的设定。"));
    return;
  }
  entries.forEach((entry, index) => {
    const row = element("article", "world-row");
    const content = element("div", "min-w-0 flex-1");
    content.append(element("p", "break-words whitespace-pre-wrap leading-7", displayMemoryText(entry.text)));
    content.append(element("span", "source-badge", entry.source === "manual" ? "手动记录" : "来自对话"));
    const remove = iconButton("trash-2", `删除第 ${index + 1} 条设定`);
    remove.addEventListener("click", () => action(remove, async epoch => {
      await client.json("/api/world/" + index, { method: "DELETE" });
      if (epoch !== client.epoch) return;
      await refreshState();
      toast("已删除设定");
    }));
    row.append(content, remove);
    $("world-list").append(row);
  });
}

function navigate(view) {
  currentView = view;
  viewRevision++;
  closeNav();
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  $("view-title").textContent = titles[view];
  $("view-subtitle").textContent = view === "chat" ? new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" }).format(new Date()) : "旧识";
  $("chat-view").hidden = view !== "chat";
  $("memory-view").hidden = view === "chat";
  for (const section of document.querySelectorAll(".memory-section")) section.hidden = section.id !== "view-" + view;
  setError("memory-error");
  if (view === "slots") loadCommandView("/slots", "slots-output");
  if (view === "timeline") loadCommandView("/timeline", "timeline-output");
  if (view === "life") loadLifeBoard();
  if (view === "relationship") loadRelationshipBoard();
  if (view === "care" && memoryCheckState !== "success") startMemoryCheck();
  if (view === "chat") { setUnread(0); scrollLatest(); }
}

/** T86/T117/T91：一行一条的列表（生活流水、她说过的话、她的经历都长这样）。
 *  `rows` 是 `[meta, text, badge, rid?]`；**全部走 textContent**（`element` 里做的），
 *  不拼 innerHTML——服务端文本一律当数据用。
 *  `action`（可选）= `{icon, label, run(button, text, rid)}`：`T91` 之后这一屏能动手了。 */
function renderRows(id, rows, action) {
  const box = $(id);
  box.replaceChildren();
  if (!rows.length) {
    box.append(element("p", "py-6 text-sm text-muted", "（还没有）"));
    return;
  }
  for (const [meta, text, badge, rid] of rows) {
    const row = element("article", "world-row");
    const content = element("div", "min-w-0 flex-1");
    content.append(element("p", "break-words whitespace-pre-wrap leading-7", text));
    content.append(element("span", "source-badge", badge ? `${meta}　${badge}` : meta));
    row.append(content);
    if (action) {
      const button = iconButton(action.icon, action.label);
      button.addEventListener("click", () => action.run(button, text, rid));
      row.append(button);
    }
    box.append(row);
  }
}

/** 「她做过什么」看板（`#117`）：她的生活一直看不见，而用户会问"薄荷是哪里来的"。
 *  所以这一屏的重点是**每条都标出处**（哪张表、谁写的、什么时候），
 *  让"她做过什么 / 她为什么这么说"能当场对上账。 */
async function loadLifeBoard() {
  $("life-state").textContent = "正在读取…";
  return lifeBoard.load();
}

function renderLifeBoard(data) {
    const st = data.state || {}, stats = data.stats || {};
    const reports = data.reports || [];
    $("life-state").textContent = [
      data.catchup?.running ? `正在补记这段时间的生活（还差 ${data.catchup.remaining_slots ?? "几"} 格），完成后会自动刷新。` : "",
      `${displayMemoryText(st.advisory_label || "自动推演 · 只当氛围")}`,
      `${displayMemoryText(st.her_name || "她")}（系统推演）：在${displayMemoryText(st.place || "？")}，${displayMemoryText(st.activity || "？")}`,
      st.mood ? `心情：${displayMemoryText(st.mood)}` : "",
      st.on_mind ? `心里挂着：${displayMemoryText(st.on_mind)}` : "",
      `时段：${st.slot_date || ""} ${st.slot_label || ""}（${st.updated_when || "—"}更新）`,
      `今天主动开口 ${stats.said_today ?? 0}/${stats.daily_max ?? "?"} 次`
        + `｜她报备过 ${reports.length} 条`
        + `｜可见流水 ${stats.events_total ?? 0} 条`
        + (stats.next_slot_when ? `｜下一格大约 ${stats.next_slot_when}` : ""),
      "（跨时段后推进；长时间离线会分段补记概况，最近几格详细记录）",
    ].filter(Boolean).map(displayMemoryText).join("\n");
    $("life-badge").textContent = String(stats.events_total ?? 0);
    renderRows("life-events", (data.events || []).map(e =>
      [`${e.when}｜${e.slot_label}`, displayMemoryText(e.text), e.source_label, e.rid]), {
      icon: "eye-off", label: "从看板隐藏（保留她的历史）",
      run: (button, text, rid) => action(button, async () => {
        const out = await client.json(`/api/life/events/${rid}`, { method: "DELETE" });
        toast(out.message || "已从看板隐藏，历史仍保留");
        await loadLifeBoard();
      }),
    });
    renderRows("life-said", reports.map(s => [s.when, displayMemoryText(s.text), s.status]), {
      icon: "wand-sparkles", label: "钉进她的牵挂（让她以后当真，不再只冒一次）",
      run: (_button, text) => {
        $("life-pin-input").value = text;
        setError("life-pin-error");
        $("life-pin-dialog").showModal();
        $("life-pin-input").focus();
      },
    });
    renderRows("life-exp", (data.experiences || []).map(x => [x.when, displayMemoryText(x.text), "记忆里的她"]));
    $("life-script").textContent = [
      `职责：${displayMemoryText(data.script?.duty || "（未写）")}`,
      `偏好：${displayMemoryText(data.script?.tastes || "（未写）")}`,
      `牵挂：${displayMemoryText(data.script?.cares || "（未写）")}`,
      `她爱去：${displayMemoryText((data.favorite_places || []).join("、") || "（未写）")}`,
      `她在意：${displayMemoryText((data.cares || []).join("、") || "（未写）")}`,
      "",
      "⚠ 这一层只有**维度**：她每一格具体做什么由她自己选（模型生成），",
      "所以「薄荷」「月季」这类具体细节是她在那一格现编的，**不在这一层里**，",
      "也不会落进任何一张表——想让她固定养某盆花，就写进上面的偏好/牵挂。",
    ].join("\n");
}

async function loadRelationshipBoard() {
  $("relationship-stage").textContent = "正在读取…";
  return relationshipBoard.load();
}

function relationshipWhen(stamp) {
  if (!Number.isFinite(Number(stamp)) || Number(stamp) <= 0) return "时间未记录";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(Number(stamp) * 1000));
}

function renderRelationship(data) {
  const stage = data.stage || {};
  const history = data.history || [];
  const latest = history.at(-1);
  $("relationship-stage").textContent = displayMemoryText(stage.key || "还没有关系记录");
  $("relationship-call").textContent = `她叫你：${displayMemoryText(data.call_him || stage.call_him || "你")}`;
  $("relationship-detail").textContent = [
    data.since ? `从 ${data.since} 开始` : "",
    Number.isFinite(data.days) ? `相识 ${data.days} 天` : "",
    Number.isFinite(data.user_msgs) ? `你说过 ${data.user_msgs} 条话` : "",
  ].filter(Boolean).join(" · ");
  $("relationship-why").textContent = latest?.why ? `最近的变化：${displayMemoryText(latest.why)}` : "关系会随着相处慢慢变化。";
  const promises = Array.isArray(data.promises) ? data.promises : [];
  const box = $("relationship-promises");
  box.replaceChildren();
  if (!promises.length) box.append(element("p", "py-6 text-sm text-muted", "还没有记下承诺。"));
  for (const promise of promises) {
    const row = element("article", "py-5");
    row.append(element("p", "break-words whitespace-pre-wrap leading-7", displayMemoryText(promise.text)));
    const overdue = promise.status === "open" && (promise.overdue === true || (Number(promise.due_at) > 0 && Number(promise.due_at) <= Date.now() / 1000));
    const status = overdue ? "已到期 · 待确认" : ({ open: "待履约", done: "已完成", missed: "这次没做到" }[promise.status] || "状态待确认");
    row.append(element("span", "source-badge", status));
    if (promise.due_at) row.append(element("p", "mt-2 text-xs text-muted", `约定时间：${relationshipWhen(promise.due_at)}`));
    if (promise.reason) row.append(element("p", "mt-2 whitespace-pre-wrap text-sm text-muted", `原因：${displayMemoryText(promise.reason)}`));
    if (promise.note) row.append(element("p", "mt-2 whitespace-pre-wrap text-sm text-muted", displayMemoryText(promise.note)));
    if (promise.status === "open") {
      const controls = element("div", "mt-3 flex flex-wrap gap-2");
      const complete = element("button", "secondary-button", "已完成");
      complete.type = "button";
      complete.addEventListener("click", () => action(complete, async epoch => {
        const revision = viewRevision;
        const result = await relationshipBoard.complete(promise.id);
        if (epoch !== client.epoch || revision !== viewRevision) return;
        await loadRelationshipBoard();
        if (epoch === client.epoch && revision === viewRevision) toast(result.message || "记下了，承诺已完成");
      }));
      const miss = element("button", "secondary-button", "这次没做到");
      miss.type = "button";
      miss.addEventListener("click", () => {
        missPromiseId = promise.id;
        $("relationship-miss-promise").textContent = displayMemoryText(promise.text);
        $("relationship-miss-reason").value = "";
        setError("relationship-miss-error");
        $("relationship-miss-dialog").showModal();
      });
      controls.append(complete, miss);
      row.append(controls);
    }
    box.append(row);
  }
  const eventLabels = { promise_created: "记下承诺", promise_completed: "承诺已完成", promise_missed: "这次没做到", promise_reminded: "聊起约定", stage_changed: "关系变化" };
  renderRows("relationship-events", [...(data.events || [])].reverse().map(event => [
    relationshipWhen(event.ts),
    event.promise ? `${displayMemoryText(event.promise)}\n${displayMemoryText(event.text || "")}` : displayMemoryText(event.text || "关系留下了一条记录"),
    eventLabels[event.kind] || "关系记录",
  ]));
}

async function loadCommandView(command, id) {
  const epoch = client.epoch, revision = viewRevision;
  $(id).textContent = "正在读取…";
  try {
    const data = await client.json("/api/command", { method: "POST", body: { command } });
    if (epoch === client.epoch && revision === viewRevision) $(id).textContent = displayMemoryText(data.output || "暂无记录。");
  } catch (error) {
    if (epoch === client.epoch && revision === viewRevision && !isQuiet(error)) $(id).textContent = error.message;
  }
}

function startMemoryCheck() {
  if (memoryCheckPromise) return memoryCheckPromise;
  const epoch = client.epoch;
  const button = $("memory-check");
  const controller = new AbortController();
  memoryCheckController = controller;
  let timedOut = false;
  memoryCheckState = "loading";
  $("care-output").textContent = "正在读取…";
  setButtonBusy(button, true);
  const request = (async () => {
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, MEMORY_CHECK_TIMEOUT_MS);
    try {
      const data = await client.json("/api/command", {
        method: "POST", body: { command: "/mem check" }, signal: controller.signal,
      });
      if (epoch !== client.epoch) return;
      const reportError = String(data.memory_check_error || "").trim();
      memoryCheckState = reportError ? "error" : "success";
      $("care-output").textContent = displayMemoryText(data.output || "检查完成。");
      if (Array.isArray(data.memory_issues)) {
        const issues = data.memory_issues;
        $("issue-badge").hidden = !issues.length && !reportError;
        $("care-summary").textContent = reportError || (issues.length ? `${issues.length} 处需要留意` : "目前没有待处理的启动检查问题");
        $("memory-issues").replaceChildren(...issues.map(issue => element("li", "error-message",
          displayMemoryText(typeof issue === "string" ? issue : JSON.stringify(issue)))));
      }
    } catch (error) {
      if (epoch !== client.epoch) return;
      memoryCheckState = "error";
      $("care-output").textContent = timedOut ? "记忆体检超时，请稍后重试。" : error.message;
    } finally {
      clearTimeout(timer);
      if (memoryCheckController === controller) memoryCheckController = null;
      if (epoch === client.epoch) setButtonBusy(button, false);
      if (memoryCheckPromise === request) memoryCheckPromise = null;
    }
  })();
  memoryCheckPromise = request;
  return request;
}

function scrollLatest(force = false) {
  if (force || followLatest) log.scrollTop = log.scrollHeight;
}

function systemMessage(text) {
  log.append(element("p", "system-message", displayMemoryText(text)));
  scrollLatest();
}

function message(role, text = "", ts = null) {
  const root = element("article", "message " + (role === "user" ? "me" : "her"));
  const content = element("div", "message-content");
  const when = ts ? new Date(Number(ts) * 1000) : new Date();
  const meta = element("p", "message-meta", (role === "user" ? "你" : state?.name || "沈知夏") + "  " + new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(when));
  const body = element("p", "message-bubble", text);
  content.append(meta, body);
  if (role !== "user") {
    const avatar = element("img", "message-avatar");
    avatar.src = "/static/assets/avatar-placeholder.png";
    avatar.alt = "";
    avatar.width = avatar.height = 32;
    root.append(avatar);
    const tools = element("div", "message-tools");
    const copy = iconButton("copy", "复制回复");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(body.textContent); toast("已复制回复"); }
      catch { toast("复制失败，请选择文字后复制。"); }
    });
    tools.append(copy);
    content.append(tools);
  }
  root.append(content);
  log.append(root);
  scrollLatest();
  return { root, content, text: body };
}

/** T86：同一轮里的**后续**气泡。真人连发消息不会每条都重挂头像和昵称，
 *  所以这里只留正文——召回明细、「她说错了」这些仍然挂在**第一条**上。 */
function continuationBubble(text = "") {
  const root = element("article", "message her continuation");
  const content = element("div", "message-content");
  const body = element("p", "message-bubble", text);
  content.append(body);
  root.append(content);
  log.append(root);
  scrollLatest();
  return { root, content, text: body };
}

function renderAssistantMessages(text, first) {
  const bubbles = first ? [first] : [];
  const splitter = new BubbleSplitter({
    onMessage: (content, index) => {
      if (!bubbles[index]) bubbles[index] = index === 0
        ? message("assistant", "") : continuationBubble("");
      bubbles[index].text.textContent = content;
    }
  });
  try {
    splitter.push(text);
    splitter.end();
  } catch (error) {
    for (const bubble of bubbles) bubble.root.remove();
    throw error;
  }
  if (!splitter.segments.length && first) first.root.remove();
  scrollLatest();
  return bubbles;
}

function renderRecall(holder, entries) {
  if (!entries.length) return;
  const details = element("details", "recall");
  const summary = element("summary");
  summary.append(icon("chevron-right"), document.createTextNode(`想起了 ${entries.length} 条`));
  details.append(summary);
  for (const entry of entries) {
    const row = element("div", "recall-item");
    row.append(element("span", "min-w-0", displayMemoryText(entry.text || "")), element("span", "shrink-0 text-faint", typeof entry.sim === "number" ? entry.sim.toFixed(2) : ""));
    details.append(row);
  }
  holder.content.append(details);
}

function updateComposer() {
  input.disabled = greetingBusy || $("app-shell").hidden || demoMode || !health?.keys_ready;
  $("send").disabled = busy || greetingBusy || !input.value.trim() || demoMode || !health?.keys_ready;
  $("send").hidden = busy;
  $("stop").hidden = !busy;
  $("composer").setAttribute("aria-busy", String(busy));
}

async function sendMessage() {
  const text = input.value.trim();
  if (demoMode) return;
  if (!text || busy || greetingBusy || $("app-shell").hidden) return;
  if (!health?.keys_ready) {
    toast("请先在设置里配置模型 API Key");
    openSettings();
    return;
  }
  const epoch = client.epoch;
  input.value = "";
  input.style.height = "";
  busy = true;
  followLatest = true;
  updateComposer();
  message("user", text);
  audio.stop();
  const controller = new AbortController();
  streamController = controller;
  let reply;
  let received = "";
  let hasAudio = false;
  let mutedThisReply = !ttsOn;
  const initialVoiceRevision = voiceRevision;
  try {
    if (text.startsWith("/")) {
      const result = await client.json("/api/command", { method: "POST", body: { command: text }, signal: controller.signal });
      if (epoch !== client.epoch) return;
      systemMessage(result.output || "命令已执行。");
      await refreshState();
      if (currentView !== "chat") navigate(currentView);
    } else {
      reply = message("assistant", "正在想…");
      // T86：一轮可能被她切成 1~3 条（`@@@` 是后端提示词约定的**内部标记**，
      // 后端只在会切分的路径上给这条指令）。第 0 条复用上面那个占位气泡——
      // 召回明细与「她说错了」都挂在它身上；之后几条走 `continuationBubble`。
      const bubbles = [reply];
      const splitter = new BubbleSplitter({
        onMessage: (chunk, index) => {
          if (epoch !== client.epoch) return;
          if (!bubbles[index]) bubbles[index] = continuationBubble("");
          bubbles[index].text.textContent = chunk;
          scrollLatest();
        }
      });
      const response = await client.request("/api/chat/stream", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text }), signal: controller.signal });
      await readEvents(response, event => {
        if (epoch !== client.epoch) return;
        if (event.type === "meta") renderRecall(reply, Array.isArray(event.recalled) ? event.recalled : []);
        else if (event.type === "delta") { received += event.text || ""; splitter.push(event.text || ""); }
        else if (event.type === "done") {
          splitter.end();
          if (!received) reply.text.textContent = "这次没有收到回复正文。";
          const note = [event.memory_note, event.profile_note].filter(Boolean).join(" · ");
          if (note) systemMessage(note);
          $("feedback-open").disabled = false;
          for (const delay of event.memory_async ? [1800, 5000] : [0]) memoryTimers.push(setTimeout(() => {
            if (epoch === client.epoch) refreshState().catch(error => { if (!isQuiet(error)) toast(error.message); });
          }, delay));
        } else if (event.type === "audio") {
          if (!ttsOn || voiceRevision !== initialVoiceRevision) mutedThisReply = true;
          if (mutedThisReply) return;
          try {
            if (!hasAudio) { audio.start(); hasAudio = true; }
            audio.push(event.wav_b64 || "");
          } catch { audio.stop(); mutedThisReply = true; toast("这次语音暂未播放，文字回复已保留。"); }
        } else if (event.type === "audio_end" && hasAudio && !mutedThisReply) audio.end();
      }, controller.signal);
      splitter.end();   // 流被中断时 `done` 不会到，这里兜一次（`end()` 可重复调用）
    }
  } catch (error) {
    if (epoch !== client.epoch) return;
    if (reply && !received) reply.root.remove();
    if (error.name === "AbortError") systemMessage("已停止回复。");
    else if (!(error instanceof ApiError && error.status === 401)) systemMessage(error.message);
  } finally {
    if (epoch === client.epoch) {
      busy = false;
      streamController = null;
      updateComposer();
      if (currentView === "chat") input.focus();
    }
  }
}

function updateVoiceButton() {
  const label = ttsOn ? "关闭语音" : "打开语音";
  $("tts-toggle").replaceChildren(icon(ttsOn ? "volume-2" : "volume-x"));
  $("tts-toggle").setAttribute("aria-pressed", String(ttsOn));
  $("tts-toggle").setAttribute("aria-label", label);
  $("tts-toggle").dataset.tooltip = label;
}

function openNav() {
  $("sidebar").classList.add("open");
  $("sidebar").inert = false;
  $("sidebar").setAttribute("role", "dialog");
  $("sidebar").setAttribute("aria-modal", "true");
  $("sidebar").nextElementSibling.inert = true;
  $("nav-backdrop").hidden = false;
  $("nav-open").setAttribute("aria-expanded", "true");
  $("nav-close").focus();
}

function closeNav() {
  const wasOpen = $("sidebar").classList.contains("open");
  $("sidebar").classList.remove("open");
  $("sidebar").inert = matchMedia("(max-width: 767px)").matches;
  $("sidebar").removeAttribute("role");
  $("sidebar").removeAttribute("aria-modal");
  $("sidebar").nextElementSibling.inert = false;
  $("nav-backdrop").hidden = true;
  $("nav-open").setAttribute("aria-expanded", "false");
  if (wasOpen && !$("app-shell").hidden) $("nav-open").focus();
}

function setupTooltips() {
  const tip = element("div", "tooltip");
  tip.hidden = true;
  tip.id = "tool-tip";
  tip.setAttribute("role", "tooltip");
  document.body.append(tip);
  for (const button of document.querySelectorAll("button[title]")) {
    button.dataset.tooltip = button.title;
    button.removeAttribute("title");
  }
  function show(event) {
    const target = event.target.closest?.("[data-tooltip]");
    if (!target || target.disabled || (event.type === "mouseover" && !matchMedia("(hover: hover)").matches)) return;
    tip.textContent = target.dataset.tooltip;
    tip.hidden = false;
    const rect = target.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(innerWidth - tip.offsetWidth - 8, rect.left + rect.width / 2 - tip.offsetWidth / 2)) + "px";
    tip.style.top = (rect.bottom + 8 + tip.offsetHeight > innerHeight ? rect.top - tip.offsetHeight - 8 : rect.bottom + 8) + "px";
  }
  const hide = () => { tip.hidden = true; };
  document.addEventListener("mouseover", show);
  document.addEventListener("focusin", show);
  document.addEventListener("mouseout", hide);
  document.addEventListener("focusout", hide);
  document.addEventListener("click", hide);
  document.addEventListener("scroll", hide, true);
  window.addEventListener("resize", hide);
}

const commands = [["查看记忆", "/mem"], ["记忆体检", "/mem check"], ["记忆事件", "/mem events"],
  ["已退场记忆", "/mem dead"], ["删除一条记忆", "/mem del "], ["世界书", "/world"],
  ["删除一条设定", "/world del "], ["整理世界书", "/world tidy"], ["时间轴", "/timeline"],
  ["状态槽位", "/slots"], ["用户信息", "/user"], ["用户画像", "/profile"],
  ["查看人设", "/personality"], ["演化人设", "/personality evolve"], ["重置人设", "/personality reset"],
  ["她说错了的修正", "/fix"], ["你现在在哪座城市", "/city"],
  ["清空当前对话", "/clear"], ["清除长期记忆", "/forget"]];
for (const [label, command] of commands) {
  const button = element("button", "command-row");
  button.type = "button";
  button.append(element("span", "text-sm", label), element("code", "shrink-0", command));
  button.addEventListener("click", () => {
    $("command-dialog").close();
    navigate("chat");
    input.value = command;
    updateComposer();
    input.focus();
  });
  $("command-list").append(button);
}

$("login-form").addEventListener("submit", event => { event.preventDefault(); login(); });
$("settings-open").addEventListener("click", openSettings);
$("sessions-open").addEventListener("click", openSessions);
$("session-new").addEventListener("click", newSession);
$("export-all").addEventListener("click", downloadAllData);
$("wizard-model").addEventListener("submit", saveWizardModel);
$("wizard-test").addEventListener("click", testWizardModel);
$("wizard-more").addEventListener("click", openSettings);
$("wizard-persona-import").addEventListener("click", () => $("persona-file").click());
$("wizard-persona-skip").addEventListener("click", finishWizard);
$("wizard-persona-start").addEventListener("click", finishWizard);
$("demo-open").addEventListener("click", openDemo);
$("demo-exit").addEventListener("click", exitDemo);
$("demo-settings").addEventListener("click", openSettings);
$("settings-form").addEventListener("submit", saveSettings);
$("settings-test").addEventListener("click", testSettings);
$("persona-import").addEventListener("click", () => $("persona-file").click());
$("persona-file").addEventListener("change", event => {
  const file = event.target.files?.[0];
  event.target.value = "";
  handlePersonaFile(file);
});
$("persona-preview-form").addEventListener("submit", confirmPersonaImport);
$("persona-export").addEventListener("click", () => $("persona-export-dialog").showModal());
$("persona-export-native").addEventListener("click", () => downloadPersona("native"));
$("persona-export-v2").addEventListener("click", () => downloadPersona("v2"));
for (const zone of document.querySelectorAll("[data-persona-drop]")) {
  zone.addEventListener("dragover", event => {
    event.preventDefault();
    zone.classList.add("is-dragging");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-dragging"));
  zone.addEventListener("drop", event => {
    event.preventDefault();
    zone.classList.remove("is-dragging");
    handlePersonaFile(event.dataTransfer?.files?.[0]);
  });
}
$("legacy-claim-form").addEventListener("submit", event => { event.preventDefault(); claimLegacy(); });
$("legacy-claim-dismiss").addEventListener("click", () => $("legacy-claim-dialog").close());
$("entry-retry").addEventListener("click", () => { client.cancelPending(); boot(); });
$("toggle-password").addEventListener("click", () => {
  const visible = $("password-input").type === "password";
  $("password-input").type = visible ? "text" : "password";
  const label = visible ? "隐藏密码" : "显示密码";
  $("toggle-password").setAttribute("aria-label", label);
  $("toggle-password").dataset.tooltip = label;
  $("toggle-password").replaceChildren(icon(visible ? "eye-off" : "eye"));
});
$("switch-identity").addEventListener("click", async () => {
  if (loginBusy) return;
  setLoginBusy(true);
  let errorMessage = "";
  try { await client.logout(); }
  catch (error) { if (error.status !== 401) errorMessage = "已退出此浏览器，服务端退出暂未完成。"; }
  resetSession();
  $("password-input").value = "";
  $("password-input").type = "password";
  $("toggle-password").setAttribute("aria-label", "显示密码");
  $("toggle-password").dataset.tooltip = "显示密码";
  $("toggle-password").replaceChildren(icon("eye"));
  $("password-confirm").value = "";
  showEntry(errorMessage);
});
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => navigate(button.dataset.view));
$("brand-home").addEventListener("click", () => {
  navigate("chat");
  input.focus();
});
$("service-details").addEventListener("click", () => { navigate("care"); $("model-details").parentElement.open = true; });
$("refresh-state").addEventListener("click", () => action($("refresh-state"), async () => {
  await refreshState();
  if (["slots", "timeline", "care", "relationship"].includes(currentView)) navigate(currentView);
  toast("状态已刷新");
}));
$("tts-toggle").addEventListener("click", () => {
  ttsOn = !ttsOn;
  localStorage.setItem("tts", ttsOn ? "on" : "off");
  if (!ttsOn) { voiceRevision++; audio.stop(); }
  updateVoiceButton();
  toast(ttsOn ? "语音已打开" : "语音已关闭");
});
$("composer").addEventListener("submit", event => { event.preventDefault(); sendMessage(); });
input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; updateComposer(); });
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); sendMessage(); }
});
$("stop").addEventListener("click", () => { streamController?.abort(); audio.stop(); });
log.addEventListener("scroll", () => { followLatest = log.scrollHeight - log.scrollTop - log.clientHeight < 80; $("scroll-bottom").hidden = followLatest; });
$("scroll-bottom").addEventListener("click", () => { followLatest = true; scrollLatest(true); });
$("world-form").addEventListener("submit", event => {
  event.preventDefault();
  const text = $("world-input").value.trim();
  if (!text) { setError("memory-error", "请先填写设定。"); return; }
  action($("world-add"), async epoch => {
    const result = await client.json("/api/world", { method: "POST", body: { text } });
    if (epoch !== client.epoch) return;
    $("world-input").value = "";
    await refreshState();
    toast(result.output || "已添加设定");
  });
});
$("relationship-form").addEventListener("submit", event => {
  event.preventDefault();
  action($("relationship-add"), async epoch => {
    const revision = viewRevision;
    const result = await relationshipBoard.add($("relationship-input").value, $("relationship-due").value);
    if (epoch !== client.epoch || revision !== viewRevision) return;
    $("relationship-input").value = "";
    $("relationship-due").value = "";
    await loadRelationshipBoard();
    if (epoch === client.epoch && revision === viewRevision) toast(result.message || "记下了承诺");
  });
});
$("relationship-miss-form").addEventListener("submit", event => {
  event.preventDefault();
  action($("relationship-miss-submit"), async epoch => {
    const revision = viewRevision;
    const result = await relationshipBoard.miss(missPromiseId, $("relationship-miss-reason").value);
    if (epoch !== client.epoch || revision !== viewRevision) return;
    $("relationship-miss-dialog").close();
    missPromiseId = "";
    await loadRelationshipBoard();
    if (epoch === client.epoch && revision === viewRevision) toast(result.message || "留下了这次没做到的记录");
  }, "relationship-miss-error");
});
$("life-pin-form").addEventListener("submit", event => {
  event.preventDefault();
  let text;
  try {
    text = validateCareText($("life-pin-input").value);
  } catch (error) {
    setError("life-pin-error", error.message);
    return;
  }
  action($("life-pin-submit"), async epoch => {
    const revision = viewRevision;
    const out = await client.json("/api/life/pin", { method: "POST", body: { text } });
    if (epoch !== client.epoch || revision !== viewRevision) return;
    $("life-pin-dialog").close();
    await loadLifeBoard();
    if (epoch === client.epoch && revision === viewRevision) toast(out.message || "已钉进去");
  }, "life-pin-error");
});
$("world-tidy").addEventListener("click", () => action($("world-tidy"), async () => {
  const result = await client.json("/api/command", { method: "POST", body: { command: "/world tidy" } });
  await refreshState();
  toast(result.output || "世界书已整理");
}));
$("memory-check").addEventListener("click", () => startMemoryCheck());
$("feedback-open").addEventListener("click", () => { setError("feedback-error"); $("feedback-dialog").showModal(); });
$("command-open").addEventListener("click", () => $("command-dialog").showModal());
$("feedback-form").addEventListener("submit", event => {
  event.preventDefault();
  action($("feedback-submit"), async epoch => {
    const data = await client.json("/api/feedback", { method: "POST", body: { note: $("feedback-note").value.trim() } });
    if (epoch !== client.epoch) return;
    $("feedback-dialog").close();
    $("feedback-note").value = "";
    systemMessage(data.output || "反馈已记录。");
    toast("反馈已记录");
  }, "feedback-error");
});
for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => $(button.dataset.close).close());
for (const dialog of document.querySelectorAll("dialog")) dialog.addEventListener("click", event => {
  const rect = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
});
$("nav-open").addEventListener("click", openNav);
$("nav-close").addEventListener("click", closeNav);
$("nav-backdrop").addEventListener("click", closeNav);
matchMedia("(min-width: 768px)").addEventListener("change", closeNav);

// 她主动说的话（`#108`）：轮询待发表。
// `#119` 改了三件事，都是用户实测撞出来的（"主动说的话我一点都不知道"）：
//   ① 投递改成**至少一次**：只有渲染成功才 `ack`，没确认的下轮还会给（后端不再"取走即标记"）；
//   ② 失败**不再静默**：原来 `catch {}` 全吞，"她说了话但我没收到"谁都查不到；
//   ③ 你在别的页时给个**看得见的信号**（导航上的未读数）——她的话只会进聊天页，
//      而你正看着看板时，它悄悄躺在被隐藏的 `#log` 里。
const seenSay = new Set();
let unreadSay = 0;
function setUnread(n) {
  unreadSay = Math.max(0, n);
  const badge = $("chat-unread");
  if (!badge) return;
  badge.textContent = String(unreadSay);
  badge.hidden = unreadSay <= 0;
}
async function pollPending() {
  if (!client.identity?.value || document.hidden) return;   // 未登录 / 页面在后台 → 不打扰
  let data;
  try {
    data = await client.json("/api/pending", { anonymous: false });
  } catch (error) {
    if (!isQuiet(error)) console.warn("取她的待发表失败（下一轮会重试）：", error.message);
    return;
  }
  const acked = [];
  for (const item of data.items || []) {
    if (seenSay.has(item.id)) { acked.push(item.id); continue; }   // 已经渲染过，补一个确认
    try {
      const replies = renderAssistantMessages(item.text);
      for (const reply of replies) reply.root.dataset.proactive = "1";
      seenSay.add(item.id);                 // **只有真的渲染成功才算"见过"**
      acked.push(item.id);
      if (currentView !== "chat") setUnread(unreadSay + 1);
    } catch (error) {
      // 不确认 → 下一轮还会投过来（前端按 id 去重，所以不会重复显示）
      console.warn("投放她的话失败（下一轮再投）：", error.message);
    }
  }
  if (!acked.length) return;
  try {
    await client.json("/api/pending/ack", { method: "POST", body: { ids: acked } });
  } catch (error) {
    console.warn("确认已读失败（下一轮会重投一次，已按 id 去重）：", error.message);
  }
}
setInterval(pollPending, 30000);
setTimeout(pollPending, 5000);      // 打开页面 5 秒后先看一次
document.addEventListener("keydown", event => {
  if (!$("sidebar").classList.contains("open")) return;
  if (event.key === "Escape") closeNav();
  if (event.key === "Tab") {
    const focusable = [...$("sidebar").querySelectorAll("button:not(:disabled)")].filter(node => node.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
window.addEventListener("pagehide", () => { streamController?.abort(); client.cancelPending(); audio.stop(); });
setupTooltips();
updateVoiceButton();
closeNav();
boot();
