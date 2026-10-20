"use strict";

const element = (id) => document.getElementById(id);
const state = { selected: null, documents: [], offset: 0, total: 0, limit: 20, busy: false,
  preview: null, previewInput: null, model: null };
const statusLabels = { uploaded: "待解析", parsed: "已解析", incomplete: "解析不完整", invalid: "数据异常" };

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = String(text);
  if (className) result.className = className;
  return result;
}

function pdfLink(documentId, sha256, pageNumber, label) {
  if (!/^[0-9a-f]{32}$/.test(documentId) || !/^[0-9a-f]{64}$/.test(sha256)
      || !Number.isInteger(pageNumber) || pageNumber < 1) {
    throw new Error("原 PDF 的来源编号、指纹或物理页号不合法，请刷新并重新检索。");
  }
  const link = node("a", label, "pdf-link");
  link.href = `/documents/${documentId}/file?sha256=${sha256}#page=${pageNumber}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function notice(text, kind = "") {
  element("notice").textContent = text;
  element("notice").className = "notice" + (kind ? " " + kind : "");
}

function controls() {
  const selected = state.selected;
  for (const id of ["pdf-file", "upload", "refresh", "chunk-size", "query", "method", "k", "model-refresh", "example-condition", "example-proof"]) element(id).disabled = state.busy;
  element("parse").disabled = state.busy || selected?.status !== "uploaded";
  element("search").disabled = state.busy || selected?.status !== "parsed" || !(selected.chunk_count > 0) || !element("query").value.trim();
  element("preview").disabled = element("search").disabled;
  const ready = previewIsCurrent() && state.preview.status === "ready";
  element("external-consent").disabled = state.busy || !ready || state.model?.status !== "ready";
  element("answer").disabled = element("external-consent").disabled || !element("external-consent").checked;
  element("answer-hint").textContent = !previewIsCurrent() ? "先免费预览当前问题；证据编号会自动绑定，无需手动复制。"
    : !ready ? "没有可用的核对结论，不会调用模型；你仍可检索并查看原 PDF。"
    : state.model?.status !== "ready" ? "证据已就绪，但本机模型尚不可用。请检查左侧模型状态；不要将密钥填到网页中。"
    : "预览未调用模型。明确勾选后，点击按钮才发送本次问题和核对结论；无自动重试，失败也可能收费。";
  element("previous").disabled = state.busy || state.offset === 0;
  element("next").disabled = state.busy || state.offset + state.limit >= state.total;
  for (const button of element("document-list").querySelectorAll("button")) button.disabled = state.busy;
  document.body.setAttribute("aria-busy", String(state.busy));
}

async function request(path, options = {}) {
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error("无法连接服务。请确认后端仍在运行，再重试；上传或解析中断时请先刷新文档，勿直接重复提交。"); }
  let body;
  try { body = await response.json(); }
  catch { throw new Error("服务返回了无法读取的响应，请检查后端终端。"); }
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join("；") : body.detail;
    const error = new Error(`HTTP ${response.status}：${detail || "请求失败"}`);
    error.httpStatus = response.status;
    throw error;
  }
  return body;
}

function jsonOptions(body) {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

async function run(pending, action) {
  if (state.busy) return;
  state.busy = true;
  controls();
  notice(pending, "pending");
  try { notice(await action() || "操作完成。"); }
  catch (error) { notice(error.message || "操作失败。", "error"); }
  finally { state.busy = false; controls(); }
}

function clearResults(message = "尚未检索") {
  element("results").replaceChildren(node("p", "检索后，原文片段和来源位置会显示在这里。", "panel empty"));
  element("result-count").textContent = message;
  element("result-summary").textContent = "";
}

function renderDocuments() {
  const list = element("document-list");
  list.replaceChildren();
  if (!state.documents.length) list.append(node("p", state.total ? "当前页没有文档。" : "还没有文档，上传一份 PDF 开始。", "empty"));
  for (const item of state.documents) {
    const button = node("button", undefined, "document-row" + (item.document_id === state.selected?.document_id ? " active" : ""));
    button.type = "button";
    button.setAttribute("aria-pressed", String(item.document_id === state.selected?.document_id));
    const label = node("span", undefined, "document-label");
    label.append(node("span", item.document_name || "名称不可用", "document-name"));
    label.append(node("span", `${statusLabels[item.status] || item.status} · ${item.document_id.slice(0, 8)}`, "document-meta"));
    button.append(node("span", "PDF", "pdf-symbol"), label);
    button.addEventListener("click", () => run("正在读取文档信息…", async () => {
      const detail = await request(`/documents/${encodeURIComponent(item.document_id)}`);
      select(detail);
      return "已选中文档。";
    }));
    list.append(button);
  }
  element("page-info").textContent = state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)} / ${state.total} 份` : "0 份文档";
}

async function refreshDocuments() {
  let body = await request(`/documents?offset=${state.offset}&limit=${state.limit}`);
  if (state.offset > 0 && state.offset >= body.total) {
    state.offset = Math.max(0, Math.floor((body.total - 1) / state.limit) * state.limit);
    body = await request(`/documents?offset=${state.offset}&limit=${state.limit}`);
  }
  state.documents = body.documents;
  state.total = body.total;
  renderDocuments();
}

function renderSelected() {
  const item = state.selected;
  const target = element("selected-info");
  if (!item) {
    target.replaceChildren(node("p", "从左侧选择一份文档，或先上传 PDF。", "empty"));
    element("parse-hint").textContent = "请先选择文档。";
    return;
  }
  const heading = node("div", undefined, "selected-title");
  heading.append(node("h3", item.document_name || "名称不可用"), node("span", statusLabels[item.status] || item.status, `badge ${item.status}`));
  if (["uploaded", "parsed", "incomplete"].includes(item.status) && /^[0-9a-f]{64}$/.test(item.sha256)) {
    heading.append(pdfLink(item.document_id, item.sha256, 1, "打开原 PDF ↗"));
  }
  target.replaceChildren(heading, node("p", `文档编号：${item.document_id}`, "document-id"));
  const stats = node("div", undefined, "stats");
  for (const [label, value] of [["PDF 物理页", item.page_count], ["非空文本页", item.nonempty_page_count], ["文本块", item.chunk_count]]) {
    const stat = node("div", undefined, "stat");
    stat.append(node("strong", value ?? "—"), node("span", label));
    stats.append(stat);
  }
  target.append(stats, node("p", item.detail || "", "hint"));
  if (item.empty_page_numbers?.length) target.append(node("p", `未提取到文本的物理页：${item.empty_page_numbers.join("、")}（不等于视觉空白页）`, "hint"));
  for (const warning of item.warnings || []) target.append(node("p", warning, "hint"));
  const hints = {
    uploaded: "解析会保存页面与分块，尚不会构建检索索引。",
    parsed: item.chunk_count ? "已解析，无需重复解析。首次检索会按需构建索引。" : "没有文本块，当前无法检索；本版本不支持 OCR。",
    incomplete: "解析文件不完整，当前不能重复解析或检索，请检查后端数据。",
    invalid: "文档数据校验异常，当前不能解析或检索。",
  };
  element("parse-hint").textContent = hints[item.status] || "请检查文档状态。";
  if (item.chunk_size) element("chunk-size").value = item.chunk_size;
}

function select(item) {
  state.selected = item;
  clearResults();
  resetAnswer();
  renderSelected();
  renderDocuments();
  controls();
}

async function refreshAfterWrite(successMessage) {
  try { await refreshDocuments(); return successMessage; }
  catch (error) { return `${successMessage} 但列表刷新失败：${error.message} 请手动刷新列表，不要重复提交。`; }
}

element("upload-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run("正在上传并保存 PDF…", async () => {
    const file = element("pdf-file").files[0];
    if (!file) throw new Error("请先选择一个 PDF。");
    if (!file.name.toLowerCase().endsWith(".pdf")) throw new Error("请选择 .pdf 文件。");
    if (!file.size) throw new Error("不能上传空文件。");
    if (file.size > 10 * 1024 * 1024) throw new Error("文件超过 10 MiB 上限。");
    const data = new FormData();
    data.append("file", file);
    const uploaded = await request("/documents", { method: "POST", body: data });
    select({ ...uploaded, detail: "原始 PDF 已保存，下一步请解析文档。", warnings: [] });
    element("pdf-file").value = "";
    return refreshAfterWrite("上传成功，已自动选中。请继续解析文档。");
  });
});

element("parse-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run("正在解析 PDF，请等待。解析当前是同步操作，没有后台进度百分比。", async () => {
    const selected = state.selected;
    if (selected?.status !== "uploaded") throw new Error("请选择一份尚未解析的有效文档。");
    const size = Number(element("chunk-size").value);
    if (!Number.isInteger(size) || size < 1 || size > 10000) throw new Error("分块字符数必须是 1～10000 的整数。");
    clearResults();
    const report = await request(`/documents/${encodeURIComponent(selected.document_id)}/parse`, jsonOptions({ chunk_size: size }));
    select({ ...selected, ...report, sha256: report.source_sha256, detail: "页面和分块已保存；检索时仍会校验来源数据。" });
    return refreshAfterWrite(report.chunk_count ? "解析成功，可以输入问题进行检索。" : "解析完成，但没有可检索文本；当前不支持 OCR。");
  });
});

function renderResults(body) {
  element("result-count").textContent = `${body.returned_count} 条证据`;
  element("result-summary").textContent = `问题：${body.query} · ${body.method === "normalized" ? "术语统一 BM25" : "原始 BM25"} · K=${body.k} · 索引 ${body.indexed_chunk_count} 块 · ${body.cache_hit ? "复用缓存" : "本次构建索引"}。分数不是概率或置信度。`;
  const results = element("results");
  results.replaceChildren();
  if (!body.results.length) {
    results.append(node("p", "没有匹配片段。可以尝试讲义中的术语；这不代表文档没有答案，也不是系统已作出拒答判断。", "panel empty"));
    return;
  }
  body.results.forEach((hit, index) => {
    const card = node("article", undefined, "panel evidence-card");
    const top = node("div", undefined, "evidence-top");
    top.append(node("span", `证据 ${String(index + 1).padStart(2, "0")} · PDF 物理第 ${hit.chunk.pdf_page_number} 页`, "evidence-rank"), node("span", `BM25 ${hit.score.toFixed(4)}`));
    card.append(top, node("pre", hit.chunk.text, "evidence-text"));
    card.append(node("p", `${hit.chunk.document_name} · 页内字符 [${hit.chunk.start_char}, ${hit.chunk.end_char}) · 编号 ${hit.chunk.document_id}`, "evidence-source"));
    const actions = node("div", undefined, "evidence-actions");
    actions.append(
      pdfLink(hit.chunk.document_id, body.source_sha256, hit.chunk.pdf_page_number, `打开 PDF 第 ${hit.chunk.pdf_page_number} 页 ↗`),
      node("span", "浏览器若未自动跳页，请按物理页号手动定位。", "hint"),
    );
    card.append(actions);
    results.append(card);
  });
}

element("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run("正在检索当前文档…", async () => {
    clearResults("正在检索…");
    try {
      if (state.selected?.status !== "parsed" || !(state.selected.chunk_count > 0)) throw new Error("请先选择并解析含有文本的文档。");
      const query = element("query").value.trim();
      const k = Number(element("k").value);
      if (!query || query.length > 500) throw new Error("问题必须是 1～500 个字符。");
      if (!Number.isInteger(k) || k < 1 || k > 20) throw new Error("返回条数必须是 1～20 的整数。");
      const body = await request(`/documents/${encodeURIComponent(state.selected.document_id)}/search`, jsonOptions({ query, method: element("method").value, k }));
      renderResults(body);
      return `检索完成，返回 ${body.returned_count} 条原文证据。`;
    } catch (error) {
      clearResults("检索失败");
      throw error;
    }
  });
});

for (const [id, type] of [["query", "input"], ["k", "input"], ["method", "change"]]) element(id).addEventListener(type, () => {
  resetAnswer("问题或检索参数已变化，请重新免费预览。");
  clearResults("问题已变化，请重新检索");
  controls();
});
element("refresh").addEventListener("click", () => run("正在刷新文档库…", async () => {
  try {
    await refreshDocuments();
    if (state.selected) select(await request(`/documents/${encodeURIComponent(state.selected.document_id)}`));
    return "文档库已刷新。";
  } catch (error) {
    select(null);
    throw new Error(`${error.message} 当前文档状态未能确认，已取消选择；请刷新后重新选择文档。`);
  }
}));
for (const [id, direction] of [["previous", -1], ["next", 1]]) element(id).addEventListener("click", () => run("正在读取文档列表…", async () => {
  const previous = state.offset;
  state.offset = Math.max(0, state.offset + direction * state.limit);
  try { await refreshDocuments(); }
  catch (error) { state.offset = previous; throw error; }
  return "文档列表已更新。";
}));

function questionInput() {
  if (state.selected?.status !== "parsed" || !(state.selected.chunk_count > 0)) throw new Error("请先选择并解析含有文本的文档。");
  const query = element("query").value.trim();
  const method = element("method").value;
  const k = Number(element("k").value);
  if (!query || query.length > 500) throw new Error("问题必须是 1～500 个字符。");
  if (!["original", "normalized"].includes(method)) throw new Error("检索方案不合法。");
  if (!Number.isInteger(k) || k < 1 || k > 20) throw new Error("返回条数必须是 1～20 的整数。");
  return { query, method, k, max_context_chars: 6000 };
}

function previewIsCurrent() {
  try {
    return !!state.preview && state.preview.document_id === state.selected?.document_id
      && state.preview.source_sha256 === state.selected?.sha256
      && JSON.stringify(state.previewInput) === JSON.stringify(questionInput());
  } catch { return false; }
}

function resetAnswer(message = "先免费预览，看看当前问题有没有可用的已核对结论。") {
  state.preview = null;
  state.previewInput = null;
  element("external-consent").checked = false;
  element("answer-status").textContent = "尚未调用模型";
  element("answer-status").className = "badge";
  element("review-preview").replaceChildren(node("p", message, "empty"));
  element("answer-result").replaceChildren(node("p", "真实回答、引用、模型名称和用量会显示在这里。", "empty"));
}

function renderModelStatus(body) {
  const labels = { ready: "本机配置就绪", disabled: "服务未启用模型", missing_key: "服务尚未配置密钥",
    busy: "模型请求进行中", limit_reached: "本服务尝试次数已用尽" };
  if (!body || body.provider !== "deepseek" || typeof body.model !== "string" || !labels[body.status]
      || !Number.isInteger(body.calls_used) || !Number.isInteger(body.max_calls)) throw new Error("模型状态响应不完整。");
  state.model = body;
  const target = element("model-status");
  target.replaceChildren(node("strong", `DeepSeek · ${body.model}`, "model-name"),
    node("span", labels[body.status], "badge" + (body.status === "ready" ? " parsed" : "")),
    node("p", `当前服务尝试 ${body.calls_used} / ${body.max_calls} 次`, "hint"),
    node("p", "不是账户余额或人民币限额；配置就绪不保证密钥有效或余额充足。", "hint"));
}

async function refreshModel() {
  try { renderModelStatus(await request("/generation-status", { cache: "no-store" })); }
  catch (error) {
    state.model = null;
    element("model-status").replaceChildren(node("p", `模型状态未知。${error.message} 若为 HTTP 404，请在原终端重启服务以加载新版代码。`, "hint"));
  }
}

function validatePreview(packet, input) {
  if (!packet || packet.document_id !== state.selected?.document_id || packet.source_sha256 !== state.selected?.sha256
      || packet.query !== input.query || packet.source_kind !== "reviewed_formula_annotation"
      || !/^[0-9a-f]{64}$/.test(packet.reviewed_context_id) || !/^[0-9a-f]{64}$/.test(packet.raw_context_id)
      || !Array.isArray(packet.evidence) || packet.evidence.length > 8 || packet.included_count !== packet.evidence.length
      || !["ready", "no_evidence", "no_reviewed_evidence", "budget_exceeded"].includes(packet.status)
      || (packet.status === "ready") !== (packet.evidence.length > 0)) throw new Error("证据预览与当前问题或来源不一致，请刷新后重新预览；未调用模型。");
  for (const item of packet.evidence) {
    if (item.source_sha256 !== packet.source_sha256 || !/^FR\d{3}$/.test(item.review_id)
        || !/^E[1-9]\d*$/.test(item.citation_id) || !/^[0-9a-f]{64}$/.test(item.review_revision)
        || typeof item.reviewed_statement !== "string" || !item.reviewed_statement.trim()
        || typeof item.raw_quote !== "string" || !Number.isInteger(item.pdf_page_number) || item.pdf_page_number < 1) {
      throw new Error("核对证据字段不完整；未调用模型，请检查后端版本。");
    }
  }
}

function renderPreview(packet) {
  const target = element("review-preview");
  target.replaceChildren();
  element("answer-status").textContent = packet.included_count ? "预览完成 · 未调用模型" : "证据不足 · 未调用模型";
  target.append(node("p", packet.included_count ? `找到 ${packet.included_count} 条已核对结论。版本已自动绑定，不需要复制编号。`
    : "当前问题没有预算内可用的已核对结论。这不证明讲义没有答案，也不是模型已经作出了拒答。", "hint"));
  for (const item of packet.evidence) {
    const card = node("article", undefined, "review-card");
    card.append(node("span", `${item.citation_id} · ${item.review_id} · PDF 物理第 ${item.pdf_page_number} 页`, "evidence-rank"),
      node("p", item.reviewed_statement, "review-statement"),
      pdfLink(packet.document_id, packet.source_sha256, item.pdf_page_number, "打开原 PDF 核对 ↗"));
    const detail = node("details", undefined, "technical-detail");
    detail.append(node("summary", "查看核对来源与提取文本"),
      node("p", `核对注释 · ${item.review_method} · ${item.reviewed_at} · 原文字符 [${item.raw_start_char}, ${item.raw_end_char})`, "hint"),
      node("p", "以下为 PDF 提取文本，可能丢失共轭横线等符号；上方才是单独保存的核对结论。", "hint"),
      node("pre", item.raw_quote, "evidence-text"));
    card.append(detail);
    target.append(card);
  }
}

element("preview").addEventListener("click", () => run("正在免费检索和预览核对证据，不调用模型…", async () => {
  resetAnswer();
  clearResults("正在免费检索…");
  try {
    const input = questionInput();
    const id = state.selected.document_id;
    const search = await request(`/documents/${id}/search`, jsonOptions({ query: input.query, method: input.method, k: input.k }));
    renderResults(search);
    const packet = await request(`/documents/${id}/reviewed-context`, jsonOptions(input));
    validatePreview(packet, input);
    state.preview = packet;
    state.previewInput = input;
    renderPreview(packet);
    await refreshModel();
    element("answer-heading").scrollIntoView?.({ block: "start" });
    return packet.included_count ? "免费预览完成。请查看核对结论；确认外发许可后，才会调用 DeepSeek。"
      : "免费预览完成，当前没有可用的已核对结论。没有调用模型，也没有模型费用。";
  } catch (error) {
    resetAnswer("预览失败，未调用模型。请检查提示后再试。");
    if (element("result-count").textContent === "正在免费检索…") clearResults("检索失败");
    throw error;
  }
}));

function renderAnswer(body, packet) {
  const generation = body?.generation;
  const check = body?.check;
  if (!body || body.document_id !== packet.document_id || body.source_sha256 !== packet.source_sha256
      || body.query !== packet.query || body.reviewed_context_id !== packet.reviewed_context_id
      || body.raw_context_id !== packet.raw_context_id || !["answered", "refused", "rejected"].includes(body.status)
      || !generation || typeof generation.model_called !== "boolean" || generation.provider !== "deepseek"
      || typeof generation.model !== "string" || !check || !Array.isArray(check.claims) || !Array.isArray(check.issues)) {
    throw new Error("回答与当前预览来源不一致或响应不完整，未展示内容；若请求已发出，可能已收费。");
  }
  if (body.status === "answered" && (check.status !== "passed" || check.candidate_decision !== "answer" || !check.claims.length)) throw new Error("回答校验状态不一致，未展示内容；可能已收费。");
  if (body.status !== "answered" && check.claims.length) throw new Error("拒答或拦截结果不能包含可展示结论；可能已收费。");
  if (body.status === "refused" && (check.status !== "passed" || check.candidate_decision !== "refuse" || !check.refusal_reason)) throw new Error("拒答响应不完整；可能已收费。");
  if (body.status === "rejected" && (check.status !== "rejected" || !check.issues.length)) throw new Error("拦截响应不完整；可能已收费。");
  const seen = new Set();
  for (const claim of check.claims) {
    if (!Array.isArray(claim.citations) || claim.citations.length !== 1) throw new Error("回答引用不完整；可能已收费。");
    const citation = claim.citations[0];
    const approved = packet.evidence.find((item) => item.citation_id === citation.citation_id);
    if (!approved || seen.has(citation.citation_id) || claim.text !== approved.reviewed_statement
        || citation.quote !== approved.reviewed_statement || citation.review_id !== approved.review_id
        || citation.review_revision !== approved.review_revision || citation.document_id !== packet.document_id
        || citation.source_sha256 !== packet.source_sha256 || citation.pdf_page_number !== approved.pdf_page_number
        || citation.raw_start_char !== approved.raw_start_char || citation.raw_end_char !== approved.raw_end_char) {
      throw new Error("回答未匹配当前核对结论或引用，未展示内容；可能已收费。");
    }
    seen.add(citation.citation_id);
  }
  const usage = generation.usage;
  if (generation.model_called && (!usage || ![usage.prompt_tokens, usage.completion_tokens, usage.total_tokens].every((value) => Number.isInteger(value) && value >= 0)
      || usage.total_tokens !== usage.prompt_tokens + usage.completion_tokens)) throw new Error("模型用量响应不完整；先核对平台账单，不自动重试。");
  const labels = { answered: "回答已返回", refused: "证据不足，已拒答", rejected: "草稿未通过检查，已拦截" };
  const target = element("answer-result");
  target.replaceChildren(node("h3", labels[body.status], "answer-title"));
  element("answer-status").textContent = generation.model_called ? "本次已调用模型" : "本次未调用模型";
  element("answer-status").className = "badge" + (body.status === "answered" ? " parsed" : "");
  for (const claim of check.claims) {
    const card = node("article", undefined, "answer-claim");
    card.append(node("p", claim.text, "answer-text"));
    for (const citation of claim.citations) card.append(node("p", `依据 ${citation.citation_id} / ${citation.review_id} · 引用的是已核对注释，不是损坏的提取公式。`, "hint"),
      pdfLink(body.document_id, body.source_sha256, citation.pdf_page_number, `查看 PDF 物理第 ${citation.pdf_page_number} 页 ↗`));
    target.append(card);
  }
  if (body.status === "refused") target.append(node("p", check.refusal_reason, "answer-text"));
  for (const issue of check.issues) target.append(node("p", `${issue.code}：${issue.detail}`, "hint"));
  target.append(node("p", `${generation.provider} · ${generation.model} · 本次${generation.model_called ? "已" : "未"}调用模型`, "generation-line"),
    node("p", usage ? `输入 ${usage.prompt_tokens} / 输出 ${usage.completion_tokens} / 合计 ${usage.total_tokens} tokens` : "本次没有返回模型 token 用量。", "hint"),
    node("p", `当前服务已尝试 ${generation.calls_used} / ${generation.max_calls} 次；不是账户账单，失败调用也可能收费。`, "hint"),
    node("p", "检查范围：格式、引用和核对结论选择。未自动证明数学正确性、问题相关性或拒答理由充分性。", "hint"));
}

element("external-consent").addEventListener("change", controls);
element("answer").addEventListener("click", () => run("正在请求 DeepSeek；请勿重复点击，调用失败也可能收费…", async () => {
  if (!previewIsCurrent() || state.preview.status !== "ready") throw new Error("当前没有有效预览，请先重新免费预览。");
  if (!element("external-consent").checked || state.model?.status !== "ready") throw new Error("请先确认外发许可及本机模型状态。");
  const packet = state.preview;
  const input = { ...state.previewInput, allow_external: true, expected_reviewed_context_id: packet.reviewed_context_id };
  element("answer-result").replaceChildren(node("p", "请求已发出，等待实际结果；没有自动重试。", "empty"));
  element("answer-status").textContent = "请求进行中 · 可能收费";
  // Consume permission and preview before the outgoing call. Every attempt needs a new preview.
  state.preview = null;
  state.previewInput = null;
  element("external-consent").checked = false;
  try {
    const body = await request(`/documents/${packet.document_id}/answer-reviewed`, jsonOptions(input));
    renderAnswer(body, packet);
    element("answer-result").scrollIntoView?.({ block: "start" });
    return body.status === "answered" ? "回答已返回，请结合引用核对原文。再次调用需重新预览和同意。"
      : body.status === "refused" ? "模型已拒答，不补造当前证据没有覆盖的内容。" : "草稿被安全拦截，没有作为答案展示；本次调用仍可能收费。";
  } catch (error) {
    element("answer-status").textContent = "请求未完成";
    element("answer-result").replaceChildren(node("p", error.message, "answer-error"),
      node("p", "没有自动重试。若提示调用前版本不同，本次未调用模型，请重新免费预览；其他网络、超时或检查异常可能已收费，请先核对平台记录。", "hint"));
    throw error;
  } finally { await refreshModel(); }
}));

element("model-refresh").addEventListener("click", () => run("正在读取本机模型状态，不调用模型…", async () => {
  await refreshModel();
  return state.model ? "本机模型状态已更新；没有验证密钥或余额，也没有调用模型。" : "模型状态暂不可用，请查看左侧提示。";
}));
for (const [id, question] of [["example-condition", "复数三角不等式在什么条件下取等号？"],
  ["example-proof", "请给出复数三角不等式取等号条件的完整证明，写出每一步推导。"]]) {
  element(id).addEventListener("click", () => {
    if (state.busy) return;
    element("query").value = question;
    resetAnswer();
    clearResults();
    controls();
    notice("已填入示例问题，没有发送请求。点击免费预览继续。");
  });
}

run("正在读取本地文档库…", async () => {
  await refreshDocuments();
  await refreshModel();
  return "文档库已就绪。选择讲义，输入问题，再免费预览证据。";
});
