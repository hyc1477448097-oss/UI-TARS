<script setup>
import { computed, onMounted, ref } from "vue";

const projects = ref([]);
const projectId = ref("");
const ticket = ref("");
const busy = ref(false);
const runId = ref("");
const status = ref("idle");
const logs = ref([]);
const screenshot = ref("");
const confirmText = ref("");
const errorText = ref("");
let socket = null;

const selected = computed(() => projects.value.find((p) => p.id === projectId.value));
const running = computed(() => ["pending", "running", "waiting_confirm"].includes(status.value));

const statusLabel = computed(() => {
  const map = {
    idle: "空闲",
    pending: "排队",
    running: "执行中",
    waiting_confirm: "等待确认",
    succeeded: "成功",
    failed: "失败",
    aborted: "已中止",
  };
  return map[status.value] || status.value;
});

async function loadProjects() {
  const res = await fetch("/api/projects");
  const data = await res.json();
  projects.value = data.projects || [];
  if (!projectId.value && projects.value.length) {
    projectId.value = projects.value[0].id;
  }
}

async function reloadKnowledge() {
  const res = await fetch("/api/knowledge/reload", { method: "POST" });
  const data = await res.json();
  projects.value = data.projects || [];
  if (projectId.value && !projects.value.some((p) => p.id === projectId.value)) {
    projectId.value = projects.value[0]?.id || "";
  }
}

function pushLog(message, step = "") {
  logs.value.push({
    time: new Date().toLocaleTimeString(),
    step,
    message,
  });
}

function applyEvent(event) {
  if (event.type === "log" || event.type === "error") {
    pushLog(event.message, event.step);
  }
  if (event.type === "error") {
    errorText.value = event.message;
  }
  if (event.type === "screenshot" && event.payload?.file && runId.value) {
    screenshot.value = `/api/runs/${runId.value}/files/${event.payload.file}?t=${Date.now()}`;
  }
  if (event.type === "confirm_required") {
    confirmText.value = event.payload?.summary || event.message;
    status.value = "waiting_confirm";
  }
  if (event.type === "status" && event.payload?.status) {
    status.value = event.payload.status;
    if (event.payload.status !== "waiting_confirm") {
      confirmText.value = "";
    }
    pushLog(event.message, event.step);
  }
}

function connectWs(id) {
  if (socket) {
    socket.close();
    socket = null;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/api/runs/${id}/events`);
  socket.onmessage = (ev) => {
    try {
      applyEvent(JSON.parse(ev.data));
    } catch {
      /* ignore */
    }
  };
}

async function start(kind) {
  if (!projectId.value || running.value) return;
  busy.value = true;
  errorText.value = "";
  confirmText.value = "";
  logs.value = [];
  screenshot.value = "";
  status.value = "pending";
  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId.value,
        kind,
        extras: { ticket: ticket.value },
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "启动失败");
    }
    runId.value = data.id;
    status.value = data.status;
    connectWs(data.id);
  } catch (err) {
    status.value = "failed";
    errorText.value = err.message;
    pushLog(err.message, "error");
  } finally {
    busy.value = false;
  }
}

async function confirm() {
  if (!runId.value) return;
  await fetch(`/api/runs/${runId.value}/confirm`, { method: "POST" });
}

async function abort() {
  if (!runId.value) return;
  await fetch(`/api/runs/${runId.value}/abort`, { method: "POST" });
}

onMounted(loadProjects);
</script>

<template>
  <div class="shell">
    <header class="top">
      <div>
        <p class="kicker">UI-TARS 工作流</p>
        <h1>测试 / 上线</h1>
      </div>
      <div class="status" :data-state="status">{{ statusLabel }}</div>
    </header>

    <section class="controls">
      <label>
        项目
        <select v-model="projectId" :disabled="running">
          <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
      </label>
      <label>
        工单号（对应知识库里的 ticket 占位符）
        <input v-model="ticket" :disabled="running" placeholder="上线时可选填写" />
      </label>
      <div class="actions">
        <button class="primary" :disabled="!projectId || running || busy" @click="start('test')">
          测试
        </button>
        <button class="primary release" :disabled="!projectId || running || busy" @click="start('release')">
          上线
        </button>
        <button :disabled="!running" @click="abort">中止</button>
        <button class="ghost" :disabled="running" @click="reloadKnowledge">重新加载配置</button>
      </div>
    </section>

    <p v-if="selected" class="hint">
      测试页 {{ selected.config.test?.deploy_url || "未配置" }} ·
      上线页 {{ selected.config.release?.url || "未配置" }}
    </p>
    <p v-if="errorText" class="error">{{ errorText }}</p>

    <div v-if="confirmText" class="confirm">
      <p>{{ confirmText }}</p>
      <button class="primary" @click="confirm">确认执行</button>
    </div>

    <main class="board">
      <section class="panel">
        <h2>当前截图</h2>
        <div class="shot">
          <img v-if="screenshot" :src="screenshot" alt="当前步骤截图" />
          <p v-else class="muted">执行后显示页面截图</p>
        </div>
      </section>
      <section class="panel">
        <h2>步骤日志</h2>
        <ol class="log">
          <li v-for="(item, i) in logs" :key="i">
            <span class="time">{{ item.time }}</span>
            <span v-if="item.step" class="step">{{ item.step }}</span>
            <pre>{{ item.message }}</pre>
          </li>
        </ol>
      </section>
    </main>
  </div>
</template>

<style scoped>
.shell {
  max-width: 1200px;
  margin: 0 auto;
  padding: 32px 28px 48px;
}

.top {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  border-bottom: 1px solid var(--line);
  padding-bottom: 20px;
}

.kicker {
  margin: 0 0 6px;
  color: var(--accent);
  letter-spacing: 0.12em;
  font-size: 12px;
  text-transform: uppercase;
}

h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 600;
}

.status {
  padding: 6px 12px;
  border: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}

.status[data-state="running"],
.status[data-state="pending"] {
  color: var(--accent);
  border-color: var(--accent-dim);
}

.status[data-state="waiting_confirm"] {
  color: var(--warn);
}

.status[data-state="succeeded"] {
  color: var(--ok);
}

.status[data-state="failed"],
.status[data-state="aborted"] {
  color: var(--danger);
}

.controls {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px 20px;
  margin-top: 24px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: var(--muted);
  font-size: 13px;
}

select,
input {
  background: var(--bg-inset);
  color: var(--text);
  border: 1px solid var(--line);
  padding: 10px 12px;
  font-size: 14px;
}

.actions {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

button {
  background: var(--bg-raised);
  color: var(--text);
  border: 1px solid var(--line);
  padding: 10px 16px;
  cursor: pointer;
}

button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

button.primary {
  background: var(--accent);
  color: #1a160c;
  border-color: var(--accent);
  font-weight: 600;
}

button.primary.release {
  background: transparent;
  color: var(--accent);
}

button.ghost {
  background: transparent;
}

.hint,
.muted {
  color: var(--muted);
  font-size: 13px;
}

.error {
  color: var(--danger);
}

.confirm {
  margin: 16px 0;
  padding: 16px;
  border: 1px solid var(--accent-dim);
  background: var(--bg-raised);
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
}

.confirm p {
  margin: 0;
  line-height: 1.5;
}

.board {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 20px;
  margin-top: 12px;
}

.panel {
  background: var(--bg-raised);
  border: 1px solid var(--line);
  padding: 16px;
  min-height: 420px;
}

h2 {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--muted);
}

.shot {
  min-height: 360px;
  background: var(--bg-inset);
  display: flex;
  align-items: center;
  justify-content: center;
}

.shot img {
  max-width: 100%;
  max-height: 520px;
}

.log {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 520px;
  overflow: auto;
}

.log li {
  border-bottom: 1px solid var(--line);
  padding: 10px 0;
}

.time,
.step {
  color: var(--muted);
  font-size: 12px;
  margin-right: 8px;
}

pre {
  margin: 6px 0 0;
  white-space: pre-wrap;
  font-family: inherit;
  font-size: 13px;
}

@media (max-width: 900px) {
  .controls,
  .board {
    grid-template-columns: 1fr;
  }
}
</style>
