const state = {
  sessionId: null,
  socket: null,
  running: false,
  pendingApproval: null,
};

const elements = {
  healthBadge: document.querySelector("#healthBadge"),
  workspaceInput: document.querySelector("#workspaceInput"),
  modeSelect: document.querySelector("#modeSelect"),
  createSessionButton: document.querySelector("#createSessionButton"),
  runStatus: document.querySelector("#runStatus"),
  stepCounter: document.querySelector("#stepCounter"),
  messageList: document.querySelector("#messageList"),
  emptyState: document.querySelector("#emptyState"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  cancelButton: document.querySelector("#cancelButton"),
  activityList: document.querySelector("#activityList"),
  refreshDiffButton: document.querySelector("#refreshDiffButton"),
  restoreButton: document.querySelector("#restoreButton"),
  diffView: document.querySelector("#diffView"),
  approvalBackdrop: document.querySelector("#approvalBackdrop"),
  approvalTitle: document.querySelector("#approvalTitle"),
  approvalReason: document.querySelector("#approvalReason"),
  approvalArguments: document.querySelector("#approvalArguments"),
  approveButton: document.querySelector("#approveButton"),
  denyButton: document.querySelector("#denyButton"),
  toast: document.querySelector("#toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return body;
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    elements.healthBadge.className = `health ${health.api_key_configured ? "ready" : "error"}`;
    elements.healthBadge.lastElementChild.textContent = health.api_key_configured
      ? `服务就绪 · ${health.model}`
      : "服务就绪 · 未配置 API Key";
  } catch (error) {
    elements.healthBadge.className = "health error";
    elements.healthBadge.lastElementChild.textContent = "服务不可用";
  }
}

async function createSession() {
  const workspace = elements.workspaceInput.value.trim();
  if (!workspace) {
    showToast("请先输入工作目录");
    return;
  }
  try {
    const result = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ workspace, mode: elements.modeSelect.value }),
    });
    closeSocket();
    state.sessionId = result.session_id;
    state.running = false;
    elements.workspaceInput.value = result.workspace;
    elements.workspaceInput.disabled = true;
    elements.createSessionButton.textContent = "已打开";
    elements.createSessionButton.disabled = true;
    elements.messageInput.disabled = false;
    elements.sendButton.disabled = false;
    elements.refreshDiffButton.disabled = false;
    elements.restoreButton.disabled = false;
    elements.emptyState?.remove();
    elements.messageList.innerHTML = "";
    elements.activityList.innerHTML = "";
    setStatus("就绪", false);
    connectSocket();
    showToast("项目已打开，Agent 只能访问该工作区");
  } catch (error) {
    showToast(error.message);
  }
}

function connectSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws/sessions/${state.sessionId}`);
  state.socket = socket;
  socket.onmessage = (message) => handleEvent(JSON.parse(message.data));
  socket.onclose = () => {
    if (state.socket === socket && state.sessionId) {
      setTimeout(connectSocket, 1200);
    }
  };
}

function closeSocket() {
  if (state.socket) {
    const socket = state.socket;
    state.socket = null;
    socket.onclose = null;
    socket.close();
  }
}

async function sendMessage() {
  if (!state.sessionId || state.running) return;
  const content = elements.messageInput.value.trim();
  if (!content) return;
  elements.messageInput.value = "";
  try {
    await api(`/api/sessions/${state.sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
    setRunning(true);
  } catch (error) {
    showToast(error.message);
    elements.messageInput.value = content;
  }
}

async function cancelRun() {
  if (!state.sessionId) return;
  try {
    await api(`/api/sessions/${state.sessionId}/cancel`, { method: "POST" });
    showToast("已请求停止 Agent");
  } catch (error) {
    showToast(error.message);
  }
}

async function resolveApproval(approved) {
  if (!state.pendingApproval) return;
  try {
    await api(`/api/sessions/${state.sessionId}/approval`, {
      method: "POST",
      body: JSON.stringify({
        tool_call_id: state.pendingApproval.id,
        approved,
      }),
    });
    elements.approvalBackdrop.classList.add("hidden");
    state.pendingApproval = null;
  } catch (error) {
    showToast(error.message);
  }
}

async function refreshDiff() {
  if (!state.sessionId) return;
  try {
    const result = await api(`/api/sessions/${state.sessionId}/diff`);
    elements.diffView.textContent = result.diff || result.error || "暂无变更";
  } catch (error) {
    elements.diffView.textContent = error.message;
  }
}

async function restoreCheckpoint() {
  if (!state.sessionId || state.running) return;
  const confirmed = window.confirm("恢复本轮任务开始前的文件状态？此操作会覆盖 Agent 的修改。");
  if (!confirmed) return;
  try {
    const result = await api(`/api/sessions/${state.sessionId}/restore`, {
      method: "POST",
    });
    showToast(`已恢复 ${result.restored.length} 个文件`);
    await refreshDiff();
  } catch (error) {
    showToast(error.message);
  }
}

function handleEvent(event) {
  const payload = event.payload || {};
  switch (event.type) {
    case "turn_started":
      addMessage("user", payload.message);
      setRunning(true);
      break;
    case "step_started":
      elements.stepCounter.textContent = `Step ${payload.step}`;
      addActivity(`开始 Step ${payload.step}`, "构造上下文并请求模型");
      break;
    case "assistant_response":
      if (payload.content) addMessage("agent", payload.content);
      if (payload.tool_calls?.length) {
        addActivity("模型选择工具", payload.tool_calls.map((call) => call.name).join(", "));
      }
      break;
    case "tool_started":
      addActivity(`执行 ${payload.name}`, summarizeArguments(payload.arguments));
      break;
    case "tool_result": {
      const result = payload.result;
      addActivity(
        `${result.ok ? "完成" : "失败"} ${payload.name}`,
        `${result.code} · ${result.message}`,
        result.ok ? "success" : "failure",
      );
      if (result.metadata?.mutated_files?.length) refreshDiff();
      break;
    }
    case "approval_requested":
      showApproval(payload);
      addActivity(`等待批准 ${payload.name}`, payload.reason);
      break;
    case "verification_required":
      addActivity("需要验证", payload.reason, "failure");
      break;
    case "checkpoint_created":
      addActivity("创建检查点", payload.path, "success");
      break;
    case "checkpoint_restored":
      addActivity("已回滚本轮修改", (payload.files || []).join(", "), "success");
      break;
    case "turn_finished":
      setRunning(false);
      setStatus(payload.status, false);
      addActivity(`任务 ${payload.status}`, payload.message, payload.status === "completed" ? "success" : "failure");
      refreshDiff();
      break;
    default:
      break;
  }
}

function addMessage(role, content) {
  if (!content) return;
  const message = document.createElement("div");
  message.className = `message ${role === "user" ? "user" : "agent"}`;
  const label = document.createElement("span");
  label.className = "role";
  label.textContent = role === "user" ? "YOU" : "CODE HELPER";
  const body = document.createElement("div");
  body.textContent = content;
  message.append(label, body);
  elements.messageList.append(message);
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

function addActivity(title, detail, className = "") {
  const item = document.createElement("div");
  item.className = `activity-item ${className}`;
  const heading = document.createElement("strong");
  heading.textContent = title;
  const body = document.createElement("span");
  body.textContent = detail || "";
  item.append(heading, body);
  elements.activityList.append(item);
  elements.activityList.scrollTop = elements.activityList.scrollHeight;
}

function showApproval(payload) {
  state.pendingApproval = payload;
  elements.approvalTitle.textContent = `允许 ${payload.name}？`;
  elements.approvalReason.textContent = payload.reason;
  elements.approvalArguments.textContent = JSON.stringify(payload.arguments, null, 2);
  elements.approvalBackdrop.classList.remove("hidden");
}

function summarizeArguments(args = {}) {
  const value = args.path || args.command || args.query || args.pattern;
  if (value) return String(value).slice(0, 180);
  return JSON.stringify(args).slice(0, 180);
}

function setRunning(running) {
  state.running = running;
  elements.sendButton.disabled = running || !state.sessionId;
  elements.cancelButton.disabled = !running;
  elements.messageInput.disabled = running || !state.sessionId;
  setStatus(running ? "运行中" : "就绪", running);
}

function setStatus(text, running) {
  elements.runStatus.textContent = text;
  elements.runStatus.classList.toggle("running", running);
}

let toastTimer;
function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.add("hidden"), 3200);
}

elements.createSessionButton.addEventListener("click", createSession);
elements.sendButton.addEventListener("click", sendMessage);
elements.cancelButton.addEventListener("click", cancelRun);
elements.refreshDiffButton.addEventListener("click", refreshDiff);
elements.restoreButton.addEventListener("click", restoreCheckpoint);
elements.approveButton.addEventListener("click", () => resolveApproval(true));
elements.denyButton.addEventListener("click", () => resolveApproval(false));
elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.ctrlKey) {
    event.preventDefault();
    sendMessage();
  }
});
elements.modeSelect.addEventListener("change", async () => {
  if (!state.sessionId || state.running) return;
  try {
    await api(`/api/sessions/${state.sessionId}/mode`, {
      method: "POST",
      body: JSON.stringify({ mode: elements.modeSelect.value }),
    });
  } catch (error) {
    showToast(error.message);
  }
});

checkHealth();
