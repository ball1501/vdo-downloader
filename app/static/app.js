"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  url: $("url"),
  outdir: $("outdir"),
  btnExtract: $("btn-extract"),
  extractStatus: $("extract-status"),
  extractError: $("extract-error"),
  result: $("result"),
  resultTitle: $("result-title"),
  resultSource: $("result-source"),
  formatsBody: $("formats-body"),
  btnDownload: $("btn-download"),
  downloadAck: $("download-ack"),
  jobs: $("jobs"),
};

const STATUS_TH = {
  queued: "รอคิว",
  extracting: "กำลังค้นหาไฟล์",
  downloading: "กำลังดาวน์โหลด",
  processing: "รวมไฟล์",
  done: "เสร็จสิ้น ✓",
  error: "ล้มเหลว",
  cancelled: "ยกเลิกแล้ว",
};
const ACTIVE = new Set(["queued", "extracting", "downloading", "processing"]);

let lastExtract = null; // {url, title, formats}

// ---------- helpers ----------
function fmtSize(bytes) {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtSpeed(bps) {
  return bps ? `${fmtSize(bps)}/s` : "—";
}

function fmtEta(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  return m > 0 ? `${m} นาที ${s} วิ` : `${s} วิ`;
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
  }
  return data;
}

// ---------- extract ----------
async function extract() {
  const url = els.url.value.trim();
  if (!url) return;

  els.btnExtract.disabled = true;
  els.extractError.classList.add("hidden");
  els.extractStatus.classList.remove("hidden");
  els.result.classList.add("hidden");
  els.downloadAck.classList.add("hidden");

  try {
    const data = await api("/api/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    lastExtract = { url, ...data };
    renderResult(data);
  } catch (err) {
    els.extractError.textContent = `❌ ${err.message}`;
    els.extractError.classList.remove("hidden");
  } finally {
    els.btnExtract.disabled = false;
    els.extractStatus.classList.add("hidden");
  }
}

function renderResult(data) {
  els.resultTitle.textContent = data.title || "(ไม่ทราบชื่อเรื่อง)";
  els.resultSource.textContent = `พบ ${data.formats.length} คุณภาพ · ตรวจพบโดย ${data.source}`;

  els.formatsBody.innerHTML = "";
  data.formats.forEach((f, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="radio" name="format" ${i === 0 ? "checked" : ""}></td>
      <td><strong></strong></td>
      <td></td>
      <td></td>`;
    tr.children[1].firstElementChild.textContent = f.label;
    tr.children[2].textContent = (f.ext || "?").toUpperCase();
    tr.children[3].textContent = fmtSize(f.filesize);
    tr.querySelector("input").value = f.format_id;
    els.formatsBody.appendChild(tr);
  });

  els.result.classList.remove("hidden");
  els.result.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- download ----------
async function queueDownload() {
  if (!lastExtract) return;
  const checked = document.querySelector('input[name="format"]:checked');
  if (!checked) return;

  els.btnDownload.disabled = true;
  try {
    await api("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: lastExtract.url,
        format_id: checked.value,
        title: lastExtract.title,
        outdir: els.outdir.value.trim() || null,
        media_url: lastExtract.media_url,
        media_headers: lastExtract.media_headers,
        cookies: lastExtract.cookies,
      }),
    });
    els.downloadAck.classList.remove("hidden");
  } catch (err) {
    els.extractError.textContent = `❌ ${err.message}`;
    els.extractError.classList.remove("hidden");
  } finally {
    els.btnDownload.disabled = false;
  }
}

// ---------- queue rendering (via SSE) ----------
function renderJobs(jobs) {
  if (!jobs.length) {
    els.jobs.innerHTML = '<p class="muted">ยังไม่มีงาน — วางลิงก์ด้านบนเพื่อเริ่ม</p>';
    return;
  }
  els.jobs.innerHTML = "";
  [...jobs].reverse().forEach((job) => els.jobs.appendChild(jobCard(job)));
}

function jobCard(job) {
  const div = document.createElement("div");
  div.className = "job";
  div.dataset.id = job.id;

  const active = ACTIVE.has(job.status);
  const pct = job.progress != null ? job.progress : null;

  const head = document.createElement("div");
  head.className = "job-head";
  const title = document.createElement("div");
  title.className = "job-title";
  title.textContent = job.title;
  title.title = job.url;
  const badge = document.createElement("span");
  badge.className = `badge ${job.status}`;
  badge.textContent = STATUS_TH[job.status] || job.status;
  head.append(title, badge);

  div.appendChild(head);

  if (job.status !== "error") {
    const track = document.createElement("div");
    track.className = "progress-track";
    const fill = document.createElement("div");
    fill.className = "progress-fill" + (pct == null && active ? " indeterminate" : "");
    if (pct != null) fill.style.width = `${pct}%`;
    track.appendChild(fill);
    div.appendChild(track);
  }

  const meta = document.createElement("div");
  meta.className = "job-meta";
  const left = document.createElement("span");
  if (job.status === "downloading") {
    left.textContent =
      `${pct != null ? pct.toFixed(1) + "%" : ""} · ${fmtSpeed(job.speed)} · เหลืออีก ${fmtEta(job.eta)}`.trim();
  } else if (job.status === "done") {
    left.textContent = "บันทึกเรียบร้อย";
  } else if (job.status === "queued") {
    left.textContent = "อยู่ในคิว...";
  }
  meta.appendChild(left);

  if (active) {
    const btn = document.createElement("button");
    btn.className = "btn-cancel";
    btn.textContent = "✕ ยกเลิก";
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api(`/api/jobs/${job.id}/cancel`, { method: "POST" });
      } catch { /* job may have finished already */ }
    };
    meta.appendChild(btn);
  }
  div.appendChild(meta);

  if (job.status === "error" && job.error) {
    const err = document.createElement("div");
    err.className = "job-error";
    err.textContent = job.error.length > 300 ? job.error.slice(0, 300) + "…" : job.error;
    div.appendChild(err);
  }
  if (job.status === "done" && job.filename) {
    const file = document.createElement("div");
    file.className = "job-file";
    file.textContent = `📄 ${job.filename}`;
    div.appendChild(file);
  }

  return div;
}

// ---------- init ----------
els.btnExtract.addEventListener("click", extract);
els.url.addEventListener("keydown", (e) => e.key === "Enter" && extract());
els.btnDownload.addEventListener("click", queueDownload);

api("/api/config")
  .then((c) => (els.outdir.value = c.default_outdir))
  .catch(() => (els.outdir.placeholder = "โฟลเดอร์ downloads ของโปรแกรม"));

const es = new EventSource("/api/events");
es.addEventListener("jobs", (e) => renderJobs(JSON.parse(e.data)));
