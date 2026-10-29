import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { writeFile } from "node:fs/promises";
import path from "node:path";

const base = process.argv[2] || "http://127.0.0.1:8768";
const python = process.argv[3] || "python";
const username = "前端验收";
const password = "isolated-auth-check-2026";
const health = await (await fetch(base + "/api/healthz")).json();
assert.equal(health.password_login, true);
assert.equal(health.account_setup_required, true);
assert.equal(health.data_dir_writable, true);
assert.equal("data_dir" in health, false, "healthz must not expose a local path");
let created = false;
const report = { status: "running", checks: [] };

async function request(endpoint, { token, body, method = "GET" } = {}) {
  const headers = {};
  if (token) headers.Authorization = "Bearer " + token;
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(base + endpoint, { method, headers, body: body ? JSON.stringify(body) : undefined });
  return { status: response.status, data: await response.json() };
}

try {
  const baseline = await request("/api/state");
  assert.equal(baseline.status, 200);
  const setup = await request("/api/auth/setup", { method: "POST", body: { username, password, inherit_default: true } });
  assert.equal(setup.status, 201);
  created = true;
  const first = setup.data.access_token;
  assert.equal((await request("/api/auth/me", { token: first })).data.username, username);
  assert.equal((await request("/api/state")).status, 401);
  report.checks.push({ id: "setup_and_authenticated_access", passed: true });
  const state = await request("/api/state", { token: first });
  assert.equal(state.status, 200);
  for (const key of ["world_count", "profile_text", "memory"]) assert.equal(state.data[key], baseline.data[key]);
  report.checks.push({ id: "default_data_association", passed: true });
  const wrong = await request("/api/auth/login", { method: "POST", body: { username, password: "wrong" } });
  assert.equal(wrong.status, 401);
  const login = await request("/api/auth/login", { method: "POST", body: { username, password } });
  assert.equal(login.status, 200);
  assert.equal(login.data.expires_in, 604800);
  report.checks.push({ id: "password_verification_and_session_issuance", passed: true });
  assert.equal((await request("/api/auth/logout", { method: "POST", token: first })).status, 200);
  assert.equal((await request("/api/auth/me", { token: first })).status, 401);
  assert.equal((await request("/api/auth/me", { token: login.data.access_token })).status, 200);
  report.checks.push({ id: "logout_revokes_only_current_session", passed: true });
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  report.error = error.message;
  throw error;
} finally {
  if (created) {
    // Only remove the account created by this check from the verified preview database.
    execFileSync(python, ["-c", "import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); con.execute('DELETE FROM tokens WHERE uid=?',(sys.argv[2],)); con.execute('DELETE FROM accounts WHERE uid=?',(sys.argv[2],)); con.commit(); con.close()",
      path.join(preview, "accounts.db"), username]);
  }
  await writeFile("_tmptest/password-api-report.json", JSON.stringify(report, null, 2));
}
console.log(`real password API checks passed (${report.checks.length} workflows, isolated preview restored)`);
