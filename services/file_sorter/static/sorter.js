(() => {
  const config = JSON.parse(document.getElementById("sorter-config").textContent);
  const $ = (id) => document.getElementById(id);
  const PROJECT_KEY = "sorter.project";

  const state = {
    projects: [],
    project: null,
    entry: null,
    progress: null,
    review: { active: false, entries: [], groups: [], byPath: new Map(), activePath: null, offset: 0, baseOffset: 0, total: 0, more: false, loading: false, request: 0, previewRequest: 0, scanRequest: 0, pendingFilter: false },
    duplicate: null,
    duplicateGroup: null,
    duplicateKeeper: 0,
    folders: [],
    byPath: new Map(),
    children: new Map(),
    selected: null,
    expanded: new Set(),
    firstLoad: false,
    browse: {
      source: { path: "", picked: null },
      target: { path: "", picked: null, create: false },
    },
    busy: false,
    dialogMode: null,
    folderTarget: null,
    searchCursor: null,
    groupPicks: new Set(),
    moveSource: null,
    moveTarget: null,
    toastTimer: null,
  };

  // ---------- helpers ----------

  function stored(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch (_) {
      return fallback;
    }
  }

  function store(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
  }

  // Folder memory belongs to a tree: projects sharing a tree share it.
  const expandedKey = () => `sorter.expanded:${state.project ? state.project.target : ""}`;

  let inFlight = 0;
  let indexPollTimer = null;
  let lastIndexCompleted = null;

  // Quiet while the index is current: only say something worth knowing.
  function indexNote(text, title = "") {
    $("index-status").textContent = text;
    $("index-status").title = title;
    $("index-status").classList.toggle("hidden", !text);
  }

  function renderIndexStatus(status) {
    if (!status) return;
    indexNote(
      status.error ? "Some files could not be indexed; known matches are still shown." : status.running ? `Indexing files: ${status.checked} / ${status.total}. Matches may be incomplete.` : status.complete ? "" : "Indexing queued. Matches may be incomplete.",
      status.error || "",
    );
  }

  function pollIndex() {
    clearTimeout(indexPollTimer);
    if (document.hidden) return;
    const projectId = state.project?.id;
    indexPollTimer = setTimeout(async () => {
      if (document.hidden || !state.review.active || state.project?.id !== projectId) return;
      try {
        const status = await papi("/index");
        if (!state.review.active || state.project?.id !== projectId) return;
        renderIndexStatus(status);
        if (status.last_completed_at !== lastIndexCompleted && !status.running && !state.busy) {
          lastIndexCompleted = status.last_completed_at;
          const result = await papi("/duplicates?scope=tree&indexed=true");
          if (!state.review.active || state.project?.id !== projectId) return;
          // An action may have started while this request was in flight: never
          // swap the list under it; the next poll catches up.
          if (state.busy) {
            lastIndexCompleted = null;
          } else {
            setDuplicates(result);
            await loadReviewFiles(false, state.review.activePath, 0, true);
          }
        }
      } catch (_) { indexNote("Could not refresh the file index status."); }
      if (state.review.active) pollIndex();
    }, 5000);
  }

  function setLoading(delta) {
    inFlight = Math.max(0, inFlight + delta);
    document.body.classList.toggle("loading", inFlight > 0);
  }

  // A stalled NAS can hold a request for minutes. Give up visibly instead of
  // leaving every button disabled; the server may still finish the action.
  const REQUEST_TIMEOUT_MS = 300000;
  const LONG_REQUEST_TIMEOUT_MS = 900000;

  async function api(path, options = {}) {
    const { timeout = REQUEST_TIMEOUT_MS, ...fetchOptions } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    setLoading(1);
    let response;
    try {
      response = await fetch(config.basePath + path, {
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        ...fetchOptions,
      });
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("The NAS is not responding. Reload in a moment to see whether the last action finished.");
      }
      throw error;
    } finally {
      clearTimeout(timer);
      setLoading(-1);
    }
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail || detail;
      } catch (_) {
        // Keep the status text when the body is not JSON.
      }
      throw new Error(typeof detail === "string" ? detail : "Request refused");
    }
    return response.status === 204 ? null : response.json();
  }

  function papi(path, options) {
    return api(`/api/projects/${state.project.id}${path}`, options);
  }

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function size(bytes) {
    if (bytes === null || bytes === undefined) return "";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }

  const parentOf = (path) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
  const leafOf = (path) => path.slice(path.lastIndexOf("/") + 1);
  const depthOf = (path) => path.split("/").length - 1;

  function movedPath(path, from, to) {
    if (path === from) return to;
    if (path.startsWith(from + "/")) return to + path.slice(from.length);
    return path;
  }

  function showError(message, id = "error") {
    $(id).textContent = message || "";
    $(id).classList.toggle("hidden", !message);
  }

  // `undo` is the response of the action the toast reports. Its UNDO then
  // reverses exactly that decision, in the project that made it, or nothing.
  function toast(message, undo) {
    $("toast-text").textContent = message;
    state.toastUndo = undo && state.project
      ? { projectId: state.project.id, decisionId: undo.decision_id ?? null }
      : null;
    $("toast-undo").classList.toggle("hidden", !state.toastUndo);
    $("toast").classList.remove("hidden");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 7000);
  }

  const ACTION_BUTTONS = ["classify", "skip", "discard", "undo"];

  async function act(work) {
    if (state.busy) {
      // Never swallow a press silently: say why nothing happened.
      toast("Still finishing the last action. Try again in a moment.", false);
      return;
    }
    state.busy = true;
    showError("");
    const before = ACTION_BUTTONS.map((id) => $(id).disabled);
    ACTION_BUTTONS.forEach((id) => {
      $(id).disabled = true;
    });
    try {
      await work();
    } catch (error) {
      showError(error.message);
      ACTION_BUTTONS.forEach((id, index) => {
        $(id).disabled = before[index];
      });
    } finally {
      state.busy = false;
      // Whatever rendered last decides which actions make sense now.
      $("skip").disabled = !state.entry || state.entry.area === "tree";
      $("discard").disabled = !state.entry || state.entry.area === "tree";
      $("undo").disabled = !state.project;
      renderDestination();
      if (state.review.active) renderReviewNav();
      if (state.review.active && state.review.pendingFilter) filterReview();
    }
  }

  // ---------- current entry ----------

  function rawUrl(path) {
    return `${config.basePath}/api/projects/${state.project.id}/raw?path=${encodeURIComponent(path)}`;
  }

  function badgeFor(entry) {
    if (!entry) return "—";
    if (entry.kind === "folder") return "FOLDER";
    const dot = entry.name.lastIndexOf(".");
    const extension = dot > 0 ? entry.name.slice(dot + 1).toUpperCase() : "";
    return extension && extension.length <= 5 ? extension : "FILE";
  }

  function previewUrl(kind, path, extra = "") {
    const area = state.entry && state.entry.area === "tree" ? "&area=tree" : "";
    return `${config.basePath}/api/projects/${state.project.id}/${kind}?path=${encodeURIComponent(path)}${area}${extra}`;
  }

  // Show "Loading preview…" until the browser reports the element ready.
  function whenLoaded(pane, node, events) {
    const cover = element("div", "Loading preview…", "loading-cover");
    pane.append(cover);
    const done = () => cover.remove();
    for (const name of events) node.addEventListener(name, done, { once: true });
    return node;
  }

  function toolbar(pane, info, entry, extra = []) {
    const bar = element("div", null, "preview-bar");
    const hint = element("span", "", "hint-text");
    bar.append(hint, ...extra);
    if (info.truncated || (info.sheets || []).some((sheet) => sheet.truncated)) {
      hint.textContent = info.mode === "table" ? "Showing the first rows only." : "Showing the beginning only.";
      if (!info.full) {
        const more = element("button", "LOAD MORE", "tool");
        more.type = "button";
        more.title = "Fetch a much larger part of this file";
        more.addEventListener("click", () =>
          act(async () => {
            const bigger = await api(previewUrl("preview", entry.path).slice(config.basePath.length));
            if (state.entry === entry) {
              entry.preview = { ...bigger, full: true };
              renderPreview(entry);
            }
          }),
        );
        bar.append(more);
      }
    } else if (info.note) {
      hint.textContent = info.note;
    }
    if (hint.textContent || bar.children.length > 1) pane.append(bar);
  }

  function sourceToggle(pane, info, renderRendered) {
    if (info.source === undefined) return [];
    const button = element("button", "SHOW SOURCE", "tool");
    button.type = "button";
    let showingSource = false;
    button.addEventListener("click", () => {
      showingSource = !showingSource;
      button.textContent = showingSource ? "SHOW RENDERED" : "SHOW SOURCE";
      const body = pane.querySelector(".preview-body");
      body.replaceChildren(showingSource ? element("pre", info.source) : renderRendered());
    });
    return [button];
  }

  function inlineMarkdown(text, parent) {
    const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\([^)\s]+\))/g;
    let last = 0;
    let match;
    while ((match = pattern.exec(text))) {
      if (match.index > last) parent.append(text.slice(last, match.index));
      const token = match[0];
      if (match[1]) parent.append(element("code", token.slice(1, -1)));
      else if (match[2]) parent.append(element("strong", token.slice(2, -2)));
      else if (match[3]) parent.append(element("em", token.slice(1, -1)));
      else {
        const split = token.indexOf("](");
        const label = token.slice(1, split);
        const url = token.slice(split + 2, -1);
        if (/^https?:\/\//i.test(url)) {
          const link = element("a", label);
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          parent.append(link);
        } else {
          parent.append(label);
        }
      }
      last = pattern.lastIndex;
    }
    if (last < text.length) parent.append(text.slice(last));
  }

  // A small Markdown subset built from DOM nodes: text is never parsed as HTML.
  function renderMarkdown(source) {
    const root = element("div", null, "markdown");
    const lines = source.split(/\r?\n/);
    let paragraph = [];
    let list = null;
    const flush = () => {
      if (paragraph.length) {
        const node = element("p");
        inlineMarkdown(paragraph.join(" "), node);
        root.append(node);
        paragraph = [];
      }
    };
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      if (/^```/.test(line)) {
        flush();
        list = null;
        const code = [];
        index += 1;
        while (index < lines.length && !/^```/.test(lines[index])) code.push(lines[index++]);
        root.append(element("pre", code.join("\n")));
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      const item = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      const quote = line.match(/^>\s?(.*)$/);
      if (heading) {
        flush();
        list = null;
        const node = element(`h${heading[1].length}`);
        inlineMarkdown(heading[2], node);
        root.append(node);
      } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flush();
        list = null;
        root.append(element("hr"));
      } else if (item) {
        flush();
        const tag = /\d/.test(item[1]) ? "OL" : "UL";
        if (!list || list.tagName !== tag) {
          list = element(tag.toLowerCase());
          root.append(list);
        }
        const node = element("li");
        inlineMarkdown(item[2], node);
        list.append(node);
      } else if (quote) {
        flush();
        list = null;
        const node = element("blockquote");
        inlineMarkdown(quote[1], node);
        root.append(node);
      } else if (!line.trim()) {
        flush();
        list = null;
      } else {
        paragraph.push(line.trim());
      }
    }
    flush();
    return root;
  }

  function renderTable(info) {
    const holder = element("div", null, "table-view");
    const sheets = info.sheets || [];
    const tabs = element("div", null, "sheet-tabs");
    const grid = element("div", null, "table-scroll");
    const show = (index) => {
      [...tabs.children].forEach((tab, at) => tab.setAttribute("aria-selected", String(at === index)));
      const sheet = sheets[index];
      const table = element("table");
      const width = Math.max(1, ...sheet.rows.map((row) => row.length));
      sheet.rows.forEach((row, at) => {
        const tr = element("tr");
        for (let column = 0; column < width; column += 1) {
          tr.append(element(at === 0 ? "th" : "td", row[column] ?? ""));
        }
        (at === 0 ? table.createTHead() : table.tBodies[0] || table.createTBody()).append(tr);
      });
      grid.replaceChildren(sheet.rows.length ? table : element("p", "This sheet is empty.", "empty"));
    };
    if (sheets.length > 1) {
      sheets.forEach((sheet, index) => {
        const tab = element("button", sheet.name, "tool");
        tab.type = "button";
        tab.addEventListener("click", () => show(index));
        tabs.append(tab);
      });
      if (info.more_sheets) tabs.append(element("span", `+${info.more_sheets} more sheets not shown`, "hint-text"));
      holder.append(tabs);
    }
    holder.append(grid);
    if (sheets.length) show(0);
    else grid.append(element("p", "No readable sheets.", "empty"));
    return holder;
  }

  function renderPreview(entry) {
    const pane = $("preview");
    pane.replaceChildren();
    if (!state.project) {
      const start = element("div", null, "empty");
      const button = element("button", "+ CREATE A PROJECT", "tool");
      button.type = "button";
      button.addEventListener("click", openProjectDialog);
      start.append(element("strong", "No project yet."), "A project sorts one dump folder into one tree folder, both inside Personal.", element("br"), element("br"), button);
      pane.append(start);
      return;
    }
    if (!entry) {
      const done = element("p", null, "empty");
      if (state.review.active) done.append("Choose an item from the list, use Previous to go back, or change the search to continue reviewing.");
      else done.append(element("strong", "All sorted."), "This dump has nothing left apart from hidden files and partial downloads.");
      pane.append(done);
      return;
    }
    const info = entry.preview;
    if (info.large && !info.loadLarge) {
      const gate = element("div", null, "empty");
      const button = element("button", `LOAD PREVIEW (${size(entry.size)})`, "tool");
      button.type = "button";
      button.addEventListener("click", () => {
        entry.preview = { ...info, loadLarge: true };
        renderPreview(entry);
      });
      gate.append(element("strong", `Large file: ${size(entry.size)}`), "Not loaded automatically, to keep the queue fast. Decide from the name, or load it.", element("br"), element("br"), button);
      pane.append(gate);
      return;
    }
    const body = element("div", null, "preview-body");
    const raw = previewUrl("raw", entry.path);
    if (info.mode === "pdf") {
      const frame = element("iframe");
      frame.src = raw;
      frame.title = entry.name;
      body.append(whenLoaded(pane, frame, ["load"]));
    } else if (info.mode === "image") {
      const renderImage = () => {
        const image = element("img");
        image.src = raw;
        image.alt = entry.name;
        image.addEventListener("error", () => {
          image.replaceWith(element("p", "This browser cannot display this image (HEIC and TIFF preview in Safari).", "empty"));
        });
        return image;
      };
      toolbar(pane, info, entry, sourceToggle(pane, info, renderImage));
      body.append(whenLoaded(pane, renderImage(), ["load", "error"]));
    } else if (info.mode === "html") {
      const renderPage = () => {
        const frame = element("iframe");
        // No scripts, no same-origin access; the server adds a CSP sandbox too.
        frame.setAttribute("sandbox", "");
        frame.src = raw;
        frame.title = entry.name;
        frame.className = "sandboxed";
        return frame;
      };
      toolbar(pane, info, entry, [element("span", "SANDBOXED · NO SCRIPTS", "badge-note"), ...sourceToggle(pane, info, renderPage)]);
      body.append(whenLoaded(pane, renderPage(), ["load"]));
    } else if (info.mode === "video" || info.mode === "audio") {
      const media = element(info.mode);
      media.controls = true;
      media.preload = "metadata";
      media.src = raw;
      body.append(whenLoaded(pane, media, ["loadedmetadata", "error"]));
    } else if (info.mode === "embedded") {
      const url = previewUrl("embedded", entry.path, `&member=${encodeURIComponent(info.member)}`);
      const picture = info.media_type === "application/pdf" ? element("iframe") : element("img");
      picture.src = url;
      picture.className = "embedded";
      toolbar(pane, info, entry, [element("span", "PREVIEW STORED IN THE FILE", "badge-note")]);
      body.append(whenLoaded(pane, picture, ["load", "error"]));
      if (info.text) {
        const details = element("details", null, "inner-text");
        details.append(element("summary", "Text inside"), element("pre", info.text));
        body.append(details);
      }
    } else if (info.mode === "markdown") {
      const fake = { ...info, source: info.text };
      toolbar(pane, info, entry, sourceToggle(pane, fake, () => renderMarkdown(info.text)));
      body.append(renderMarkdown(info.text));
    } else if (info.mode === "table") {
      toolbar(pane, info, entry);
      body.append(renderTable(info));
    } else if (info.mode === "text") {
      toolbar(pane, info, entry);
      body.append(element("pre", info.text || "(no text found)"));
    } else if (info.mode === "listing") {
      const shown = info.items.length;
      const more = info.more || shown < info.total ? ` — first ${shown} shown` : "";
      body.append(element("p", `${info.total}${info.more ? "+" : ""} item${info.total === 1 ? "" : "s"} inside${more}.${entry.kind === "folder" ? " Moves as one unit." : ""}`, "listing-head"));
      const list = element("ul", null, "listing");
      for (const item of info.items) {
        const row = element("li", null, item.kind);
        row.append(element("span", item.kind === "folder" ? item.path + "/" : item.path), element("span", size(item.size), "size"));
        list.append(row);
      }
      body.append(list);
    } else {
      const none = element("p", null, "empty");
      none.append(element("strong", "No preview for this type."), info.error || "Decide from the name, size and date.");
      body.append(none);
    }
    pane.append(body);
  }

  function renderProgress(progress) {
    if (state.review.active) {
      $("progress-fill").style.width = "0%";
      const groups = state.review.groups.length;
      $("progress").textContent = `${state.review.total} file${state.review.total === 1 ? "" : "s"} in this review${groups ? ` · ${groups} duplicate group${groups === 1 ? "" : "s"}` : ""}`;
      return;
    }
    const done = progress.sorted + progress.discarded;
    const total = done + progress.remaining;
    $("progress-fill").style.width = total ? `${(100 * done) / total}%` : "0";
    const text = $("progress");
    text.replaceChildren();
    const parts = [
      [progress.sorted, "sorted"],
      [progress.discarded, "discarded"],
      [progress.skipped, "skipped"],
      [progress.remaining, "left"],
    ];
    parts.forEach(([count, word], index) => {
      if (index) text.append(" · ");
      text.append(element("b", String(count)), ` ${word}`);
    });
  }

  function renderUpcoming(names) {
    const list = $("upnext");
    list.replaceChildren();
    if (!names.length) list.append(element("span", "nothing else queued", "label"));
    for (const path of names) {
      const button = element("button", path);
      button.type = "button";
      button.title = `Open ${path} now`;
      button.addEventListener("click", () => act(() => load(path)));
      list.append(button);
    }
  }

  function renderEntry(payload) {
    const { entry, progress, upcoming } = payload;
    state.entry = entry;
    state.duplicate = null;
    if (entry && state.review.active) {
      const known = state.review.byPath.get(entry.path);
      if (known && known.copies.some(c => c.path === entry.path && c.size === entry.size && c.mtime_ns === entry.mtime_ns)) state.duplicate = known;
    }
    state.progress = progress;
    const inTree = entry && entry.area === "tree";
    // The dump workflow requires a category; the tree's top level is a
    // destination only for a file already in the tree.
    if (!inTree && state.selected === "") state.selected = null;
    $("duplicate-status").classList.add("hidden");
    renderProgress(progress);
    renderUpcoming(upcoming || []);
    renderPreview(entry);
    $("duplicate").classList.add("hidden");
    $("entry-badge").textContent = badgeFor(entry);
    for (const id of ["skip", "discard"]) $(id).disabled = !entry || inTree || state.busy;
    $("filename").disabled = !entry;
    if (!entry) {
      $("entry-name").textContent = state.project ? "Nothing left to sort" : "Create a project to start";
      $("entry-meta").textContent = "";
      $("filename").value = "";
    } else {
      $("entry-name").textContent = entry.name;
      $("entry-meta").textContent = [
        entry.folder ? `in ${entry.folder}/` : "",
        entry.kind === "folder" ? "folder · moves whole" : size(entry.size),
        `modified ${entry.modified.slice(0, 10)}`,
        entry.skipped ? "skipped before" : "",
      ].filter(Boolean).join(" · ");
      $("filename").value = entry.name.trim();
      if (entry.kind === "file") checkDuplicate(entry.path);
    }
    renderDestination();
  }

  async function checkDuplicate(path, force = false) {
    const checkingEntry = state.entry;
    const status = $("duplicate-status");
    status.replaceChildren("Checking for duplicates…");
    status.classList.remove("hidden");
    try {
      const area = checkingEntry.area || "dump";
      const result = await papi(`/duplicate?area=${area}&scope=${state.review.active ? "tree" : "library"}&path=${encodeURIComponent(path)}${force ? "&force=true" : "&indexed=true"}`);
      if (state.entry !== checkingEntry) return;
      if (state.entry === checkingEntry) status.classList.add("hidden");
      if (!result.skipped) state.duplicate = result.copies && result.copies.length > 1 ? result : null;
      renderDestination();
      if (state.duplicate) {
        $("duplicate-message").textContent = `${state.duplicate.copies.length} copies have identical content. Keep one.`;
        $("duplicate").classList.remove("hidden");
      } else if (result.skipped === "large") {
        const button = element("button", "CHECK NOW", "tool");
        button.type = "button";
        button.addEventListener("click", () => checkDuplicate(path, true));
        status.replaceChildren("Large file: duplicate check skipped. ", button);
        status.classList.remove("hidden");
      } else if (result.complete === false) {
        status.replaceChildren("Index updating: duplicate matches may be incomplete.");
        status.classList.remove("hidden");
      }
    } catch (error) {
      if (state.entry !== checkingEntry) return;
      const retry = element("button", "RETRY", "tool");
      retry.type = "button";
      retry.addEventListener("click", () => checkDuplicate(path, force));
      status.replaceChildren(`Duplicate check failed: ${error.message} `, retry);
      status.classList.remove("hidden");
    }
  }

  // ---------- duplicates and review ----------

  function setDuplicates(result) {
    state.review.groups = result.groups;
    state.review.byPath = new Map();
    for (const group of result.groups) for (const copy of group.copies) state.review.byPath.set(copy.path, group);
    $("recover-banner").classList.toggle("hidden", !result.pending_recovery);
    const count = result.groups.length;
    $("review-badge").textContent = String(count);
    $("review-badge").classList.toggle("hidden", !count);
    $("tab-review").title = count ? `Review sorted files: ${count} duplicate group${count === 1 ? "" : "s"} to resolve (R)` : "Review sorted files (R)";
    if (state.review.active) {
      renderIndexStatus(result.index);
      renderReviewFiles();
    }
  }

  // Keeps the Review badge, its banner and the recovery warning current.
  // Never throws: a stale badge must not fail the action that asked for it.
  // While the index is still filling (first start), it looks again shortly.
  let duplicatesRetry = null;
  async function refreshDuplicates() {
    if (!state.project) return;
    clearTimeout(duplicatesRetry);
    const request = ++state.review.scanRequest;
    const projectId = state.project.id;
    try {
      const result = await papi("/duplicates?scope=tree&indexed=true");
      if (request !== state.review.scanRequest || state.project?.id !== projectId) return;
      setDuplicates(result);
      if (result.complete === false && !document.hidden) duplicatesRetry = setTimeout(() => {
        if (!document.hidden) refreshDuplicates();
      }, 10000);
    } catch (error) {
      if (state.review.active) showError(`Duplicate check incomplete: ${error.message}`, "review-error");
    }
  }

  function reviewMatches(path) {
    const folder = $("review-folder").value;
    return (!folder || ($("review-recursive").checked ? path.startsWith(folder + "/") : path.slice(0, path.lastIndexOf("/")) === folder)) && path.toLowerCase().includes($("review-search").value.trim().toLowerCase());
  }

  function renderDuplicateBanner() {
    const groups = state.review.groups.filter(g => g.copies.some(c => reviewMatches(c.path)));
    $("dup-banner").classList.toggle("hidden", !groups.length);
    $("dup-banner-title").textContent = `${groups.length} group${groups.length === 1 ? "" : "s"} of identical files · choose one copy of each to keep`;
    const list = $("dup-groups");
    list.replaceChildren();
    for (const group of groups) {
      const row = element("li");
      const button = element("button");
      button.type = "button";
      const places = [...new Set(group.copies.map(c => parentOf(c.path) || "top level"))];
      button.append(element("span", group.copies[0].name, "review-file-name"), element("span", `${group.copies.length} copies · ${places.join(" · ")}`, "review-file-path"));
      button.addEventListener("click", () => showDuplicateDialog(group));
      row.append(button);
      list.append(row);
    }
  }

  function renderReviewFiles() {
    renderProgress(state.progress);
    renderDuplicateBanner();
    const list = $("review-list"); list.replaceChildren();
    for (const entry of state.review.entries) {
      const row = element("li"); const button = element("button");
      button.type = "button"; button.setAttribute("aria-current", String(entry.path === state.review.activePath));
      const group = state.review.byPath.get(entry.path);
      const duplicate = group && group.copies.some(c => c.path === entry.path && c.mtime_ns === entry.mtime_ns && c.size === entry.size);
      button.append(element("span", entry.name, "review-file-name"), element("span", entry.folder || "Tree top level", "review-file-path"),
        element("span", duplicate ? `${group.copies.length} IDENTICAL COPIES` : entry.reviewed ? "REVIEWED" : "TO REVIEW", `review-file-status${duplicate ? " duplicate" : ""}`));
      button.addEventListener("click", () => { if (!state.busy) selectReviewFile(entry.path).catch(e => showError(e.message, "review-error")); });
      row.append(button); list.append(row);
    }
    if (!list.children.length) list.append(element("li", "No files match this view.", "none"));
    $("review-summary").textContent = state.review.more ? `${state.review.entries.length} of ${state.review.total} shown · scroll for more` : "";
    renderReviewNav();
  }

  function renderReviewNav() {
    if (!state.review.active) return;
    const items = state.review.entries;
    const index = items.findIndex(e => e.path === state.review.activePath);
    $("review-previous").disabled = state.busy || (index < 0 ? !items.length : index === 0 && !state.review.baseOffset);
    $("review-next").disabled = state.busy || index < 0 || (index >= items.length - 1 && !state.review.more);
    $("review-position").textContent = index >= 0 ? `${index + 1 + state.review.baseOffset} / ${state.review.total}` : items.length ? "Choose a file" : "";
  }

  async function selectReviewFile(path) {
    const request = ++state.review.previewRequest;
    state.review.activePath = path;
    if (!path) {
      renderEntry({entry: null, progress: state.progress, upcoming: []});
      $("entry-name").textContent = state.review.entries.length ? "End of the list: choose a file" : "No files in this view";
      renderReviewFiles(); return;
    }
    renderReviewFiles();
    renderEntry({entry: null, progress: state.progress, upcoming: []});
    $("entry-name").textContent = "Loading preview…";
    $("preview").replaceChildren(element("p", "Loading preview…", "empty"));
    const payload = await papi(`/sorted-entry?path=${encodeURIComponent(path)}`);
    if (request !== state.review.previewRequest || !state.review.active) return;
    state.selected = payload.entry.folder;
    renderEntry({...payload, upcoming: []});
    select(state.selected);
    renderReviewNav();
  }

  async function loadReviewFiles(append = false, selected, pageOffset = 0, preservePreview = false) {
    const request = ++state.review.request;
    const offset = append ? state.review.offset : pageOffset;
    showError("", "review-error");
    const query = new URLSearchParams({search: $("review-search").value.trim(), folder: $("review-folder").value,
      unreviewed: String($("review-unreviewed").checked), recursive: String($("review-recursive").checked), offset: String(offset)});
    if (!append && selected) query.set("anchor", selected);
    state.review.loading = true;
    let result;
    try {
      result = await papi(`/review?${query}`);
    } finally {
      if (request === state.review.request) state.review.loading = false;
    }
    if (request !== state.review.request || !state.review.active) return;
    state.review.entries = append ? [...state.review.entries, ...result.entries] : result.entries;
    if (!append) state.review.baseOffset = result.offset;
    state.review.offset = result.next_offset; state.review.total = result.total; state.review.more = result.more;
    renderIndexStatus(result.index);
    renderReviewFiles();
    if (!append) {
      const path = selected === null ? null : state.review.entries.some(e => e.path === selected) ? selected : state.review.entries[0]?.path;
      if (!preservePreview || state.review.activePath !== (path || null)) await selectReviewFile(path || null);
    }
  }

  async function stepReview(direction) {
    if (state.busy || !state.review.active) return;
    const index = state.review.entries.findIndex(e => e.path === state.review.activePath);
    if (index < 0 && direction < 0 && state.review.entries.length) {
      await selectReviewFile(state.review.entries.at(-1).path);
      return;
    }
    if (direction < 0 && index === 0 && state.review.baseOffset) {
      await loadReviewFiles(false, undefined, Math.max(0, state.review.baseOffset - 500));
      const last = state.review.entries.at(-1);
      if (last) await selectReviewFile(last.path);
      return;
    }
    if (direction > 0 && index === state.review.entries.length - 1 && state.review.more) await loadReviewFiles(true);
    const next = state.review.entries[index + direction];
    if (next) await selectReviewFile(next.path);
  }

  function resetReviewFilters() {
    $("review-search").value = ""; $("review-folder").value = "";
    $("review-unreviewed").checked = false; $("review-recursive").checked = false;
  }

  function renderTabs() {
    $("tab-sort").setAttribute("aria-selected", String(!state.review.active));
    $("tab-review").setAttribute("aria-selected", String(state.review.active));
  }

  // Opens the Review tab, on `path` when given. Filters persist between
  // visits, except when a specific file must be shown.
  async function startReview(path) {
    if (!state.project) return;
    if (path) resetReviewFilters();
    if (state.review.active) {
      await loadReviewFiles(false, path || state.review.activePath);
      return;
    }
    state.review.active = true;
    renderTabs();
    document.querySelector(".workspace").classList.add("reviewing");
    for (const id of ["review-browser", "review-nav"]) $(id).classList.remove("hidden");
    $("dump-upnext").classList.add("hidden");
    await loadFolders();
    const folder = $("review-folder").value;
    $("review-folder").replaceChildren();
    for (const value of ["", ...state.folders.map(f => f.path)]) {
      const option = element("option", value || "All folders"); option.value = value; $("review-folder").append(option);
    }
    $("review-folder").value = state.byPath.has(folder) ? folder : "";
    state.selected = null;
    await refreshDuplicates();
    await loadReviewFiles(false, path);
    pollIndex();
  }

  function leaveReview() {
    clearTimeout(indexPollTimer);
    state.review.active = false; state.review.previewRequest++; state.review.request++;
    renderTabs();
    document.querySelector(".workspace").classList.remove("reviewing");
    for (const id of ["review-browser", "review-nav"]) $(id).classList.add("hidden");
    $("dump-upnext").classList.remove("hidden");
  }

  function showTab(review) {
    if (!state.project || review === state.review.active) return;
    act(async () => {
      if (review) {
        await startReview();
      } else {
        leaveReview();
        await load();
      }
    });
  }

  function showDuplicateDialog(group) {
    if (!group || state.busy) return;
    showError("", "duplicates-error");
    $("duplicate-note").textContent = `${group.copies.length} copies have identical content${group.scope === "tree" ? " inside the sorted tree" : ""}. Pick the one to keep: the others move to _discarded, and Undo restores them all.`;
    $("duplicate-advanced").open = false;
    chooseDuplicateGroup(group);
    $("duplicates-dialog").showModal();
    $("duplicate-copies").querySelector('[aria-checked="true"]')?.focus();
  }

  function modifiedOf(copy) {
    const date = new Date(Number(copy.mtime_ns) / 1e6);
    return Number.isNaN(date.getTime()) ? "" : ` · modified ${date.toISOString().slice(0, 10)}`;
  }

  function chooseDuplicateGroup(group) {
    state.duplicateGroup = group;
    state.duplicateKeeper = Math.max(0, group.copies.findIndex(c => c.area === "tree"));
    const folder = $("duplicate-folder");
    folder.replaceChildren();
    for (const path of ["", ...state.folders.map(f => f.path)]) {
      const option = element("option", path || "Tree top level");
      option.value = path;
      folder.append(option);
    }
    const names = $("duplicate-names");
    names.replaceChildren();
    for (const name of new Set(group.copies.map(c => c.name))) {
      const chip = element("button", name, "tool");
      chip.type = "button";
      chip.title = "Use this name";
      chip.addEventListener("click", () => { $("duplicate-filename").value = name.trim(); renderDuplicateOutcome(); });
      names.append(chip);
    }
    if (names.children.length < 2) names.replaceChildren();
    const cards = $("duplicate-copies");
    cards.replaceChildren();
    group.copies.forEach((copy, index) => {
      const card = element("button", null, "copy-card");
      card.type = "button";
      card.setAttribute("role", "radio");
      const base = copy.area === "tree" ? state.project.target : state.project.source;
      const folderPath = parentOf(copy.path);
      card.append(
        element("span", copy.area === "tree" ? "IN THE TREE" : "IN THE DUMP", "copy-area"),
        element("span", copy.name, "copy-name"),
        element("span", `${base}/${folderPath ? folderPath + "/" : ""}`, "copy-path"),
        element("span", `${size(copy.size)}${modifiedOf(copy)}`, "copy-meta"),
      );
      card.addEventListener("click", () => {
        state.duplicateKeeper = index;
        defaultDuplicateChoice();
      });
      // Double-click is "keep this one, now".
      card.addEventListener("dblclick", () => $("duplicates-form").requestSubmit());
      cards.append(card);
    });
    defaultDuplicateChoice();
  }

  function defaultDuplicateChoice() {
    [...$("duplicate-copies").children].forEach((card, index) => card.setAttribute("aria-checked", String(index === state.duplicateKeeper)));
    const copy = state.duplicateGroup.copies[state.duplicateKeeper];
    const folder = copy.area === "tree" ? parentOf(copy.path) : (state.selected || "");
    $("duplicate-folder").value = state.byPath.has(folder) ? folder : "";
    $("duplicate-filename").value = copy.name.trim();
    renderDuplicateOutcome();
  }

  function renderDuplicateOutcome() {
    if (!state.duplicateGroup) return;
    const folder = $("duplicate-folder").value;
    const filename = $("duplicate-filename").value.trim();
    const extras = state.duplicateGroup.copies.length - 1;
    $("duplicate-outcome").textContent = `Keeps ${state.project.target}/${folder ? folder + "/" : ""}${filename}. Moves ${extras} other ${extras === 1 ? "copy" : "copies"} to _discarded.`;
  }

  async function resolveDuplicateGroup(event) {
    event.preventDefault();
    if (!state.duplicateGroup || state.busy) return;
    state.busy = true;
    $("duplicate-submit").disabled = true;
    $("duplicates-close").disabled = true;
    showError("", "duplicates-error");
    try {
      const group = state.duplicateGroup;
      const result = await papi("/duplicates/resolve", {method: "POST", timeout: LONG_REQUEST_TIMEOUT_MS, body: JSON.stringify({
        sha256: group.sha256,
        copies: group.copies.map(c => ({...guard(c), area: c.area})),
        keeper: state.duplicateKeeper, folder: $("duplicate-folder").value,
        filename: $("duplicate-filename").value.trim(),
        scope: group.scope || "library",
      })});
      $("duplicates-dialog").close();
      await loadFolders();
      await refreshDuplicates();
      if (state.review.active) {
        // Resolved from the file on screen: move on past the group. Resolved
        // from the banner: stay where the owner was.
        const active = state.review.activePath;
        const index = state.review.entries.findIndex(e => e.path === active);
        const inGroup = group.copies.some(c => c.path === active);
        const next = state.review.entries.slice(index + 1).find(e => !group.copies.some(c => c.path === e.path));
        await loadReviewFiles(false, inGroup ? (next ? next.path : null) : active);
      } else await load();
      toast(`Kept ${result.destination}; ${result.discarded} duplicate ${result.discarded === 1 ? "copy" : "copies"} discarded.`, result);
    } catch (error) {
      showError(error.message, "duplicates-error");
    } finally {
      state.busy = false;
      $("duplicate-submit").disabled = false;
      $("duplicates-close").disabled = false;
      renderDestination();
      if (state.review.active) renderReviewNav();
    }
  }

  async function load(path) {
    showError("");
    if (!state.project) {
      renderEntry({ entry: null, progress: { sorted: 0, discarded: 0, skipped: 0, remaining: 0 }, upcoming: [] });
      return;
    }
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    renderEntry(await papi(`/current${query}`));
  }

  // ---------- destination ----------

  function renderDestination() {
    const crumbs = $("crumbs");
    crumbs.replaceChildren();
    const chosen = state.selected;
    const hasChoice = chosen !== null;
    $("destination-block").classList.toggle("chosen", hasChoice);
    if (!hasChoice) {
      crumbs.append(element("span", "No folder chosen · N creates at the top level", "none"));
    } else {
      // In Review the top level is a real destination; in Sort it clears the
      // choice, as Esc does.
      const root = element("button", "Top level", "root");
      root.type = "button";
      root.title = state.review.active ? "Keep it at the tree's top level" : "Clear the choice (Esc)";
      if (chosen === "") root.setAttribute("aria-current", "true");
      root.addEventListener("click", () => select(state.review.active ? "" : null));
      crumbs.append(root);
      const parts = chosen ? chosen.split("/") : [];
      parts.forEach((part, index) => {
        crumbs.append(element("span", "/", "sep"));
        const button = element("button", part);
        button.type = "button";
        button.title = "Choose this level instead";
        const path = parts.slice(0, index + 1).join("/");
        button.addEventListener("click", () => select(path));
        crumbs.append(button);
      });
    }
    const filename = $("filename").value.trim();
    const destination = `${chosen ? chosen + "/" : ""}${filename}`;
    $("final-path").textContent = hasChoice && filename ? `→ ${destination}` : "";
    if (state.review.active) {
      const group = state.duplicate;
      $("classify").disabled = state.busy || !state.entry || (!group && (!hasChoice || !filename));
      $("classify-label").textContent = !state.entry ? "CHOOSE A FILE" : group ? "CHOOSE ONE COPY TO KEEP" : destination === state.entry?.path ? "CONFIRM · NEXT" : hasChoice ? `MOVE · NEXT → ${leafOf(chosen) || "TOP LEVEL"}` : "CHOOSE A FOLDER";
      return;
    }
    const ready = Boolean(state.entry && !state.duplicate && chosen && filename);
    // While an action runs, the button must not look pressable: act() would
    // refuse the press (the 0.4.0 and 0.8.0 silent no-ops).
    $("classify").disabled = !ready || state.busy;
    $("classify-label").textContent = chosen ? `CLASSIFY → ${leafOf(chosen)}` : "CHOOSE A FOLDER";
  }

  // ---------- folder tree ----------

  function indexFolders(folders) {
    state.folders = folders;
    state.byPath = new Map(folders.map((folder) => [folder.path, folder]));
    state.children = new Map([["", []]]);
    for (const folder of folders) {
      state.children.set(folder.path, state.children.get(folder.path) || []);
      const parent = parentOf(folder.path);
      if (!state.children.has(parent)) state.children.set(parent, []);
      state.children.get(parent).push(folder.path);
    }
  }

  async function loadFolders() {
    indexFolders(state.project ? (await papi("/folders")).folders : []);
    if (state.firstLoad) {
      state.children.get("").forEach((path) => state.expanded.add(path));
      state.firstLoad = false;
      store(expandedKey(), [...state.expanded]);
    }
    if (state.selected && !state.byPath.has(state.selected)) state.selected = null;
    renderTree();
    renderDestination();
  }

  // The folder filter. "/school/eco" walks the tree from the top like a
  // shell: the last segment narrows that folder's subfolders by prefix, and
  // "/school/" lists them all. Without the leading "/", or when no path
  // matches, the text matches anywhere in a folder's path.
  function folderMatches() {
    const raw = $("search").value.trim();
    if (!raw) return null;
    let text = raw.toLowerCase();
    if (raw.startsWith("/")) {
      const typed = raw.replace(/^\/+/, "").replace(/\/{2,}/g, "/");
      const cut = typed.lastIndexOf("/");
      const base = typed.slice(0, Math.max(cut, 0)).toLowerCase();
      const leaf = typed.slice(cut + 1);
      const partial = leaf.toLowerCase();
      const hits = state.folders.map((folder) => folder.path).filter((path) =>
        parentOf(path).toLowerCase() === base && leafOf(path).toLowerCase().startsWith(partial));
      if (hits.length || !typed) return { hits, prefix: true, base, leaf, partial };
      text = typed.toLowerCase().replace(/\/$/, "");
    }
    const hits = state.folders.map((folder) => folder.path).filter((path) => path.toLowerCase().includes(text));
    return { hits, prefix: false, term: text };
  }

  // What Enter in the filter chooses: the exact path when typed in full,
  // the folder itself for "/school/", otherwise the first match.
  function searchChoice() {
    const matches = folderMatches();
    if (!matches) return null;
    if (matches.prefix) {
      const exact = matches.hits.find((path) => leafOf(path) === matches.leaf) ||
        matches.hits.find((path) => leafOf(path).toLowerCase() === matches.partial);
      if (exact) return exact;
      if (!matches.partial && matches.base) {
        return state.folders.find((folder) => folder.path.toLowerCase() === matches.base)?.path ?? null;
      }
    }
    return matches.hits[0] ?? null;
  }

  // ↑ ↓ in the filter move a cursor through the matches, in tree order, so
  // similar names ("slides", "Epicall slides") are one keypress apart.
  // Enter takes the cursor; typing resets it to the best match.
  function searchCursor() {
    const matches = folderMatches();
    if (!matches) return null;
    return matches.hits.includes(state.searchCursor) ? state.searchCursor : searchChoice();
  }

  function moveSearchCursor(delta) {
    const matches = folderMatches();
    if (!matches || !matches.hits.length) return;
    const hits = new Set(matches.hits);
    const order = visibleRows().filter((path) => hits.has(path));
    const at = order.indexOf(searchCursor());
    state.searchCursor = order[at < 0 ? 0 : Math.min(order.length - 1, Math.max(0, at + delta))];
    renderTree();
    $("tree").querySelector(`li[data-path="${CSS.escape(state.searchCursor)}"]`)?.scrollIntoView({ block: "nearest" });
  }

  // Tab completes the last path segment as far as the matches agree.
  function completeSearch() {
    const matches = folderMatches();
    if (!matches || !matches.prefix || !matches.hits.length) return false;
    const leaves = matches.hits.map(leafOf);
    let common = leaves[0];
    for (const leaf of leaves) {
      while (!leaf.toLowerCase().startsWith(common.toLowerCase())) common = common.slice(0, -1);
    }
    const parent = parentOf(matches.hits[0]);
    let value = `/${parent ? parent + "/" : ""}${common}`;
    if (matches.hits.length === 1 && (state.children.get(matches.hits[0]) || []).length) value += "/";
    $("search").value = value;
    state.searchCursor = null;
    renderTree();
    return true;
  }

  function visibleRows() {
    const matches = folderMatches();
    if (matches) {
      const keep = new Set();
      for (let path of matches.hits) {
        while (path) {
          keep.add(path);
          path = parentOf(path);
        }
      }
      return state.folders.filter((folder) => keep.has(folder.path)).map((folder) => folder.path);
    }
    const rows = [];
    const walk = (parent) => {
      for (const path of state.children.get(parent) || []) {
        rows.push(path);
        if (state.expanded.has(path)) walk(path);
      }
    };
    walk("");
    return rows;
  }

  function highlighted(name, filter) {
    const span = element("span", null, "name");
    const at = filter ? name.toLowerCase().indexOf(filter) : -1;
    if (at < 0) {
      span.textContent = name;
    } else {
      span.append(name.slice(0, at), element("mark", name.slice(at, at + filter.length)), name.slice(at + filter.length));
    }
    return span;
  }

  function renderTree() {
    const tree = $("tree");
    tree.replaceChildren();
    const matches = folderMatches();
    const hits = new Set(matches ? matches.hits : []);
    const cursor = searchCursor();
    const rows = visibleRows();
    for (const path of rows) {
      const folder = state.byPath.get(path);
      const hasChildren = (state.children.get(path) || []).length > 0;
      const open = matches ? true : state.expanded.has(path);
      const row = element("li");
      row.setAttribute("role", "treeitem");
      row.setAttribute("aria-level", String(depthOf(path) + 1));
      row.setAttribute("aria-selected", String(path === state.selected));
      if (path === cursor) row.classList.add("search-cursor");
      if (hasChildren) row.setAttribute("aria-expanded", String(open));
      row.dataset.path = path;
      row.style.paddingLeft = `${4 + depthOf(path) * 16}px`;
      row.title = folder.description || path;

      const caret = element("button", hasChildren ? (open ? "▾" : "▸") : "", "caret");
      caret.type = "button";
      caret.tabIndex = -1;
      if (hasChildren) {
        caret.setAttribute("aria-label", open ? "Collapse" : "Expand");
        caret.addEventListener("click", (event) => {
          event.stopPropagation();
          toggle(path);
        });
      }
      row.append(caret);

      const term = !matches ? "" : matches.prefix ? (hits.has(path) ? matches.partial : "") : matches.term;
      row.append(highlighted(leafOf(path), term));
      if (folder.subfolders > 12) {
        const crowded = element("span", `${folder.subfolders} SUB`, "crowded");
        crowded.title = `${folder.subfolders} subfolders: consider grouping some (⋯ → Group with siblings)`;
        row.append(crowded);
      }
      if (folder.items) row.append(element("span", String(folder.items), "count"));
      const more = element("button", "⋯", "row-more");
      more.type = "button";
      more.tabIndex = -1;
      more.title = "Folder actions";
      more.setAttribute("aria-label", `Actions for ${path}`);
      more.setAttribute("aria-haspopup", "menu");
      more.addEventListener("click", (event) => {
        event.stopPropagation();
        openRowMenu(path, more);
      });
      row.append(more);

      row.addEventListener("click", () => select(path));
      row.addEventListener("contextmenu", (event) => {
        event.preventDefault();
        openRowMenu(path, more);
      });
      tree.append(row);
    }
    if (!rows.length) {
      tree.append(element("li", state.folders.length ? "No matching folder" : "No folders yet: press + NEW", "none"));
    }
  }

  function toggle(path, open) {
    const next = open === undefined ? !state.expanded.has(path) : open;
    if (next) state.expanded.add(path);
    else state.expanded.delete(path);
    store(expandedKey(), [...state.expanded]);
    renderTree();
  }

  function select(path) {
    state.selected = path;
    if (path) {
      let parent = parentOf(path);
      while (parent) {
        state.expanded.add(parent);
        parent = parentOf(parent);
      }
      store(expandedKey(), [...state.expanded]);
    }
    renderTree();
    renderDestination();
    const row = path && $("tree").querySelector(`li[data-path="${CSS.escape(path)}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
  }

  function step(delta) {
    const rows = visibleRows();
    if (!rows.length) return;
    const at = rows.indexOf(state.selected);
    const next = at < 0 ? (delta > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, at + delta));
    select(rows[next]);
  }

  function stepSideways(right) {
    const path = state.selected;
    if (!path) return step(1);
    const children = state.children.get(path) || [];
    if (right) {
      if (children.length && !state.expanded.has(path)) toggle(path, true);
      else if (children.length) select(children[0]);
    } else if (children.length && state.expanded.has(path)) {
      toggle(path, false);
    } else if (parentOf(path)) {
      select(parentOf(path));
    }
  }

  function followMoves(pairs) {
    // Keep the owner's view stable when folders move under it.
    for (const [from, to] of pairs) {
      if (state.selected) state.selected = movedPath(state.selected, from, to);
      state.expanded = new Set([...state.expanded].map((path) => movedPath(path, from, to)));
    }
    store(expandedKey(), [...state.expanded]);
  }

  // ---------- entry actions ----------

  function guard(entry) {
    return { path: entry.path, size: entry.size, mtime_ns: entry.mtime_ns };
  }

  // Decide from state, not from the button: act() disables every action
  // button while it runs, so the button itself always reads as disabled.
  const canClassify = () => {
    const filename = $("filename").value.trim();
    return Boolean(state.entry && !state.duplicate && state.selected !== null && filename &&
      (state.review.active || state.selected !== ""));
  };

  const classify = () => {
    if (state.review.active && state.duplicate && !state.busy) {
      showDuplicateDialog(state.duplicate);
      return;
    }
    if (!canClassify()) return;
    act(async () => {
      const folder = state.selected;
      const name = state.entry.name;
      if (state.review.active) {
        const previousPath = state.entry.path;
        const index = state.review.entries.findIndex(e => e.path === previousPath);
        if (index === state.review.entries.length - 1 && state.review.more) await loadReviewFiles(true);
        const next = state.review.entries[index + 1]?.path || null;
        const destination = `${folder ? folder + "/" : ""}${$("filename").value.trim()}`;
        const same = destination === previousPath;
        const body = same ? guard(state.entry) : {...guard(state.entry), folder, filename: $("filename").value.trim(), review: true};
        const result = await papi(same ? "/review/accept" : "/reclassify", {method: "POST", body: JSON.stringify(body)});
        if (!same) await Promise.all([loadFolders(), refreshDuplicates()]);
        await loadReviewFiles(false, next);
        toast(same ? `Confirmed ${previousPath}` : `Moved “${name.trim()}” → ${result.destination}`, result);
        return;
      }
      const result = await papi("/classify", {
        method: "POST",
        body: JSON.stringify({ ...guard(state.entry), folder, filename: $("filename").value.trim() }),
      });
      await Promise.all([load(), loadFolders(), refreshDuplicates()]);
      toast(`Moved “${name.trim()}” → ${result.destination}`, result);
    });
  };

  const skip = () => act(async () => {
    if (!state.entry || state.entry.area === "tree") return;
    const name = state.entry.name;
    await papi("/skip", { method: "POST", body: JSON.stringify({ path: state.entry.path }) });
    await load();
    toast(`Skipped “${name.trim()}”. It comes back after the rest.`, false);
  });

  const discard = () => act(async () => {
    if (!state.entry || state.entry.area === "tree") return;
    const name = state.entry.name;
    const result = await papi("/discard", { method: "POST", body: JSON.stringify(guard(state.entry)) });
    await load();
    toast(`Discarded “${name.trim()}” → ${result.destination}`, result);
  });

  const undo = (expectDecisionId = null) => act(async () => {
    const result = await papi("/undo", {
      method: "POST",
      // Undoing a KEEP ONE group re-reads every copy before moving it back.
      timeout: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ expect_decision_id: expectDecisionId }),
    });
    await Promise.all([loadFolders(), refreshDuplicates()]);
    const restored = result.restored_copies ? `Restored ${result.restored_copies} original copies` : null;
    if (result.area === "tree") {
      // A file returning to a sorted location is shown in Review, where tree
      // files are handled; the Sort tab only ever shows the dump.
      await startReview(result.path);
      toast(restored || `Restored ${result.path}`, false);
    } else {
      if (state.review.active) leaveReview();
      await load(result.path);
      toast(restored || `Restored “${result.path.trim()}” to the dump`, false);
    }
  });

  // ---------- folder menu ----------

  const MENUS = ["row-menu", "more-menu", "project-menu"];

  function openMenu() {
    return MENUS.map($).find((menu) => !menu.classList.contains("hidden")) || null;
  }

  function closeMenus() {
    $("row-menu").classList.add("hidden");
    $("more-menu").classList.add("hidden");
    $("more-button").setAttribute("aria-expanded", "false");
    closeProjectMenu();
  }

  function menuItems(menu) {
    return [...menu.querySelectorAll('[role="menuitem"]')].filter((item) => !item.disabled && !item.classList.contains("hidden"));
  }

  function openRowMenu(path, anchor) {
    if (!state.project) return;
    closeMenus();
    state.menuPath = path;
    const menu = $("row-menu");
    menu.classList.remove("hidden");
    const box = anchor.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(box.right - width, innerWidth - width - 8))}px`;
    menu.style.top = `${box.bottom + 4 + height > innerHeight ? Math.max(8, box.top - height - 4) : box.bottom + 4}px`;
    menuItems(menu)[0]?.focus();
  }

  function openSelectedMenu() {
    const row = state.selected && $("tree").querySelector(`li[data-path="${CSS.escape(state.selected)}"]`);
    if (row) openRowMenu(state.selected, row.querySelector(".row-more"));
  }

  function rowAction(work) {
    const path = state.menuPath;
    closeMenus();
    if (path) work(path);
  }

  function renderSiblingPicks() {
    const holder = $("folder-siblings");
    holder.replaceChildren(element("p", "Folders to put inside the new one", "label"));
    for (const path of state.children.get(parentOf(state.folderTarget)) || []) {
      const label = element("label", null, "toggle");
      const box = element("input");
      box.type = "checkbox";
      box.checked = state.groupPicks.has(path);
      box.addEventListener("change", () => {
        if (box.checked) state.groupPicks.add(path);
        else state.groupPicks.delete(path);
        renderFolderPreview();
      });
      label.append(box, leafOf(path));
      holder.append(label);
    }
  }

  // `path` is the folder the dialog acts on: the parent for "new", the
  // folder itself for "rename", and the first pick for "group".
  function openFolderDialog(mode, path = state.selected || "") {
    if (!state.project) return;
    state.dialogMode = mode;
    state.folderTarget = path;
    showError("", "folder-error");
    const name = $("folder-name");
    const description = $("folder-description");
    description.value = "";
    $("folder-description-field").classList.toggle("hidden", mode === "rename");
    $("folder-siblings").classList.toggle("hidden", mode !== "group");
    if (mode === "new") {
      $("folder-dialog-kicker").textContent = "NEW FOLDER";
      $("folder-dialog-note").textContent = path ? "Creates inside the chosen folder. For a top-level folder, press Esc before N." : "Creates a top-level category.";
      $("folder-submit").textContent = "CREATE";
      name.value = "";
    } else if (mode === "group") {
      const parent = parentOf(path);
      state.groupPicks = new Set([path]);
      $("folder-dialog-kicker").textContent = "INSERT A FOLDER LEVEL";
      $("folder-dialog-title").textContent = `Group folders in ${parent || "the top level"}`;
      $("folder-dialog-note").textContent = "The ticked folders move inside a new folder, with everything in them. Labels already given follow automatically.";
      $("folder-submit").textContent = "GROUP";
      name.value = "";
      renderSiblingPicks();
    } else {
      $("folder-dialog-kicker").textContent = "RENAME FOLDER";
      $("folder-dialog-title").textContent = `Rename ${path}`;
      $("folder-dialog-note").textContent = "Everything inside keeps its place. Labels already given follow automatically.";
      $("folder-submit").textContent = "RENAME";
      name.value = leafOf(path);
    }
    renderFolderPreview();
    $("folder-dialog").showModal();
    name.focus();
    name.select();
  }

  function renderFolderPreview() {
    const list = $("folder-dialog-preview");
    list.replaceChildren();
    const name = $("folder-name").value.trim() || "…";
    const line = (prefix, bold, suffix) => {
      const item = element("li");
      item.append(prefix, element("b", bold), suffix);
      list.append(item);
    };
    const path = state.folderTarget;
    if (state.dialogMode === "new") {
      $("folder-dialog-title").textContent = `New folder in ${path || "the top level"}`;
      line(`${state.project.target}/${path ? path + "/" : ""}`, name, "");
    } else if (state.dialogMode === "group") {
      const parent = parentOf(path);
      for (const pick of groupPaths()) line(parent ? `${parent}/` : "", name, `/${leafOf(pick)}`);
      if (!state.groupPicks.size) list.append(element("li", "Tick at least one folder", "none"));
    } else if (state.dialogMode === "rename") {
      line(parentOf(path) ? `${parentOf(path)}/` : "", name, "");
    }
  }

  // Picks in the tree's own order, so the preview and the request agree.
  function groupPaths() {
    return (state.children.get(parentOf(state.folderTarget)) || []).filter((path) => state.groupPicks.has(path));
  }

  async function submitFolderDialog() {
    const name = $("folder-name").value.trim();
    const description = $("folder-description").value.trim();
    if (!name) return;
    try {
      if (state.dialogMode === "new") {
        const created = await papi("/folders", {
          method: "POST",
          body: JSON.stringify({ parent: state.folderTarget || "", name, description }),
        });
        $("folder-dialog").close();
        await loadFolders();
        select(created.path);
        toast(`Created ${created.path}`, false);
      } else if (state.dialogMode === "group") {
        const paths = groupPaths();
        if (!paths.length) return showError("Tick at least one folder to group.", "folder-error");
        const result = await papi("/folders/group", {
          method: "POST",
          body: JSON.stringify({ paths, name, description }),
        });
        $("folder-dialog").close();
        followMoves(paths.map((path, index) => [path, result.moved[index]]));
        state.expanded.add(result.path);
        await loadFolders();
        toast(`Grouped ${paths.length} folder${paths.length === 1 ? "" : "s"} into ${result.path}`, false);
      } else {
        const path = state.folderTarget;
        const result = await papi("/folders/move", {
          method: "POST",
          body: JSON.stringify({ path, parent: parentOf(path), name }),
        });
        $("folder-dialog").close();
        followMoves([[path, result.path]]);
        await loadFolders();
        toast(`Renamed to ${result.path}`, false);
      }
    } catch (error) {
      showError(error.message, "folder-error");
      // A partial group still changed the tree; show the truth behind the dialog.
      await loadFolders().catch(() => {});
    }
  }

  function openMoveDialog(path) {
    state.moveSource = path;
    state.moveTarget = null;
    $("move-title").textContent = `Move ${path} to…`;
    $("move-search").value = "";
    $("move-submit").disabled = true;
    showError("", "move-error");
    renderMoveTree();
    $("move-dialog").showModal();
    $("move-search").focus();
  }

  function renderMoveTree() {
    const moving = state.moveSource;
    const filter = $("move-search").value.trim().toLowerCase();
    const list = $("move-tree");
    list.replaceChildren();
    const current = parentOf(moving);
    const options = [""].concat(
      state.folders
        .map((folder) => folder.path)
        .filter((path) => path !== moving && !path.startsWith(moving + "/")),
    );
    for (const path of options) {
      if (filter && !(path || "top level").toLowerCase().includes(filter)) continue;
      // The current parent stays visible so the tree keeps its shape.
      const here = path === current;
      const row = element("li", null, here ? "none" : "");
      row.style.paddingLeft = `${8 + (path ? depthOf(path) * 16 : 0)}px`;
      row.setAttribute("aria-selected", String(path === state.moveTarget));
      row.append(element("span", path ? (filter ? path : leafOf(path)) : "(top level)", "name"));
      if (here) {
        row.append(element("span", "current", "count"));
      } else {
        row.addEventListener("click", () => {
          state.moveTarget = path;
          $("move-submit").disabled = false;
          renderMoveTree();
        });
      }
      list.append(row);
    }
  }

  async function submitMove() {
    if (state.moveTarget === null) return;
    const path = state.moveSource;
    try {
      const result = await papi("/folders/move", {
        method: "POST",
        body: JSON.stringify({ path, parent: state.moveTarget, name: leafOf(path) }),
      });
      $("move-dialog").close();
      followMoves([[path, result.path]]);
      if (state.moveTarget) state.expanded.add(state.moveTarget);
      await loadFolders();
      toast(`Moved folder to ${result.path}`, false);
    } catch (error) {
      showError(error.message, "move-error");
    }
  }

  // ---------- projects ----------

  function routeText(project) {
    return `${project.source} → ${project.target} · ${project.mode === "files" ? "every file" : "top level"}`;
  }

  function renderProjectButton() {
    const project = state.project;
    $("project-name").textContent = project ? project.name : "NO PROJECT";
    $("project-route").textContent = project ? routeText(project) : "create one to start";
    $("project-rename").disabled = !project;
    $("project-archive").disabled = !project;
  }

  function renderProjectMenu() {
    const list = $("project-list");
    list.replaceChildren();
    for (const project of state.projects) {
      const item = element("li");
      const button = element("button");
      button.type = "button";
      button.setAttribute("aria-current", String(Boolean(state.project && project.id === state.project.id)));
      const left = project.remaining === null ? "" : ` · ${project.remaining} left`;
      button.append(
        element("span", project.name, "title"),
        element("span", routeText(project), "route"),
        element(
          "span",
          project.status === "ok" ? `${project.sorted} sorted · ${project.discarded} discarded${left}` : "folders missing",
          project.status === "ok" ? "stats" : "stats missing",
        ),
      );
      button.addEventListener("click", () => {
        closeProjectMenu();
        act(() => chooseProject(project.id));
      });
      item.append(button);
      list.append(item);
    }
    if (!state.projects.length) list.append(element("li", "No projects yet", "stats"));
  }

  function closeProjectMenu() {
    $("project-menu").classList.add("hidden");
    $("project-button").setAttribute("aria-expanded", "false");
  }

  async function loadProjects() {
    state.projects = (await api("/api/projects")).projects;
  }

  async function chooseProject(id) {
    // A notification belongs to the project that raised it.
    $("toast").classList.add("hidden");
    state.toastUndo = null;
    leaveReview();
    await loadProjects();
    const project = state.projects.find((item) => item.id === id) || state.projects[0] || null;
    state.project = project;
    if (project) {
      localStorage.setItem(PROJECT_KEY, String(project.id));
      history.replaceState(null, "", `#p=${project.id}`);
    }
    state.selected = null;
    closeMenus();
    resetReviewFilters();
    setDuplicates({ groups: [], pending_recovery: false });
    const saved = stored(expandedKey(), null);
    state.expanded = new Set(saved || []);
    state.firstLoad = saved === null;
    $("search").value = "";
    renderProjectButton();
    for (const id of ["new-folder", "undo", "tab-review", "more-rescan", "index-verify"]) {
      $(id).disabled = !project || (id === "undo" && state.busy);
    }
    $("export").classList.toggle("hidden", !project);
    $("export-log").classList.toggle("hidden", !project);
    if (project) $("export").href = `${config.basePath}/api/projects/${project.id}/labels.jsonl`;
    if (project) $("export-log").href = `${config.basePath}/api/projects/${project.id}/decision-log.jsonl`;
    await Promise.all([load(), loadFolders(), refreshDuplicates()]);
  }

  async function renameProject() {
    const name = window.prompt("Rename this project", state.project.name);
    if (!name || !name.trim()) return;
    await api(`/api/projects/${state.project.id}`, { method: "PATCH", body: JSON.stringify({ name: name.trim() }) });
    await chooseProject(state.project.id);
  }

  async function archiveProject() {
    const ok = window.confirm(
      `Archive “${state.project.name}”? Files stay where they are and its labels stay in the tree's export; only the project shortcut is removed.`,
    );
    if (!ok) return;
    await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
    localStorage.removeItem(PROJECT_KEY);
    await chooseProject(null);
  }

  // ---------- new-project dialog ----------

  function openProjectDialog() {
    closeProjectMenu();
    state.browse = { source: { path: "", picked: null }, target: { path: "", picked: null, create: false } };
    $("project-name-input").value = "";
    $("new-tree-name").value = "";
    showError("", "project-error");
    renderPicked();
    browse("source", "");
    browse("target", "");
    $("project-dialog").showModal();
    $("project-name-input").focus();
  }

  function renderPicked() {
    for (const side of ["source", "target"]) {
      const pick = state.browse[side];
      const label = $(`${side}-picked`);
      label.textContent = pick.picked ? `${pick.picked}${pick.create ? "  (new)" : ""}` : "Choose a folder";
      label.classList.toggle("unset", !pick.picked);
    }
  }

  async function browse(side, path) {
    const result = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    state.browse[side].path = result.path;
    const crumbs = $(`${side}-crumbs`);
    crumbs.replaceChildren();
    const home = element("button", "Personal");
    home.type = "button";
    home.addEventListener("click", () => browse(side, ""));
    crumbs.append(home);
    const parts = result.path ? result.path.split("/") : [];
    parts.forEach((part, index) => {
      crumbs.append(element("span", "/", "sep"));
      const button = element("button", part);
      button.type = "button";
      button.addEventListener("click", () => browse(side, parts.slice(0, index + 1).join("/")));
      crumbs.append(button);
    });
    const list = $(`${side}-browse`);
    list.replaceChildren();
    for (const folder of result.folders) {
      const row = element("li");
      row.setAttribute("aria-selected", String(state.browse[side].picked === folder.path));
      row.title = `${folder.entries} entries, ${folder.folders} folders`;
      row.append(element("span", folder.name, "name"));
      if (folder.role) row.append(element("span", folder.role.toUpperCase(), "role"));
      row.append(element("span", String(folder.entries), "count"));
      if (folder.folders) {
        const open = element("button", "OPEN ›", "open");
        open.type = "button";
        open.addEventListener("click", (event) => {
          event.stopPropagation();
          browse(side, folder.path);
        });
        row.append(open);
      }
      row.addEventListener("click", () => {
        state.browse[side] = { ...state.browse[side], picked: folder.path, create: false };
        renderPicked();
        for (const other of list.children) other.setAttribute("aria-selected", "false");
        row.setAttribute("aria-selected", "true");
        if (side === "source" && !$("project-name-input").value.trim()) {
          $("project-name-input").value = folder.name;
        }
      });
      list.append(row);
    }
    if (!result.folders.length) list.append(element("li", "No folders here", "none"));
  }

  function useNewTree() {
    const name = $("new-tree-name").value.trim();
    if (!name) return;
    const base = state.browse.target.path;
    state.browse.target = { ...state.browse.target, picked: base ? `${base}/${name}` : name, create: true };
    renderPicked();
  }

  async function submitProject() {
    const name = $("project-name-input").value.trim();
    const source = state.browse.source.picked;
    const target = state.browse.target.picked;
    if (!name || !source || !target) {
      showError("Give the project a name and choose both folders.", "project-error");
      return;
    }
    const mode = document.querySelector('input[name="mode"]:checked').value;
    try {
      const created = await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({ name, source, target, mode, create_target: state.browse.target.create }),
      });
      $("project-dialog").close();
      await chooseProject(created.id);
      toast(`Created project ${created.name}`, false);
    } catch (error) {
      showError(error.message, "project-error");
    }
  }

  // ---------- wiring ----------

  $("tab-sort").addEventListener("click", () => showTab(false));
  $("tab-review").addEventListener("click", () => showTab(true));
  $("more-button").addEventListener("click", (event) => {
    event.stopPropagation();
    const open = $("more-menu").classList.contains("hidden");
    closeMenus();
    if (!open) return;
    $("more-menu").classList.remove("hidden");
    $("more-button").setAttribute("aria-expanded", "true");
    menuItems($("more-menu"))[0]?.focus();
  });
  // Every entry closes the menu; links still follow their href.
  $("more-menu").addEventListener("click", (event) => {
    if (event.target.closest('[role="menuitem"]')) closeMenus();
  });
  $("more-rescan").addEventListener("click", () => act(async () => {
    const [result] = await Promise.all([papi("/rescan", { method: "POST" }), papi("/index/refresh", { method: "POST" })]);
    if (state.review.active) {
      await loadReviewFiles(false, state.review.activePath, 0, true);
      pollIndex();
      toast("Looking for new and changed files in the tree", false);
    } else {
      await load();
      toast(`Looked again: ${result.remaining} in the queue`, false);
    }
  }));
  $("index-verify").addEventListener("click", () => act(async () => {
    await papi("/index/refresh?verify_all=true", {method: "POST"});
    toast("Checking every file's contents in the background. Duplicates update when it finishes.", false);
  }));
  $("recover-button").addEventListener("click", () => act(async () => {
    await papi("/duplicates/recover", {method: "POST", timeout: LONG_REQUEST_TIMEOUT_MS});
    await Promise.all([loadFolders(), refreshDuplicates()]);
    if (state.review.active) await loadReviewFiles(false, state.review.activePath);
    else await load();
    toast("Restored the original copies", false);
  }));
  $("row-new").addEventListener("click", () => rowAction((path) => openFolderDialog("new", path)));
  $("row-rename").addEventListener("click", () => rowAction((path) => openFolderDialog("rename", path)));
  $("row-move").addEventListener("click", () => rowAction(openMoveDialog));
  $("row-group").addEventListener("click", () => rowAction((path) => openFolderDialog("group", path)));
  $("tree").addEventListener("scroll", () => $("row-menu").classList.add("hidden"));

  $("duplicate-review").addEventListener("click", () => showDuplicateDialog(state.duplicate));
  $("duplicates-close").addEventListener("click", () => { if (!state.busy) $("duplicates-dialog").close(); });
  $("duplicates-dialog").addEventListener("cancel", event => { if (state.busy) event.preventDefault(); });
  $("duplicates-form").addEventListener("submit", resolveDuplicateGroup);
  $("duplicate-folder").addEventListener("change", renderDuplicateOutcome);
  $("duplicate-filename").addEventListener("input", renderDuplicateOutcome);

  $("review-previous").addEventListener("click", () => stepReview(-1).catch(e => showError(e.message, "review-error")));
  $("review-next").addEventListener("click", () => stepReview(1).catch(e => showError(e.message, "review-error")));
  // More files arrive as the list nears its end.
  $("review-list").addEventListener("scroll", () => {
    const list = $("review-list");
    if (!state.review.more || state.review.loading || state.busy) return;
    if (list.scrollTop + list.clientHeight < list.scrollHeight - 240) return;
    loadReviewFiles(true).catch(e => showError(e.message, "review-error"));
  });
  let reviewSearchTimer;
  function filterReview() {
    // A filter changed mid-action is applied when the action ends, never lost.
    if (state.busy) {
      state.review.pendingFilter = true;
      return;
    }
    state.review.pendingFilter = false;
    loadReviewFiles().catch(e => showError(e.message, "review-error"));
  }
  $("review-search").addEventListener("input", () => { state.review.request++; clearTimeout(reviewSearchTimer); reviewSearchTimer = setTimeout(filterReview, 180); });
  $("review-folder").addEventListener("change", filterReview);
  $("review-unreviewed").addEventListener("change", filterReview);
  $("review-recursive").addEventListener("change", filterReview);
  $("classify").addEventListener("click", classify);
  $("skip").addEventListener("click", skip);
  $("discard").addEventListener("click", discard);
  // Plain UNDO steps back from the latest decision; never pass the event on.
  $("undo").addEventListener("click", () => undo());
  $("toast-undo").addEventListener("click", () => {
    const target = state.toastUndo;
    $("toast").classList.add("hidden");
    state.toastUndo = null;
    if (!target) return;
    if (!state.project || target.projectId !== state.project.id) {
      toast("That notification belonged to another project; switch back to undo it.", false);
      return;
    }
    undo(target.decisionId);
  });
  $("filename").addEventListener("input", renderDestination);
  $("search").addEventListener("input", () => {
    state.searchCursor = null;
    renderTree();
  });
  $("new-folder").addEventListener("click", () => openFolderDialog("new"));
  $("folder-name").addEventListener("input", renderFolderPreview);
  $("folder-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    // One request per dialog: a second Enter must not create or move twice.
    if ($("folder-submit").disabled) return;
    $("folder-submit").disabled = true;
    try {
      await submitFolderDialog();
    } finally {
      $("folder-submit").disabled = false;
    }
  });
  $("folder-cancel").addEventListener("click", () => $("folder-dialog").close());
  $("move-search").addEventListener("input", renderMoveTree);
  $("move-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitMove();
  });
  $("move-cancel").addEventListener("click", () => $("move-dialog").close());
  $("help-open").addEventListener("click", () => $("help-dialog").showModal());
  $("project-button").addEventListener("click", async (event) => {
    event.stopPropagation();
    const menu = $("project-menu");
    if (!menu.classList.contains("hidden")) return closeProjectMenu();
    closeMenus();
    await loadProjects().catch(() => {});
    renderProjectMenu();
    menu.classList.remove("hidden");
    $("project-button").setAttribute("aria-expanded", "true");
  });
  $("project-menu").addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", closeMenus);
  $("project-new").addEventListener("click", openProjectDialog);
  $("project-rename").addEventListener("click", () => {
    closeProjectMenu();
    act(renameProject);
  });
  $("project-archive").addEventListener("click", () => {
    closeProjectMenu();
    act(archiveProject);
  });
  $("project-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitProject();
  });
  $("project-cancel").addEventListener("click", () => $("project-dialog").close());
  $("new-tree-use").addEventListener("click", useNewTree);
  // Coming back to the window is when outside changes are likely, so pick
  // them up then instead of offering a refresh button. Throttled, and never
  // under a running action or an open dialog.
  const FOCUS_REFRESH_MS = 60000;
  let lastFocusRefresh = Date.now();
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(indexPollTimer);
      clearTimeout(duplicatesRetry);
    } else if (state.project) {
      if (state.review.active) pollIndex();
      else if (!state.busy) refreshDuplicates();
    }
  });
  window.addEventListener("focus", () => {
    if (!state.project || state.busy || document.querySelector("dialog[open]")) return;
    if (Date.now() - lastFocusRefresh < FOCUS_REFRESH_MS) return;
    lastFocusRefresh = Date.now();
    if (state.review.active) {
      // The poll reloads the list once the scan this starts completes.
      papi("/index/refresh", { method: "POST" }).then(pollIndex, () => {});
    } else if (!state.entry) {
      act(async () => {
        await papi("/rescan", { method: "POST" });
        await load();
      });
    }
    refreshDuplicates();
  });

  document.addEventListener("keydown", (event) => {
    if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing) return;
    if (document.querySelector("dialog[open]")) return;
    const target = event.target;
    const menu = openMenu();
    if (menu) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeMenus();
        if (menu.id === "row-menu") $("tree").focus();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const items = menuItems(menu);
        const at = items.indexOf(document.activeElement);
        items[(at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
      } else if (event.key === "Tab") {
        closeMenus();
      }
      // Enter and Space press the focused entry; nothing else leaks through.
      return;
    }
    if (event.key === "Escape") {
      if (!state.project || state.busy) return;
      event.preventDefault();
      if (target instanceof HTMLElement) target.blur();
      $("search").value = "";
      select(null);
      return;
    }
    if (target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement) return;
    if (target instanceof HTMLInputElement) {
      if (target.id === "search" && event.key === "Tab" && !event.shiftKey && completeSearch()) {
        event.preventDefault();
        return;
      }
      if (target.id === "search" && (event.key === "ArrowDown" || event.key === "ArrowUp") && folderMatches()) {
        event.preventDefault();
        moveSearchCursor(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        if (target.id === "search") {
          // Ancestors are shown for context; Enter takes the cursor's match.
          // With none, the field keeps focus so the typing can be fixed.
          const choice = searchCursor();
          if (choice) {
            target.value = "";
            state.searchCursor = null;
            select(choice);
            target.blur();
          }
        } else if (target.id === "filename") {
          classify();
        }
      }
      return;
    }
    if (target instanceof HTMLButtonElement && (event.key === "Enter" || event.key === " ")) return;
    if (event.shiftKey && event.key === "F10") {
      event.preventDefault();
      openSelectedMenu();
      return;
    }
    const reviewStep = (direction) => state.review.active && stepReview(direction).catch(e => showError(e.message, "review-error"));
    const handlers = {
      // J sits left of K: back, then forward.
      j: () => reviewStep(-1),
      k: () => reviewStep(1),
      Enter: classify,
      s: skip,
      d: discard,
      u: () => undo(),
      n: () => openFolderDialog("new"),
      F2: () => state.selected && openFolderDialog("rename", state.selected),
      ContextMenu: openSelectedMenu,
      r: () => showTab(!state.review.active),
      "?": () => $("help-dialog").showModal(),
      // An empty filter starts as "/", ready for a path; typing a plain
      // word after it still finds it anywhere.
      "/": () => {
        const search = $("search");
        search.focus();
        if (search.value.trim()) {
          search.select();
        } else {
          search.value = "/";
          state.searchCursor = null;
          renderTree();
        }
      },
      ArrowDown: () => step(1),
      ArrowUp: () => step(-1),
      ArrowRight: () => stepSideways(true),
      ArrowLeft: () => stepSideways(false),
    };
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const handler = handlers[key] || handlers[event.key];
    if (handler && !state.project && key !== "?") return;
    if (handler) {
      event.preventDefault();
      handler();
    }
  });

  act(async () => {
    const fromHash = Number((location.hash.match(/p=(\d+)/) || [])[1]);
    const remembered = Number(localStorage.getItem(PROJECT_KEY));
    await chooseProject(fromHash || remembered || null);
  });
})();
