const state = {
  sessionId: null,
  socket: null,
  running: false,
  pendingApproval: null,
  provider: null,
  workspace: null,
  selectedFileRow: null,
  layout: {
    leftOpen: true,
    leftView: "explorer",
    inspectorOpen: true,
    inspectorTab: "trace",
    terminalOpen: false,
    compact: false,
  },
};

const elements = {
  healthBadge: document.querySelector("#healthBadge"),
  providerLabel: document.querySelector("#providerLabel"),
  connectionLabel: document.querySelector("#connectionLabel"),
  workspaceTitle: document.querySelector("#workspaceTitle"),
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
  leftDock: document.querySelector("#leftDock"),
  explorerView: document.querySelector("#explorerView"),
  settingsView: document.querySelector("#settingsView"),
  explorerRoot: document.querySelector("#explorerRoot"),
  refreshFilesButton: document.querySelector("#refreshFilesButton"),
  explorerRailButton: document.querySelector("#explorerRailButton"),
  traceRailButton: document.querySelector("#traceRailButton"),
  terminalRailButton: document.querySelector("#terminalRailButton"),
  diffRailButton: document.querySelector("#diffRailButton"),
  settingsRailButton: document.querySelector("#settingsRailButton"),
  terminalDock: document.querySelector("#terminalDock"),
  terminalOutput: document.querySelector("#terminalOutput"),
  clearTerminalButton: document.querySelector("#clearTerminalButton"),
  copyTerminalButton: document.querySelector("#copyTerminalButton"),
  closeTerminalButton: document.querySelector("#closeTerminalButton"),
  inspector: document.querySelector("#inspector"),
  traceTab: document.querySelector("#traceTab"),
  planTab: document.querySelector("#planTab"),
  diffTab: document.querySelector("#diffTab"),
  tracePane: document.querySelector("#tracePane"),
  planPane: document.querySelector("#planPane"),
  diffPane: document.querySelector("#diffPane"),
  planList: document.querySelector("#planList"),
  planProgress: document.querySelector("#planProgress"),
  closeInspectorButton: document.querySelector("#closeInspectorButton"),
  toggleInspector: document.querySelector("#toggleInspector"),
  toggleTerminal: document.querySelector("#toggleTerminal"),
  compactDensity: document.querySelector("#compactDensity"),
  resetLayoutButton: document.querySelector("#resetLayoutButton"),
};

const LAYOUT_STORAGE_KEY = "code-helper-layout-v1";

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
    state.provider = `${health.provider}/${health.model}`;
    elements.healthBadge.className = `health ${health.api_key_configured ? "ready" : "error"}`;
    elements.healthBadge.lastElementChild.textContent = health.api_key_configured
      ? `服务就绪 · ${state.provider}`
      : "服务就绪 · 未配置 API Key";
    elements.providerLabel.textContent = `MODEL · ${state.provider.toUpperCase()}`;
  } catch (error) {
    elements.healthBadge.className = "health error";
    elements.healthBadge.lastElementChild.textContent = "服务不可用";
    elements.providerLabel.textContent = "MODEL · OFFLINE";
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
    state.workspace = result.workspace;
    state.running = false;
    elements.workspaceInput.value = result.workspace;
    elements.workspaceInput.disabled = true;
    elements.createSessionButton.querySelector("span").textContent = "项目已打开";
    elements.createSessionButton.disabled = true;
    elements.messageInput.disabled = false;
    elements.sendButton.disabled = false;
    elements.refreshDiffButton.disabled = false;
    elements.restoreButton.disabled = false;
    elements.refreshFilesButton.disabled = false;
    elements.workspaceTitle.textContent = workspaceName(result.workspace);
    elements.connectionLabel.textContent = `SESSION · ${result.session_id.slice(0, 8).toUpperCase()}`;
    elements.emptyState?.remove();
    elements.messageList.innerHTML = "";
    elements.activityList.innerHTML = "";
    setStatus("就绪", false);
    connectSocket();
    await loadRootFiles();
    showToast("项目已安全打开，文件树与 Agent 已连接");
  } catch (error) {
    showToast(error.message);
  }
}

function workspaceName(path) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.at(-1) || path;
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
  elements.diffView.textContent = "正在读取差异…";
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
    await Promise.all([refreshDiff(), loadRootFiles()]);
  } catch (error) {
    showToast(error.message);
  }
}

async function loadRootFiles() {
  if (!state.sessionId) return;
  elements.explorerRoot.innerHTML = '<span class="tree-status">正在读取工作区…</span>';
  try {
    const result = await listFiles(".");
    elements.explorerRoot.innerHTML = "";
    renderFileEntries(result.entries, elements.explorerRoot);
    if (!result.entries.length) {
      elements.explorerRoot.innerHTML = '<span class="tree-status">工作区为空</span>';
    }
  } catch (error) {
    elements.explorerRoot.textContent = error.message;
    elements.explorerRoot.classList.add("tree-status");
  }
}

function listFiles(path) {
  const params = new URLSearchParams({ path });
  return api(`/api/sessions/${state.sessionId}/files?${params}`);
}

function renderFileEntries(entries, container) {
  for (const entry of entries) {
    const group = document.createElement("div");
    group.className = "tree-group";
    const row = document.createElement("button");
    row.className = "tree-row";
    row.type = "button";
    row.title = entry.path;

    if (entry.kind === "directory") {
      row.append(makeIcon("chevron", "m9 6 6 6-6 6"));
      row.append(makeIcon("folder", "M3.5 6.5h6l2 2h9v10h-17z"));
    } else {
      row.append(makeIcon("chevron", ""));
      row.append(makeIcon("file", "M6 3.5h8l4 4v13H6zM14 3.5v4h4"));
    }
    const label = document.createElement("span");
    label.className = "tree-label";
    label.textContent = entry.name;
    row.append(label);
    group.append(row);
    container.append(group);

    row.addEventListener("click", async () => {
      if (entry.kind === "file") {
        selectFileRow(row, entry.path);
        return;
      }
      const existing = group.querySelector(":scope > .tree-children");
      if (existing) {
        const hidden = existing.classList.toggle("hidden");
        row.classList.toggle("expanded", !hidden);
        return;
      }
      row.classList.add("expanded");
      const children = document.createElement("div");
      children.className = "tree-children";
      const loading = document.createElement("span");
      loading.className = "tree-status";
      loading.textContent = "读取中…";
      children.append(loading);
      group.append(children);
      try {
        const result = await listFiles(entry.path);
        children.innerHTML = "";
        renderFileEntries(result.entries, children);
        if (!result.entries.length) {
          loading.textContent = "空目录";
          children.append(loading);
        }
      } catch (error) {
        loading.textContent = error.message;
      }
    });
  }
}

function makeIcon(className, pathData) {
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.classList.add(className);
  svg.setAttribute("viewBox", "0 0 24 24");
  if (pathData) {
    const path = document.createElementNS(namespace, "path");
    path.setAttribute("d", pathData);
    svg.append(path);
  }
  return svg;
}

function selectFileRow(row, path) {
  state.selectedFileRow?.classList.remove("selected");
  state.selectedFileRow = row;
  row.classList.add("selected");
  if (!state.sessionId) {
    showToast("请先打开项目");
    return;
  }
  const prefix = elements.messageInput.value.trim();
  elements.messageInput.value = `${prefix}${prefix ? " " : ""}@${path} `;
  elements.messageInput.focus();
  showToast(`已将 ${path} 添加到任务`);
}

function handleEvent(event) {
  const payload = event.payload || {};
  switch (event.type) {
    case "turn_started":
      addMessage("user", payload.message);
      setRunning(true);
      break;
    case "step_started":
      elements.stepCounter.textContent = `STEP ${payload.step}`;
      addActivity(`开始 Step ${payload.step}`, "构造上下文并请求模型");
      break;
    case "context_compacted":
      addActivity("上下文已压缩", `约 ${payload.estimated_chars || 0} 字符，保留摘要继续执行`, "warning");
      break;
    case "model_started":
      addActivity("模型处理中", "等待模型选择下一步操作");
      break;
    case "assistant_response":
      streamingAgentMessage = null;
      if (payload.content) addMessage("agent", payload.content);
      if (payload.tool_calls?.length) {
        addActivity("模型选择工具", payload.tool_calls.map((call) => call.name).join(", "));
      }
      break;
    case "assistant_delta":
      appendStreamingAgentText(payload.content || "");
      break;
    case "tool_started":
      addActivity(`执行 ${payload.name}`, summarizeArguments(payload.arguments));
      if (payload.name === "run_command") {
        appendTerminal(`❯ ${payload.arguments.command}`, "command");
      }
      break;
    case "tool_result": {
      const result = payload.result;
      addActivity(
        `${result.ok ? "完成" : "失败"} ${payload.name}`,
        `${result.code} · ${result.message}`,
        result.ok ? "success" : "failure",
      );
      if (payload.name === "run_command") {
        mirrorCommandResult(result);
      }
      if (result.metadata?.mutated_files?.length) {
        refreshDiff();
        loadRootFiles();
      }
      break;
    }
    case "plan_updated":
      renderPlan(payload.plan || []);
      addActivity("计划已更新", payload.reason || "Agent 调整了执行步骤", "success");
      break;
    case "approval_requested":
      showApproval(payload);
      addActivity(`等待批准 ${payload.name}`, payload.reason);
      break;
    case "verification_required":
      addActivity("需要验证", payload.reason, "failure");
      break;
    case "repair_attempt":
      addActivity(`自动修复尝试 ${payload.attempt}/${payload.max_attempts}`, payload.reason, "warning");
      break;
    case "checkpoint_created":
      addActivity("创建检查点", payload.path, "success");
      break;
    case "checkpoint_restored":
      addActivity("已回滚本轮修改", (payload.files || []).join(", "), "success");
      loadRootFiles();
      break;
    case "turn_finished":
      setRunning(false);
      setStatus(statusLabel(payload.status), false);
      addActivity(
        `任务 ${statusLabel(payload.status)}`,
        payload.message,
        payload.status === "completed" ? "success" : "failure",
      );
      refreshDiff();
      break;
    default:
      break;
  }
}

function statusLabel(status) {
  return {
    completed: "已完成",
    partial: "部分完成",
    failed: "失败",
    cancelled: "已停止",
    ready: "就绪",
  }[status] || status;
}

function renderPlan(plan) {
  const items = Array.isArray(plan) ? plan : [];
  const completed = items.filter((item) => item.status === "completed").length;
  elements.planProgress.textContent = `${completed} / ${items.length}`;
  if (!items.length) return;
  elements.planList.innerHTML = items.map((item, index) => {
    const status = item.status || "pending";
    const icon = status === "completed" ? "✓" : status === "in_progress" ? "●" : `${index + 1}`;
    return `<div class="plan-item ${status}"><span class="plan-marker">${icon}</span><span>${escapeHtml(item.step || "")}</span></div>`;
  }).join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function addMessage(role, content) {
  if (!content) return;
  const message = document.createElement("div");
  message.className = `message ${role === "user" ? "user" : "agent"}`;
  const label = document.createElement("span");
  label.className = "role";
  label.textContent = role === "user" ? "YOU · REQUEST" : "CODE HELPER · RESPONSE";
  const body = document.createElement("div");
  body.textContent = content;
  message.append(label, body);
  elements.messageList.append(message);
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

let streamingAgentMessage = null;
function appendStreamingAgentText(content) {
  if (!content) return;
  if (!streamingAgentMessage) {
    streamingAgentMessage = document.createElement("div");
    streamingAgentMessage.className = "message agent streaming";
    elements.messageList.appendChild(streamingAgentMessage);
  }
  streamingAgentMessage.textContent += content;
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

function addActivity(title, detail, className = "") {
  elements.activityList.querySelector(".inspector-empty")?.remove();
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
  elements.denyButton.focus();
}

function summarizeArguments(args = {}) {
  const value = args.path || args.command || args.query || args.pattern;
  if (value) return String(value).slice(0, 180);
  return JSON.stringify(args).slice(0, 180);
}

function mirrorCommandResult(result) {
  const data = result.data || {};
  if (data.stdout) appendTerminal(data.stdout.replace(/\s+$/, ""), "");
  if (data.stderr) appendTerminal(data.stderr.replace(/\s+$/, ""), "stderr");
  const exitCode = Number.isInteger(data.exit_code) ? data.exit_code : "?";
  appendTerminal(
    `[exit ${exitCode}] ${result.message}`,
    result.ok ? "success" : "failure",
  );
}

function appendTerminal(text, className = "") {
  if (!text) return;
  elements.terminalOutput.querySelector(".dim")?.remove();
  const line = document.createElement("div");
  line.className = `terminal-line ${className}`;
  line.textContent = text;
  elements.terminalOutput.append(line);
  elements.terminalOutput.scrollTop = elements.terminalOutput.scrollHeight;
}

function setRunning(running) {
  state.running = running;
  elements.sendButton.disabled = running || !state.sessionId;
  elements.cancelButton.disabled = !running;
  elements.messageInput.disabled = running || !state.sessionId;
  setStatus(running ? "运行中" : "就绪", running);
}

function setStatus(text, running) {
  elements.runStatus.querySelector("strong").textContent = text;
  elements.runStatus.classList.toggle("running", running);
}

function showLeftView(view) {
  if (state.layout.leftOpen && state.layout.leftView === view) {
    state.layout.leftOpen = false;
  } else {
    state.layout.leftOpen = true;
    state.layout.leftView = view;
  }
  applyLayout();
}

function showInspector(tab) {
  state.layout.inspectorOpen = true;
  state.layout.inspectorTab = tab;
  applyLayout();
  if (tab === "diff") refreshDiff();
  if (tab === "plan" && state.sessionId) api(`/api/sessions/${state.sessionId}`).then((data) => renderPlan(data.plan || [])).catch(() => {});
}

function setInspectorTab(tab) {
  state.layout.inspectorTab = tab;
  elements.traceTab.classList.toggle("active", tab === "trace");
  elements.planTab.classList.toggle("active", tab === "plan");
  elements.diffTab.classList.toggle("active", tab === "diff");
  elements.traceTab.setAttribute("aria-selected", String(tab === "trace"));
  elements.planTab.setAttribute("aria-selected", String(tab === "plan"));
  elements.diffTab.setAttribute("aria-selected", String(tab === "diff"));
  elements.tracePane.classList.toggle("hidden", tab !== "trace");
  elements.planPane.classList.toggle("hidden", tab !== "plan");
  elements.diffPane.classList.toggle("hidden", tab !== "diff");
}

function applyLayout({ persist = true } = {}) {
  document.body.classList.toggle("left-closed", !state.layout.leftOpen);
  document.body.classList.toggle("inspector-closed", !state.layout.inspectorOpen);
  document.body.classList.toggle("compact", state.layout.compact);
  elements.explorerView.classList.toggle("hidden", state.layout.leftView !== "explorer");
  elements.settingsView.classList.toggle("hidden", state.layout.leftView !== "settings");
  elements.terminalDock.classList.toggle("hidden", !state.layout.terminalOpen);
  elements.toggleInspector.checked = state.layout.inspectorOpen;
  elements.toggleTerminal.checked = state.layout.terminalOpen;
  elements.compactDensity.checked = state.layout.compact;
  setInspectorTab(state.layout.inspectorTab);
  updateRailState(elements.explorerRailButton, state.layout.leftOpen && state.layout.leftView === "explorer");
  updateRailState(elements.settingsRailButton, state.layout.leftOpen && state.layout.leftView === "settings");
  updateRailState(elements.terminalRailButton, state.layout.terminalOpen);
  updateRailState(
    elements.traceRailButton,
    state.layout.inspectorOpen && state.layout.inspectorTab === "trace",
  );
  updateRailState(
    elements.diffRailButton,
    state.layout.inspectorOpen && state.layout.inspectorTab === "diff",
  );
  if (persist) saveLayout();
}

function updateRailState(button, active) {
  button.classList.toggle("active", active);
  button.setAttribute("aria-pressed", String(active));
}

function saveLayout() {
  localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(state.layout));
}

function loadLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_STORAGE_KEY));
    if (saved && typeof saved === "object") {
      state.layout = { ...state.layout, ...saved };
    }
  } catch {
    localStorage.removeItem(LAYOUT_STORAGE_KEY);
  }
  if (window.matchMedia("(max-width: 760px)").matches) {
    state.layout.leftOpen = false;
    state.layout.inspectorOpen = false;
    state.layout.terminalOpen = false;
  }
  applyLayout({ persist: false });
}

function resetLayout() {
  state.layout = {
    leftOpen: true,
    leftView: "explorer",
    inspectorOpen: true,
    inspectorTab: "trace",
    terminalOpen: false,
    compact: false,
  };
  applyLayout();
  showToast("已恢复默认布局");
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
elements.refreshFilesButton.addEventListener("click", loadRootFiles);
elements.approveButton.addEventListener("click", () => resolveApproval(true));
elements.denyButton.addEventListener("click", () => resolveApproval(false));
elements.explorerRailButton.addEventListener("click", () => showLeftView("explorer"));
elements.settingsRailButton.addEventListener("click", () => showLeftView("settings"));
elements.traceRailButton.addEventListener("click", () => showInspector("trace"));
elements.diffRailButton.addEventListener("click", () => showInspector("diff"));
elements.terminalRailButton.addEventListener("click", () => {
  state.layout.terminalOpen = !state.layout.terminalOpen;
  applyLayout();
});
elements.traceTab.addEventListener("click", () => showInspector("trace"));
elements.planTab.addEventListener("click", () => showInspector("plan"));
elements.diffTab.addEventListener("click", () => showInspector("diff"));
elements.closeInspectorButton.addEventListener("click", () => {
  state.layout.inspectorOpen = false;
  applyLayout();
});
elements.closeTerminalButton.addEventListener("click", () => {
  state.layout.terminalOpen = false;
  applyLayout();
});
elements.clearTerminalButton.addEventListener("click", () => {
  elements.terminalOutput.innerHTML =
    '<div class="terminal-line dim">终端输出已清空。</div>';
});
elements.copyTerminalButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(elements.terminalOutput.innerText);
    showToast("终端输出已复制");
  } catch {
    showToast("浏览器未授予剪贴板权限");
  }
});
elements.toggleInspector.addEventListener("change", () => {
  state.layout.inspectorOpen = elements.toggleInspector.checked;
  applyLayout();
});
elements.toggleTerminal.addEventListener("change", () => {
  state.layout.terminalOpen = elements.toggleTerminal.checked;
  applyLayout();
});
elements.compactDensity.addEventListener("change", () => {
  state.layout.compact = elements.compactDensity.checked;
  applyLayout();
});
elements.resetLayoutButton.addEventListener("click", resetLayout);
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
    showToast(`已切换为 ${elements.modeSelect.value.toUpperCase()} 模式`);
  } catch (error) {
    showToast(error.message);
  }
});
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    if (!state.sessionId) {
      showToast("请先打开一个本地项目");
      elements.workspaceInput.focus();
      return;
    }
    elements.messageInput.value = button.dataset.prompt;
    elements.messageInput.focus();
  });
});

loadLayout();
checkHealth();
