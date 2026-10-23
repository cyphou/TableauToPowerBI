/* Local browser client. No token or folder path is persisted in browser storage. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {
    migration: "Migration", probe: "Probe Power BI",
    compare: "Comparaison Tableau/Power BI"
  };
  const statusLabels = {
    not_run: "Non exécuté", queued: "En attente", pending: "En attente",
    running: "En cours", succeeded: "Terminé", failed: "Échec",
    cancelled: "Annulé"
  };
  const activeStatuses = new Set(["queued", "pending", "running"]);
  const terminalStatuses = new Set(["not_run", "succeeded", "failed", "cancelled"]);
  const state = {
    token: null, connected: false, jobs: [], selectedId: null,
    submitting: false, uncertain: false, refreshPromise: null, timer: null,
    requestBusy: false, pendingNative: null, lastAnnouncement: ""
  };
  const form = $("job-form");
  const dialog = $("native-dialog");

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.status = status;
    }
  }

  function showMessage(message, error = false) {
    const node = $("action-message");
    node.textContent = message;
    node.classList.toggle("notice-error", error);
    node.hidden = !message;
  }

  function connection(ok, message = "") {
    state.connected = ok;
    const badge = $("connection-status");
    badge.textContent = ok ? "Serveur local connecté" : "Serveur local indisponible";
    badge.dataset.state = ok ? "connected" : "disconnected";
    $("network-error").textContent = message;
    $("network-error").hidden = !message;
    updateControls();
  }

  // All calls are same-origin. Polling and submission explicitly await each other.
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    state.requestBusy = true;
    try {
      const response = await fetch(path, {
        ...options, credentials: "same-origin", cache: "no-store",
        signal: controller.signal,
        headers: { Accept: "application/json", ...(options.headers || {}) }
      });
      const text = await response.text();
      let data;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        throw new ApiError(
          `Réponse non JSON du serveur (HTTP ${response.status}). Vérifiez que cette page est ouverte depuis le serveur local.`,
          response.status
        );
      }
      if (!response.ok) {
        const detail = data && typeof data.error === "string" ? data.error :
          data && typeof data.message === "string" ? data.message : response.statusText;
        throw new ApiError(`HTTP ${response.status} — ${detail || "Requête refusée."}`, response.status);
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error.name === "AbortError") {
        throw new ApiError("Le serveur n’a pas répondu dans les 20 secondes.");
      }
      throw new ApiError(`Connexion impossible au serveur local : ${error.message || "erreur réseau"}.`);
    } finally {
      clearTimeout(timeout);
      state.requestBusy = false;
    }
  }

  function normalizeStatus(value) {
    const raw = String(value || "unknown").toLowerCase();
    const aliases = {
      success: "succeeded", completed: "succeeded", done: "succeeded",
      error: "failed", skipped: "not_run", not_started: "not_run",
      canceled: "cancelled"
    };
    return aliases[raw] || raw;
  }

  function statusText(status) {
    return statusLabels[status] || `État inconnu (${status})`;
  }

  function normalizeJob(job) {
    if (!job || typeof job !== "object" || job.id == null) {
      throw new ApiError("Réponse de tâche invalide : identifiant manquant.");
    }
    return {
      ...job, id: String(job.id), status: normalizeStatus(job.status),
      logs: Array.isArray(job.logs) ? job.logs.filter((line) => typeof line === "string") : [],
      artifacts: Array.isArray(job.artifacts) ? job.artifacts : []
    };
  }

  function jobTime(job) {
    if (!job.created_at) return 0;
    const timestamp = Date.parse(job.created_at);
    return Number.isNaN(timestamp) ? 0 : timestamp;
  }

  function sortedJobs(jobs) {
    // Prefer timestamps, otherwise preserve the server's list order.
    return jobs.slice().sort((a, b) => jobTime(b) - jobTime(a));
  }

  function isActive(job) {
    // An unfamiliar server status must not accidentally enable another launch.
    return activeStatuses.has(job.status) || !terminalStatuses.has(job.status);
  }

  function updateControls() {
    const active = state.jobs.some(isActive);
    const disabled = !state.connected || active || state.submitting || state.uncertain;
    document.querySelectorAll(".job-action").forEach((button) => {
      button.disabled = disabled;
    });
    $("reconnect").disabled = state.submitting || Boolean(state.refreshPromise);
    $("native-confirm").disabled = disabled;
    $("availability-hint").textContent = state.submitting ? "Envoi de la demande…" :
      state.uncertain ? "Envoi incertain : vérifiez les tâches avant de réessayer." :
      !state.connected ? "Connexion au serveur local requise." :
      active ? "Une tâche est active. Attendez sa fin avant un nouveau lancement." :
      "Prêt · Les diagnostics restent facultatifs.";
    form.setAttribute("aria-busy", state.submitting ? "true" : "false");
  }

  function schedulePoll() {
    clearTimeout(state.timer);
    if (document.hidden) return;
    state.timer = setTimeout(() => refresh(), 2000);
  }

  async function refresh() {
    if (state.refreshPromise) return state.refreshPromise;
    if (state.submitting || document.hidden) return;
    state.refreshPromise = (async () => {
      try {
        if (!state.token) {
          const session = await api("/api/session");
          if (!session || typeof session.token !== "string" || !session.token) {
            throw new ApiError("Session locale invalide : le serveur n’a pas fourni de jeton.");
          }
          state.token = session.token;
        }
        const data = await api("/api/jobs");
        const jobs = Array.isArray(data) ? data : data && data.jobs;
        if (!Array.isArray(jobs)) {
          throw new ApiError("Liste des tâches invalide : tableau ou objet { jobs: [...] } attendu.");
        }
        state.jobs = sortedJobs(jobs.map(normalizeJob));
        if (!state.selectedId || !state.jobs.some((job) => job.id === state.selectedId)) {
          state.selectedId = state.jobs.length ? state.jobs[0].id : null;
        }
        connection(true);
        render();
        $("polling-status").textContent = "Actualisation toutes les 2 s";
      } catch (error) {
        if (error.status === 401 || error.status === 403) state.token = null;
        connection(false, `${error.message} Les données affichées peuvent être anciennes. Nouvelle tentative automatique toutes les 2 s.`);
        $("polling-status").textContent = "Échec d’actualisation · données conservées";
      }
    })();
    updateControls();
    try {
      await state.refreshPromise;
    } finally {
      state.refreshPromise = null;
      updateControls();
      schedulePoll();
    }
  }

  function setBadge(node, status) {
    node.textContent = statusText(status);
    node.dataset.state = status;
  }

  function formatDate(value) {
    if (!value) return "Date non fournie";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) :
      date.toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
  }

  function safeArtifactUrl(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value, window.location.href);
      // Never navigate to javascript:, data:, external hosts or credentialed URLs.
      if (!["http:", "https:"].includes(url.protocol) ||
          url.origin !== window.location.origin || url.username || url.password) return null;
      // A report must not be able to reach this page through window.opener.
      return url.href;
    } catch {
      return null;
    }
  }

  function renderArtifacts(job) {
    const list = $("artifact-list");
    list.replaceChildren();
    let count = 0;
    for (const artifact of job ? job.artifacts : []) {
      if (!artifact || typeof artifact !== "object") continue;
      const item = document.createElement("li");
      const name = typeof artifact.name === "string" ? artifact.name : "Fichier de résultat";
      const url = safeArtifactUrl(artifact.url);
      if (url) {
        const link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = name;
        link.setAttribute("aria-label", `${name} (nouvel onglet)`);
        item.append(link);
        count++;
      } else {
        const note = document.createElement("span");
        note.className = "hint";
        note.textContent = `${name} — lien absent ou bloqué (seuls les liens du serveur local sont autorisés).`;
        item.append(note);
      }
      list.append(item);
    }
    $("artifact-count").textContent = String(count);
    $("artifacts-empty").hidden = list.childElementCount > 0;
    const download = $("download-zip");
    // A failed/not_run task must not appear to have generated a downloadable project.
    download.hidden = !job || job.status !== "succeeded" || job.action !== "migration";
    if (!download.hidden) {
      download.href = `/api/download/${encodeURIComponent(job.id)}`;
    } else {
      download.removeAttribute("href");
    }
  }

  function renderHistory() {
    const list = $("job-history");
    const focusedId = list.contains(document.activeElement) ?
      document.activeElement.dataset.jobId : null;
    list.replaceChildren();
    $("history-empty").hidden = state.jobs.length > 0;
    const recent = state.jobs.slice(0, 6);
    const selected = state.jobs.find((job) => job.id === state.selectedId);
    if (selected && !recent.includes(selected)) recent.push(selected);
    for (const job of recent) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "history-button";
      button.dataset.jobId = job.id;
      button.setAttribute("aria-pressed", job.id === state.selectedId ? "true" : "false");
      const label = document.createElement("span");
      label.className = "history-label";
      const title = document.createElement("span");
      title.textContent = labels[job.action] || String(job.action || "Tâche");
      const date = document.createElement("small");
      date.textContent = `${formatDate(job.created_at)} · #${job.id}`;
      label.append(title, date);
      const badge = document.createElement("span");
      badge.className = "badge";
      setBadge(badge, job.status);
      button.append(label, badge);
      button.addEventListener("click", () => {
        state.selectedId = job.id;
        render();
      });
      item.append(button);
      list.append(item);
      if (focusedId === job.id) button.focus({ preventScroll: true });
    }
  }

  function render() {
    const job = state.jobs.find((item) => item.id === state.selectedId);
    $("current-action").textContent = job ? labels[job.action] || String(job.action || "Tâche") : "Aucune tâche";
    setBadge($("current-status"), job ? job.status : "not_run");
    const active = state.jobs.find(isActive);
    let description = job ? `Tâche #${job.id} · ${formatDate(job.created_at)}.` :
      "Lancez une migration pour afficher sa progression et ses résultats.";
    if (job && job.status === "not_run") description += " Cette action n’a pas été exécutée ; ce n’est pas un succès.";
    if (job && job.status === "running") description += " Traitement en cours ; aucun pourcentage n’est fourni par le serveur.";
    if (job && job.status === "succeeded") description += " Le serveur indique que la tâche est terminée. Consultez les rapports : cet état seul ne prouve ni l’actualisation des données ni la parité des résultats.";
    if (active && (!job || active.id !== job.id)) description += ` Une autre tâche est active : #${active.id} (${statusText(active.status)}).`;
    $("current-description").textContent = description;
    const error = job && job.error ? (typeof job.error === "string" ? job.error : JSON.stringify(job.error)) : "";
    $("job-error").textContent = error;
    $("job-error").hidden = !error;
    for (const action of ["probe", "compare"]) {
      const last = state.jobs.find((item) => item.action === action);
      setBadge($(`${action}-status`), last ? last.status : "not_run");
    }
    renderArtifacts(job);
    renderHistory();
    const logs = job ? job.logs.slice(-500) : [];
    const logNode = $("job-logs");
    const nearBottom = logNode.scrollHeight - logNode.scrollTop - logNode.clientHeight < 50;
    const text = logs.length ? logs.join("\n") : "Aucun journal disponible.";
    if (logNode.textContent !== text) {
      logNode.textContent = text;
      if (nearBottom) logNode.scrollTop = logNode.scrollHeight;
    }
    $("log-count").textContent = `· ${logs.length} ligne${logs.length > 1 ? "s" : ""}`;
    const announcement = job ? `${labels[job.action] || "Tâche"} #${job.id} : ${statusText(job.status)}.` : "Aucune tâche.";
    if (announcement !== state.lastAnnouncement) {
      $("job-announcement").textContent = announcement;
      state.lastAnnouncement = announcement;
    }
    updateControls();
  }

  function fieldError(inputId, errorId, message) {
    const input = $(inputId);
    input.setAttribute("aria-invalid", message ? "true" : "false");
    $(errorId).textContent = message;
    $(errorId).hidden = !message;
    return message ? input : null;
  }

  function pathError(path, source) {
    if (!path) return "Saisissez le chemin absolu du dossier.";
    if (/[\x00-\x1f]/.test(path)) return "Le chemin contient un caractère de contrôle non autorisé.";
    if (path.startsWith('"') || path.endsWith('"') || path.startsWith("'") || path.endsWith("'")) {
      return "Saisissez le chemin sans guillemets.";
    }
    const absolute = /^[A-Za-z]:[\\/]/.test(path) || /^\\\\[^\\]+\\[^\\]+/.test(path) || path.startsWith("/");
    if (!absolute) return "Utilisez un chemin absolu, par exemple C:\\Tableau\\Sources ou /home/utilisateur/tableau.";
    if (source && /\.(twb|twbx)$/i.test(path.replace(/[\\/]+$/, ""))) {
      return "Sélectionnez le dossier contenant le classeur, pas le fichier .twb ou .twbx.";
    }
    return "";
  }

  function readPayload(action) {
    const source = $("source-dir").value.trim();
    const output = $("output-dir").value.trim();
    const firstSource = fieldError("source-dir", "source-error", pathError(source, true));
    const firstOutput = fieldError("output-dir", "output-error", pathError(output, false));
    const modelMode = $("model-mode").value;
    const firstModelMode = fieldError("model-mode", "model-mode-error",
      ["auto", "import"].includes(modelMode) ? "" : "Choisissez « Automatique » ou « Import ».");
    const start = $("calendar-start").valueAsNumber;
    const end = $("calendar-end").valueAsNumber;
    const calendarInvalid = !Number.isInteger(start) || !Number.isInteger(end) ||
      start < 1 || end > 9999 || start > end;
    $("calendar-error").textContent = calendarInvalid ? "Indiquez deux années entières entre 1 et 9999, dans l’ordre croissant." : "";
    $("calendar-error").hidden = !calendarInvalid;
    for (const id of ["calendar-start", "calendar-end"]) {
      $(id).setAttribute("aria-invalid", calendarInvalid ? "true" : "false");
    }
    const invalid = firstSource || firstOutput || firstModelMode || (calendarInvalid ? $("calendar-start") : null);
    if (invalid) {
      if (calendarInvalid) $("calendar-start").closest("details").open = true;
      invalid.focus();
      showMessage("Vérifiez les champs signalés avant de lancer une tâche.", true);
      return null;
    }
    return {
      action, source_dir: source, output_dir: output,
      opts: {
        model_mode: modelMode,
        output_format: $("output-format").value, culture: $("culture").value,
        calendar_start: start, calendar_end: end,
        quality_policy: $("quality-policy").value, resume: $("resume").checked,
        all_pages: $("all-pages").checked,
        screen_comparison: $("screen-comparison").value
      },
      ...(action !== "migration" ? {
        confirm_native: true
      } : {})
    };
  }

  function canLaunch() {
    if (!state.connected || state.submitting || state.uncertain || state.jobs.some(isActive)) {
      showMessage("Lancement indisponible : vérifiez la connexion et attendez la fin de la tâche active.", true);
      return false;
    }
    return true;
  }

  async function submitJob(payload) {
    if (!canLaunch()) return;
    state.submitting = true;
    clearTimeout(state.timer);
    updateControls();
    showMessage(`Envoi de la demande : ${labels[payload.action]}…`);
    try {
      // Let any already-started poll finish before posting; never overlap requests.
      if (state.refreshPromise) await state.refreshPromise;
      if (!state.connected || state.jobs.some(isActive) || !state.token) {
        showMessage("Le serveur est indisponible ou une autre tâche a démarré. Aucun nouvel envoi effectué.", true);
        return;
      }
      const data = await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Local-Token": state.token },
        body: JSON.stringify(payload)
      });
      // Both a direct job and { job: ... } are accepted.
      const job = normalizeJob(data && data.job ? data.job : data);
      state.jobs = sortedJobs([job, ...state.jobs.filter((item) => item.id !== job.id)]);
      state.selectedId = job.id;
      connection(true);
      showMessage(`${labels[payload.action]} : demande acceptée. Suivez son état dans les résultats.`);
      render();
      $("results").scrollIntoView({ behavior: "auto", block: "start" });
      $("results-title").setAttribute("tabindex", "-1");
      $("results-title").focus({ preventScroll: true });
    } catch (error) {
      if (error.status === 401 || error.status === 403) state.token = null;
      // A timeout or malformed successful response may still mean the task was created.
      // Never retry a POST automatically: require a list refresh and human acknowledgement.
      const uncertain = !error.status || (error.status >= 200 && error.status < 300) || error.status >= 500;
      if (uncertain) {
        state.uncertain = true;
        connection(false, `${error.message} L’envoi a peut-être été accepté. Consultez les tâches puis utilisez « Réessayer la connexion » pour autoriser un nouveau lancement ; aucune demande ne sera renvoyée automatiquement.`);
      } else if (error.status === 401 || error.status === 403) {
        connection(false, `${error.message} La session locale doit être renouvelée.`);
      }
      showMessage(`${labels[payload.action]} : ${error.message}${uncertain ? " Vérifiez les tâches pour éviter un doublon." : ""}`, true);
    } finally {
      state.submitting = false;
      updateControls();
      await refresh();
    }
  }

  function requestNative(action) {
    if (!canLaunch()) return;
    const payload = readPayload(action);
    if (!payload) return;
    state.pendingNative = payload;
    $("confirm-action").textContent = labels[action];
    $("confirm-source").textContent = payload.source_dir;
    $("confirm-output").textContent = payload.output_dir;
    $("confirm-options").textContent = `${payload.opts.all_pages ? "Toutes les pages" : "Pages par défaut du serveur"} · ${payload.opts.screen_comparison === "local" ? "différence locale de pixels" : "examen manuel"}`;
    dialog.returnValue = "cancel";
    dialog.showModal();
    $("native-cancel").focus();
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!canLaunch()) return;
    const payload = readPayload("migration");
    if (payload) void submitJob(payload);
  });
  $("probe-button").addEventListener("click", () => requestNative("probe"));
  $("compare-button").addEventListener("click", () => requestNative("compare"));
  dialog.addEventListener("close", () => {
    const payload = state.pendingNative;
    state.pendingNative = null;
    if (dialog.returnValue === "confirm" && payload) void submitJob(payload);
  });
  $("reconnect").addEventListener("click", async () => {
    // This acknowledges an ambiguous POST, but does not resubmit it.
    state.uncertain = false;
    state.token = null;
    showMessage("Vérification de la session et des tâches. Aucune tâche n’est relancée.");
    clearTimeout(state.timer);
    await refresh();
  });
  document.addEventListener("visibilitychange", () => {
    clearTimeout(state.timer);
    if (document.hidden) {
      $("polling-status").textContent = "Actualisation en pause · onglet masqué";
    } else {
      void refresh();
    }
  });
  window.addEventListener("pagehide", () => clearTimeout(state.timer));
  if (!["http:", "https:"].includes(window.location.protocol)) {
    connection(false, "Ouvrez cette interface à l’adresse HTTP du serveur local, pas directement comme fichier. Les routes /api/session et /api/jobs doivent être disponibles sur la même origine.");
  } else {
    void refresh();
  }
})();
