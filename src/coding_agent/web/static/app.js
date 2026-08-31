const PANEL_LAYOUT_KEY = "code-helper.panel-layout.v1";
const WORKSPACE_STATE_KEY = "code-helper.workspace-state.v1";
const RESTORABLE_VIEWS = new Set(["chat", "trace", "plan", "intelligence"]);
const RESTORABLE_MODES = new Set(["act", "plan", "ask"]);
const RESTORABLE_REASONING = new Set(["auto", "fast", "balanced", "deep"]);
const RESTORABLE_TASK_PROFILES = new Set(["auto", "project", "algorithm"]);
const LAYOUT_MODES = new Set(["editor", "focus"]);
const PANEL_DEFAULTS = Object.freeze({
  explorer: 252,
  assistant: 420,
  threads: 112,
  focusSidebar: 264,
  focusFile: 460,
});
const PANEL_LIMITS = Object.freeze({
  explorer: { min: 180, max: 480 },
  assistant: { min: 300, max: 640 },
  threads: { min: 72, max: 360 },
  focusSidebar: { min: 220, max: 420 },
  focusFile: { min: 320, max: 720 },
  editorMin: 360,
  assistantContentMin: 220,
});

const state = {
  sessionId: null,
  workspace: null,
  socket: null,
  running: false,
  stopping: false,
  runSyncTimer: null,
  runProgressTimer: null,
  runStartedAt: null,
  runProgressElapsed: 0,
  runEpoch: 0,
  pendingUserEchoes: [],
  approvalPolicy: "ask",
  pendingApproval: null,
  seenSequences: new Set(),
  restoringHistory: false,
  historyBuffer: null,
  historyMetadata: null,
  selectedFilePath: null,
  selectedFileRow: null,
  openFiles: [],
  fileCache: new Map(),
  activeFilePath: null,
  browserPath: "",
  browserParent: null,
  browserSelection: null,
  browserPurpose: "workspace",
  restorePreview: [],
  activeView: "chat",
  appSettings: null,
  clearApiKey: false,
  settingsOpen: false,
  layoutMode: "editor",
  filePanelVisible: false,
  sessionMode: "act",
  modeSyncPromise: null,
  pendingMode: null,
  sessionListView: "active",
  workspaceSessions: [],
  archivedSessions: [],
  userMenuAnchor: null,
  panelLayout: loadPanelLayout(),
};

const elements = Object.fromEntries([
  "healthBadge", "providerLabel", "workspaceTitle", "workspaceInput", "taskProfileSelect", "approvalPolicySelect", "workbench",
  "conversationsPane", "focusSessionList", "focusNewSessionButton", "focusFilesButton",
  "focusWorkspaceTitle", "focusUserMenuButton", "focusAccountStatus", "closeFilePanelButton", "assistantTitle",
  "browseWorkspaceButton", "createSessionButton", "modeSelect", "reasoningSelect",
  "refreshFilesButton", "insertFileButton", "explorerPath", "explorerRoot",
  "editorTabs", "editorBreadcrumbs", "editorLanguage", "copyFileButton",
  "reloadFileButton", "editorEmpty", "codeScroll", "codeLines", "fileStatus",
  "fileEncoding", "filePosition", "fileSize", "newSessionButton", "sessionList",
  "explorerResizer", "assistantResizer", "threadResizer",
  "messageList", "messageInput", "sendButton", "cancelButton", "runStatus", "thinkingIndicator", "thinkingStatus",
  "runProgress", "runProgressTitle", "runProgressDetail", "runProgressElapsed",
  "questionNavigator", "questionNavigatorToggle", "questionNavigatorCount", "questionNavigatorPanel", "questionNavigatorList", "closeQuestionNavigator",
  "stepCounter", "activityList", "planProgress", "planList", "restoreButton",
  "refreshIntelligenceButton", "exportTraceButton", "intelligenceContent",
  "browserBackdrop", "browserPath", "browserUpButton", "browserList",
  "chooseWorkspaceButton", "closeBrowserButton", "cancelBrowserButton",
  "approvalBackdrop", "approvalTitle", "approvalReason", "approvalArguments",
  "approveButton", "grantButton", "denyButton", "toast",
  "restoreBackdrop", "restoreFileList", "restorePreviewDiff", "confirmRestoreButton", "closeRestoreButton", "cancelRestoreButton",
  "userMenuButton", "accountStatus", "userMenu", "userMenuModel", "openSettingsButton",
  "settingsPage", "settingsBackButton", "settingsForm", "saveSettingsButton",
  "settingsApiKey", "settingsApiStatus", "settingsProviderModel", "toggleApiKeyButton", "clearApiKeyButton",
  "settingsDefaultWorkspace", "browseDefaultWorkspaceButton", "useCurrentWorkspaceButton", "clearDefaultWorkspaceButton",
  "settingsMode", "settingsReasoning", "settingsTaskProfile", "settingsApprovalPolicy",
  "settingsLayoutMode",
  "settingsSkillsList", "enableAllSkillsButton", "disableAllSkillsButton",
].map((id) => [id, document.querySelector(`#${id}`)]));

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail;
    const error = new Error(typeof detail === "string" ? detail : detail?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return body;
}

async function checkHealth() {
  try {
    const health = await api("/api/health");
    const model = `${health.provider}/${health.model}`;
    elements.healthBadge.className = `health ${health.api_key_configured ? "ready" : "error"}`;
    elements.healthBadge.lastElementChild.textContent = health.api_key_configured ? "服务就绪" : "缺少 API Key";
    elements.providerLabel.textContent = `MODEL · ${model.toUpperCase()}`;
    if (!state.sessionId) elements.reasoningSelect.value = effortToProfile(health.reasoning_effort);
  } catch {
    elements.healthBadge.className = "health error";
    elements.healthBadge.lastElementChild.textContent = "服务离线";
    elements.providerLabel.textContent = "MODEL · OFFLINE";
  }
}

function updateSettingsIdentity(settings) {
  const model = `${settings.provider}/${settings.model}`;
  elements.userMenuModel.textContent = model;
  elements.accountStatus.textContent = settings.api_key_configured
    ? `${settings.provider} · 已连接`
    : "需要配置 API Key";
  elements.focusAccountStatus.textContent = elements.accountStatus.textContent;
}

function renderSettingsSkills(skills) {
  elements.settingsSkillsList.innerHTML = "";
  if (!skills.length) {
    elements.settingsSkillsList.innerHTML = '<div class="settings-loading">当前项目没有可用的 Skills。</div>';
    return;
  }
  skills.forEach((skill) => {
    const label = document.createElement("label");
    label.className = "settings-skill";
    label.innerHTML = `<span class="settings-skill-copy"><strong>${escapeHtml(skill.name)}</strong><small>${escapeHtml(skill.description || skill.when_to_use || "项目工作流")}</small></span><span class="skill-toggle"><input type="checkbox" name="enabled-skill" value="${escapeHtml(skill.name)}" ${skill.enabled ? "checked" : ""}><i aria-hidden="true"></i></span>`;
    elements.settingsSkillsList.append(label);
  });
}

function renderSettingsForm(settings) {
  state.appSettings = settings;
  state.clearApiKey = false;
  elements.settingsApiKey.value = "";
  elements.settingsApiKey.disabled = false;
  elements.settingsApiKey.type = "password";
  elements.toggleApiKeyButton.textContent = "显示";
  elements.clearApiKeyButton.textContent = "清除";
  elements.settingsApiStatus.className = settings.api_key_configured ? "configured" : "cleared";
  elements.settingsApiStatus.textContent = settings.api_key_configured
    ? `已配置 · ${settings.api_key_hint}`
    : "尚未配置 API Key";
  elements.settingsProviderModel.textContent = `${settings.provider}/${settings.model}`;
  elements.settingsDefaultWorkspace.value = settings.default_workspace || "";
  elements.settingsMode.value = settings.default_mode;
  elements.settingsReasoning.value = settings.default_reasoning_profile;
  elements.settingsTaskProfile.value = settings.default_task_profile;
  elements.settingsApprovalPolicy.value = settings.default_approval_policy;
  const layoutInput = elements.settingsLayoutMode.querySelector(`input[value="${settings.default_layout_mode || "editor"}"]`);
  if (layoutInput) layoutInput.checked = true;
  renderSettingsSkills(settings.skills || []);
  updateSettingsIdentity(settings);
}

async function loadAppSettings() {
  try {
    const settings = await api("/api/settings");
    renderSettingsForm(settings);
    applyLayoutMode(settings.default_layout_mode || "editor");
    if (!state.sessionId) {
      elements.modeSelect.value = settings.default_mode;
      elements.reasoningSelect.value = settings.default_reasoning_profile;
      elements.taskProfileSelect.value = settings.default_task_profile;
      elements.approvalPolicySelect.value = settings.default_approval_policy;
      state.approvalPolicy = settings.default_approval_policy;
      updateApprovalPolicyVisual();
    }
    return settings;
  } catch (error) {
    elements.settingsSkillsList.innerHTML = `<div class="settings-loading">${escapeHtml(error.message)}</div>`;
    elements.accountStatus.textContent = "设置不可用";
    elements.focusAccountStatus.textContent = "设置不可用";
    return null;
  }
}

function positionUserMenu() {
  const anchorElement = state.userMenuAnchor || elements.userMenuButton;
  const anchor = anchorElement.getBoundingClientRect();
  const menuWidth = 260;
  const left = Math.min(window.innerWidth - menuWidth - 10, Math.max(10, anchor.left));
  const menuHeight = elements.userMenu.offsetHeight || 120;
  const top = Math.max(10, anchor.top - menuHeight - 7);
  elements.userMenu.style.left = `${left}px`;
  elements.userMenu.style.top = `${top}px`;
}

function closeUserMenu() {
  elements.userMenu.classList.add("hidden");
  elements.userMenuButton.setAttribute("aria-expanded", "false");
  elements.focusUserMenuButton.setAttribute("aria-expanded", "false");
  state.userMenuAnchor = null;
}

function toggleUserMenu(event) {
  const opening = elements.userMenu.classList.contains("hidden");
  if (!opening) return closeUserMenu();
  state.userMenuAnchor = event?.currentTarget || elements.userMenuButton;
  elements.userMenu.classList.remove("hidden");
  state.userMenuAnchor.setAttribute("aria-expanded", "true");
  positionUserMenu();
  elements.openSettingsButton.focus();
}

async function openSettingsPage() {
  closeUserMenu();
  state.settingsOpen = true;
  elements.workbench.classList.add("hidden");
  elements.settingsPage.classList.remove("hidden");
  await loadAppSettings();
  elements.settingsBackButton.focus();
}

function closeSettingsPage() {
  state.settingsOpen = false;
  elements.settingsPage.classList.add("hidden");
  elements.workbench.classList.remove("hidden");
  applyLayoutMode(state.layoutMode);
  applyPanelLayout();
  (state.layoutMode === "focus" ? elements.focusUserMenuButton : elements.userMenuButton).focus();
}

async function syncCurrentSessionDefaults(settings) {
  elements.modeSelect.value = settings.default_mode;
  elements.reasoningSelect.value = settings.default_reasoning_profile;
  elements.taskProfileSelect.value = settings.default_task_profile;
  state.approvalPolicy = settings.default_approval_policy;
  elements.approvalPolicySelect.value = state.approvalPolicy;
  updateApprovalPolicyVisual();
  if (!state.sessionId || state.running) return !state.running;
  try {
    await Promise.all([
      api(`/api/sessions/${state.sessionId}/mode`, {
        method: "POST",
        body: JSON.stringify({ mode: settings.default_mode }),
      }),
      api(`/api/sessions/${state.sessionId}/reasoning`, {
        method: "POST",
        body: JSON.stringify({ profile: settings.default_reasoning_profile }),
      }),
      api(`/api/sessions/${state.sessionId}/approval-policy`, {
        method: "POST",
        body: JSON.stringify({ policy: settings.default_approval_policy }),
      }),
    ]);
    state.sessionMode = settings.default_mode;
    saveWorkspaceState();
    return true;
  } catch {
    return false;
  }
}

async function saveSettings(event) {
  event?.preventDefault();
  const enabledSkills = [...elements.settingsSkillsList.querySelectorAll('input[name="enabled-skill"]:checked')]
    .map((input) => input.value);
  const payload = {
    api_key: elements.settingsApiKey.value.trim() || null,
    clear_api_key: state.clearApiKey,
    default_workspace: elements.settingsDefaultWorkspace.value.trim(),
    default_mode: elements.settingsMode.value,
    default_reasoning_profile: elements.settingsReasoning.value,
    default_task_profile: elements.settingsTaskProfile.value,
    default_approval_policy: elements.settingsApprovalPolicy.value,
    default_layout_mode: elements.settingsLayoutMode.querySelector('input[name="layout-mode"]:checked')?.value || "editor",
    enabled_skills: enabledSkills,
  };
  elements.saveSettingsButton.disabled = true;
  elements.saveSettingsButton.querySelector("span").textContent = "保存中…";
  try {
    const settings = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
    renderSettingsForm(settings);
    applyLayoutMode(settings.default_layout_mode || "editor", { persist: true });
    const synced = await syncCurrentSessionDefaults(settings);
    await checkHealth();
    showToast(synced ? "设置已保存" : "设置已保存；当前任务结束后应用会话默认值");
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.saveSettingsButton.disabled = false;
    elements.saveSettingsButton.querySelector("span").textContent = "保存更改";
  }
}

function workspaceName(path) {
  return path.split(/[\\/]/).filter(Boolean).at(-1) || path;
}

function effortToProfile(value) {
  return ({ low: "fast", medium: "balanced", high: "deep" })[value] || "auto";
}

function loadWorkspaceState() {
  try {
    const saved = JSON.parse(localStorage.getItem(WORKSPACE_STATE_KEY) || "null");
    if (!saved || typeof saved.workspace !== "string" || !saved.workspace.trim()) return null;
    const openFiles = Array.isArray(saved.openFiles)
      ? saved.openFiles.filter((path) => typeof path === "string" && path.trim()).slice(0, 24)
      : [];
    return {
      workspace: saved.workspace,
      sessionId: typeof saved.sessionId === "string" && saved.sessionId ? saved.sessionId : null,
      openFiles,
      activeFilePath: typeof saved.activeFilePath === "string" ? saved.activeFilePath : null,
      activeView: RESTORABLE_VIEWS.has(saved.activeView) ? saved.activeView : "chat",
      mode: RESTORABLE_MODES.has(saved.mode) ? saved.mode : "act",
      reasoningProfile: RESTORABLE_REASONING.has(saved.reasoningProfile) ? saved.reasoningProfile : "auto",
      taskProfile: RESTORABLE_TASK_PROFILES.has(saved.taskProfile) ? saved.taskProfile : "auto",
    };
  } catch {
    return null;
  }
}

function saveWorkspaceState() {
  if (!state.workspace || !state.sessionId) return;
  const snapshot = {
    workspace: state.workspace,
    sessionId: state.sessionId,
    openFiles: state.openFiles.slice(0, 24),
    activeFilePath: state.activeFilePath,
    activeView: state.activeView,
    mode: elements.modeSelect.value,
    reasoningProfile: elements.reasoningSelect.value,
    taskProfile: elements.taskProfileSelect.value,
  };
  try { localStorage.setItem(WORKSPACE_STATE_KEY, JSON.stringify(snapshot)); } catch { /* localStorage may be disabled */ }
}

function clearWorkspaceState() {
  try { localStorage.removeItem(WORKSPACE_STATE_KEY); } catch { /* localStorage may be disabled */ }
}

function loadPanelLayout() {
  try {
    const saved = JSON.parse(localStorage.getItem(PANEL_LAYOUT_KEY) || "{}");
    return Object.fromEntries(Object.entries(PANEL_DEFAULTS).map(([key, fallback]) => {
      const value = Number(saved[key]);
      return [key, Number.isFinite(value) ? value : fallback];
    }));
  } catch {
    return { ...PANEL_DEFAULTS };
  }
}

function savePanelLayout() {
  try { localStorage.setItem(PANEL_LAYOUT_KEY, JSON.stringify(state.panelLayout)); } catch { /* localStorage may be disabled */ }
}

function clampPanelValue(value, limits) {
  return Math.round(Math.min(limits.max, Math.max(limits.min, Number(value) || limits.min)));
}

function syncFocusLayoutState() {
  const showFilePanel = state.layoutMode === "focus" && state.filePanelVisible && Boolean(state.activeFilePath);
  elements.workbench.classList.toggle("has-file-panel", showFilePanel);
  elements.closeFilePanelButton.classList.toggle("hidden", state.layoutMode !== "focus");
  elements.focusFilesButton.classList.toggle("active", elements.workbench.classList.contains("focus-explorer-open"));
}

function resizeLayoutKey(kind) {
  if (state.layoutMode !== "focus") return kind;
  if (kind === "explorer") return "focusSidebar";
  if (kind === "assistant") return "focusFile";
  return kind;
}

function syncPanelResizerLabels() {
  const focusMode = state.layoutMode === "focus";
  const explorerLabel = focusMode ? "调整对话栏宽度" : "调整文件栏宽度";
  const assistantLabel = focusMode ? "调整文件预览栏宽度" : "调整对话与执行栏宽度";
  elements.explorerResizer.setAttribute("aria-label", explorerLabel);
  elements.explorerResizer.title = `${explorerLabel}，双击恢复默认`;
  elements.assistantResizer.setAttribute("aria-label", assistantLabel);
  elements.assistantResizer.title = `${assistantLabel}，双击恢复默认`;
}

function applyLayoutMode(mode, { persist = false } = {}) {
  const normalized = LAYOUT_MODES.has(mode) ? mode : "editor";
  const changed = state.layoutMode !== normalized;
  state.layoutMode = normalized;
  elements.workbench.classList.toggle("layout-focus", normalized === "focus");
  elements.workbench.classList.toggle("layout-editor", normalized === "editor");
  elements.assistantTitle.textContent = normalized === "focus" ? "当前对话" : "会话与执行";
  if (normalized === "focus" && changed && state.activeFilePath) state.filePanelVisible = true;
  if (normalized === "editor") elements.workbench.classList.remove("focus-explorer-open");
  syncPanelResizerLabels();
  syncFocusLayoutState();
  applyPanelLayout({ persist });
}

function toggleFocusExplorer(force) {
  if (state.layoutMode !== "focus" || !state.workspace) return;
  const next = typeof force === "boolean"
    ? force
    : !elements.workbench.classList.contains("focus-explorer-open");
  elements.workbench.classList.toggle("focus-explorer-open", next);
  syncFocusLayoutState();
  if (next) elements.refreshFilesButton.focus();
}

function setFilePanelVisible(visible) {
  state.filePanelVisible = Boolean(visible) && Boolean(state.activeFilePath);
  syncFocusLayoutState();
  applyPanelLayout();
}

function assistantPane() {
  return elements.threadResizer.closest(".assistant-pane");
}

function availableThreadHeight() {
  const pane = assistantPane();
  if (!pane?.clientHeight) return PANEL_LIMITS.threads.max;
  const header = pane.querySelector(".assistant-header")?.offsetHeight || 59;
  const tabs = pane.querySelector(".assistant-tabs")?.offsetHeight || 37;
  const status = pane.querySelector(".assistant-status")?.offsetHeight || 25;
  return Math.max(
    PANEL_LIMITS.threads.min,
    Math.min(PANEL_LIMITS.threads.max, pane.clientHeight - header - tabs - status - 8 - PANEL_LIMITS.assistantContentMin),
  );
}

function applyPanelLayout({ persist = false } = {}) {
  const layout = state.panelLayout;
  const viewportWidth = window.innerWidth;
  const workbenchWidth = Math.max(0, elements.workbench.clientWidth - 16);

  layout.explorer = clampPanelValue(layout.explorer, PANEL_LIMITS.explorer);
  layout.assistant = clampPanelValue(layout.assistant, PANEL_LIMITS.assistant);
  if (state.layoutMode === "focus") {
    layout.focusSidebar = clampPanelValue(layout.focusSidebar, PANEL_LIMITS.focusSidebar);
    layout.focusFile = clampPanelValue(layout.focusFile, PANEL_LIMITS.focusFile);
    elements.workbench.style.setProperty("--explorer-width", `${layout.focusSidebar}px`);
    elements.workbench.style.setProperty("--assistant-width", `${layout.focusFile}px`);
    elements.explorerResizer.setAttribute("aria-valuemin", String(PANEL_LIMITS.focusSidebar.min));
    elements.explorerResizer.setAttribute("aria-valuemax", String(PANEL_LIMITS.focusSidebar.max));
    elements.explorerResizer.setAttribute("aria-valuenow", String(layout.focusSidebar));
    elements.assistantResizer.setAttribute("aria-valuemin", String(PANEL_LIMITS.focusFile.min));
    elements.assistantResizer.setAttribute("aria-valuemax", String(PANEL_LIMITS.focusFile.max));
    elements.assistantResizer.setAttribute("aria-valuenow", String(layout.focusFile));
    if (persist) savePanelLayout();
    return;
  }
  if (viewportWidth > 930 && workbenchWidth) {
    const maximumSides = Math.max(
      PANEL_LIMITS.explorer.min + PANEL_LIMITS.assistant.min,
      workbenchWidth - 16 - PANEL_LIMITS.editorMin,
    );
    let overflow = layout.explorer + layout.assistant - maximumSides;
    if (overflow > 0) {
      const explorerReduction = Math.min(overflow, layout.explorer - PANEL_LIMITS.explorer.min);
      layout.explorer -= explorerReduction;
      overflow -= explorerReduction;
      layout.assistant = Math.max(PANEL_LIMITS.assistant.min, layout.assistant - overflow);
    }
  } else if (viewportWidth > 650 && workbenchWidth) {
    layout.explorer = Math.min(
      layout.explorer,
      Math.max(PANEL_LIMITS.explorer.min, workbenchWidth - 8 - PANEL_LIMITS.editorMin),
    );
  }

  const threadLimits = { ...PANEL_LIMITS.threads, max: availableThreadHeight() };
  layout.threads = clampPanelValue(layout.threads, threadLimits);
  elements.workbench.style.setProperty("--explorer-width", `${layout.explorer}px`);
  elements.workbench.style.setProperty("--assistant-width", `${layout.assistant}px`);
  assistantPane().style.setProperty("--thread-strip-height", `${layout.threads}px`);
  elements.explorerResizer.setAttribute("aria-valuemin", String(PANEL_LIMITS.explorer.min));
  elements.explorerResizer.setAttribute("aria-valuemax", String(PANEL_LIMITS.explorer.max));
  elements.explorerResizer.setAttribute("aria-valuenow", String(layout.explorer));
  elements.assistantResizer.setAttribute("aria-valuemin", String(PANEL_LIMITS.assistant.min));
  elements.assistantResizer.setAttribute("aria-valuemax", String(PANEL_LIMITS.assistant.max));
  elements.assistantResizer.setAttribute("aria-valuenow", String(layout.assistant));
  elements.threadResizer.setAttribute("aria-valuenow", String(layout.threads));
  elements.threadResizer.setAttribute("aria-valuemax", String(threadLimits.max));
  if (persist) savePanelLayout();
}

function resizePanelFromPointer(kind, event) {
  const workbenchRect = elements.workbench.getBoundingClientRect();
  const layoutKey = resizeLayoutKey(kind);
  if (kind === "explorer") {
    state.panelLayout[layoutKey] = event.clientX - workbenchRect.left - 8;
  } else if (kind === "assistant") {
    state.panelLayout[layoutKey] = workbenchRect.right - 8 - event.clientX;
  } else if (kind === "threads") {
    const pane = assistantPane();
    const headerHeight = pane.querySelector(".assistant-header")?.offsetHeight || 59;
    state.panelLayout.threads = event.clientY - pane.getBoundingClientRect().top - headerHeight;
  }
  applyPanelLayout();
}

function beginPanelResize(event) {
  if (event.button !== 0) return;
  const resizer = event.currentTarget;
  const kind = resizer.dataset.resize;
  if (resizer.classList.contains("panel-resizer-column") && window.innerWidth <= 650) return;
  event.preventDefault();
  resizer.classList.add("active");
  if (typeof resizer.setPointerCapture === "function") resizer.setPointerCapture(event.pointerId);
  document.body.classList.add("is-resizing", resizer.classList.contains("panel-resizer-row") ? "is-resizing-row" : "is-resizing-column");
  const move = (moveEvent) => resizePanelFromPointer(kind, moveEvent);
  const finish = () => {
    resizer.classList.remove("active");
    document.body.classList.remove("is-resizing", "is-resizing-row", "is-resizing-column");
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", finish);
    window.removeEventListener("pointercancel", finish);
    if (typeof resizer.hasPointerCapture === "function" && resizer.hasPointerCapture(event.pointerId)) {
      resizer.releasePointerCapture(event.pointerId);
    }
    applyPanelLayout({ persist: true });
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", finish, { once: true });
  window.addEventListener("pointercancel", finish, { once: true });
}

function adjustPanelWithKeyboard(event) {
  const kind = event.currentTarget.dataset.resize;
  const layoutKey = resizeLayoutKey(kind);
  const step = event.shiftKey ? 40 : 16;
  let delta = 0;
  if (kind === "threads") {
    if (event.key === "ArrowUp") delta = -step;
    if (event.key === "ArrowDown") delta = step;
  } else {
    if (event.key === "ArrowLeft") delta = -step;
    if (event.key === "ArrowRight") delta = step;
    if (kind === "assistant") delta *= -1;
  }
  if (!delta) return;
  event.preventDefault();
  state.panelLayout[layoutKey] += delta;
  applyPanelLayout({ persist: true });
}

function resetPanelSize(event) {
  const kind = event.currentTarget.dataset.resize;
  const layoutKey = resizeLayoutKey(kind);
  state.panelLayout[layoutKey] = PANEL_DEFAULTS[layoutKey];
  applyPanelLayout({ persist: true });
  const panelName = state.layoutMode === "focus"
    ? (kind === "explorer" ? "对话栏" : kind === "assistant" ? "文件预览栏" : "会话列表")
    : (kind === "explorer" ? "文件栏" : kind === "assistant" ? "对话与执行栏" : "会话列表");
  showToast(`${panelName}已恢复默认大小`);
}

function initializePanelResizers() {
  [elements.explorerResizer, elements.assistantResizer, elements.threadResizer].forEach((resizer) => {
    resizer.addEventListener("pointerdown", beginPanelResize);
    resizer.addEventListener("keydown", adjustPanelWithKeyboard);
    resizer.addEventListener("dblclick", resetPanelSize);
  });
  let resizeFrame = null;
  window.addEventListener("resize", () => {
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = null;
      applyPanelLayout();
    });
  });
  applyPanelLayout();
}

async function openWorkspace(workspace, sessionId = null, preserveEditor = false, restoring = false) {
  const candidate = String(workspace || "").trim();
  if (!candidate) return showToast("请选择一个本地文件夹");
  try {
    const result = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        workspace: candidate,
        mode: elements.modeSelect.value,
        session_id: sessionId,
        reasoning_profile: elements.reasoningSelect.value,
        task_profile: elements.taskProfileSelect.value,
        approval_policy: elements.approvalPolicySelect.value,
      }),
    });
    closeSocket();
    clearRunSyncTimer();
    state.runEpoch += 1;
    state.pendingUserEchoes = [];
    state.sessionId = result.session_id;
    state.workspace = result.workspace;
    state.sessionMode = result.mode || "act";
    state.pendingMode = null;
    state.modeSyncPromise = null;
    state.running = false;
    state.seenSequences.clear();
    state.pendingApproval = null;
    elements.workspaceInput.value = result.workspace;
    elements.workspaceTitle.textContent = workspaceName(result.workspace);
    elements.focusWorkspaceTitle.textContent = workspaceName(result.workspace);
    elements.explorerPath.textContent = result.workspace;
    elements.reasoningSelect.value = result.reasoning_profile || elements.reasoningSelect.value;
    elements.modeSelect.value = state.sessionMode;
    elements.taskProfileSelect.value = result.task_profile || elements.taskProfileSelect.value;
    state.approvalPolicy = result.approval_policy || "ask";
    elements.approvalPolicySelect.value = state.approvalPolicy;
    updateApprovalPolicyVisual();
    enableWorkspaceControls(true);
    resetConversationSurface();
    if (!preserveEditor) resetEditor();
    setRunning(false);
    connectSocket();
    await Promise.all([loadRootFiles(), loadWorkspaceSessions()]);
    saveWorkspaceState();
    if (!restoring) showToast(sessionId ? "已切换对话" : "工作区已打开");
    return true;
  } catch (error) {
    showToast(error.message);
    return false;
  }
}

async function restoreLastWorkspace() {
  const saved = loadWorkspaceState();
  if (!saved) return false;
  elements.workspaceInput.value = saved.workspace;
  elements.modeSelect.value = saved.mode;
  elements.reasoningSelect.value = saved.reasoningProfile;
  elements.taskProfileSelect.value = saved.taskProfile;
  try {
    await api(`/api/fs/browse?${new URLSearchParams({ path: saved.workspace })}`);
  } catch {
    clearWorkspaceState();
    elements.workspaceInput.value = "";
    showToast("上次工作区已不可用，请重新选择文件夹");
    return false;
  }
  const opened = await openWorkspace(saved.workspace, saved.sessionId, false, true);
  if (!opened) return false;
  for (const path of saved.openFiles) await openFile(path);
  if (saved.activeFilePath && state.fileCache.has(saved.activeFilePath)) {
    state.activeFilePath = saved.activeFilePath;
    renderEditorTabs();
    renderFile(state.fileCache.get(saved.activeFilePath));
  }
  setAssistantView(saved.activeView);
  saveWorkspaceState();
  showToast("已恢复上次工作区");
  return true;
}

function enableWorkspaceControls(enabled) {
  elements.messageInput.disabled = !enabled;
  elements.sendButton.disabled = !enabled;
  elements.refreshFilesButton.disabled = !enabled;
  elements.restoreButton.disabled = !enabled;
  elements.newSessionButton.disabled = !enabled;
  elements.focusNewSessionButton.disabled = !enabled;
  elements.focusFilesButton.disabled = !enabled;
  elements.approvalPolicySelect.disabled = !enabled;
}

function resetConversationSurface() {
  state.restoringHistory = false;
  state.historyBuffer = null;
  state.historyMetadata = null;
  elements.messageList.innerHTML = '<div class="chat-empty" id="chatEmpty"><span class="chat-brand-mark" aria-hidden="true"><img src="/static/code-helper-logo.png?v=20260831.7" alt="" draggable="false" /></span><span class="empty-kicker">CODE HELPER / READY</span><h3>开始一段新的对话</h3><p>描述你希望理解、修改或验证的任务。</p></div><div class="thinking-indicator hidden" id="thinkingIndicator" role="status" aria-live="polite"><span class="thinking-mark" aria-hidden="true"><i></i><i></i><i></i></span><span class="thinking-copy"><strong>模型正在思考</strong><span id="thinkingStatus">正在分析你的请求并选择下一步操作…</span></span></div>';
  elements.thinkingIndicator = elements.messageList.querySelector("#thinkingIndicator");
  elements.thinkingStatus = elements.messageList.querySelector("#thinkingStatus");
  elements.activityList.innerHTML = '<div class="view-empty">Agent 的模型请求、工具调用和验证过程会显示在这里。</div>';
  elements.planList.innerHTML = '<div class="view-empty">当前会话还没有执行计划。</div>';
  elements.planProgress.textContent = "0 / 0";
  elements.stepCounter.textContent = "STEP 0";
  hideThinkingIndicator();
  resetQuestionNavigator();
  streamingAgentMessage = null;
}

function connectSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const sessionId = state.sessionId;
  const socket = new WebSocket(`${protocol}://${location.host}/ws/sessions/${sessionId}`);
  state.socket = socket;
  socket.onopen = () => reconcileRunState(sessionId, { quiet: true, epoch: state.runEpoch });
  socket.onmessage = (message) => {
    if (state.sessionId !== sessionId) return;
    const event = JSON.parse(message.data);
    if (event.type === "history_start") {
      state.historyBuffer = [];
      state.historyMetadata = event.payload || {};
      return;
    }
    if (event.type === "history_chunk") {
      if (state.historyBuffer) state.historyBuffer.push(...(event.payload?.events || []));
      return;
    }
    if (event.type === "history_end") {
      restoreEventHistory(state.historyBuffer || [], state.historyMetadata || {});
      state.historyBuffer = null;
      state.historyMetadata = null;
      return;
    }
    handleEvent(event);
  };
  socket.onclose = () => {
    if (state.socket === socket && state.sessionId === sessionId) setTimeout(connectSocket, 1200);
  };
}

function closeSocket() {
  if (!state.socket) return;
  state.socket.onclose = null;
  state.socket.close();
  state.socket = null;
}

async function loadWorkspaceSessions() {
  if (!state.workspace) return;
  try {
    const params = new URLSearchParams({ workspace: state.workspace });
    const result = await api(`/api/workspaces/sessions?${params}`);
    state.workspaceSessions = result.sessions || [];
    state.archivedSessions = result.archived_sessions || [];
    renderSessionList();
    return result;
  } catch (error) {
    [elements.sessionList, elements.focusSessionList].forEach((container) => {
      container.innerHTML = `<div class="thread-empty">${escapeHtml(error.message)}</div>`;
    });
    return null;
  }
}

function renderSessionList() {
  const archivedView = state.sessionListView === "archived";
  const sessions = archivedView ? state.archivedSessions : state.workspaceSessions;
  [elements.sessionList, elements.focusSessionList].forEach((container) => {
    container.innerHTML = "";
    const toolbar = document.createElement("div");
    toolbar.className = "thread-list-toolbar";
    toolbar.setAttribute("aria-label", "对话列表筛选");
    toolbar.innerHTML = `<button type="button" class="${archivedView ? "" : "active"}" data-session-view="active">对话 <span>${state.workspaceSessions.length}</span></button><button type="button" class="${archivedView ? "active" : ""}" data-session-view="archived">已归档 <span>${state.archivedSessions.length}</span></button>`;
    toolbar.querySelectorAll("[data-session-view]").forEach((button) => button.addEventListener("click", () => {
      state.sessionListView = button.dataset.sessionView;
      renderSessionList();
    }));
    container.append(toolbar);
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "thread-empty";
      empty.textContent = archivedView ? "还没有归档的对话" : "还没有保存的对话";
      container.append(empty);
      return;
    }
    sessions.forEach((session) => {
      const row = document.createElement("div");
      row.className = `thread-item ${session.session_id === state.sessionId ? "active" : ""} ${archivedView ? "archived" : ""}`;
      const openButton = document.createElement("button");
      openButton.type = "button";
      openButton.className = "thread-open";
      openButton.innerHTML = `<span class="thread-state ${escapeHtml(session.status)}"></span><span class="thread-copy"><strong>${escapeHtml(session.title)}</strong><small>${escapeHtml(session.preview)}</small></span><time>${formatRelativeTime(archivedView ? session.archived_at : session.updated_at)}</time>`;
      openButton.addEventListener("click", () => {
        if (archivedView) return restoreSession(session, openButton);
        if (session.session_id !== state.sessionId) {
          openWorkspace(state.workspace, session.session_id, true);
        }
      });
      const action = document.createElement("button");
      action.type = "button";
      action.className = `thread-row-action ${archivedView ? "restore" : "archive"}`;
      action.setAttribute("aria-label", archivedView ? `恢复对话：${session.title}` : `归档对话：${session.title}`);
      action.title = archivedView ? "恢复对话" : (session.session_id === state.sessionId && state.running ? "运行中的对话不能归档" : "归档对话");
      action.disabled = !archivedView && session.session_id === state.sessionId && state.running;
      action.innerHTML = archivedView
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v5h5"/></svg>'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 8h16v11H4zM3 5h18v3H3zM9 12h6"/></svg>';
      action.addEventListener("click", () => archivedView ? restoreSession(session, action) : archiveSession(session, action));
      row.append(openButton, action);
      container.append(row);
    });
  });
}

async function archiveSession(session, button) {
  if (!state.workspace || button.disabled) return;
  button.disabled = true;
  try {
    await api(`/api/workspaces/sessions/${encodeURIComponent(session.session_id)}/archive`, {
      method: "POST",
      body: JSON.stringify({ workspace: state.workspace }),
    });
    const archivedCurrent = session.session_id === state.sessionId;
    const result = await loadWorkspaceSessions();
    if (archivedCurrent && result) {
      state.sessionListView = "active";
      const nextSession = result.sessions?.[0];
      await openWorkspace(state.workspace, nextSession?.session_id || null, true);
    }
    showToast("对话已归档，可在“已归档”中恢复");
  } catch (error) {
    button.disabled = false;
    showToast(error.message);
  }
}

async function restoreSession(session, button) {
  if (!state.workspace || button.disabled) return;
  button.disabled = true;
  try {
    await api(`/api/workspaces/sessions/${encodeURIComponent(session.session_id)}/restore`, {
      method: "POST",
      body: JSON.stringify({ workspace: state.workspace }),
    });
    await loadWorkspaceSessions();
    showToast("对话已恢复");
  } catch (error) {
    button.disabled = false;
    showToast(error.message);
  }
}

function formatRelativeTime(value) {
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta)) return "";
  if (delta < 60_000) return "刚刚";
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h`;
  return `${Math.floor(delta / 86_400_000)}d`;
}

async function loadRootFiles() {
  if (!state.sessionId) return;
  elements.explorerRoot.innerHTML = '<div class="tree-loading">正在读取工作区…</div>';
  try {
    const result = await listFiles(".");
    elements.explorerRoot.innerHTML = "";
    renderFileEntries(result.entries, elements.explorerRoot, 0);
    if (!result.entries.length) elements.explorerRoot.innerHTML = '<div class="tree-loading">工作区为空</div>';
  } catch (error) {
    elements.explorerRoot.innerHTML = `<div class="tree-loading error">${escapeHtml(error.message)}</div>`;
  }
}

function listFiles(path) {
  return api(`/api/sessions/${state.sessionId}/files?${new URLSearchParams({ path })}`);
}

function renderFileEntries(entries, container, depth) {
  entries.forEach((entry) => {
    let children = null;
    const group = document.createElement("div");
    group.className = "tree-group";
    const row = document.createElement("button");
    row.type = "button";
    row.className = "tree-row";
    row.style.setProperty("--depth", depth);
    row.title = entry.path;
    row.innerHTML = entry.kind === "directory"
      ? `${iconSvg("chevron")} ${iconSvg("folder")}<span>${escapeHtml(entry.name)}</span>`
      : `${iconSvg("blank")} ${iconSvg("file")}<span>${escapeHtml(entry.name)}</span>`;
    if (entry.kind === "directory") row.setAttribute("aria-expanded", "false");
    group.append(row);
    container.append(group);
    row.addEventListener("click", async () => {
      if (entry.kind === "file") {
        selectFileRow(row, entry.path);
        await openFile(entry.path);
        toggleFocusExplorer(false);
        return;
      }
      if (children) {
        children.classList.toggle("hidden");
        const expanded = !children.classList.contains("hidden");
        row.classList.toggle("expanded", expanded);
        row.setAttribute("aria-expanded", String(expanded));
        return;
      }
      row.classList.add("expanded");
      row.setAttribute("aria-expanded", "true");
      children = document.createElement("div");
      children.className = "tree-children";
      children.innerHTML = '<div class="tree-loading nested">读取中…</div>';
      group.append(children);
      try {
        const result = await listFiles(entry.path);
        children.innerHTML = "";
        renderFileEntries(result.entries, children, depth + 1);
        if (!result.entries.length) children.innerHTML = '<div class="tree-loading nested">空目录</div>';
      } catch (error) {
        children.innerHTML = `<div class="tree-loading nested error">${escapeHtml(error.message)}</div>`;
      }
    });
  });
}

function selectFileRow(row, path) {
  state.selectedFileRow?.classList.remove("selected");
  state.selectedFileRow = row;
  state.selectedFilePath = path;
  row.classList.add("selected");
  elements.insertFileButton.disabled = false;
}

async function openFile(path, force = false) {
  if (!state.sessionId) return;
  elements.fileStatus.textContent = "LOADING";
  try {
    let file = state.fileCache.get(path);
    if (!file || force) {
      file = await api(`/api/sessions/${state.sessionId}/file?${new URLSearchParams({ path })}`);
      state.fileCache.set(path, file);
    }
    if (!state.openFiles.includes(path)) state.openFiles.push(path);
    state.activeFilePath = path;
    state.filePanelVisible = true;
    renderEditorTabs();
    renderFile(file);
    syncFocusLayoutState();
    applyPanelLayout();
    saveWorkspaceState();
  } catch (error) {
    if (error.status === 415) {
      const file = {
        path,
        name: path.split("/").at(-1),
        content: "",
        size: 0,
        line_count: 0,
        language: "binary",
        truncated: false,
        binary: true,
        previewMessage: error.message,
      };
      state.fileCache.set(path, file);
      if (!state.openFiles.includes(path)) state.openFiles.push(path);
      state.activeFilePath = path;
      state.filePanelVisible = true;
      renderEditorTabs();
      renderFile(file);
      syncFocusLayoutState();
      applyPanelLayout();
      saveWorkspaceState();
      return;
    }
    elements.fileStatus.textContent = "ERROR";
    showToast(error.message);
  }
}

function renderEditorTabs() {
  elements.editorTabs.innerHTML = "";
  if (!state.openFiles.length) {
    elements.editorTabs.innerHTML = '<div class="editor-tab-placeholder">没有打开的文件</div>';
    return;
  }
  state.openFiles.forEach((path) => {
    const tab = document.createElement("div");
    tab.className = `editor-tab ${path === state.activeFilePath ? "active" : ""}`;
    tab.innerHTML = `${iconSvg("file")}<button class="tab-label" type="button">${escapeHtml(path.split("/").at(-1))}</button><button class="tab-close" type="button" aria-label="关闭 ${escapeHtml(path)}">×</button>`;
    tab.querySelector(".tab-label").addEventListener("click", () => {
      state.activeFilePath = path;
      state.filePanelVisible = true;
      renderEditorTabs();
      renderFile(state.fileCache.get(path));
      syncFocusLayoutState();
      applyPanelLayout();
      saveWorkspaceState();
    });
    tab.querySelector(".tab-close").addEventListener("click", () => closeFile(path));
    elements.editorTabs.append(tab);
  });
}

function closeFile(path) {
  const index = state.openFiles.indexOf(path);
  if (index < 0) return;
  state.openFiles.splice(index, 1);
  if (state.activeFilePath === path) {
    state.activeFilePath = state.openFiles[Math.min(index, state.openFiles.length - 1)] || null;
  }
  renderEditorTabs();
  if (state.activeFilePath) renderFile(state.fileCache.get(state.activeFilePath));
  else {
    state.filePanelVisible = false;
    showEditorEmpty();
  }
  syncFocusLayoutState();
  applyPanelLayout();
  saveWorkspaceState();
}

function renderFile(file) {
  if (!file) return showEditorEmpty();
  elements.editorEmpty.classList.add("hidden");
  elements.codeScroll.classList.remove("hidden");
  elements.editorBreadcrumbs.innerHTML = file.path.split("/").map((part) => `<span>${escapeHtml(part)}</span>`).join('<b>›</b>');
  elements.editorLanguage.textContent = file.language;
  if (file.binary) {
    elements.fileStatus.textContent = "BINARY";
    elements.filePosition.textContent = "不可预览";
    elements.fileSize.textContent = "—";
    elements.copyFileButton.disabled = true;
    elements.reloadFileButton.disabled = false;
    elements.codeScroll.classList.remove("markdown-mode");
    elements.codeLines.innerHTML = `<div class="file-preview-notice"><span class="preview-file-icon" aria-hidden="true">01</span><h2>这是一个二进制文件</h2><p>${escapeHtml(file.previewMessage || "该文件不能作为文本安全地显示。")}</p><small>${escapeHtml(file.path)}</small></div>`;
    elements.codeScroll.scrollTop = 0;
    elements.codeScroll.scrollLeft = 0;
    return;
  }
  elements.fileStatus.textContent = file.truncated ? "TRUNCATED" : "READY";
  elements.filePosition.textContent = `${file.line_count} lines`;
  elements.fileSize.textContent = formatBytes(file.size);
  elements.copyFileButton.disabled = false;
  elements.reloadFileButton.disabled = false;
  elements.codeScroll.classList.toggle("markdown-mode", file.language === "markdown");
  if (file.language === "markdown") {
    elements.codeLines.innerHTML = `<article class="markdown-document">${CodeHelperRendering.renderMarkdown(file.content)}</article>`;
    elements.codeScroll.scrollTop = 0;
    elements.codeScroll.scrollLeft = 0;
    return;
  }
  const lines = file.content.split("\n");
  const maxLines = 8000;
  const highlightedLines = CodeHelperRendering.highlightCode(lines.slice(0, maxLines).join("\n"), file.language).split("\n");
  elements.codeLines.innerHTML = highlightedLines.map((line, index) => `<div class="code-line"><span class="line-number">${index + 1}</span><code>${line || " "}</code></div>`).join("");
  if (lines.length > maxLines) elements.codeLines.insertAdjacentHTML("beforeend", `<div class="code-limit">为保证性能，仅显示前 ${maxLines} 行。</div>`);
  elements.codeScroll.scrollTop = 0;
  elements.codeScroll.scrollLeft = 0;
}

function resetEditor() {
  state.openFiles = [];
  state.fileCache.clear();
  state.activeFilePath = null;
  state.filePanelVisible = false;
  state.selectedFilePath = null;
  state.selectedFileRow = null;
  elements.insertFileButton.disabled = true;
  renderEditorTabs();
  showEditorEmpty();
  syncFocusLayoutState();
}

function showEditorEmpty() {
  elements.editorEmpty.classList.remove("hidden");
  elements.codeScroll.classList.add("hidden");
  elements.codeScroll.classList.remove("markdown-mode");
  elements.editorBreadcrumbs.textContent = "选择左侧文件以预览代码";
  elements.editorLanguage.textContent = "—";
  elements.copyFileButton.disabled = true;
  elements.reloadFileButton.disabled = true;
  elements.fileStatus.textContent = "READY";
  elements.filePosition.textContent = "Ln 1, Col 1";
  elements.fileSize.textContent = "0 bytes";
}

function formatBytes(size) {
  if (size < 1024) return `${size} bytes`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function resizeMessageInput() {
  const input = elements.messageInput;
  if (!input) return;
  input.style.height = "auto";
  const maxHeight = Number.parseFloat(getComputedStyle(input).maxHeight) || 150;
  const nextHeight = Math.min(maxHeight, input.scrollHeight);
  input.style.height = `${nextHeight}px`;
  input.style.overflowY = input.scrollHeight > maxHeight ? "auto" : "hidden";
}

function insertSelectedFile() {
  if (!state.selectedFilePath) return;
  const existing = elements.messageInput.value.trim();
  elements.messageInput.value = `${existing}${existing ? " " : ""}@${state.selectedFilePath} `;
  resizeMessageInput();
  elements.messageInput.focus();
  setAssistantView("chat");
}

async function sendMessage() {
  if (!state.sessionId || state.running) return;
  if (state.modeSyncPromise) {
    const synced = await state.modeSyncPromise;
    if (synced === false) return;
  }
  if (state.pendingMode) {
    const synced = await synchronizeMode(state.pendingMode);
    if (synced === false) return;
  }
  if (state.pendingMode || state.running) return;
  const content = elements.messageInput.value.trim();
  if (!content) return;
  const sessionId = state.sessionId;
  const requestEpoch = ++state.runEpoch;
  const echo = { content, node: addMessage("user", content) };
  state.pendingUserEchoes.push(echo);
  elements.messageInput.value = "";
  resizeMessageInput();
  try {
    await api(`/api/sessions/${sessionId}/messages`, { method: "POST", body: JSON.stringify({ content }) });
    if (state.sessionId !== sessionId) return;
    state.stopping = false;
    if (state.runEpoch === requestEpoch) setRunning(true);
  } catch (error) {
    const pendingIndex = state.pendingUserEchoes.indexOf(echo);
    if (pendingIndex >= 0) {
      state.pendingUserEchoes.splice(pendingIndex, 1);
      echo.node?.remove();
    }
    elements.messageInput.value = content;
    resizeMessageInput();
    showToast(error.message);
  }
}

async function synchronizeMode(mode) {
  if (!state.sessionId || state.running) return false;
  const requested = ["ask", "plan", "act"].includes(mode) ? mode : "act";
  try {
    const result = await api(`/api/sessions/${state.sessionId}/mode`, {
      method: "POST",
      body: JSON.stringify({ mode: requested }),
    });
    state.sessionMode = result.mode || requested;
    elements.modeSelect.value = state.sessionMode;
    state.pendingMode = null;
    saveWorkspaceState();
    return true;
  } catch (error) {
    elements.modeSelect.value = state.sessionMode;
    state.pendingMode = null;
    showToast(error.message);
    return false;
  }
}

function schedulePendingModeSync() {
  if (!state.pendingMode || state.running || state.modeSyncPromise) return;
  const promise = synchronizeMode(state.pendingMode);
  state.modeSyncPromise = promise;
  promise.finally(() => {
    if (state.modeSyncPromise === promise) state.modeSyncPromise = null;
  });
}

async function cancelRun() {
  if (!state.sessionId) return;
  const sessionId = state.sessionId;
  try {
    await api(`/api/sessions/${state.sessionId}/cancel`, { method: "POST" });
    state.stopping = true;
    elements.cancelButton.disabled = true;
    updateRunStatus();
    reconcileRunState(sessionId, { epoch: state.runEpoch });
    showToast("已请求停止 Agent");
  } catch (error) { showToast(error.message); }
}

function clearRunSyncTimer() {
  if (state.runSyncTimer !== null) clearTimeout(state.runSyncTimer);
  state.runSyncTimer = null;
}

async function reconcileRunState(sessionId, { quiet = false, attempt = 0, epoch = state.runEpoch } = {}) {
  if (!sessionId || state.sessionId !== sessionId || state.runEpoch !== epoch) return;
  clearRunSyncTimer();
  try {
    const details = await api(`/api/sessions/${sessionId}`);
    if (state.sessionId !== sessionId || state.runEpoch !== epoch) return;
    if (!details.running) {
      state.stopping = false;
      setRunning(false);
      if (!quiet) showToast(details.status === "cancelled" ? "Agent 已停止，可以继续对话" : "Agent 已结束运行");
      return;
    }
    setRunning(true);
    if (state.stopping) updateRunStatus();
  } catch (error) {
    if (!quiet && attempt === 0) showToast(`状态同步失败：${error.message}`);
  }
  if (state.sessionId !== sessionId || state.runEpoch !== epoch || attempt >= 60) return;
  const delay = attempt < 8 ? 250 : Math.min(2000, 500 + attempt * 50);
  state.runSyncTimer = setTimeout(
    () => reconcileRunState(sessionId, { quiet: true, attempt: attempt + 1, epoch }),
    delay,
  );
}

async function restoreCheckpoint() {
  if (!state.sessionId || state.running) return;
  try {
    const result = await api(`/api/sessions/${state.sessionId}/checkpoint`);
    state.restorePreview = result.preview || [];
    renderRestoreDialog();
    if (!state.restorePreview.length) return showToast("当前会话没有可恢复的检查点");
    elements.restoreBackdrop.classList.remove("hidden");
  } catch (error) { showToast(error.message); }
}

function renderRestoreDialog() {
  elements.restoreFileList.innerHTML = state.restorePreview.map((item, index) => `<label class="restore-file-item"><input type="checkbox" data-restore-path="${escapeHtml(item.path)}" checked><span><b>${escapeHtml(item.path)}</b><small>${item.conflict ? "检测到外部修改" : "可安全恢复"}</small></span></label>`).join("");
  elements.restoreFileList.querySelectorAll("input[data-restore-path]").forEach((input) => input.addEventListener("change", () => updateRestorePreview(input.dataset.restorePath || "")));
  elements.restoreFileList.querySelectorAll(".restore-file-item").forEach((row) => row.addEventListener("click", () => updateRestorePreview(row.querySelector("input")?.dataset.restorePath || "")));
  updateRestorePreview(state.restorePreview[0]?.path || "");
}

function updateRestorePreview(path) {
  const item = state.restorePreview.find((entry) => entry.path === path) || state.restorePreview[0];
  if (!item) { elements.restorePreviewDiff.textContent = "选择文件查看恢复差异。"; return; }
  const diff = item.conflict ? (item.external_diff || item.restore_diff) : item.restore_diff;
  elements.restorePreviewDiff.textContent = `${item.path}\n${item.reason}\n\n${diff || "无文本差异可预览。"}`;
  elements.restoreFileList.querySelectorAll(".restore-file-item").forEach((row) => row.classList.toggle("active", row.querySelector("input")?.dataset.restorePath === item.path));
}

async function confirmRestoreSelection() {
  const paths = [...elements.restoreFileList.querySelectorAll("input[data-restore-path]:checked")].map((input) => input.dataset.restorePath);
  if (!paths.length) return showToast("至少选择一个文件");
  if (!window.confirm("恢复选中文件会覆盖 Agent 的修改，是否继续？")) return;
  try {
    let result;
    try {
      result = await requestCheckpointRestore(false, null, paths);
    } catch (error) {
      if (error.status !== 409 || error.detail?.code !== "RESTORE_CONFLICT") throw error;
      const conflicts = (error.detail.conflicts || []).filter((item) => paths.includes(item.path));
      const names = conflicts.map((item) => item.path).join("、");
      if (!window.confirm(`检测到外部编辑：${names}\n\n继续会覆盖这些新内容。确认强制恢复吗？`)) return showToast("已保留外部修改，未执行回滚");
      result = await requestCheckpointRestore(true, Object.fromEntries(conflicts.map((item) => [item.path, item.current_sha256 ?? null])), paths);
    }
    elements.restoreBackdrop.classList.add("hidden");
    state.fileCache.clear();
    showToast(`${result.forced ? "已强制恢复" : "已恢复"} ${result.restored.length} 个文件`);
    await loadRootFiles();
    if (state.activeFilePath) await openFile(state.activeFilePath, true);
  } catch (error) { showToast(error.message); }
}

function requestCheckpointRestore(force, confirmedHashes = null, paths = null) {
  return api(`/api/sessions/${state.sessionId}/restore`, {
    method: "POST",
    body: JSON.stringify({ force, paths, confirmed_hashes: confirmedHashes }),
  });
}

async function resolveApproval(approved, scope = "once") {
  if (!state.pendingApproval) return;
  const toolCallId = String(state.pendingApproval.tool_call_id || state.pendingApproval.id || "").trim();
  if (!toolCallId) {
    elements.approvalBackdrop.classList.add("hidden");
    state.pendingApproval = null;
    showToast("审批请求已失效，请重新发送任务");
    return;
  }
  try {
    const result = await api(`/api/sessions/${state.sessionId}/approval`, { method: "POST", body: JSON.stringify({ tool_call_id: toolCallId, approved, scope }) });
    elements.approvalBackdrop.classList.add("hidden");
    state.pendingApproval = null;
    if (result.grant) {
      const scopeText = result.grant.path_prefix || result.grant.command_prefix || "当前工作区范围";
      showToast(`已授予本会话权限（${scopeText}）`);
      loadIntelligence();
    }
  } catch (error) {
    if (error.status === 409 || error.status === 422) {
      elements.approvalBackdrop.classList.add("hidden");
      state.pendingApproval = null;
      showToast(error.status === 422 ? "审批请求格式已失效，请重新发送任务" : "审批请求已过期，请重新发送任务");
      return;
    }
    showToast(error.message);
  }
}

function updateApprovalPolicyVisual() {
  const control = elements.approvalPolicySelect.closest(".approval-policy-control");
  if (control) control.dataset.policy = state.approvalPolicy;
  const descriptions = {
    ask: "写文件和执行命令前请求你的批准",
    auto: "自动批准常规操作，仍拒绝越界路径和危险命令",
    full: "当前 Act 会话不再拦截工具操作；请仅在可信任务中使用",
  };
  if (control) control.title = descriptions[state.approvalPolicy] || "工具审批策略";
}

async function changeApprovalPolicy() {
  if (!state.sessionId) return;
  const requested = elements.approvalPolicySelect.value;
  const previous = state.approvalPolicy;
  if (requested === "full" && !window.confirm(
    "完全放开会允许当前 Act 会话执行原本会被拒绝的危险命令和越界操作。\n\n仅在你信任当前任务时启用。是否继续？",
  )) {
    elements.approvalPolicySelect.value = previous;
    return;
  }
  try {
    const result = await api(`/api/sessions/${state.sessionId}/approval-policy`, {
      method: "POST",
      body: JSON.stringify({ policy: requested }),
    });
    state.approvalPolicy = result.approval_policy || requested;
    elements.approvalPolicySelect.value = state.approvalPolicy;
    updateApprovalPolicyVisual();
    if (result.approved_pending) {
      elements.approvalBackdrop.classList.add("hidden");
      state.pendingApproval = null;
    }
    showToast(`审批策略：${approvalPolicyLabel(state.approvalPolicy)}${result.approved_pending ? "，当前操作已批准" : ""}`);
    loadIntelligence();
  } catch (error) {
    elements.approvalPolicySelect.value = previous;
    showToast(error.message);
  }
}

function restoreHistoricalEvent(event) {
  const payload = event.payload || {};
  switch (event.type) {
    case "turn_started": addMessage("user", payload.message); break;
    case "run_cancel_requested": addActivity("请求停止任务", "等待当前操作安全退出", "warning"); break;
    case "run_cancelled": addActivity("任务已停止", cancellationReason(payload.reason), "failure"); break;
    case "run_budget_exhausted": addActivity("运行预算已耗尽", `${payload.code || "BUDGET_EXHAUSTED"} · ${payload.message || ""}`, "warning"); break;
    case "run_failed": addActivity("任务执行失败", `${payload.code || "UNEXPECTED_AGENT_ERROR"} · ${payload.message || ""}`, "failure"); break;
    case "approval_policy_changed": addActivity("审批策略已更改", approvalPolicyLabel(payload.policy || "ask"), payload.policy === "full" ? "warning" : "success"); break;
    case "step_started": elements.stepCounter.textContent = `STEP ${payload.step}`; addActivity(`开始 Step ${payload.step}`, "构造上下文并请求模型"); break;
    case "context_built": {
      const selectedCount = Number(payload.repo_map_selected_count || 0);
      addActivity("上下文已构建", `Repo Map ${selectedCount} 个文件 · ${formatNumber(payload.estimated_chars || 0)} 字符`, "success");
      break;
    }
    case "task_profile_selected": addActivity("任务类型已确定", `${payload.profile || "project"} · ${payload.reason || ""}`, "success"); break;
    case "context_compacted": addActivity("上下文已压缩", `约 ${payload.estimated_chars || 0} 字符`, "warning"); break;
    case "model_started": addActivity("模型处理", "已请求模型选择下一步操作"); break;
    case "stuck_recovery": addActivity("检测到重复编辑，正在恢复", payload.message || "请重新读取文件后选择下一步", "warning"); break;
    case "duplicate_write_satisfied": addActivity("已阻止重复写入", payload.message || "目标内容已经存在", "warning"); break;
    case "stuck_terminal": addActivity("已停止重复写入", payload.message || "已保留当前修改", "warning"); break;
    case "assistant_response":
      if (payload.content) addMessage("agent", payload.content);
      if (payload.tool_calls?.length) addActivity("模型选择工具", payload.tool_calls.map((call) => call.name).join(", "));
      break;
    case "tool_started": addActivity(`执行 ${payload.name}`, summarizeArguments(payload.arguments)); break;
    case "tool_result": {
      const result = payload.result || {};
      addActivity(`${result.ok ? "完成" : "失败"} ${payload.name}`, `${result.code || ""} · ${result.message || ""}`, result.ok ? "success" : "failure");
      break;
    }
    case "plan_updated": renderPlan(payload.plan || []); addActivity("计划已更新", payload.reason || "执行步骤发生变化", "success"); break;
    case "approval_requested": addActivity(`等待批准 ${payload.name}`, payload.reason, "warning"); break;
    case "verification_required": addActivity("需要验证", payload.reason, "failure"); break;
    case "repair_attempt": addActivity(`自动修复 ${payload.attempt}/${payload.max_attempts}`, payload.reason, "warning"); break;
    case "checkpoint_created": addActivity("创建检查点", payload.path, "success"); break;
    case "checkpoint_tracking_failed": addActivity("检查点跟踪失败", `${payload.code || ""} · ${payload.message || ""}`, "failure"); break;
    case "checkpoint_restored": addActivity(payload.forced ? "已强制回滚本轮修改" : "已回滚本轮修改", (payload.files || []).join(", "), payload.forced ? "warning" : "success"); break;
    case "verification_recorded": {
      const evidence = payload.evidence || {};
      addActivity(evidence.accepted ? "验证证据已接受" : "验证证据不足", `${String(evidence.kind || "unknown").toUpperCase()} · ${evidence.reason || ""}`, evidence.accepted ? "success" : "warning");
      break;
    }
    case "turn_finished": addActivity(`任务${statusLabel(payload.status)}`, payload.message, payload.status === "completed" ? "success" : "failure"); break;
    default: break;
  }
}

function restoreEventHistory(events, metadata = {}) {
  state.restoringHistory = true;
  let assistantParts = [];
  const flushAssistantParts = () => {
    if (!assistantParts.length) return;
    addMessage("agent", assistantParts.join("\n\n"));
    assistantParts = [];
  };
  try {
    for (const event of events) {
      if (event.sequence && state.seenSequences.has(event.sequence)) continue;
      if (event.sequence) state.seenSequences.add(event.sequence);
      if (event.type === "turn_started") flushAssistantParts();
      if (event.type === "assistant_response") {
        const payload = event.payload || {};
        if (payload.content) assistantParts.push(payload.content);
        if (payload.tool_calls?.length) addActivity("模型选择工具", payload.tool_calls.map((call) => call.name).join(", "));
        continue;
      }
      if (event.type === "turn_finished") flushAssistantParts();
      restoreHistoricalEvent(event);
    }
    flushAssistantParts();
  } finally {
    state.restoringHistory = false;
    streamingAgentMessage = null;
    hideThinkingIndicator();
    hideRunProgress();
    finishLiveActivity();
  }
  if (metadata.status === "waiting_approval" && metadata.pending_approval) {
    showApproval(metadata.pending_approval);
  } else {
    elements.approvalBackdrop.classList.add("hidden");
  }
  setRunning(Boolean(metadata.running));
  requestAnimationFrame(() => {
    elements.messageList.scrollTop = elements.messageList.scrollHeight;
    elements.activityList.scrollTop = elements.activityList.scrollHeight;
  });
}

function handleEvent(event, { historical = false } = {}) {
  if (event.sequence && state.seenSequences.has(event.sequence)) return;
  if (event.sequence) state.seenSequences.add(event.sequence);
  if (historical) return restoreHistoricalEvent(event);
  const payload = event.payload || {};
  switch (event.type) {
    case "turn_started": {
      state.runEpoch += 1;
      state.stopping = false;
      const pendingIndex = state.pendingUserEchoes.findIndex((item) => item.content === payload.message);
      if (pendingIndex >= 0) state.pendingUserEchoes.splice(pendingIndex, 1);
      else addMessage("user", payload.message);
      setRunning(true);
      setRunProgress("正在理解任务", "解析请求并确定执行路径");
      break;
    }
    case "run_budget_started":
    case "run_budget_updated":
      refreshIntelligenceIfVisible();
      break;
    case "run_cancel_requested":
      state.stopping = true;
      updateRunStatus();
      setRunProgress("正在安全停止", "等待当前模型请求或工具进程退出");
      addActivity("正在停止任务", "等待当前模型请求或工具进程安全退出", "warning");
      refreshIntelligenceIfVisible();
      break;
    case "run_cancelled":
      hideThinkingIndicator();
      hideRunProgress();
      addActivity("任务已停止", cancellationReason(payload.reason), "failure");
      refreshIntelligenceIfVisible();
      break;
    case "run_budget_exhausted":
      addActivity("运行预算已耗尽", `${payload.code || "BUDGET_EXHAUSTED"} · ${payload.message || ""}`, "warning");
      refreshIntelligenceIfVisible();
      break;
    case "run_failed":
      hideThinkingIndicator();
      setRunProgress("任务执行失败", payload.message || payload.code || "请查看轨迹中的错误信息");
      addActivity("任务执行失败", `${payload.code || "UNEXPECTED_AGENT_ERROR"} · ${payload.message || ""}`, "failure");
      break;
    case "approval_policy_changed":
      state.approvalPolicy = payload.policy || "ask";
      elements.approvalPolicySelect.value = state.approvalPolicy;
      updateApprovalPolicyVisual();
      if (payload.approved_pending) {
        elements.approvalBackdrop.classList.add("hidden");
        state.pendingApproval = null;
      }
      addActivity("审批策略已更改", approvalPolicyLabel(state.approvalPolicy), state.approvalPolicy === "full" ? "warning" : "success");
      refreshIntelligenceIfVisible();
      break;
    case "step_started": elements.stepCounter.textContent = `STEP ${payload.step}`; setRunProgress(`Step ${payload.step} · 构建上下文`, "正在选择与任务相关的历史、规则和仓库文件"); addActivity(`开始 Step ${payload.step}`, "构造上下文并请求模型"); break;
    case "context_built": {
      const repoSelection = payload.repo_map?.selected || [];
      const ruleCount = payload.rule_sources?.length || 0;
      const conflictCount = payload.rule_conflicts?.length || 0;
      addActivity("上下文已构建", `规则 ${ruleCount} 条${conflictCount ? ` · 潜在冲突 ${conflictCount} 条` : ""} · Repo Map ${repoSelection.length} 个文件 · ${formatNumber(payload.estimated_chars || 0)} 字符`, conflictCount ? "warning" : "success");
      setRunProgress("上下文准备完成", `已选择 ${repoSelection.length} 个相关文件，正在请求模型`);
      refreshIntelligenceIfVisible();
      break;
    }
    case "task_profile_selected":
      if (elements.taskProfileSelect) elements.taskProfileSelect.value = payload.profile || "project";
      addActivity("任务类型已确定", `${payload.profile || "project"} · ${payload.reason || ""}`, "success");
      break;
    case "mode_changed":
      if (["ask", "plan", "act"].includes(payload.mode)) {
        state.sessionMode = payload.mode;
        if (!state.pendingMode) elements.modeSelect.value = payload.mode;
      }
      break;
    case "context_compacted":
      addActivity("上下文已压缩", `约 ${payload.estimated_chars || 0} 字符`, "warning");
      refreshIntelligenceIfVisible();
      break;
    case "model_started": {
      const outputLimit = Number(payload.max_output_tokens || 0);
      showThinkingIndicator("正在分析你的请求并选择下一步操作…");
      setRunProgress("模型正在思考", `正在分析上下文并选择下一步${outputLimit ? ` · 输出上限 ${formatNumber(outputLimit)} tokens` : ""}`, 0);
      startLiveActivity("模型处理中", `正在选择下一步操作${outputLimit ? ` · 输出上限 ${formatNumber(outputLimit)} tokens` : ""}`);
      break;
    }
    case "model_progress": {
      const elapsed = Number(payload.elapsed_seconds || 0);
      const timeout = Number(payload.request_timeout_seconds || 0);
      showThinkingIndicator(`已思考 ${elapsed.toFixed(0)} 秒${timeout ? ` · 请求上限 ${timeout} 秒` : ""}`);
      setRunProgress("模型仍在思考", `复杂任务可能需要更长时间${timeout ? ` · 单次请求上限 ${timeout} 秒` : ""}`, elapsed);
      updateLiveActivity("模型仍在处理", `已等待 ${elapsed.toFixed(0)} 秒${timeout ? ` · 单次请求上限 ${timeout} 秒` : ""}`, "warning");
      break;
    }
    case "stuck_recovery":
      addActivity("检测到重复编辑，正在恢复", payload.message || "请重新读取文件后选择下一步", "warning");
      break;
    case "duplicate_write_satisfied":
      addActivity("已阻止重复写入", payload.message || "目标内容已经存在，未再次修改文件", "warning");
      break;
    case "stuck_terminal":
      addActivity("已停止重复写入", payload.message || "已保留当前修改，请先验证结果", "warning");
      break;
    case "assistant_delta": setRunProgress("正在生成回复", "模型内容正在流式返回"); appendStreamingAgentText(payload.content || ""); break;
    case "assistant_response": finishAssistantResponse(payload); break;
    case "tool_started": setRunProgress(`正在执行 ${payload.name}`, summarizeArguments(payload.arguments)); addActivity(`执行 ${payload.name}`, summarizeArguments(payload.arguments)); if (payload.name === "run_command") appendTerminal(`❯ ${payload.arguments.command}`, "command"); break;
    case "tool_output_delta": appendTerminal(payload.content || "", payload.stream === "stderr" ? "stderr" : ""); break;
    case "tool_result": {
      const result = payload.result || {};
      addActivity(`${result.ok ? "完成" : "失败"} ${payload.name}`, `${result.code || ""} · ${result.message || ""}`, result.ok ? "success" : "failure");
      setRunProgress(result.ok ? "工具执行完成" : "工具执行失败", result.message || "正在准备下一步操作");
      if (payload.name === "run_command") mirrorCommandResult(result);
      if (payload.name === "analyze_complexity" && result.ok) {
        const complexity = result.data?.complexity || {};
        addActivity(
          "复杂度估计",
          `${complexity.estimated_time_complexity || "未知"} · 循环嵌套 ${complexity.max_loop_nesting ?? 0} 层${complexity.recursive_functions?.length ? " · 检测到递归" : ""}`,
          "success",
        );
      }
      if (result.metadata?.mutated_files?.length) { state.fileCache.clear(); loadRootFiles(); if (state.activeFilePath) openFile(state.activeFilePath, true); }
      refreshIntelligenceIfVisible();
      break;
    }
    case "plan_updated": renderPlan(payload.plan || []); addActivity("计划已更新", payload.reason || "执行步骤发生变化", "success"); break;
    case "approval_requested": setRunProgress("等待你的批准", `${payload.name} · ${payload.reason || "需要授权后继续"}`); showApproval(payload); addActivity(`等待批准 ${payload.name}`, payload.reason, "warning"); break;
    case "verification_required": setRunProgress("正在准备验证", payload.reason || "修改完成后需要运行测试"); addActivity("需要验证", payload.reason, "failure"); break;
    case "repair_attempt": addActivity(`自动修复 ${payload.attempt}/${payload.max_attempts}`, payload.reason, "warning"); break;
    case "checkpoint_created": addActivity("创建检查点", payload.path, "success"); break;
    case "checkpoint_tracking_failed": addActivity("检查点跟踪失败", `${payload.code || ""} · ${payload.message || ""}`, "failure"); break;
    case "checkpoint_restored": addActivity(payload.forced ? "已强制回滚本轮修改" : "已回滚本轮修改", (payload.files || []).join(", "), payload.forced ? "warning" : "success"); break;
    case "verification_recorded": {
      const evidence = payload.evidence || {};
      addActivity(evidence.accepted ? "验证证据已接受" : "验证证据不足", `${String(evidence.kind || "unknown").toUpperCase()} · ${evidence.reason || ""}`, evidence.accepted ? "success" : "warning");
      refreshIntelligenceIfVisible();
      break;
    }
    case "turn_finished": hideThinkingIndicator(); hideRunProgress(); clearRunSyncTimer(); state.runEpoch += 1; state.stopping = false; setRunning(false); schedulePendingModeSync(); addActivity(`任务${statusLabel(payload.status)}`, payload.message, payload.status === "completed" ? "success" : "failure"); loadWorkspaceSessions(); refreshIntelligenceIfVisible(); break;
    default: break;
  }
}

function finishAssistantResponse(payload) {
  hideThinkingIndicator();
  finishLiveActivity();
  setRunProgress(payload.tool_calls?.length ? "正在准备工具" : "正在整理结果", payload.tool_calls?.length ? payload.tool_calls.map((call) => call.name).join(" · ") : "即将完成本轮任务");
  if (streamingAgentMessage) {
    const body = streamingAgentMessage.querySelector(".message-body");
    if (body) {
      const finalContent = payload.content || streamingAgentMessage.dataset.rawContent || "";
      streamingAgentMessage.dataset.rawContent = finalContent;
      renderMessageBody(body, finalContent, "agent");
    }
    streamingAgentMessage.classList.remove("streaming");
    streamingAgentMessage = null;
  } else if (payload.content) addMessage("agent", payload.content);
  if (payload.tool_calls?.length) addActivity("模型选择工具", payload.tool_calls.map((call) => call.name).join(", "));
}

function addMessage(role, content) {
  if (!content) return;
  elements.messageList.querySelector(".chat-empty")?.remove();
  const message = document.createElement("article");
  message.className = `message ${role}`;
  message.innerHTML = `<div class="message-meta"><span>${role === "user" ? "YOU" : "CODE HELPER"}</span><time>${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time></div><div class="message-body"></div>`;
  message.dataset.rawContent = content;
  renderMessageBody(message.querySelector(".message-body"), content, role);
  elements.messageList.append(message);
  if (role === "user") registerUserQuestion(message, content);
  if (!state.restoringHistory) elements.messageList.scrollTop = elements.messageList.scrollHeight;
  return message;
}

function questionPreview(content) {
  const compact = String(content || "").replace(/\s+/g, " ").trim();
  return compact.length > 72 ? `${compact.slice(0, 72)}…` : compact;
}

function resetQuestionNavigator() {
  elements.questionNavigatorList.innerHTML = "";
  elements.questionNavigatorCount.textContent = "0";
  elements.questionNavigator.classList.add("hidden");
  closeQuestionNavigator();
}

function closeQuestionNavigator() {
  elements.questionNavigatorPanel.classList.add("hidden");
  elements.questionNavigatorToggle.setAttribute("aria-expanded", "false");
}

function toggleQuestionNavigator() {
  const willOpen = elements.questionNavigatorPanel.classList.contains("hidden");
  elements.questionNavigatorPanel.classList.toggle("hidden", !willOpen);
  elements.questionNavigatorToggle.setAttribute("aria-expanded", String(willOpen));
}

function registerUserQuestion(message, content) {
  const index = elements.questionNavigatorList.children.length + 1;
  message.id = `user-question-${index}`;
  message.tabIndex = -1;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "question-navigator-item";
  button.setAttribute("aria-label", `定位到第 ${index} 次提问：${questionPreview(content)}`);
  const number = document.createElement("span");
  number.textContent = String(index).padStart(2, "0");
  const copy = document.createElement("span");
  const label = document.createElement("strong");
  label.textContent = `第 ${index} 次提问`;
  const preview = document.createElement("small");
  preview.textContent = questionPreview(content) || "空白提问";
  copy.append(label, preview);
  button.append(number, copy);
  button.addEventListener("click", () => {
    closeQuestionNavigator();
    message.scrollIntoView({ behavior: "smooth", block: "start" });
    message.focus({ preventScroll: true });
    message.classList.remove("message-located");
    requestAnimationFrame(() => message.classList.add("message-located"));
    window.setTimeout(() => message.classList.remove("message-located"), 1500);
  });
  elements.questionNavigatorList.append(button);
  elements.questionNavigatorCount.textContent = String(index);
  elements.questionNavigator.classList.remove("hidden");
}

let streamingAgentMessage = null;
function showThinkingIndicator(detail = "正在分析你的请求并选择下一步操作…") {
  if (!elements.thinkingIndicator) return;
  elements.thinkingStatus.textContent = detail;
  elements.thinkingIndicator.classList.remove("hidden");
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

function hideThinkingIndicator() {
  elements.thinkingIndicator?.classList.add("hidden");
}

function formatRunElapsed(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  if (value < 60) return `${value} 秒`;
  return `${Math.floor(value / 60)}分 ${String(value % 60).padStart(2, "0")}秒`;
}

function updateRunProgressElapsed() {
  if (!elements.runProgressElapsed || state.runStartedAt === null) return;
  const localElapsed = (Date.now() - state.runStartedAt) / 1000;
  const elapsed = Math.max(state.runProgressElapsed || 0, localElapsed);
  elements.runProgressElapsed.textContent = formatRunElapsed(elapsed);
}

function setRunProgress(title, detail, elapsedSeconds = null) {
  if (!elements.runProgress) return;
  if (state.runStartedAt === null) state.runStartedAt = Date.now();
  if (elapsedSeconds !== null) state.runProgressElapsed = Math.max(0, Number(elapsedSeconds) || 0);
  elements.runProgressTitle.textContent = title || "Agent 正在运行";
  elements.runProgressDetail.textContent = detail || "正在准备下一步操作…";
  elements.runProgress.classList.remove("hidden");
  updateRunProgressElapsed();
  if (state.runProgressTimer === null) {
    state.runProgressTimer = window.setInterval(updateRunProgressElapsed, 1000);
  }
}

function hideRunProgress() {
  elements.runProgress?.classList.add("hidden");
  if (state.runProgressTimer !== null) window.clearInterval(state.runProgressTimer);
  state.runProgressTimer = null;
  state.runStartedAt = null;
  state.runProgressElapsed = 0;
  if (elements.runProgressElapsed) elements.runProgressElapsed.textContent = "0 秒";
}

function appendStreamingAgentText(content) {
  if (!content) return;
  hideThinkingIndicator();
  if (!streamingAgentMessage) {
    streamingAgentMessage = addMessage("agent", "");
    if (!streamingAgentMessage) {
      elements.messageList.querySelector(".chat-empty")?.remove();
      streamingAgentMessage = document.createElement("article");
      streamingAgentMessage.className = "message agent streaming";
      streamingAgentMessage.innerHTML = '<div class="message-meta"><span>CODE HELPER</span><time>LIVE</time></div><div class="message-body"></div>';
      elements.messageList.append(streamingAgentMessage);
    }
  }
  streamingAgentMessage.classList.add("streaming");
  const rawContent = `${streamingAgentMessage.dataset.rawContent || ""}${content}`;
  streamingAgentMessage.dataset.rawContent = rawContent;
  streamingAgentMessage.querySelector(".message-body").textContent = rawContent;
  elements.messageList.scrollTop = elements.messageList.scrollHeight;
}

function renderMessageBody(body, content, role) {
  if (!body) return;
  if (role === "agent") {
    body.classList.add("markdown-body");
    body.innerHTML = CodeHelperRendering.renderMarkdown(content);
  } else {
    body.classList.remove("markdown-body");
    body.textContent = content;
  }
}

function addActivity(title, detail, className = "") {
  elements.activityList.querySelector(".view-empty")?.remove();
  const item = document.createElement("div");
  item.className = `activity-item ${className}`;
  item.innerHTML = `<i></i><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail || "")}</span></div>`;
  elements.activityList.append(item);
  if (!state.restoringHistory) elements.activityList.scrollTop = elements.activityList.scrollHeight;
  return item;
}

function startLiveActivity(title, detail) {
  finishLiveActivity();
  const item = addActivity(title, detail);
  item.dataset.liveActivity = "model";
}

function updateLiveActivity(title, detail, className = "") {
  const item = elements.activityList.querySelector('[data-live-activity="model"]');
  if (!item) return startLiveActivity(title, detail);
  item.className = `activity-item ${className}`;
  item.querySelector("strong").textContent = title;
  item.querySelector("span").textContent = detail || "";
  elements.activityList.scrollTop = elements.activityList.scrollHeight;
}

function finishLiveActivity() {
  elements.activityList.querySelector('[data-live-activity="model"]')?.removeAttribute("data-live-activity");
}

function renderPlan(plan) {
  const items = Array.isArray(plan) ? plan : [];
  const completed = items.filter((item) => item.status === "completed").length;
  elements.planProgress.textContent = `${completed} / ${items.length}`;
  elements.planList.innerHTML = items.length ? items.map((item, index) => `<div class="plan-item ${escapeHtml(item.status || "pending")}"><span>${item.status === "completed" ? "✓" : index + 1}</span><p>${escapeHtml(item.step || "")}</p></div>`).join("") : '<div class="view-empty">当前会话还没有执行计划。</div>';
}

function normalizeApprovalPayload(payload = {}) {
  const source = payload && typeof payload === "object" ? payload : {};
  const nestedCall = source.call && typeof source.call === "object" ? source.call : {};
  const request = source.request && typeof source.request === "object" ? source.request : {};
  const call = {
    id: String(source.tool_call_id || source.toolCallId || source.id || nestedCall.id || request.tool_call_id || request.id || "").trim(),
    name: String(source.name || nestedCall.name || request.name || "").trim(),
    arguments: source.arguments ?? nestedCall.arguments ?? request.arguments ?? {},
  };
  return {
    ...source,
    id: call.id,
    tool_call_id: call.id,
    name: call.name || "未知操作",
    arguments: call.arguments,
    call,
    reason: String(source.reason || "此操作需要你的明确批准"),
  };
}

function showApproval(payload) {
  const normalized = normalizeApprovalPayload(payload);
  state.pendingApproval = normalized;
  elements.approvalTitle.textContent = `允许 ${normalized.name}？`;
  elements.approvalReason.textContent = normalized.reason;
  elements.approvalArguments.textContent = JSON.stringify(normalized.arguments, null, 2);
  elements.approvalBackdrop.classList.remove("hidden");
  elements.denyButton.focus();
}

function summarizeArguments(args = {}) { return String(args.path || args.command || args.query || args.pattern || JSON.stringify(args)).slice(0, 180); }
function statusLabel(status) { return ({ completed: "已完成", partial: "部分完成", failed: "失败", cancelled: "已停止" })[status] || status; }
function cancellationReason(reason) { return ({ user_requested: "用户主动停止", task_cancelled: "后台任务被中止", state_cancel_requested: "会话请求停止" })[reason] || reason || "运行已取消"; }
function approvalPolicyLabel(policy) { return ({ ask: "请求批准", auto: "帮我批准", full: "完全放开" })[policy] || policy; }

function mirrorCommandResult(result) {
  const data = result.data || {};
  if (!result.metadata?.output_streamed) {
    if (data.stdout) appendTerminal(data.stdout.replace(/\s+$/, ""));
    if (data.stderr) appendTerminal(data.stderr.replace(/\s+$/, ""), "stderr");
  }
  appendTerminal(`[exit ${Number.isInteger(data.exit_code) ? data.exit_code : "?"}] ${result.message || ""}`, result.ok ? "success" : "failure");
}

function appendTerminal(text, className = "") {
  // Command output remains available in the trace activity; the dedicated
  // terminal surface is intentionally not exposed in the compact workspace.
}

function setRunning(running) {
  state.running = running;
  elements.sendButton.disabled = running || !state.sessionId;
  elements.cancelButton.disabled = !running || state.stopping;
  elements.messageInput.disabled = running || !state.sessionId;
  elements.newSessionButton.disabled = !state.workspace;
  elements.exportTraceButton.disabled = !state.sessionId;
  elements.reasoningSelect.disabled = running;
  elements.runStatus.classList.toggle("running", running);
  if (running && elements.runProgress?.classList.contains("hidden")) {
    setRunProgress("Agent 正在运行", "正在同步当前任务状态…");
  } else if (!running) {
    hideRunProgress();
  }
  updateRunStatus();
}

async function exportTrace() {
  if (!state.sessionId) return;
  try {
    const response = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/trace`, {
      headers: { "Accept": "application/json" },
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `code-helper-trace-${state.sessionId}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast("Trace 已导出，可用 Perfetto 打开");
  } catch (error) {
    showToast(`Trace 导出失败：${error.message}`);
  }
}

function updateRunStatus() {
  elements.runStatus.querySelector("span").textContent = state.stopping
    ? "正在停止 · 可新建对话"
    : state.running
      ? "Agent 运行中"
      : state.sessionId ? "就绪" : "未连接";
}

async function loadIntelligence() {
  if (!state.sessionId) return;
  if (state.activeView === "intelligence") {
    elements.intelligenceContent.innerHTML = '<div class="view-empty">正在分析上下文状态…</div>';
  }
  try {
    const data = await api(`/api/sessions/${state.sessionId}/intelligence`);
    renderIntelligence(data);
  } catch (error) {
    elements.intelligenceContent.innerHTML = `<div class="view-empty error">${escapeHtml(error.message)}</div>`;
  }
}

function refreshIntelligenceIfVisible() {
  if (state.activeView === "intelligence") loadIntelligence();
}

function renderIntelligence(data) {
  const context = data.context || {};
  const contextBuild = context.last_build || {};
  const contextRepo = contextBuild.repo_map || {};
  const contextRules = contextBuild.rule_sources || [];
  const contextConflicts = contextBuild.rule_conflicts || [];
  const contextSummaryMeta = context.summary_meta || {};
  const budget = data.budget || {};
  const verification = data.verification || { evidence: [] };
  const verificationConfig = data.verification_config || { commands: [], rules: [], active_commands: [], diagnostics: [] };
  const repo = data.repo_map || {};
  const totals = repo.totals || {};
  const skills = data.skills || { available: [], loaded: [] };
  const loaded = new Set(skills.loaded || []);
  const toolTotals = data.tool_totals || {};
  const usage = data.token_usage || {};
  const storage = data.storage || { events: {}, tool_results: {} };
  const eventStorage = storage.events || {};
  const resultStorage = storage.tool_results || {};
  const percent = Math.min(100, Math.round(((context.estimated_chars || 0) / Math.max(context.max_chars || 1, 1)) * 100));
  const successRate = toolTotals.calls ? Math.round(((toolTotals.successes || 0) / toolTotals.calls) * 100) : 0;
  const tokenTotal = usage.total_tokens ?? ((usage.prompt_tokens || 0) + (usage.completion_tokens || 0));
  const elapsedSeconds = Number(budget.elapsed_seconds || 0);
  const maxSeconds = Number(budget.max_seconds || 0);
  const timePercent = maxSeconds ? Math.min(100, Math.round((elapsedSeconds / maxSeconds) * 100)) : 0;
  const tokenLimit = Number(budget.token_limit || 0);
  const consumedTokens = Number(budget.consumed_tokens || 0);
  const tokenPercent = tokenLimit ? Math.min(100, Math.round((consumedTokens / tokenLimit) * 100)) : 0;
  const sessionTokenLimit = Number(budget.session_token_limit || 0);
  const sessionConsumedTokens = Number(budget.session_consumed_tokens || 0);
  const sessionTokenPercent = sessionTokenLimit ? Math.min(100, Math.round((sessionConsumedTokens / sessionTokenLimit) * 100)) : 0;
  const configuredOutputLimit = Number(budget.configured_max_output_tokens || 0);
  const effectiveOutputLimit = Number(budget.effective_max_output_tokens || 0);
  const consumedCost = Number(budget.consumed_cost_usd || 0);
  const costLimit = Number(budget.cost_limit_usd || 0);
  const costPercent = costLimit ? Math.min(100, Math.round((consumedCost / costLimit) * 100)) : 0;
  const sessionConsumedCost = Number(budget.session_consumed_cost_usd || 0);
  const sessionCostLimit = Number(budget.session_cost_limit_usd || 0);
  const sessionCostPercent = sessionCostLimit ? Math.min(100, Math.round((sessionConsumedCost / sessionCostLimit) * 100)) : 0;
  const stepLimit = Number(budget.max_steps || 0);
  const currentStep = Number(data.step || 0);
  const runBudgetState = timePercent >= 100 || tokenPercent >= 100 || costPercent >= 100 || sessionCostPercent >= 100 || (stepLimit && currentStep >= stepLimit) ? "warning" : "ready";
  const skillBadges = (skills.available || []).map((skill) => `<span class="skill-badge ${loaded.has(skill.name) ? "loaded" : ""}" title="${escapeHtml(skill.description || "")}"><i></i>${escapeHtml(skill.name)}</span>`).join("");
  const topFiles = (repo.top_files || []).slice(0, 5).map((file) => `<li><span>${escapeHtml(file.path)}</span><b>${file.score}</b></li>`).join("");
  const toolRows = Object.entries(data.tool_stats || {}).sort((a, b) => (b[1].calls || 0) - (a[1].calls || 0)).slice(0, 5).map(([name, stat]) => `<div class="metric-row"><span>${escapeHtml(name)}</span><b>${stat.successes || 0}/${stat.calls || 0}</b><em>${formatDuration(stat.duration_ms || 0)}</em></div>`).join("");
  const hooks = data.hooks || {};
  const outputs = data.outputs || {};
  const observability = data.observability || {};
  const cancellation = observability.cancellation || {};
  const spanLabels = { context_build: "上下文构建", model_request: "模型请求", approval_wait: "审批等待", hook_pipeline: "Hook 管线", hook: "单个 Hook" };
  const spanRows = (observability.spans || []).map((span) => {
    const hookDetail = span.kind === "hook" && span.hook ? ` · ${span.lifecycle || ""}/${span.hook}` : "";
    return `<div class="metric-row"><span>${escapeHtml(spanLabels[span.kind] || span.kind || "未知阶段")}${escapeHtml(hookDetail)}</span><b>${span.count || 0} 次</b><em>均值 ${formatDuration(span.average_duration_ms || 0)} · P95 ${formatDuration(span.p95_duration_ms || 0)} · 总计 ${formatDuration(span.total_duration_ms || 0)}</em></div>`;
  }).join("");
  const cache = data.cache || {};
  const memory = data.memory || { count: 0, categories: {}, recent: [], recalled: [] };
  const summaryMemory = memory.summaries || { count: 0, pending_candidates: 0, candidates: [] };
  const userMemory = data.user_memory || { enabled: false, count: 0, recent: [], recalled: [] };
  const permissions = data.permissions || { grants: [] };
  const approvalPolicy = permissions.approval_policy || state.approvalPolicy || "ask";
  const interrupted = data.interrupted_tool_calls || [];
  const memoryCategories = memory.categories || {};
  const recalledIds = new Set((memory.recalled || []).map((item) => item.memory?.id || item.id));
  const memoryRows = (memory.recent || []).map((item) => `<li class="${recalledIds.has(item.id) ? "recalled" : ""}"><span><b>${escapeHtml(item.category)}</b>${escapeHtml(item.content)}</span><em>${item.importance || 3}</em></li>`).join("");
  const candidateRows = (summaryMemory.candidates || []).map((item) => `<li class="memory-candidate"><span><b>${escapeHtml(item.category)}</b>${escapeHtml(item.content)}</span><div class="memory-actions"><button data-memory-action="confirm" data-candidate-id="${escapeHtml(item.id)}" type="button">保留</button><button data-memory-action="reject" data-candidate-id="${escapeHtml(item.id)}" type="button">忽略</button></div></li>`).join("");
  const userRows = (userMemory.recent || []).map((item) => `<li><span><b>${escapeHtml(item.category)}</b>${escapeHtml(item.content)}</span><em>${item.importance || 3}</em></li>`).join("");
  const evidenceRows = (verification.evidence || []).slice(-5).reverse().map((item) => `<li class="evidence-row ${item.accepted ? "accepted" : "rejected"}"><div><span><b>${escapeHtml(String(item.kind || "unknown").toUpperCase())}</b><em>${escapeHtml(item.source || "untrusted")}</em></span><code>${escapeHtml(item.command || "")}</code><small>${escapeHtml(item.reason || "")}</small></div><i title="${item.accepted ? "满足完成契约" : "不满足完成契约"}">${item.accepted ? "✓" : "!"}</i></li>`).join("");
  const verificationConfigRows = (verificationConfig.commands || []).map((command) => `<li><span>全局</span><code>${escapeHtml(command)}</code></li>`).join("");
  const verificationRuleRows = (verificationConfig.rules || []).map((rule) => {
    const profiles = (rule.task_profiles || []).length ? rule.task_profiles.join(" · ") : "全部任务";
    const paths = (rule.paths || []).length ? rule.paths.join(" · ") : "全部路径";
    const commands = (rule.commands || []).map((command) => `<code>${escapeHtml(command)}</code>`).join("<br>");
    return `<li><span>${escapeHtml(profiles)} · ${escapeHtml(paths)}</span><div>${commands}</div></li>`;
  }).join("");
  const verificationConfigCount = (verificationConfig.commands || []).length + (verificationConfig.rules || []).length;
  const permissionRows = (permissions.grants || []).map((grant) => {
    const scope = grant.path_prefix || grant.command_prefix || "当前工作区";
    const expiry = grant.expires_at ? new Date(grant.expires_at * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "未知";
    return `<li class="permission-row"><div><b>${escapeHtml((grant.capabilities || []).join(" · "))}</b><span title="${escapeHtml(scope)}">${escapeHtml(scope)}</span><small>到期 ${escapeHtml(expiry)}</small></div><button data-permission-action="revoke" data-grant-id="${escapeHtml(grant.grant_id)}" type="button">撤销</button></li>`;
  }).join("");
  const recoveryRows = interrupted.map((item) => {
    const id = escapeHtml(item.id || "");
    const name = escapeHtml(item.name || "unknown");
    const detail = item.arguments?.path || item.arguments?.command || "副作用未确认";
    return `<li class="permission-row recovery-row"><div><b>${name}</b><span title="${escapeHtml(String(detail))}">${escapeHtml(String(detail))}</span><small>工具已启动但结果未持久化</small></div><div class="memory-actions"><button data-recovery-action="abandon" data-tool-call-id="${id}" type="button">放弃</button><button data-recovery-action="retry" data-tool-call-id="${id}" type="button">人工重试</button></div></li>`;
  }).join("");
  const ruleConflictRows = contextConflicts.slice(0, 12).map((item) => `<li class="context-conflict"><span><b>${escapeHtml(item.heading || "同名规则")}</b>${escapeHtml(item.source || "")} ↔ ${escapeHtml(item.other_source || "")}</span><em>目标 ${escapeHtml(item.target || ".")}</em></li>`).join("");
  elements.intelligenceContent.innerHTML = `
    <section class="intelligence-section run-budget-section ${runBudgetState}">
      <div class="intelligence-heading"><div><span class="intel-icon">RUN</span><strong>运行预算</strong></div><b>${runBudgetState === "warning" ? "LIMIT" : "ACTIVE"}</b></div>
      <div class="run-budget-grid">
        <div><span>STEP</span><strong>${currentStep}<em> / ${stepLimit || "∞"}</em></strong></div>
        <div><span>TIME</span><strong>${formatDuration(elapsedSeconds * 1000)}<em> / ${maxSeconds ? formatDuration(maxSeconds * 1000) : "∞"}</em></strong></div>
        <div><span>TOKENS</span><strong>${formatNumber(consumedTokens)}<em> / ${tokenLimit ? formatNumber(tokenLimit) : "∞"}</em></strong></div>
        ${sessionTokenLimit ? `<div><span>SESSION TOKENS</span><strong>${formatNumber(sessionConsumedTokens)}<em> / ${formatNumber(sessionTokenLimit)}</em></strong></div>` : ""}
        ${(configuredOutputLimit || effectiveOutputLimit) ? `<div><span>OUTPUT CAP</span><strong>${formatNumber(effectiveOutputLimit || configuredOutputLimit)}<em>${configuredOutputLimit ? ` / 配置 ${formatNumber(configuredOutputLimit)}` : " · 剩余额度"}</em></strong></div>` : ""}
        ${costLimit ? `<div><span>TURN COST</span><strong>${formatUsd(consumedCost)}<em> / ${formatUsd(costLimit)}${budget.cost_estimated ? " · 估算" : ""}</em></strong></div>` : ""}
        ${sessionCostLimit ? `<div><span>SESSION COST</span><strong>${formatUsd(sessionConsumedCost)}<em> / ${formatUsd(sessionCostLimit)}${budget.session_cost_estimated ? " · 估算" : ""}</em></strong></div>` : ""}
      </div>
      <div class="budget-meter-row"><span>时间</span><div class="budget-track"><i style="width:${timePercent}%"></i></div><b>${timePercent}%</b></div>
      ${tokenLimit ? `<div class="budget-meter-row"><span>Token</span><div class="budget-track token"><i style="width:${tokenPercent}%"></i></div><b>${tokenPercent}%</b></div>` : '<p class="intel-note">Token 预算未设上限；仍会记录供应商返回的用量。</p>'}
      ${sessionTokenLimit ? `<div class="budget-meter-row"><span>会话</span><div class="budget-track session-token"><i style="width:${sessionTokenPercent}%"></i></div><b>${sessionTokenPercent}%</b></div>` : ""}
      ${costLimit ? `<div class="budget-meter-row"><span>本轮费用</span><div class="budget-track token"><i style="width:${costPercent}%"></i></div><b>${costPercent}%</b></div>` : ""}
      ${sessionCostLimit ? `<div class="budget-meter-row"><span>会话费用</span><div class="budget-track session-token"><i style="width:${sessionCostPercent}%"></i></div><b>${sessionCostPercent}%</b></div>` : ""}
    </section>
    <section class="intelligence-section verification-section">
      <div class="intelligence-heading"><div><span class="intel-icon">VER</span><strong>验证证据</strong></div><b class="${verification.fresh ? "status-on" : "status-off"}">${verification.fresh ? "FRESH" : "STALE"}</b></div>
      <ul class="evidence-list">${evidenceRows || '<li class="empty-evidence">尚无验证证据；普通成功命令不会被当作测试。</li>'}</ul>
      ${verificationConfigCount ? `<details><summary>项目验证配置（${verificationConfigCount}）</summary><ul class="context-source-list">${verificationConfigRows}${verificationRuleRows}</ul><p class="intel-note">当前作用域已选择 ${(verificationConfig.active_commands || []).length} 条命令。</p></details>` : ""}
      ${verificationConfig.diagnostics?.length ? `<p class="intel-note">验证配置诊断：${verificationConfig.diagnostics.map(escapeHtml).join("；")}</p>` : ""}
      <p class="intel-note">仅用户明确指定，或可识别的测试、构建、Lint、类型检查命令能满足完成契约。</p>
    </section>
    <section class="intelligence-section context-section">
      <div class="intelligence-heading"><div><span class="intel-icon">CTX</span><strong>上下文预算</strong></div><b>${percent}%</b></div>
      <div class="budget-track"><i style="width:${percent}%"></i></div>
      <div class="intel-facts"><span>${formatNumber(context.estimated_chars || 0)} / ${formatNumber(context.max_chars || 0)} chars</span><span>${formatNumber(contextBuild.estimated_tokens || 0)} tokens · ${escapeHtml(contextBuild.token_estimator || "char_proxy")}</span><span>${context.messages || 0} 条消息 · ${context.compactions || 0} 次压缩</span></div>
      <div class="intel-facts"><span>规则 ${contextRules.length} 条 / ${formatNumber(contextBuild.rule_chars || 0)} chars</span><span>Repo Map ${((contextRepo.selected || []).length)} 文件 / ${formatNumber(contextRepo.selected_chars || 0)} chars</span><span class="${contextConflicts.length ? "context-conflict-count" : ""}">${contextConflicts.length ? `潜在冲突 ${contextConflicts.length} 条` : "规则冲突 0 条"}</span></div>
      ${(contextRules.length || (contextRepo.selected || []).length) ? `<details><summary>查看本 Step 上下文来源</summary><ul class="context-source-list">${contextRules.slice(0, 8).map((item) => `<li><span>规则 · ${escapeHtml(item.path || "")}${item.conflicts?.length ? ` · ⚠ ${item.conflicts.length}` : ""}</span><em>${escapeHtml(item.kind || "default")}${item.truncated ? " · 已截断" : ""}</em></li>`).join("")}${(contextRepo.selected || []).slice(0, 8).map((item) => `<li><span>Repo Map · ${escapeHtml(item.path || "")}</span><em>${item.score || 0} · ${(item.reason || []).map(escapeHtml).join(", ")}</em></li>`).join("")}</ul></details>` : ""}
      ${contextConflicts.length ? `<details open><summary>查看规则冲突</summary><ul class="context-source-list">${ruleConflictRows}</ul><p class="intel-note">冲突表示同一目标规则链中出现同名标题但内容不同；系统仍按目录深度顺序应用，需人工确认优先级。</p></details>` : ""}
      ${contextSummaryMeta.version ? `<p class="intel-note">摘要 v${contextSummaryMeta.version}，覆盖 ${contextSummaryMeta.covered_message_count || 0} 条历史消息，事件序列 ≤ ${contextSummaryMeta.covered_event_sequence || 0}</p>` : ""}
      ${context.summary ? `<details><summary>查看历史摘要</summary><p>${escapeHtml(context.summary)}</p></details>` : '<p class="intel-note">尚未触发历史压缩，最近原始上下文会完整保留。</p>'}
    </section>
    <section class="intelligence-section memory-section">
      <div class="intelligence-heading"><div><span class="intel-icon">MEM</span><strong>跨对话项目记忆</strong></div><b>${memory.count || 0} 条</b></div>
      <div class="intel-facts"><span>${memoryCategories.fact || 0} 事实</span><span>${memoryCategories.decision || 0} 决策</span><span>${memoryCategories.preference || 0} 偏好</span><span>${memoryCategories.task || 0} 待办</span></div>
      <ul class="memory-list">${memoryRows || '<li class="empty"><span>尚未保存项目长期记忆</span></li>'}</ul>
      <p class="intel-note">本轮自动召回 ${(memory.recalled || []).length} 条；高亮项已注入当前上下文。</p>
    </section>
    <section class="intelligence-section candidate-section">
      <div class="intelligence-heading"><div><span class="intel-icon">ASK</span><strong>待确认记忆</strong></div><b>${summaryMemory.pending_candidates || 0} 条</b></div>
      <p class="intel-note">每轮会生成结构化总结，但候选内容只有经你确认后才会长期保存。</p>
      <ul class="memory-list candidate-list">${candidateRows || '<li class="empty"><span>目前没有等待确认的候选</span></li>'}</ul>
    </section>
    <section class="intelligence-section user-memory-section">
      <div class="intelligence-heading"><div><span class="intel-icon">USR</span><strong>跨项目用户记忆</strong></div><b class="${userMemory.enabled ? "status-on" : "status-off"}">${userMemory.enabled ? "已启用" : "已关闭"}</b></div>
      <div class="memory-toolbar"><button data-user-memory-action="toggle" type="button">${userMemory.enabled ? "停用" : "启用"}</button><button data-user-memory-action="export" type="button">导出</button><button data-user-memory-action="clear" type="button" ${userMemory.enabled && userMemory.count ? "" : "disabled"}>清空</button></div>
      <ul class="memory-list">${userRows || '<li class="empty"><span>没有跨项目用户记忆</span></li>'}</ul>
      <p class="intel-note">${userMemory.count || 0} 条，存放于工作区之外；当前会话召回 ${(userMemory.recalled || []).length} 条。</p>
    </section>
    <section class="intelligence-section permission-section">
      <div class="intelligence-heading"><div><span class="intel-icon">PER</span><strong>审批与授权</strong></div><b class="${approvalPolicy === "full" ? "status-off" : "status-on"}">${escapeHtml(approvalPolicyLabel(approvalPolicy))}</b></div>
      <ul class="permission-list">${permissionRows || '<li class="empty"><span>没有长期授权；审批默认只对当前操作生效</span></li>'}</ul>
      <p class="intel-note">${approvalPolicy === "ask" ? "写入与命令会请求批准。" : approvalPolicy === "auto" ? "常规操作自动批准，硬性安全拒绝仍生效。" : "Act 模式工具已完全放开；Ask/Plan 的工具集合不会因此扩大。"} 范围授权 ${(permissions.grants || []).length} 条。</p>
    </section>
    <section class="intelligence-section recovery-section">
      <div class="intelligence-heading"><div><span class="intel-icon">REC</span><strong>中断操作恢复</strong></div><b class="${interrupted.length ? "status-off" : "status-on"}">${interrupted.length} 条</b></div>
      <ul class="permission-list">${recoveryRows || '<li class="empty"><span>没有待处理的中断操作</span></li>'}</ul>
      <p class="intel-note">恢复不会自动重放副作用；“人工重试”会再次执行选中的工具，“放弃”只记录为未执行。</p>
    </section>
    <section class="intelligence-section">
      <div class="intelligence-heading"><div><span class="intel-icon">MAP</span><strong>Repo Map Lite</strong></div><b>${repo.calls || 0} 次调用</b></div>
      <div class="intel-facts"><span>${totals.files_seen || 0} 文件</span><span>${totals.python_files || 0} Python</span><span>${totals.test_files || 0} 测试</span><span>${(totals.build_roots || []).length} 构建入口</span></div>
      ${(totals.build_roots || []).length ? `<details><summary>查看构建入口</summary><ul class="context-source-list">${totals.build_roots.slice(0, 8).map((path) => `<li><span>入口 · ${escapeHtml(path)}</span><em>build root</em></li>`).join("")}</ul></details>` : ""}
      <ol class="repo-rank">${topFiles || '<li><span>暂无可排名文件</span></li>'}</ol>
    </section>
    <section class="intelligence-section">
      <div class="intelligence-heading"><div><span class="intel-icon">SKL</span><strong>Skills 按需加载</strong></div><b>${loaded.size}/${(skills.available || []).length}</b></div>
      <div class="skill-badges">${skillBadges || '<span class="intel-note">没有发现 Skill</span>'}</div>
      <p class="intel-note">实心标记代表当前会话已调用 load_skill。</p>
    </section>
    <section class="intelligence-section compact-section">
      <div class="intelligence-heading"><div><span class="intel-icon">SYS</span><strong>管线状态</strong></div><b>${escapeHtml(data.reasoning_profile || "auto").toUpperCase()}</b></div>
      <div class="pipeline-grid">
        <div><strong>${eventStorage.files || 0}</strong><span>事件文件</span></div>
        <div><strong>${resultStorage.files || 0}</strong><span>输出引用</span></div>
        <div><strong>${cache.file_summaries || 0}</strong><span>摘要缓存</span></div>
        <div><strong>${cache.observed_files || 0}</strong><span>文件观察</span></div>
        <div><strong>${outputs.stored_count || 0}</strong><span>完整输出引用</span></div>
        <div><strong>${(hooks.pre || 0) + (hooks.post || 0) + (hooks.external || 0)}</strong><span>自定义 Hooks</span></div>
      </div>
      <p class="intel-note">Hook 管线${hooks.pipeline_enabled ? "已启用" : "未启用"}：${hooks.pre || 0} Pre / ${hooks.post || 0} Post / ${hooks.verification || 0} Verification / ${hooks.task_end || 0} TaskEnd / ${hooks.external || 0} External。${(hooks.diagnostics || []).length ? `配置诊断：${hooks.diagnostics.map(escapeHtml).join("；")}` : ""}</p>
    </section>
    <section class="intelligence-section">
      <div class="intelligence-heading"><div><span class="intel-icon">TIM</span><strong>阶段耗时</strong></div><b>${observability.active_spans ? `${observability.active_spans} 进行中` : "已同步"}</b></div>
      <div class="metric-list">${spanRows || '<p class="intel-note">本轮尚未记录阶段耗时。</p>'}</div>
      <p class="intel-note">耗时来自事件日志，覆盖上下文构建、模型请求与审批等待；工具耗时见下方本轮统计。</p>
    </section>
    <section class="intelligence-section compact-section">
      <div class="intelligence-heading"><div><span class="intel-icon">CAN</span><strong>取消响应</strong></div><b>${cancellation.completed || 0}/${cancellation.requests || 0}</b></div>
      <div class="statistics-strip"><div><strong>${formatDuration(cancellation.average_ms || 0)}</strong><span>平均</span></div><div><strong>${formatDuration(cancellation.p95_ms || 0)}</strong><span>P95</span></div><div><strong>${(cancellation.samples_ms || []).length}</strong><span>样本</span></div></div>
      <p class="intel-note">统计从取消请求事件到 Agent 发布取消事件的墙钟延迟；未完成请求不会伪造耗时。</p>
    </section>
    <section class="intelligence-section">
      <div class="intelligence-heading"><div><span class="intel-icon">MET</span><strong>本轮统计</strong></div><b>${successRate}% 成功</b></div>
      <div class="statistics-strip"><div><strong>${formatNumber(tokenTotal)}</strong><span>Tokens</span></div><div><strong>${toolTotals.calls || 0}</strong><span>工具调用</span></div><div><strong>${formatDuration(toolTotals.duration_ms || 0)}</strong><span>工具耗时</span></div></div>
      <div class="metric-list">${toolRows || '<p class="intel-note">本轮还没有工具调用。</p>'}</div>
    </section>`;
}

async function handleIntelligenceAction(event) {
  const recoveryButton = event.target.closest("[data-recovery-action]");
  if (recoveryButton) {
    const action = recoveryButton.dataset.recoveryAction;
    const toolCallId = recoveryButton.dataset.toolCallId;
    if (action === "retry" && !window.confirm("该工具的执行结果未知，重试可能重复文件修改或命令副作用。确定继续吗？")) return;
    try {
      await api(`/api/sessions/${state.sessionId}/recovery`, {
        method: "POST",
        body: JSON.stringify({ action, tool_call_id: toolCallId, confirm: action === "retry" }),
      });
      showToast(action === "retry" ? "已提交人工重试" : "已放弃中断操作");
      if (action === "retry") setRunning(true);
      return loadIntelligence();
    } catch (error) { return showToast(error.message); }
  }
  const permissionButton = event.target.closest("[data-permission-action]");
  if (permissionButton) {
    if (permissionButton.dataset.permissionAction !== "revoke") return;
    try {
      await api(`/api/sessions/${state.sessionId}/permissions/${encodeURIComponent(permissionButton.dataset.grantId)}`, { method: "DELETE" });
      showToast("会话授权已撤销");
      return loadIntelligence();
    } catch (error) { return showToast(error.message); }
  }
  const candidateButton = event.target.closest("[data-memory-action]");
  if (candidateButton) {
    try {
      await api(`/api/sessions/${state.sessionId}/memory/candidates/${candidateButton.dataset.candidateId}`, { method: "POST", body: JSON.stringify({ action: candidateButton.dataset.memoryAction }) });
      showToast(candidateButton.dataset.memoryAction === "confirm" ? "记忆已确认并保存" : "候选记忆已忽略");
      return loadIntelligence();
    } catch (error) { return showToast(error.message); }
  }
  const userButton = event.target.closest("[data-user-memory-action]");
  if (!userButton) return;
  const action = userButton.dataset.userMemoryAction;
  try {
    if (action === "toggle") {
      const enabled = !document.querySelector(".user-memory-section .status-on");
      await api(`/api/sessions/${state.sessionId}/user-memory/enabled`, { method: "POST", body: JSON.stringify({ enabled }) });
      showToast(enabled ? "跨项目用户记忆已启用" : "跨项目用户记忆已停用");
    } else if (action === "export") {
      const data = await api(`/api/sessions/${state.sessionId}/user-memory/export`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const link = Object.assign(document.createElement("a"), { href: url, download: "code-helper-user-memory.json" });
      link.click(); URL.revokeObjectURL(url); showToast("用户记忆已导出");
    } else if (action === "clear" && window.confirm("确定清空全部跨项目用户记忆吗？此操作不可撤销。")) {
      const result = await api(`/api/sessions/${state.sessionId}/user-memory`, { method: "DELETE" });
      showToast(`已清空 ${result.cleared || 0} 条用户记忆`);
    }
    await loadIntelligence();
  } catch (error) { showToast(error.message); }
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN", { notation: value >= 10000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value || 0);
}

function formatUsd(value) {
  const amount = Number(value || 0);
  return `$${amount.toFixed(amount > 0 && amount < 0.01 ? 6 : 4)}`;
}

function formatDuration(milliseconds) {
  if (milliseconds < 1000) return `${milliseconds}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function setAssistantView(view) {
  const visibleView = RESTORABLE_VIEWS.has(view) ? view : "chat";
  state.activeView = visibleView;
  document.querySelectorAll(".assistant-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.view === visibleView));
  const ids = { chat: "chatView", trace: "traceView", plan: "planView", intelligence: "intelligenceView" };
  document.querySelectorAll(".assistant-view").forEach((pane) => pane.classList.toggle("active", pane.id === ids[visibleView]));
  if (visibleView === "intelligence") loadIntelligence();
  syncFocusLayoutState();
  saveWorkspaceState();
}

async function browseWorkspace() {
  state.browserPurpose = "workspace";
  document.querySelector("#folderDialogTitle").textContent = "打开文件夹";
  elements.chooseWorkspaceButton.textContent = "选择并打开";
  try {
    const picker = window.pywebview?.api?.pick_folder;
    if (picker) {
      const selected = await picker();
      if (selected) await openWorkspace(selected);
      return;
    }
  } catch (error) { showToast(`原生选择器不可用：${error.message}`); }
  elements.browserBackdrop.classList.remove("hidden");
  await browseTo(state.workspace || "");
}

async function browseDefaultWorkspace() {
  state.browserPurpose = "settings";
  document.querySelector("#folderDialogTitle").textContent = "选择默认文件夹";
  elements.chooseWorkspaceButton.textContent = "设为默认";
  try {
    const picker = window.pywebview?.api?.pick_folder;
    if (picker) {
      const selected = await picker();
      if (selected) elements.settingsDefaultWorkspace.value = selected;
      return;
    }
  } catch (error) { showToast(`原生选择器不可用：${error.message}`); }
  elements.browserBackdrop.classList.remove("hidden");
  await browseTo(elements.settingsDefaultWorkspace.value || state.workspace || "");
}

async function browseTo(path) {
  elements.browserList.innerHTML = '<div class="folder-loading">正在读取目录…</div>';
  try {
    const result = await api(`/api/fs/browse?${new URLSearchParams({ path: path || "" })}`);
    state.browserPath = result.path;
    state.browserParent = result.parent;
    state.browserSelection = result.path || null;
    elements.browserPath.textContent = result.path || "此电脑";
    elements.browserPath.title = result.path || "此电脑";
    elements.browserUpButton.disabled = !result.parent && !result.path;
    elements.chooseWorkspaceButton.disabled = !state.browserSelection;
    renderBrowserList(result.entries || []);
  } catch (error) {
    elements.browserList.innerHTML = `<div class="folder-loading error">${escapeHtml(error.message)}</div>`;
  }
}

function renderBrowserList(entries) {
  elements.browserList.innerHTML = "";
  if (!entries.length) elements.browserList.innerHTML = '<div class="folder-loading">此文件夹中没有子文件夹</div>';
  entries.forEach((entry) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "folder-row";
    row.innerHTML = `${iconSvg(entry.kind === "drive" ? "drive" : "folder")}<span><strong>${escapeHtml(entry.name)}</strong><small>${escapeHtml(entry.path)}</small></span><b>›</b>`;
    row.addEventListener("click", () => {
      elements.browserList.querySelector(".folder-row.selected")?.classList.remove("selected");
      row.classList.add("selected");
      state.browserSelection = entry.path;
      elements.chooseWorkspaceButton.disabled = false;
    });
    row.addEventListener("dblclick", () => browseTo(entry.path));
    elements.browserList.append(row);
  });
}

function closeBrowser() {
  elements.browserBackdrop.classList.add("hidden");
  state.browserSelection = null;
}

function iconSvg(kind) {
  const paths = {
    chevron: '<path d="m9 6 6 6-6 6"/>',
    folder: '<path d="M3.5 6h6l2 2h9v10.5h-17z"/>',
    file: '<path d="M6 3.5h8l4 4v13H6zM14 3.5v4h4"/>',
    drive: '<rect x="4" y="5" width="16" height="14" rx="2"/><path d="M7 15h.1M11 15h6"/>',
    blank: "",
  };
  return `<svg class="tree-icon ${kind}" viewBox="0 0 24 24" aria-hidden="true">${paths[kind] || ""}</svg>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

async function copyTextToClipboard(value, successMessage = "文字已复制") {
  const text = String(value ?? "");
  const nativeCopy = window.pywebview?.api?.copy_text;
  if (typeof nativeCopy === "function") {
    try {
      if (await nativeCopy(text)) {
        showToast(successMessage);
        return true;
      }
    } catch { /* fall through to browser clipboard paths */ }
  }
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      showToast(successMessage);
      return true;
    } catch { /* older WebViews require the selection-based fallback */ }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.cssText = "position:fixed;inset:-9999px auto auto -9999px;opacity:0";
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try { copied = document.execCommand("copy"); } catch { copied = false; }
  textarea.remove();
  showToast(copied ? successMessage : "当前环境无法访问剪贴板");
  return copied;
}

let toastTimer;
function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.add("hidden"), 3200);
}

elements.createSessionButton.addEventListener("click", () => openWorkspace(elements.workspaceInput.value));
elements.workspaceInput.addEventListener("keydown", (event) => { if (event.key === "Enter") openWorkspace(elements.workspaceInput.value); });
elements.browseWorkspaceButton.addEventListener("click", browseWorkspace);
document.querySelector("#emptyBrowseButton").addEventListener("click", browseWorkspace);
elements.refreshFilesButton.addEventListener("click", loadRootFiles);
elements.insertFileButton.addEventListener("click", insertSelectedFile);
elements.reloadFileButton.addEventListener("click", () => state.activeFilePath && openFile(state.activeFilePath, true));
elements.copyFileButton.addEventListener("click", () => copyTextToClipboard(state.fileCache.get(state.activeFilePath)?.content || "", "文件内容已复制"));
elements.newSessionButton.addEventListener("click", () => openWorkspace(state.workspace, null, true));
elements.focusNewSessionButton.addEventListener("click", () => openWorkspace(state.workspace, null, true));
elements.focusFilesButton.addEventListener("click", () => toggleFocusExplorer());
elements.closeFilePanelButton.addEventListener("click", () => setFilePanelVisible(false));
elements.sendButton.addEventListener("click", sendMessage);
elements.cancelButton.addEventListener("click", cancelRun);
elements.messageInput.addEventListener("keydown", (event) => { if (event.key === "Enter" && event.ctrlKey) { event.preventDefault(); sendMessage(); } });
elements.modeSelect.addEventListener("change", async () => {
  const requested = elements.modeSelect.value;
  if (!state.sessionId) {
    state.sessionMode = requested;
    saveWorkspaceState();
    return;
  }
  if (state.running) {
    state.pendingMode = requested;
    showToast(`本轮结束后将切换为 ${requested.toUpperCase()} 模式`);
    return;
  }
  const promise = synchronizeMode(requested);
  state.modeSyncPromise = promise;
  await promise;
  if (state.modeSyncPromise === promise) state.modeSyncPromise = null;
  if (state.sessionMode === requested) showToast(`已切换为 ${requested.toUpperCase()} 模式`);
});
elements.reasoningSelect.addEventListener("change", async () => {
  if (!state.sessionId) return;
  if (state.running) {
    showToast("Agent 运行时不能切换推理档位");
    return;
  }
  try {
    const result = await api(`/api/sessions/${state.sessionId}/reasoning`, {
      method: "POST",
      body: JSON.stringify({ profile: elements.reasoningSelect.value }),
    });
    elements.reasoningSelect.value = result.profile;
    saveWorkspaceState();
    showToast(`推理档位已切换为 ${result.profile.toUpperCase()}`);
    loadIntelligence();
  } catch (error) { showToast(error.message); }
});
elements.approvalPolicySelect.addEventListener("change", changeApprovalPolicy);
elements.taskProfileSelect.addEventListener("change", saveWorkspaceState);
document.querySelectorAll(".assistant-tabs button").forEach((button) => button.addEventListener("click", () => setAssistantView(button.dataset.view)));
elements.refreshIntelligenceButton.addEventListener("click", loadIntelligence);
elements.exportTraceButton.addEventListener("click", exportTrace);
elements.intelligenceContent.addEventListener("click", handleIntelligenceAction);
elements.confirmRestoreButton.addEventListener("click", confirmRestoreSelection);
elements.closeRestoreButton.addEventListener("click", () => elements.restoreBackdrop.classList.add("hidden"));
elements.cancelRestoreButton.addEventListener("click", () => elements.restoreBackdrop.classList.add("hidden"));
elements.restoreButton.addEventListener("click", restoreCheckpoint);
elements.approveButton.addEventListener("click", () => resolveApproval(true));
elements.grantButton.addEventListener("click", () => resolveApproval(true, "session"));
elements.denyButton.addEventListener("click", () => resolveApproval(false));
elements.closeBrowserButton.addEventListener("click", closeBrowser);
elements.cancelBrowserButton.addEventListener("click", closeBrowser);
elements.browserBackdrop.addEventListener("click", (event) => { if (event.target === elements.browserBackdrop) closeBrowser(); });
elements.browserUpButton.addEventListener("click", () => browseTo(state.browserParent || ""));
elements.messageInput.addEventListener("input", resizeMessageInput);
elements.questionNavigatorToggle.addEventListener("click", toggleQuestionNavigator);
elements.closeQuestionNavigator.addEventListener("click", closeQuestionNavigator);
elements.chooseWorkspaceButton.addEventListener("click", async () => {
  const selected = state.browserSelection;
  if (!selected) return;
  const purpose = state.browserPurpose;
  closeBrowser();
  if (purpose === "settings") {
    elements.settingsDefaultWorkspace.value = selected;
    return;
  }
  elements.workspaceInput.value = selected;
  await openWorkspace(selected);
});
elements.userMenuButton.addEventListener("click", toggleUserMenu);
elements.focusUserMenuButton.addEventListener("click", toggleUserMenu);
elements.openSettingsButton.addEventListener("click", openSettingsPage);
elements.settingsBackButton.addEventListener("click", closeSettingsPage);
elements.settingsForm.addEventListener("submit", saveSettings);
elements.toggleApiKeyButton.addEventListener("click", () => {
  const visible = elements.settingsApiKey.type === "text";
  elements.settingsApiKey.type = visible ? "password" : "text";
  elements.toggleApiKeyButton.textContent = visible ? "显示" : "隐藏";
});
elements.clearApiKeyButton.addEventListener("click", () => {
  state.clearApiKey = !state.clearApiKey;
  elements.settingsApiKey.value = "";
  elements.settingsApiKey.disabled = state.clearApiKey;
  elements.clearApiKeyButton.textContent = state.clearApiKey ? "撤销" : "清除";
  elements.settingsApiStatus.className = state.clearApiKey ? "cleared" : (state.appSettings?.api_key_configured ? "configured" : "cleared");
  elements.settingsApiStatus.textContent = state.clearApiKey
    ? "保存后移除已配置的 API Key"
    : (state.appSettings?.api_key_configured ? `已配置 · ${state.appSettings.api_key_hint}` : "尚未配置 API Key");
});
elements.settingsApiKey.addEventListener("input", () => {
  if (elements.settingsApiKey.value) {
    state.clearApiKey = false;
    elements.settingsApiKey.disabled = false;
    elements.clearApiKeyButton.textContent = "清除";
    elements.settingsApiStatus.className = "configured";
    elements.settingsApiStatus.textContent = "将使用新输入的 API Key";
  }
});
elements.browseDefaultWorkspaceButton.addEventListener("click", browseDefaultWorkspace);
elements.useCurrentWorkspaceButton.addEventListener("click", () => {
  if (!state.workspace) return showToast("请先打开一个工作区");
  elements.settingsDefaultWorkspace.value = state.workspace;
});
elements.clearDefaultWorkspaceButton.addEventListener("click", () => { elements.settingsDefaultWorkspace.value = ""; });
elements.enableAllSkillsButton.addEventListener("click", () => elements.settingsSkillsList.querySelectorAll('input[name="enabled-skill"]').forEach((input) => { input.checked = true; }));
elements.disableAllSkillsButton.addEventListener("click", () => elements.settingsSkillsList.querySelectorAll('input[name="enabled-skill"]').forEach((input) => { input.checked = false; }));
document.querySelectorAll("[data-settings-section]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-settings-section]").forEach((item) => item.classList.toggle("active", item === button));
  document.querySelector(`#${button.dataset.settingsSection}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}));
document.addEventListener("click", (event) => {
  if (!elements.userMenu.classList.contains("hidden") && !elements.userMenu.contains(event.target) && !elements.userMenuButton.contains(event.target) && !elements.focusUserMenuButton.contains(event.target)) closeUserMenu();
  const explorerPane = document.querySelector(".explorer-pane");
  if (elements.workbench.classList.contains("focus-explorer-open") && !explorerPane.contains(event.target) && !elements.focusFilesButton.contains(event.target)) toggleFocusExplorer(false);
  if (!elements.questionNavigatorPanel.classList.contains("hidden") && !elements.questionNavigator.contains(event.target)) closeQuestionNavigator();
});
window.addEventListener("resize", () => { if (!elements.userMenu.classList.contains("hidden")) positionUserMenu(); });
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    if (!elements.browserBackdrop.classList.contains("hidden")) closeBrowser();
    else if (!elements.userMenu.classList.contains("hidden")) closeUserMenu();
    else if (!elements.questionNavigatorPanel.classList.contains("hidden")) closeQuestionNavigator();
    else if (elements.workbench.classList.contains("focus-explorer-open")) toggleFocusExplorer(false);
  }
  if (event.altKey && event.key.toLowerCase() === "f" && state.layoutMode === "focus" && !state.settingsOpen) {
    event.preventDefault();
    toggleFocusExplorer();
  }
  if (event.ctrlKey && event.key === ",") {
    event.preventDefault();
    openSettingsPage();
  }
  if (event.ctrlKey && event.key.toLowerCase() === "s" && state.settingsOpen) {
    event.preventDefault();
    saveSettings(event);
  }
});

async function initializeApplication() {
  initializePanelResizers();
  resetEditor();
  resizeMessageInput();
  setAssistantView("chat");
  await checkHealth();
  const settings = await loadAppSettings();
  const restored = await restoreLastWorkspace();
  if (!restored && settings?.default_workspace) {
    elements.workspaceInput.value = settings.default_workspace;
    await openWorkspace(settings.default_workspace, null, false, true);
    showToast("已打开默认工作区");
  }
}

initializeApplication();
