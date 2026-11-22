/* PhotoSweep Frontend Controller */

const state = {
  sources: [],
  currentJobId: null,
  pollTimer: null,
  browserMode: "source", // 'source' | 'dest'
  browserCurrentPath: "",
  browserParentPath: "",
  sampleCategory: "exact_duplicate",
  lastSummary: null,
};

const $ = (id) => document.getElementById(id);

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = bytes;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx++;
  }
  return `${val.toFixed(idx >= 2 ? 2 : 0)} ${units[idx]}`;
}

function formatEta(seconds) {
  if (seconds == null || !isFinite(seconds)) return "Calculating...";
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  if (mins < 60) return `${mins}m ${secs}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

function showToast(msg, duration = 3500) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), duration);
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  return data;
}

// ---------------- System & Sources ----------------

async function loadSystem() {
  const info = await api("/api/system");
  $("cDriveBadge").textContent = `C: Free ${info.c_drive_free_gb} GB`;

  const chips = $("quickChips");
  chips.innerHTML = "";
  for (const sc of info.shortcuts) {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.textContent = sc.is_onedrive ? `☁ ${sc.label}` : `📁 ${sc.label}`;
    btn.onclick = () => addSource(sc.path, sc.is_onedrive);
    chips.appendChild(btn);
  }
  for (const drv of info.drives) {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.textContent = `💾 ${drv.label} (${drv.free_gb} GB free)`;
    btn.onclick = () => openBrowser("source", drv.path);
    chips.appendChild(btn);
  }
}

async function loadJobsHistory() {
  const data = await api("/api/jobs");
  const sel = $("jobHistorySelect");
  sel.innerHTML = '<option value="">+ New Session</option>';
  for (const j of data.jobs) {
    const opt = document.createElement("option");
    opt.value = j.id;
    const dt = new Date(j.created_at * 1000).toLocaleString();
    opt.textContent = `${dt} — ${j.status} (${j.total_files.toLocaleString()} files)`;
    sel.appendChild(opt);
  }
  if (data.active_job_id) {
    sel.value = data.active_job_id;
    attachToJob(data.active_job_id);
  } else if (data.jobs.length > 0 && ["paused", "drive_disconnected"].includes(data.jobs[0].status)) {
    // Automatically surface an interrupted/disconnected session so the user can resume immediately
    sel.value = data.jobs[0].id;
    attachToJob(data.jobs[0].id);
  }
}

function addSource(path, isOneDrive = false) {
  const norm = path.toLowerCase();
  if (state.sources.some((s) => s.path.toLowerCase() === norm)) {
    showToast("Folder is already in the list.");
    return;
  }
  state.sources.push({ path, isOneDrive });
  renderSources();
}

function renderSources() {
  const container = $("selectedSourcesList");
  if (state.sources.length === 0) {
    container.innerHTML =
      '<div class="empty-state">No folders selected yet. Click a drive/shortcut above or click <strong>+ Browse Folders / Drives</strong>.</div>';
    return;
  }
  container.innerHTML = "";
  state.sources.forEach((src, idx) => {
    const row = document.createElement("div");
    row.className = "source-item";
    row.innerHTML = `
      <div class="source-left">
        <span class="priority-badge">#${idx + 1}</span>
        <span class="source-path" title="${src.path}">${src.path}</span>
        ${src.isOneDrive ? '<span class="pill pill-onedrive">OneDrive</span>' : ""}
      </div>
      <div class="source-actions">
        <button class="btn btn-secondary btn-sm" ${idx === 0 ? "disabled" : ""} data-act="up">▲</button>
        <button class="btn btn-secondary btn-sm" ${idx === state.sources.length - 1 ? "disabled" : ""} data-act="down">▼</button>
        <button class="btn btn-danger-outline btn-sm" data-act="rm">Remove</button>
      </div>
    `;
    row.querySelector('[data-act="up"]').onclick = () => {
      [state.sources[idx - 1], state.sources[idx]] = [state.sources[idx], state.sources[idx - 1]];
      renderSources();
    };
    row.querySelector('[data-act="down"]').onclick = () => {
      [state.sources[idx + 1], state.sources[idx]] = [state.sources[idx], state.sources[idx + 1]];
      renderSources();
    };
    row.querySelector('[data-act="rm"]').onclick = () => {
      state.sources.splice(idx, 1);
      renderSources();
    };
    container.appendChild(row);
  });
}

// ---------------- Folder Browser Modal ----------------

async function openBrowser(mode, startPath = "") {
  state.browserMode = mode;
  $("browserModalTitle").textContent =
    mode === "source" ? "Select Source Folder or Drive" : "Select Destination Folder (Drive or OneDrive)";
  $("browserModal").classList.remove("hidden");
  await navigateBrowser(startPath);
}

function closeBrowser() {
  $("browserModal").classList.add("hidden");
}

async function navigateBrowser(path) {
  try {
    const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    state.browserCurrentPath = data.current;
    state.browserParentPath = data.parent;
    $("browserPathInput").value = data.current;
    $("btnBrowseUp").disabled = !data.current;
    $("btnConfirmBrowser").disabled = !data.current;
    $("browserHint").textContent = data.is_onedrive
      ? "OneDrive folder detected — SSD-safe throttling enabled."
      : "";

    const list = $("browserList");
    list.innerHTML = "";
    if (data.entries.length === 0) {
      list.innerHTML = '<div class="empty-state" style="border:none">No subfolders here. Click "Select This Folder".</div>';
      return;
    }
    for (const entry of data.entries) {
      const div = document.createElement("div");
      div.className = "browser-entry";
      const rightText = entry.is_drive
        ? `<span class="hint">${entry.free_gb} GB free / ${entry.total_gb} GB</span>`
        : entry.is_onedrive
        ? '<span class="pill pill-onedrive">OneDrive</span>'
        : "";
      div.innerHTML = `<span>${entry.is_drive ? "💾" : "📁"} ${entry.name}</span>${rightText}`;
      div.onclick = () => navigateBrowser(entry.path);
      list.appendChild(div);
    }
  } catch (err) {
    showToast(err.message);
  }
}

// ---------------- Analysis & Polling ----------------

async function startAnalysis() {
  if (state.sources.length === 0) {
    showToast("Please add at least one folder or drive first.");
    return;
  }
  try {
    const payload = {
      sources: state.sources.map((s) => s.path),
      preset: $("presetSelect").value,
      onedrive_mode: $("onedriveModeSelect").value,
      min_ssd_free_gb: parseFloat($("minSsdInput").value || "15"),
      enable_duplicates: $("chkDupes").checked,
      enable_other_duplicates: $("chkOtherDupes").checked,
      enable_blur: $("chkBlur").checked,
      enable_similar: $("chkSimilar").checked,
    };
    const data = await api("/api/jobs/analyze", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    attachToJob(data.job.id);
    loadJobsHistory();
  } catch (err) {
    showToast(err.message);
  }
}

function attachToJob(jobId, intervalMs = 800) {
  state.currentJobId = jobId;
  clearInterval(state.pollTimer);
  pollJob();
  state.pollTimer = setInterval(pollJob, intervalMs);
}

async function pollJob() {
  if (!state.currentJobId) return;
  try {
    const data = await api(`/api/jobs/${state.currentJobId}`);
    const job = data.job;
    const rt = data.runtime || {};
    $("cDriveBadge").textContent = `C: Free ${data.c_drive_free_gb} GB`;

    // Drive Disconnect Banner
    const banner = $("disconnectBanner");
    if (job.status === "drive_disconnected") {
      banner.classList.remove("hidden");
      if (data.drive_reconnected) {
        banner.className = "banner banner-reconnected";
        $("disconnectTitle").textContent = "Drive Reconnected!";
        $("disconnectMessage").textContent = `${job.disconnected_path} is back online. Click below to continue right where you left off.`;
      } else {
        banner.className = "banner banner-danger";
        $("disconnectTitle").textContent = "Drive Disconnected!";
        $("disconnectMessage").textContent = `Lost connection to ${job.disconnected_path}. All progress is saved — plug it back in and click Reconnect & Resume.`;
      }
    } else {
      banner.classList.add("hidden");
    }

    const activeStatuses = ["created", "scanning", "deduping", "analyzing", "clustering", "executing"];
    const pausedStatuses = ["paused", "drive_disconnected", "error"];

    if (activeStatuses.includes(job.status) || pausedStatuses.includes(job.status)) {
      $("setupPanel").classList.add("hidden");
      $("reportPanel").classList.add("hidden");
      $("progressPanel").classList.remove("hidden");

      const phaseNames = {
        scanning: "Phase 1/4: Scanning Drives",
        deduping: "Phase 2/4: Exact Duplicates",
        analyzing: "Phase 3/4: Face, Blur & Visual Analysis",
        clustering: "Phase 4/4: Burst Best-Shot Selection",
        quarantining: "Moving to Quarantine",
        deleting: "Permanently Deleting",
        copying: "Copying Kept Files",
        restoring: "Undoing Quarantine",
      };
      $("phaseBadge").textContent = phaseNames[job.phase] || job.phase;
      $("progressTitle").textContent = job.status === "paused" ? "Paused" : job.status === "drive_disconnected" ? "Waiting for Drive" : "Processing";
      $("statusMessage").textContent = job.status_message || "";
      $("currentFileText").textContent = job.current_file || "—";

      const done = job.processed_files || 0;
      const total = job.total_files || 0;
      const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : job.phase === "scanning" ? 15 : 0;
      $("progressBar").style.width = `${pct}%`;
      $("metricProcessed").textContent = total > 0 ? `${done.toLocaleString()} / ${total.toLocaleString()} (${pct}%)` : `${done.toLocaleString()} found`;

      // Show byte-based MB/s + source type when available, falling back to files/s
      if (rt.mb_per_sec && rt.mb_per_sec > 0) {
        const tag = rt.active_source_type === "cloud" ? "Cloud" : "Local";
        $("metricSpeed").textContent = `${rt.mb_per_sec} MB/s (${tag})`;
      } else {
        $("metricSpeed").textContent = rt.rate_per_sec ? `${rt.rate_per_sec} files/s` : "—";
      }

      $("metricEta").textContent = data.is_active ? formatEta(rt.eta_seconds) : "—";

      const remL = rt.remaining_local || 0;
      const remC = rt.remaining_cloud || 0;
      if (remC > 0 || remL > 0) {
        $("metricQueue").textContent =
          remC > 0
            ? `${remL.toLocaleString()} local / ${remC.toLocaleString()} cloud`
            : `${remL.toLocaleString()} local`;
      } else {
        $("metricQueue").textContent = total > 0 ? `${Math.max(0, total - done).toLocaleString()} left` : "—";
      }

      $("metricStatus").textContent = job.status.replace("_", " ");

      $("btnPause").classList.toggle("hidden", !data.is_active);
      $("btnCancel").classList.toggle("hidden", !data.is_active);
      $("btnResume").classList.toggle("hidden", data.is_active || job.status === "error");
    } else if (job.status === "analyzed" || job.status === "completed") {
      clearInterval(state.pollTimer);
      const wasExecuting = !$("progressPanel").classList.contains("hidden");
      $("setupPanel").classList.add("hidden");
      $("progressPanel").classList.add("hidden");
      $("reportPanel").classList.remove("hidden");
      renderReport(job);
      if (wasExecuting && job.status_message) {
        showToast(job.status_message, 5000);
      }
    } else if (job.status === "cancelled") {
      clearInterval(state.pollTimer);
      $("progressPanel").classList.add("hidden");
      $("reportPanel").classList.add("hidden");
      $("setupPanel").classList.remove("hidden");
      showToast("Job cancelled.");
    }
  } catch (err) {
    console.error(err);
  }
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setBoxDisabled(boxId, disabled) {
  const box = $(boxId);
  box.classList.toggle("disabled-box", disabled);
  box.querySelectorAll("button, input, select").forEach((el) => {
    el.disabled = disabled;
  });
}

function renderAnalyzedFolders(sources) {
  const box = $("analyzedFoldersBox");
  if (!sources || sources.length === 0) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const listHtml = sources
    .map(
      (p, idx) =>
        `<li><span class="priority-badge">#${idx + 1}</span><span>${escapeHtml(p)}</span></li>`
    )
    .join("");

  if (sources.length <= 2) {
    box.innerHTML = `
      <div class="analyzed-folders-header">Analyzed Folders (${sources.length})</div>
      <ul class="analyzed-path-list">${listHtml}</ul>
    `;
  } else {
    box.innerHTML = `
      <details>
        <summary>
          <span>Analyzed Folders (${sources.length}): <code>${escapeHtml(sources[0])}</code> + ${sources.length - 1} more</span>
        </summary>
        <ul class="analyzed-path-list">${listHtml}</ul>
      </details>
    `;
  }
}

function renderReport(job) {
  const s = job.summary || {};
  state.lastSummary = s;
  const cats = s.categories || {};
  const act = s.actioned || {};
  const dupes = cats.exact_duplicate || { count: 0, bytes: 0, photos: 0, videos: 0, other: 0 };
  const blurry = cats.blurry || { count: 0, bytes: 0 };
  const similar = cats.similar_loser || { count: 0, bytes: 0 };

  $("reportSubtitle").textContent =
    job.status_message || "Analysis complete. Zero files modified until you choose an action below.";
  $("statSavedBytes").textContent = formatBytes(s.flagged_bytes || 0);
  $("statFlaggedCount").textContent = `${(s.flagged_count || 0).toLocaleString()} files flagged`;

  $("statDupesCount").textContent = dupes.count.toLocaleString();
  const otherPart = dupes.other ? `, ${dupes.other} other` : "";
  $("statDupesBytes").textContent = `${formatBytes(dupes.bytes)} (${dupes.photos} photos, ${dupes.videos} videos${otherPart})`;

  $("statBlurryCount").textContent = blurry.count.toLocaleString();
  $("statBlurryBytes").textContent = `${formatBytes(blurry.bytes)} (well-lit human portraits)`;

  $("statSimilarCount").textContent = similar.count.toLocaleString();
  $("statSimilarBytes").textContent = `${formatBytes(similar.bytes)} (burst losers)`;

  $("statKeptCount").textContent = (s.kept_count || 0).toLocaleString();
  $("statKeptBytes").textContent = `${formatBytes(s.kept_bytes || 0)} clean library`;

  $("undoBar").classList.toggle("hidden", !s.can_undo_quarantine);

  // Render analyzed folder paths above Option A / B
  renderAnalyzedFolders((job.config && job.config.sources) || []);

  // Post-deletion complete state: show completion banner and disable Option A & B
  const allDeleted = Boolean(s.all_flagged_deleted);
  const deletedCnt = act.deleted || 0;
  const deletedBytes = act.deleted_bytes || 0;
  $("allDeletedBanner").classList.toggle("hidden", !allDeleted);
  if (allDeleted) {
    $("allDeletedMessage").textContent =
      `All ${deletedCnt.toLocaleString()} flagged files have been permanently deleted (${formatBytes(deletedBytes)} freed).`;
  }
  setBoxDisabled("optionABox", allDeleted);
  setBoxDisabled("optionBBox", allDeleted);

  // Failed deletion / download banner + Retry + Download CSV
  const failedFlagged = act.failed_flagged || 0;
  const failedTotal = act.failed || 0;
  const dlErrors = act.download_errors || 0;
  const hasFailures = failedTotal > 0 || dlErrors > 0;
  $("failedBanner").classList.toggle("hidden", !hasFailures);
  if (hasFailures) {
    const parts = [];
    if (deletedCnt > 0 && failedFlagged > 0) {
      parts.push(`Deleted ${deletedCnt.toLocaleString()} files, but ${failedFlagged.toLocaleString()} file(s) could not be deleted.`);
    } else if (failedFlagged > 0) {
      parts.push(`${failedFlagged.toLocaleString()} flagged file(s) could not be deleted or moved.`);
    } else if (failedTotal > 0) {
      parts.push(`${failedTotal.toLocaleString()} file(s) failed during the last file operation.`);
    }
    if (dlErrors > 0) {
      parts.push(`${dlErrors.toLocaleString()} file(s) could not be downloaded or read during analysis.`);
    }
    $("failedBannerTitle").textContent =
      failedFlagged > 0 ? "Some Files Could Not Be Deleted" : "Some Files Could Not Be Downloaded / Processed";
    $("failedBannerMessage").textContent = parts.join(" ");

    const retryAnalysisBtn = $("btnRetryFailedAnalysis");
    if (dlErrors > 0) {
      retryAnalysisBtn.textContent = `Retry Failed Files (${dlErrors.toLocaleString()})`;
      retryAnalysisBtn.classList.remove("hidden");
    } else {
      retryAnalysisBtn.classList.add("hidden");
    }

    const retryBtn = $("btnRetryFailedDelete");
    if (failedFlagged > 0) {
      retryBtn.textContent = `Retry Failed Deletions (${failedFlagged.toLocaleString()})`;
      retryBtn.classList.remove("hidden");
    } else {
      retryBtn.classList.add("hidden");
    }
  }

  if ($("sampleDrawer").open) {
    loadSamples(state.sampleCategory);
  }
}

function getSelectedActionCategories() {
  const cats = [];
  if ($("actDupes").checked) cats.push("exact_duplicate");
  if ($("actBlurry").checked) cats.push("blurry");
  if ($("actSimilar").checked) cats.push("similar_loser");
  return cats;
}

async function runExecute(payload) {
  try {
    const labelMap = {
      delete: "Permanently Deleting",
      quarantine: "Moving to Quarantine",
      copy_kept: "Copying Kept Files",
      undo_quarantine: "Undoing Quarantine",
    };
    // Immediately transition UI to the progress bar view
    $("reportPanel").classList.add("hidden");
    $("setupPanel").classList.add("hidden");
    $("progressPanel").classList.remove("hidden");
    $("phaseBadge").textContent = labelMap[payload.mode] || "Executing";
    $("progressTitle").textContent = "Processing";
    $("statusMessage").textContent = "Starting...";
    $("progressBar").style.width = "0%";
    $("metricProcessed").textContent = "0 / 0";
    $("metricSpeed").textContent = "—";
    $("metricEta").textContent = "Calculating...";
    $("metricStatus").textContent = "executing";
    $("currentFileText").textContent = "—";

    await api(`/api/jobs/${state.currentJobId}/execute`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    attachToJob(state.currentJobId, 350);
  } catch (err) {
    $("progressPanel").classList.add("hidden");
    $("reportPanel").classList.remove("hidden");
    showToast(err.message);
  }
}

async function loadSamples(category) {
  state.sampleCategory = category;
  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.cat === category);
  });
  const grid = $("sampleGrid");
  grid.innerHTML = '<div class="hint">Loading samples...</div>';
  try {
    const data = await api(
      `/api/jobs/${state.currentJobId}/samples?category=${encodeURIComponent(category)}&limit=24`
    );
    grid.innerHTML = "";
    if (!data.items.length) {
      grid.innerHTML = '<div class="hint">No files flagged in this category.</div>';
      return;
    }
    for (const item of data.items) {
      const card = document.createElement("div");
      card.className = "sample-card";
      const hasKept = item.kept_file_id && item.media_type === "photo";
      const thumbsHtml =
        item.media_type === "photo"
          ? `<div class="sample-thumbs ${hasKept ? "" : "single"}">
              <div class="thumb-wrap">
                <span class="thumb-tag tag-flag">Flagged</span>
                <img src="/api/thumb/${item.id}" loading="lazy" alt="" />
              </div>
              ${
                hasKept
                  ? `<div class="thumb-wrap">
                      <span class="thumb-tag tag-keep">Kept #1</span>
                      <img src="/api/thumb/${item.kept_file_id}" loading="lazy" alt="" />
                    </div>`
                  : ""
              }
            </div>`
          : item.media_type === "video"
          ? `<div class="hint">🎬 Video (${formatBytes(item.size_bytes)})</div>`
          : `<div class="hint">📄 File (${formatBytes(item.size_bytes)})</div>`;
      card.innerHTML = `
        ${thumbsHtml}
        <div><strong>${item.filename}</strong> (${formatBytes(item.size_bytes)})</div>
        <div class="hint">${item.decision_reason || ""}</div>
      `;
      grid.appendChild(card);
    }
  } catch (err) {
    grid.innerHTML = `<div class="hint">${err.message}</div>`;
  }
}

// ---------------- Event Wiring ----------------

$("btnBrowseSource").onclick = () => openBrowser("source", "");
$("btnBrowseDest").onclick = () => openBrowser("dest", "");
$("btnCloseBrowser").onclick = closeBrowser;
$("btnCancelBrowser").onclick = closeBrowser;
$("btnBrowseUp").onclick = () => navigateBrowser(state.browserParentPath);
$("btnBrowseGo").onclick = () => navigateBrowser($("browserPathInput").value.trim());
$("btnConfirmBrowser").onclick = () => {
  const chosen = state.browserCurrentPath;
  if (!chosen) return;
  if (state.browserMode === "source") {
    const isOd = $("browserHint").textContent.includes("OneDrive");
    addSource(chosen, isOd);
  } else {
    $("copyDestInput").value = chosen;
  }
  closeBrowser();
};

$("btnStartAnalysis").onclick = startAnalysis;

$("btnPause").onclick = async () => {
  await api(`/api/jobs/${state.currentJobId}/pause`, { method: "POST" });
  showToast("Pausing after current batch...");
};

$("btnCancel").onclick = async () => {
  await api(`/api/jobs/${state.currentJobId}/cancel`, { method: "POST" });
};

async function handleResume() {
  try {
    await api(`/api/jobs/${state.currentJobId}/resume`, { method: "POST" });
    attachToJob(state.currentJobId);
  } catch (err) {
    showToast(err.message);
  }
}
$("btnResume").onclick = handleResume;
$("btnCheckReconnect").onclick = handleResume;

function resetToNewScan() {
  clearInterval(state.pollTimer);
  state.currentJobId = null;
  $("jobHistorySelect").value = "";
  $("reportPanel").classList.add("hidden");
  $("progressPanel").classList.add("hidden");
  $("disconnectBanner").classList.add("hidden");
  $("setupPanel").classList.remove("hidden");
}

$("btnNewAnalysis").onclick = resetToNewScan;
$("btnNewAnalysisBanner").onclick = resetToNewScan;

$("btnRetryFailedAnalysis").onclick = async () => {
  if (!state.currentJobId) return;
  try {
    $("reportPanel").classList.add("hidden");
    $("setupPanel").classList.add("hidden");
    $("progressPanel").classList.remove("hidden");
    $("phaseBadge").textContent = "Retrying Failed Files";
    $("progressTitle").textContent = "Processing";
    $("statusMessage").textContent = "Retrying files that previously failed to download or read...";
    $("progressBar").style.width = "0%";
    $("metricProcessed").textContent = "0 / 0";
    $("metricSpeed").textContent = "—";
    $("metricEta").textContent = "Calculating...";
    $("metricQueue").textContent = "—";
    $("metricStatus").textContent = "analyzing";
    $("currentFileText").textContent = "—";

    await api(`/api/jobs/${state.currentJobId}/retry-analysis`, { method: "POST" });
    attachToJob(state.currentJobId, 400);
  } catch (err) {
    $("progressPanel").classList.add("hidden");
    $("reportPanel").classList.remove("hidden");
    showToast(err.message);
  }
};

$("btnRetryFailedDelete").onclick = () => {
  runExecute({
    mode: "delete",
    categories: ["exact_duplicate", "blurry", "similar_loser"],
    retry_failed: true,
  });
};

$("btnDownloadFailedCsv").onclick = () => {
  if (!state.currentJobId) return;
  window.location.href = `/api/jobs/${encodeURIComponent(state.currentJobId)}/failed-csv`;
};

$("jobHistorySelect").onchange = (e) => {
  const val = e.target.value;
  if (!val) {
    $("btnNewAnalysis").click();
  } else {
    attachToJob(val);
  }
};

$("btnQuarantine").onclick = () => {
  const cats = getSelectedActionCategories();
  if (!cats.length) return showToast("Select at least one category.");
  runExecute({ mode: "quarantine", categories: cats });
};

$("btnUndoQuarantine").onclick = () => {
  runExecute({ mode: "undo_quarantine" });
};

$("btnDeletePermanent").onclick = () => {
  const cats = getSelectedActionCategories();
  if (!cats.length) return showToast("Select at least one category.");
  $("deleteModalText").textContent =
    "This will permanently delete the selected flagged files from disk (cannot be undone). Consider using 'Move to Quarantine' first if unsure.";
  $("deleteConfirmCheck").checked = false;
  $("btnConfirmDelete").disabled = true;
  $("deleteModal").classList.remove("hidden");
};

$("deleteConfirmCheck").onchange = (e) => {
  $("btnConfirmDelete").disabled = !e.target.checked;
};
$("btnCloseDeleteModal").onclick = () => $("deleteModal").classList.add("hidden");
$("btnCancelDelete").onclick = () => $("deleteModal").classList.add("hidden");
$("btnConfirmDelete").onclick = () => {
  $("deleteModal").classList.add("hidden");
  runExecute({ mode: "delete", categories: getSelectedActionCategories() });
};

$("btnCopyKept").onclick = () => {
  const dest = $("copyDestInput").value.trim();
  if (!dest) return showToast("Please browse and select a destination folder first.");
  runExecute({
    mode: "copy_kept",
    destination: dest,
    folder_layout: $("folderLayoutSelect").value,
  });
};

$("sampleDrawer").ontoggle = () => {
  if ($("sampleDrawer").open && state.currentJobId) {
    loadSamples(state.sampleCategory);
  }
};
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.onclick = () => loadSamples(btn.dataset.cat);
});

// Boot
loadSystem();
loadJobsHistory();
