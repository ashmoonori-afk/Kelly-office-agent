"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  actor: null,
  csrf: null,
  project: null,
  sources: [],
  folders: [],
  bots: [],
  selected: new Set(),
  runId: null,
  detail: null,
  settings: null,
  editingBot: null,
  preview: null,
  editingMemory: null,
  inputBase: null,
  pendingRun: null,
  busy: 0,
  polling: false,
  signature: "",
  inputRunId: null,
};
const labels = {
  queued: "대기",
  preparing: "업무 준비",
  running: "진행 중",
  checking: "검증 중",
  ready: "준비됨",
  awaiting_input: "입력 필요",
  retry_wait: "재시도 대기",
  awaiting_budget: "사용 한도 확인 필요",
  awaiting_approval: "승인 필요",
  cancel_requested: "취소 처리 중",
  cancelled: "취소됨",
  succeeded: "완료",
  rejected: "거절됨",
  expired: "만료됨",
  failed: "실패",
  outcome_unknown: "실행 결과 확인 필요",
  pending: "대기",
  approved: "승인됨",
  consumed: "승인 사용됨",
  skipped: "건너뜀",
  sending: "전달 중",
  sent: "전달 완료",
  unknown: "확인되지 않음",
  provider: "공급자 측정",
  estimated: "추정",
  not_used: "모델 미사용",
  mechanical: "규칙 작업",
  simple: "간단",
  standard: "일반",
  complex: "복잡",
  pass: "통과",
  fail: "실패",
  needs_input: "입력 필요",
  unverified: "미검증",
  preflight: "파일 구조",
  source_grounding: "출처 연결",
  numeric: "수치",
  report_structure: "문서 구성",
  semantic_review: "의미 검토",
  preference: "선호",
  decision: "프로젝트 결정",
  template: "양식",
  disabled: "사용 안 함",
  key_missing: "키 없음",
  availability_unchecked: "가용성 미확인",
  discovery_failed: "목록 조회 실패",
  unavailable: "사용 불가",
  unqualified: "품질 자격 미확인",
  missing: "없음",
  unverified_hash: "보고서 해시 미확인",
  missing_file: "보고서 없음",
  invalid_report: "보고서 형식 오류",
  failed_checks: "검증 미통과",
  insufficient_coverage: "검증 범위 부족",
  verified: "검증됨",
};
const recipes = {
  aggregate: "표 집계",
  summarize: "자료 요약",
  weekly_report: "보고서",
  meeting_actions: "회의 할 일",
  draft_reply: "답장 초안",
  search_answer: "자료에서 찾기",
};
const formats = {
  text: "텍스트",
  md: "Markdown",
  csv: "CSV",
  xlsx: "Excel 표",
  docx: "Word 문서",
};
const terminal = new Set([
  "succeeded",
  "failed",
  "cancelled",
  "rejected",
  "expired",
  "outcome_unknown",
]);
let serial = 0;
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === "class") node.className = value;
    else if (
      [
        "checked",
        "disabled",
        "hidden",
        "required",
        "multiple",
        "open",
      ].includes(key)
    )
      node[key] = Boolean(value);
    else if (key === "value") node.value = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat())
    if (child != null)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  return node;
}
const tr = (value) => labels[value] || value || "미확인";
const num = (value) =>
  typeof value === "number" ? value.toLocaleString("ko-KR") : "미확인";
const query = (values) => new URLSearchParams(values).toString();
const text = (id, value) => {
  $(id).textContent = value ?? "";
};
const badge = (value, title) =>
  el("span", { class: "badge", "data-state": value }, title || tr(value));
const empty = (value) => el("p", { class: "empty-note" }, value);
const jsonView = (value) => el("pre", {}, JSON.stringify(value, null, 2));
const details = (title, node) =>
  el("details", {}, el("summary", {}, title), node);
const date = (value) =>
  value ? new Date(value).toLocaleString("ko-KR") : "아직 없음";
const lines = (value) =>
  value
    .split(/\r?\n/)
    .map((x) => x.trim())
    .filter(Boolean);
const commas = (value) =>
  value
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
function localDate(value) {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? ""
    : new Date(d.valueOf() - d.getTimezoneOffset() * 60000)
        .toISOString()
        .slice(0, 16);
}
function iso(value) {
  const d = new Date(value);
  if (Number.isNaN(d.valueOf())) throw new Error("실행 시각을 입력해 주세요.");
  return d.toISOString();
}
function announce(message, error = false) {
  text("notice", message);
  $("notice").hidden = false;
  $("notice").classList.toggle("error", error);
  const target = document.querySelector("dialog[open] .dialog-status");
  if (target) target.textContent = message;
  if (!state.actor) text("login-message", message);
}
async function api(
  path,
  { method = "GET", body, headers = {}, raw = false } = {},
) {
  const options = {
    method,
    credentials: "same-origin",
    headers: { Accept: "application/json", ...headers },
  };
  if (method !== "GET") options.headers["X-CSRF-Token"] = state.csrf || "";
  if (body !== undefined) {
    options.body = raw ? body : JSON.stringify(body);
    if (!raw) options.headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error(
      "서버에 연결하지 못했습니다. 실행 창을 확인하고 다시 시도해 주세요.",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/bootstrap") {
      state.actor = null;
      state.csrf = null;
      $("app").hidden = true;
      $("login").hidden = false;
      $("bootstrap-code").focus();
    }
    const error = new Error(
      data.error?.message_ko ||
        "요청을 처리하지 못했습니다. 입력과 연결 상태를 확인해 주세요.",
    );
    error.status = response.status;
    error.code = data.error?.code;
    throw error;
  }
  return data;
}
async function action(node, fn) {
  if (node?.disabled) return;
  if (node) {
    node.disabled = true;
    node.setAttribute("aria-busy", "true");
  }
  state.busy++;
  $("project").disabled = true;
  try {
    return await fn();
  } catch (error) {
    announce(error.message, true);
    if (error.status === 409 || error.status === 410)
      await refreshCurrent().catch(() => {});
  } finally {
    state.busy--;
    if (node) {
      node.disabled = false;
      node.removeAttribute("aria-busy");
    }
    $("project").disabled = state.busy > 0;
  }
}
function button(title, handler, style = "quiet") {
  const node = el("button", { type: "button", class: style }, title);
  node.addEventListener("click", () => action(node, handler));
  return node;
}
function form(id, handler) {
  $(id).addEventListener("submit", (event) => {
    event.preventDefault();
    action(
      $(id).querySelector('button[type="submit"],button:not([type])'),
      handler,
    );
  });
}
function switchView(view, focus = false) {
  for (const name of ["conversation", "automations", "history"])
    $("view-" + name).hidden = name !== view;
  for (const node of document.querySelectorAll("[data-view]")) {
    if (node.dataset.view === view) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  }
  if (focus) $("main").focus();
  if (view === "automations")
    loadAutomations().catch((error) => announce(error.message, true));
  if (view === "history")
    loadHistory().catch((error) => announce(error.message, true));
}
const selected = (id) =>
  [...$(id).querySelectorAll('input[type="checkbox"]:checked')].map(
    (node) => node.value,
  );
const sourceId = (item) => item.id || item.artifact_id;
const sourceName = (item) =>
  item.provenance_json?.filename ||
  item.filename ||
  item.name ||
  sourceId(item);
function sourceChoices(id, ids = []) {
  const choices = new Set(ids);
  $(id).replaceChildren(
    ...(state.sources.length
      ? state.sources.map((item) =>
          el(
            "label",
            { class: "check" },
            el("input", {
              type: "checkbox",
              value: sourceId(item),
              checked: choices.has(sourceId(item)),
            }),
            el(
              "span",
              {},
              sourceName(item),
              el("small", {}, " · " + num(item.byte_size) + " bytes"),
            ),
          ),
        )
      : [empty("먼저 자료를 추가해 주세요.")]),
  );
}
function attachmentCount() {
  state.selected = new Set(selected("source-choices"));
  text("attachment-count", "자료 " + state.selected.size + "개 선택");
}
async function loadSources() {
  const project = state.project,
    data = await api("/api/artifacts?" + query({ project_id: project }));
  if (project !== state.project) return;
  const auto = selected("auto-sources"),
    input = selected("input-sources");
  state.sources = data.artifacts || [];
  $("source-field").hidden = state.sources.length === 0;
  state.selected = new Set(
    [...state.selected].filter((id) =>
      state.sources.some((item) => sourceId(item) === id),
    ),
  );
  sourceChoices("source-choices", [...state.selected]);
  sourceChoices("auto-sources", auto);
  sourceChoices("input-sources", input);
  attachmentCount();
}
async function upload(file) {
  if (file.size > 25 * 1024 * 1024)
    throw new Error("파일당 25 MB까지 추가할 수 있습니다.");
  const data = await api(
    "/api/artifacts?" +
      query({ project_id: state.project, filename: file.name }),
    {
      method: "POST",
      body: file,
      raw: true,
      headers: { "Content-Type": file.type || "application/octet-stream" },
    },
  );
  state.selected.add(sourceId(data.artifact));
  await loadSources();
  return data.artifact;
}
async function startRun() {
  if (state.uploading)
    throw new Error("자료 추가가 끝난 뒤 업무를 시작해 주세요.");
  attachmentCount();
  if (state.selected.size > 20)
    throw new Error("자료를 최대 20개 선택해 주세요.");
  const body = {
      request: $("request").value.trim(),
      project_id: state.project,
      artifact_ids: [...state.selected],
      quality_mode: $("quality").value,
      output_format: $("output-format").value,
    },
    serialized = JSON.stringify(body);
  if (state.pendingRun?.serialized !== serialized)
    state.pendingRun = { serialized, key: crypto.randomUUID() };
  const data = await api("/api/runs", {
    method: "POST",
    body,
    headers: { "Idempotency-Key": state.pendingRun.key },
  });
  state.pendingRun = null;
  await selectRun(data.run.id);
  await loadHistory();
  announce("업무를 접수했습니다. 할 일과 결과를 확인해 주세요.");
}
async function selectRun(id) {
  state.runId = id;
  state.signature = "";
  state.inputRunId = null;
  switchView("conversation");
  await refreshCurrent();
  $("run-title").focus();
}
function validationView(value) {
  if (!value)
    return el("p", { class: "field-note" }, "검증 기록이 아직 없습니다.");
  const node = el(
    "div",
    {},
    badge(value.status),
    el(
      "ul",
      { class: "verification-list" },
      (value.checked_layers || []).map((layer) => el("li", {}, tr(layer))),
    ),
  );
  if (value.errors?.length)
    node.append(
      el(
        "p",
        { class: "error-text" },
        value.errors
          .map((error) =>
            typeof error === "string" ? tr(error) : JSON.stringify(error),
          )
          .join(" · "),
      ),
    );
  node.append(
    el(
      "p",
      { class: "field-note" },
      value.visual_verified === true
        ? "실제 화면 검증 기록 있음"
        : "실제 화면 모양은 검증되지 않았습니다.",
    ),
  );
  return node;
}
function usageView(value) {
  const item = value || {},
    list = el("dl", { class: "usage-table" });
  const rows = [
    ["측정 상태", tr(item.usage_state)],
    ["입력 토큰", num(item.input_tokens_total)],
    ["출력 토큰", num(item.output_tokens_total)],
    ["캐시 입력", num(item.cached_input_tokens)],
    ["추론 토큰", num(item.reasoning_tokens)],
    ["모델 호출", num(item.logical_calls)],
    ["HTTP 시도 · 재시도 포함", num(item.transport_attempts)],
    ["미확인 호출", num(item.unknown_calls)],
    [
      "측정된 비용",
      item.actual_micro_usd == null
        ? "가격 미확인"
        : "$" + (item.actual_micro_usd / 1000000).toFixed(6),
    ],
  ];
  for (const [name, count] of rows)
    list.append(el("dt", {}, name), el("dd", {}, count));
  if (item.price_stale)
    list.append(el("dt", {}, "가격 기준"), el("dd", {}, "현재 가격 확인 필요"));
  return list;
}
function todoView(step, calls = []) {
  const spec = step.spec_json || {},
    todo = spec.todo || {},
    executor = todo.executor || {},
    role = executor.type === "tool" ? "D" : executor.role || "미배정",
    stepCalls = calls.filter((call) => call.step_id === step.id),
    purposes = {
      plan: "계획",
      generate: "생성",
      repair: "보완",
      review: "검토",
      replan: "재계획",
    };
  const main = el(
    "div",
    { class: "todo-main" },
    el("h3", {}, todo.title || "할 일"),
    el(
      "div",
      { class: "todo-meta" },
      badge(todo.kind),
      badge(step.state),
      badge(
        role,
        role +
          (role === "D" ? " · 도구" : stepCalls.length ? "" : " · 호출 전"),
      ),
    ),
  );
  if (role !== "D")
    main.append(el("p", { class: "todo-reason" }, todo.reason || ""));
  stepCalls.forEach((call, index) =>
    main.append(
      el(
        "p",
        { class: "field-note" },
        [
          String(index + 1) + ". " + (purposes[call.purpose] || call.purpose),
          [call.provider, call.model_id].filter(Boolean).join(" / ") ||
            "모델 미확인",
          call.role,
          call.reasoning_effort ? "추론 " + call.reasoning_effort : null,
          tr(call.state),
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    ),
  );
  const extra = el("div", {}, validationView(step.validation_json));
  if (spec.correction_reason)
    extra.append(
      el("p", { class: "correction" }, "분류 정정 · " + spec.correction_reason),
    );
  else if (role === "D" && todo.reason)
    extra.append(el("p", { class: "field-note" }, todo.reason));
  if (todo.done_when?.length)
    extra.append(
      el(
        "p",
        { class: "field-note" },
        "완료 조건: " + todo.done_when.join(" · "),
      ),
    );
  if (todo.verify_with?.length)
    extra.append(
      el(
        "p",
        { class: "field-note" },
        "예정 검증: " + todo.verify_with.map(tr).join(" · "),
      ),
    );
  if (spec.original_classification || spec.final_classification)
    extra.append(
      details(
        "처음 분류와 현재 분류",
        jsonView({
          처음: spec.original_classification,
          현재: spec.final_classification,
        }),
      ),
    );
  if (step.output_json)
    extra.append(details("출력과 근거 원문", jsonView(step.output_json)));
  return el(
    "li",
    { "data-step": step.id, "data-state": step.state },
    el(
      "div",
      { class: "todo-head" },
      el(
        "span",
        { class: "todo-number", "aria-hidden": "true" },
        step.state === "succeeded" ? "✓" : step.ordinal + 1,
      ),
      main,
    ),
    details("근거와 검증 보기", extra),
  );
}
function approvalView(item) {
  const proposal = item.proposal_json || {},
    node = el(
      "section",
      { class: "approval-card" },
      el("h3", {}, "확인 후 승인해 주세요"),
      el("p", {}, proposal.impact || "표시된 작업 내용을 확인해 주세요."),
      el("p", { class: "field-note" }, "만료: " + date(item.expires_at)),
      details("정확한 대상·자료·변경 내용", jsonView(proposal)),
    );
  if (item.state === "pending")
    node.append(
      el(
        "div",
        { class: "actions" },
        ...["approve", "reject"].map((decision) =>
          button(
            decision === "approve" ? "표시된 작업 승인" : "거절",
            async () => {
              await api(
                "/api/approvals/" + encodeURIComponent(item.id) + "/resolve",
                {
                  method: "POST",
                  body: {
                    decision,
                    proposal_hash: item.proposal_hash,
                    expected_revision: item.revision,
                  },
                },
              );
              await refreshCurrent();
              announce("승인 응답을 저장했습니다.");
            },
            decision === "approve" ? "primary" : "quiet",
          ),
        ),
      ),
    );
  else node.append(badge(item.state));
  return node;
}
function fillInput(data) {
  const spec = data.task_spec || {
    intent: (data.run.request_json?.request || "업무 요청").slice(0, 500),
    source_ids: data.run.request_json?.artifact_ids || [],
    inputs: {},
    acceptance: [
      (data.run.request_json?.request || "요청의 조건을 충족한다").slice(
        0,
        500,
      ),
    ],
    required_capabilities: [],
    ambiguity: "missing_input",
  };
  state.inputBase = {
    spec: structuredClone(spec),
    revision: data.run.revision,
  };
  state.inputRunId = data.run.id;
  $("input-intent").value = recipes[spec.intent] ? spec.intent : "custom";
  sourceChoices("input-sources", spec.source_ids);
  for (const [id, key] of [
    ["input-sheet", "sheet"],
    ["input-range", "cell_range"],
    ["input-group", "group_by"],
    ["input-query", "query"],
  ])
    $(id).value = spec.inputs?.[key] || "";
  $("input-values").value = (spec.inputs?.value_columns || []).join(", ");
  $("input-acceptance").value = (spec.acceptance || []).join("\n");
}
function renderRun(data) {
  const run = data.run;
  const archived = run.result_json?.archived === true;
  const steps = archived ? [] : data.steps || [];
  const completed = steps.filter((step) => step.state === "succeeded").length;
  text(
    "progress-count",
    steps.length ? completed + " / " + steps.length + " 완료" : "준비 중",
  );
  $("run-progress").max = Math.max(steps.length, 1);
  $("run-progress").value = completed;
  state.detail = data;
  $("run-empty").hidden = true;
  $("run-detail").hidden = false;
  text("run-title", run.request_json?.request || "선택한 업무");
  text("run-status", tr(run.state) + (archived ? " · 보관 기간 지남" : ""));
  $("run-status").dataset.state = run.state;
  $("cancel-run").hidden =
    terminal.has(run.state) || run.state === "cancel_requested";
  $("run-error").hidden = !run.error_code;
  text(
    "run-error",
    run.error_code
      ? "업무 상태: " +
          tr(run.error_code) +
          " · 필요한 입력이나 설정을 확인해 주세요."
      : "",
  );
  const opened = new Set(
    [...$("todos").children]
      .filter((node) => node.querySelector("details")?.open)
      .map((node) => node.dataset.step),
  );
  $("todos").replaceChildren(
    ...(steps.length
      ? steps.map((step) => {
          const node = todoView(step, data.calls || []);
          if (opened.has(step.id)) node.querySelector("details").open = true;
          return node;
        })
      : [
          empty(
            archived
              ? "보관 기간이 지나 업무 내용을 정리했습니다."
              : run.state === "awaiting_input"
                ? "자료와 입력을 보완하면 할 일을 정리합니다."
                : "할 일을 준비하고 있습니다.",
          ),
        ]),
  );
  $("run-approvals").replaceChildren(
    ...(data.approvals || [])
      .filter((item) => item.state === "pending")
      .map(approvalView),
  );
  $("input-details").hidden = !["queued", "awaiting_input"].includes(run.state);
  $("retry-compose").hidden = !terminal.has(run.state);
  if (
    state.inputRunId !== run.id ||
    (!$("input-details").open &&
      !$("input-form").contains(document.activeElement))
  )
    fillInput(data);
  const results = (data.artifacts || []).filter((item) =>
    ["draft", "result"].includes(item.kind),
  );
  const published = results.filter((item) => item.kind === "result");
  const drafts = results.filter((item) => item.kind === "draft");
  const resultNode = (item) =>
    el(
      "div",
      { class: "result-item", "data-kind": item.kind },
      el(
        "a",
        {
          class: "result-link",
          href:
            "/api/artifacts/" +
            encodeURIComponent(sourceId(item)) +
            "/download",
        },
        el(
          "span",
          { class: "file-type", "aria-hidden": "true" },
          sourceName(item).split(".").pop().slice(0, 5).toUpperCase(),
        ),
        el(
          "span",
          { class: "file-name" },
          sourceName(item),
          el(
            "small",
            {},
            (item.kind === "draft" ? "초안" : "결과") +
              " · " +
              num(item.byte_size) +
              " bytes",
          ),
        ),
        el("span", { class: "download-label" }, "다운로드 ↓"),
      ),
      button(item.pinned ? "고정 해제" : "계속 보관", async () => {
        await api(
          "/api/artifacts/" + encodeURIComponent(sourceId(item)) + "/pin",
          {
            method: "POST",
            body: { pinned: !item.pinned },
          },
        );
        await refreshCurrent();
      }),
    );
  $("run-results").replaceChildren(
    el("h3", {}, run.state === "succeeded" ? "결과 파일" : "작업 파일"),
    ...(results.length
      ? (published.length ? published : drafts).map(resultNode)
      : [el("p", { class: "field-note" }, "아직 생성된 파일이 없습니다.")]),
  );
  if (published.length && drafts.length)
    $("run-results").append(
      details(
        "작성 중 생성된 초안 " + drafts.length + "개",
        el("div", {}, ...drafts.map(resultNode)),
      ),
    );
  if (results.length)
    $("run-results").append(
      el(
        "p",
        { class: "field-note" },
        "완료한 업무와 고정하지 않은 결과는 30일 뒤 정리합니다. 원본 자료와 기억은 보관합니다.",
      ),
    );
  if (run.state !== "succeeded" && results.length)
    $("run-results").append(
      el(
        "p",
        { class: "field-note" },
        "완료되지 않은 업무의 파일입니다. 내용을 확인해 주세요.",
      ),
    );
  $("run-deliveries").replaceChildren(
    ...(data.deliveries || []).map((item) => {
      const card = el(
        "div",
        { class: "delivery-card" },
        badge(item.state, "전달 · " + tr(item.state)),
        el(
          "p",
          { class: "field-note" },
          item.state === "unknown"
            ? "전달 여부를 확인해야 합니다. 자동으로 다시 보내지 않습니다."
            : "파일 생성과 전달은 별도로 기록합니다.",
        ),
      );
      if (["pending", "retry_wait"].includes(item.state))
        card.append(
          button("이 전달 취소", async () => {
            await api(
              "/api/deliveries/" + encodeURIComponent(item.id) + "/cancel",
              {
                method: "POST",
                body: {},
              },
            );
            await refreshCurrent();
          }),
        );
      return card;
    }),
  );
  $("run-usage").replaceChildren(usageView(data.usage));
  $("run-evidence").replaceChildren(
    details("완료 출력과 receipt", jsonView(run.result_json || {})),
    details("단계별 사용량", jsonView(data.usage?.by_step || {})),
  );
}
async function refreshCurrent() {
  if (!state.actor || !state.runId) return;
  const id = state.runId,
    data = await api("/api/runs/" + encodeURIComponent(id));
  if (id !== state.runId) return;
  const signature = JSON.stringify(data);
  if (signature !== state.signature) {
    state.signature = signature;
    renderRun(data);
  }
}
async function saveInputs() {
  if (!state.inputBase || !state.runId) return;
  const spec = structuredClone(state.inputBase.spec),
    inputs = { ...spec.inputs };
  spec.intent =
    $("input-intent").value === "custom"
      ? spec.intent
      : $("input-intent").value;
  spec.source_ids = selected("input-sources");
  spec.acceptance = lines($("input-acceptance").value);
  spec.ambiguity = "clear";
  for (const [id, key] of [
    ["input-sheet", "sheet"],
    ["input-range", "cell_range"],
    ["input-group", "group_by"],
    ["input-query", "query"],
  ]) {
    const value = $(id).value.trim();
    if (value) inputs[key] = value;
    else delete inputs[key];
  }
  const values = commas($("input-values").value);
  if (values.length) inputs.value_columns = values;
  else delete inputs.value_columns;
  if (
    ["aggregate", "weekly_report"].includes(spec.intent) &&
    spec.source_ids.length === 1
  )
    inputs.artifact_id = spec.source_ids[0];
  spec.inputs = inputs;
  await api("/api/runs/" + encodeURIComponent(state.runId) + "/input", {
    method: "POST",
    body: { expected_revision: state.inputBase.revision, task_spec: spec },
  });
  $("input-details").open = false;
  state.inputRunId = null;
  await refreshCurrent();
  announce("보완한 입력을 저장했습니다.");
}
function usageTile(title, data) {
  return el(
    "section",
    { class: "usage-tile" },
    el("h3", {}, title),
    el("p", { class: "amount" }, num(data?.total_tokens) + " 토큰"),
    el("p", {}, tr(data?.usage_state)),
    details("측정 내역", usageView(data)),
  );
}
async function loadHistory() {
  const project = state.project,
    settled = await Promise.allSettled([
      api("/api/runs?" + query({ project_id: project })),
      api("/api/usage?" + query({ project_id: project })),
    ]);
  if (project !== state.project) return;
  if (settled[0].status === "fulfilled") {
    const runs = settled[0].value.runs || [];
    $("history-list").replaceChildren(
      ...(runs.length
        ? runs.map((run) =>
            el(
              "div",
              { class: "history-item" },
              button(run.request_json?.request || "업무", () =>
                selectRun(run.id),
              ),
              el(
                "div",
                {},
                badge(run.state),
                el("small", {}, date(run.created_at)),
              ),
            ),
          )
        : [empty("아직 업무 기록이 없습니다.")]),
    );
  } else throw settled[0].reason;
  if (settled[1].status === "fulfilled")
    $("usage-overview").replaceChildren(
      usageTile("오늘 사용량", settled[1].value.today),
      usageTile("이번 달 사용량", settled[1].value.month),
    );
  else {
    $("usage-overview").replaceChildren(
      empty("사용량을 조회하지 못했습니다. 측정값은 미확인입니다."),
    );
    throw settled[1].reason;
  }
}
function schedule(trigger) {
  if (!trigger) return "실행 조건 없음";
  if (trigger.kind === "once") return "한 번 · " + date(trigger.at);
  if (trigger.kind === "interval")
    return trigger.seconds / 60 + "분마다 · 시작 " + date(trigger.anchor);
  if (trigger.kind === "file_changed")
    return "파일 변화 확인 · " + trigger.poll_seconds + "초마다";
  return (
    (trigger.weekdays || [0, 1, 2, 3, 4, 5, 6])
      .map((day) => ["월", "화", "수", "목", "금", "토", "일"][day])
      .join("·") +
    " " +
    trigger.local_time +
    " · " +
    trigger.timezone
  );
}
async function loadFolders() {
  const project = state.project,
    data = await api("/api/folders?" + query({ project_id: project }));
  if (project !== state.project) return;
  state.folders = data.folders || [];
  const old = $("auto-folder").value;
  $("auto-folder").replaceChildren(
    el("option", { value: "" }, "폴더 선택"),
    ...state.folders.map((folder) =>
      el("option", { value: folder.id }, folder.name || folder.id),
    ),
  );
  $("auto-folder").value = old;
  $("folder-list").replaceChildren(
    ...(state.folders.length
      ? state.folders.map((folder) =>
          el("p", { class: "field-note" }, folder.name || folder.id),
        )
      : [empty("등록한 폴더가 없습니다.")]),
  );
}
async function loadAutomations() {
  const project = state.project,
    data = await api("/api/automations?" + query({ project_id: project }));
  if (project !== state.project) return;
  state.bots = data.automations || [];
  $("automation-list").replaceChildren(
    ...(state.bots.length
      ? state.bots.map((bot) => {
          const config = bot.config_json || {};
          return el(
            "article",
            { class: "card automation-card" },
            badge(
              bot.enabled ? "running" : "pending",
              bot.enabled ? "활성" : "비활성",
            ),
            el("h2", {}, config.name || bot.name),
            el("p", {}, schedule(config.trigger)),
            el(
              "p",
              {},
              recipes[config.recipe] + " · " + formats[config.output_format],
            ),
            el("p", {}, "다음 실행: " + date(bot.next_due_utc)),
            el(
              "div",
              { class: "actions" },
              button(bot.enabled ? "일시 정지" : "검토 후 활성화", async () => {
                if (bot.enabled) {
                  await api("/api/automations/" + encodeURIComponent(bot.id), {
                    method: "PATCH",
                    body: {
                      expected_revision: bot.revision,
                      enabled: false,
                      confirmed_hash: null,
                    },
                  });
                  await loadAutomations();
                  announce("자동화를 일시 정지했습니다.");
                } else await previewAutomation(bot.id);
              }),
              button("수정", () => editAutomation(bot)),
            ),
          );
        })
      : [empty("등록한 자동화가 없습니다. 반복할 업무를 하나 정해 보세요.")]),
  );
}
function autoFields() {
  const kind = $("auto-trigger").value;
  for (const node of document.querySelectorAll("[data-triggers]")) {
    node.hidden = !node.dataset.triggers.split(" ").includes(kind);
    for (const input of node.querySelectorAll("input"))
      input.disabled = node.hidden;
  }
  if (kind === "file_changed") $("auto-source-mode").value = "folder";
  $("auto-source-mode").disabled = kind === "file_changed";
  $("auto-files-field").hidden = $("auto-source-mode").value !== "files";
  $("auto-folder-field").hidden = $("auto-source-mode").value !== "folder";
  $("auto-send-field").hidden = $("auto-delivery").value !== "telegram";
}
function editAutomation(bot = null) {
  state.editingBot = bot;
  const config = bot?.config_json;
  $("automation-form").reset();
  text(
    "automation-editor-title",
    config ? "자동화 수정 · 저장하면 비활성 상태가 됩니다" : "새 자동화 검토안",
  );
  $("auto-at").value = localDate(new Date(Date.now() + 3600000).toISOString());
  for (const node of $("auto-weekdays").querySelectorAll("input"))
    node.checked = true;
  sourceChoices("auto-sources", config?.source_scope?.artifact_ids || []);
  if (config) {
    const trigger = config.trigger;
    for (const [id, key] of [
      ["auto-name", "name"],
      ["auto-recipe", "recipe"],
      ["auto-instruction", "instruction"],
      ["auto-output", "output_format"],
    ])
      $(id).value = config[key];
    $("auto-trigger").value = trigger.kind;
    $("auto-timezone").value = trigger.timezone;
    $("auto-missed").value = trigger.missed_policy;
    $("auto-at").value = localDate(trigger.at || trigger.anchor);
    $("auto-time").value = trigger.local_time || "09:00";
    $("auto-interval").value = (trigger.seconds || 3600) / 60;
    $("auto-poll").value = trigger.poll_seconds || 60;
    $("auto-initial").checked = trigger.run_initial === true;
    for (const node of $("auto-weekdays").querySelectorAll("input"))
      node.checked = (trigger.weekdays || [0, 1, 2, 3, 4, 5, 6]).includes(
        Number(node.value),
      );
    $("auto-limit").value = config.limits.max_runs_per_day;
    $("auto-source-mode").value = config.source_scope.folder_id
      ? "folder"
      : config.source_scope.artifact_ids.length
        ? "files"
        : "none";
    $("auto-folder").value = config.source_scope.folder_id || "";
    $("auto-max-files").value = config.source_scope.max_files;
    $("auto-delivery").value = config.delivery.channel;
    $("auto-send").checked = config.authority.send_result_to_owner;
  }
  autoFields();
  $("automation-editor").open = true;
  switchView("automations");
  $("auto-name").focus();
}
function automationConfig() {
  const kind = $("auto-trigger").value,
    trigger = {
      kind,
      timezone: $("auto-timezone").value.trim(),
      missed_policy: $("auto-missed").value,
    };
  if (kind === "once") trigger.at = iso($("auto-at").value);
  if (["daily", "weekly"].includes(kind)) {
    trigger.local_time = $("auto-time").value;
    trigger.weekdays = selected("auto-weekdays").map(Number);
  }
  if (kind === "interval") {
    trigger.seconds = Number($("auto-interval").value) * 60;
    trigger.anchor = iso($("auto-at").value);
  }
  if (kind === "file_changed") {
    trigger.poll_seconds = Number($("auto-poll").value);
    trigger.run_initial = $("auto-initial").checked;
  }
  const mode = $("auto-source-mode").value,
    channel = $("auto-delivery").value;
  if (channel === "telegram" && !$("auto-send").checked)
    throw new Error("Telegram 전달은 결과 전송 허용을 선택해 주세요.");
  if (mode === "folder" && !$("auto-folder").value)
    throw new Error("등록한 폴더를 선택해 주세요.");
  return {
    name: $("auto-name").value.trim(),
    project_id: state.project,
    recipe: $("auto-recipe").value,
    instruction: $("auto-instruction").value.trim(),
    trigger,
    source_scope: {
      artifact_ids: mode === "files" ? selected("auto-sources") : [],
      folder_id: mode === "folder" ? $("auto-folder").value : null,
      max_files: Number($("auto-max-files").value),
    },
    quality_mode: "balanced",
    review_mode: "normal",
    delivery: { channel, recipient_ref: "owner" },
    authority: {
      mode: "read_and_draft",
      send_result_to_owner: channel === "telegram" && $("auto-send").checked,
    },
    limits: { max_runs_per_day: Number($("auto-limit").value), max_todos: 8 },
    output_format: $("auto-output").value,
  };
}
async function saveAutomation() {
  const config = automationConfig(),
    old = state.editingBot;
  const result = old
    ? await api("/api/automations/" + encodeURIComponent(old.id), {
        method: "PATCH",
        body: { expected_revision: old.revision, config },
      })
    : await api("/api/automations", { method: "POST", body: config });
  state.editingBot = null;
  $("automation-editor").open = false;
  await loadAutomations();
  announce("검토안을 저장했습니다. 활성화 전 범위를 확인해 주세요.");
  await previewAutomation(result.automation.id);
}
async function previewAutomation(id) {
  await loadAutomations();
  const bot = state.bots.find((item) => item.id === id);
  if (!bot) throw new Error("자동화 정보를 다시 확인해 주세요.");
  state.preview = bot;
  const config = bot.config_json,
    scope = config.source_scope;
  const sources = scope.folder_id
    ? "등록 폴더 " +
      (state.folders.find((item) => item.id === scope.folder_id)?.name ||
        scope.folder_id) +
      " · 최대 " +
      scope.max_files +
      "개"
    : scope.artifact_ids
        .map((id) =>
          sourceName(
            state.sources.find((item) => sourceId(item) === id) || { id },
          ),
        )
        .join(", ") || "자료 없음";
  const list = el("dl", { class: "preview-list" });
  for (const [name, value] of [
    ["이름", config.name],
    ["업무", config.instruction],
    ["자료", sources],
    ["일정", schedule(config.trigger)],
    [
      "누락 처리",
      config.trigger.missed_policy === "skip"
        ? "건너뛰기"
        : "최근 24시간의 마지막 한 번",
    ],
    ["결과", formats[config.output_format]],
    [
      "대상",
      config.delivery.channel === "telegram"
        ? "현재 연결한 내 Telegram"
        : "이 업무 공간",
    ],
    ["횟수", "하루 최대 " + config.limits.max_runs_per_day + "회"],
    [
      "권한",
      config.authority.send_result_to_owner
        ? "읽기 · 새 결과 생성 · 연결한 본인에게 전달"
        : "읽기 · 새 결과 생성",
    ],
  ])
    list.append(el("dt", {}, name), el("dd", {}, value));
  $("automation-preview").replaceChildren(
    list,
    details(
      "정확한 설정과 승인 해시",
      jsonView({
        revision: bot.revision,
        proposal_hash: bot.proposal_hash,
        config,
      }),
    ),
  );
  $("preview-dialog").querySelector(".dialog-status").textContent = "";
  $("preview-dialog").showModal();
}
function field(title, control) {
  const id = "control-" + ++serial;
  control.id = id;
  return el("div", {}, el("label", { for: id }, title), control);
}
function profileInput(name, value, attrs = {}) {
  return el("input", { "data-field": name, value: value ?? "", ...attrs });
}
function profileCard(profile = null) {
  const base = profile || {
    binding: {
      provider: "xai",
      model_id: "",
      roles: ["L"],
      role: null,
      enabled: false,
      available: false,
      allowed_projects: [state.project],
      capabilities: [],
      context_window: 0,
      max_output: 0,
      request_timeout_s: 60,
      reasoning_efforts: [],
      reasoning_effort: null,
      quality_evidence_id: null,
      metering_mode: "api",
    },
    price_snapshot: {
      rates: {
        input: null,
        cached_input: null,
        cache_write: null,
        output: null,
      },
      checked_at: null,
      source_url: null,
      cache_write_billing: "unknown",
    },
    quality_evidence_sha256: null,
  };
  const binding = base.binding,
    card = el("section", { class: "profile-card" });
  card._profile = structuredClone(base);
  card.append(
    el(
      "div",
      { class: "section-top" },
      el("h3", {}, "xAI 모델 프로필"),
      button("프로필 제거", () => card.remove(), "quiet danger"),
    ),
    field(
      "정확한 모델 ID",
      profileInput("model_id", binding.model_id, {
        required: true,
        maxlength: 200,
        placeholder: "계정에 제공된 모델 ID",
      }),
    ),
  );
  card.append(
    el(
      "fieldset",
      {},
      el("legend", {}, "배정 가능한 역할"),
      el(
        "div",
        { class: "check-row" },
        ["L", "M", "H"].map((role) =>
          el(
            "label",
            { class: "check" },
            el("input", {
              type: "checkbox",
              "data-role": role,
              value: role,
              checked: binding.roles.includes(role),
            }),
            role,
          ),
        ),
      ),
    ),
    el(
      "label",
      { class: "check" },
      el("input", {
        type: "checkbox",
        "data-field": "enabled",
        checked: binding.enabled,
      }),
      "이 프로필 사용",
    ),
  );
  card.append(
    el(
      "div",
      { class: "form-row" },
      field(
        "문맥 한도 · 확인된 토큰 수",
        profileInput("context_window", binding.context_window || "", {
          type: "number",
          min: 1024,
          max: 2000000,
          required: true,
        }),
      ),
      field(
        "출력 한도 · 토큰 수",
        profileInput("max_output", binding.max_output || "", {
          type: "number",
          min: 1,
          max: 131072,
          required: true,
        }),
      ),
    ),
    field(
      "지원 기능 · 쉼표로 구분",
      profileInput("capabilities", binding.capabilities.join(", "), {
        required: true,
        placeholder: "structured_output, summarize",
      }),
    ),
  );
  const extra = el(
    "div",
    {},
    field(
      "실제 품질 검증 보고서 ID",
      profileInput("quality_evidence_id", binding.quality_evidence_id, {
        maxlength: 96,
      }),
    ),
    field(
      "검증 보고서 SHA-256",
      profileInput("quality_evidence_sha256", base.quality_evidence_sha256, {
        pattern: "[a-f0-9]{64}",
        maxlength: 64,
      }),
    ),
    el(
      "p",
      { class: "field-note" },
      "로컬 evaluations 폴더에 준비한 실제 보고서의 ID와 해시입니다. 키나 역할 선택만으로 품질 검증을 통과하지 않습니다.",
    ),
  );
  extra.append(
    field(
      "사용 가능한 추론 설정 · 쉼표로 구분",
      profileInput("reasoning_efforts", binding.reasoning_efforts.join(", "), {
        placeholder: "none, low, high",
      }),
    ),
    field(
      "사용할 추론 설정 · 없으면 비움",
      profileInput("reasoning_effort", binding.reasoning_effort),
    ),
    field(
      "요청 제한 시간 · 초",
      profileInput("request_timeout_s", binding.request_timeout_s, {
        type: "number",
        min: 1,
        max: 300,
        required: true,
      }),
    ),
    el(
      "fieldset",
      {},
      el("legend", {}, "자료 사용을 허용할 프로젝트"),
      state.actor.project_ids.map((project) =>
        el(
          "label",
          { class: "check" },
          el("input", {
            type: "checkbox",
            "data-project": project,
            value: project,
            checked: binding.allowed_projects.includes(project),
          }),
          project,
        ),
      ),
    ),
  );
  card.append(details("품질 근거와 세부 설정", extra));
  return card;
}
function readProfiles() {
  return [...$("model-profiles").children].map((card) => {
    const profile = structuredClone(card._profile),
      get = (name) => card.querySelector('[data-field="' + name + '"]'),
      binding = profile.binding;
    binding.model_id = get("model_id").value.trim();
    binding.enabled = get("enabled").checked;
    binding.available = false;
    binding.role = null;
    binding.roles = [...card.querySelectorAll("[data-role]:checked")].map(
      (node) => node.value,
    );
    if (!binding.roles.length)
      throw new Error("모델의 L·M·H 역할을 하나 이상 선택해 주세요.");
    binding.capabilities = commas(get("capabilities").value);
    binding.context_window = Number(get("context_window").value);
    binding.max_output = Number(get("max_output").value);
    binding.request_timeout_s = Number(get("request_timeout_s").value);
    binding.reasoning_efforts = commas(get("reasoning_efforts").value);
    binding.reasoning_effort = get("reasoning_effort").value.trim() || null;
    binding.quality_evidence_id =
      get("quality_evidence_id").value.trim() || null;
    binding.allowed_projects = [
      ...card.querySelectorAll("[data-project]:checked"),
    ].map((node) => node.value);
    profile.quality_evidence_sha256 =
      get("quality_evidence_sha256").value.trim() || null;
    return profile;
  });
}
function keyStatus(data) {
  text(
    "key-state",
    "xAI 키: " +
      (data.key_present ? "등록됨" : "없음") +
      " · Telegram 토큰: " +
      (data.telegram_key_present ? "등록됨" : "없음"),
  );
}
function modelStatus(data) {
  const statuses = Array.isArray(data.model_status)
    ? data.model_status
    : data.model_status?.statuses || [];
  $("model-status").replaceChildren(
    ...(statuses.length
      ? statuses.map((item) =>
          el(
            "div",
            { class: "model-observation" },
            el("strong", {}, item.model_id),
            el(
              "p",
              {},
              "가용성: " +
                tr(item.state) +
                " · 품질 근거: " +
                tr(item.qualification),
            ),
          ),
        )
      : [
          el(
            "p",
            { class: "field-note" },
            "모델 가용성과 품질 자격은 아직 확인되지 않았습니다.",
          ),
        ]),
  );
}
function renderSettings(data) {
  state.settings = data;
  const config = data.config;
  modelStatus(data);
  $("model-profiles").replaceChildren(
    ...(config.models || []).map(profileCard),
  );
  const privacy = (config.privacy || []).find(
    (item) => item.project_id === state.project,
  );
  $("privacy-cloud").checked = privacy?.allow_cloud === true;
  $("privacy-local").checked = privacy?.local_only === true;
  $("telegram-enabled").checked = config.telegram?.enabled === true;
  $("telegram-connection").value = config.telegram?.connection_id || "telegram";
  $("telegram-project").value = config.telegram?.project_id || state.project;
  const connection = data.telegram_connection;
  text(
    "telegram-state",
    connection?.user_id && connection?.chat_id
      ? "개인 대화 연결됨 · 연결 revision " + connection.revision
      : "개인 대화가 연결되지 않았습니다.",
  );
  keyStatus(data);
  $("capabilities").replaceChildren(
    data.capabilities
      ? jsonView(data.capabilities)
      : el("p", { class: "field-note" }, "지원 상태 정보가 아직 없습니다."),
  );
}
async function loadSettings() {
  renderSettings(await api("/api/settings"));
}
async function saveSettings() {
  if (!state.settings) throw new Error("설정을 먼저 불러와 주세요.");
  const old = (state.settings.config.privacy || []).find(
      (item) => item.project_id === state.project,
    ),
    allow = $("privacy-cloud").checked,
    local = $("privacy-local").checked;
  const changed =
    !old ||
    old.allow_cloud !== allow ||
    old.local_only !== local ||
    JSON.stringify(old.allowed_providers) !==
      JSON.stringify(allow ? ["xai"] : []);
  const privacy = (state.settings.config.privacy || []).filter(
    (item) => item.project_id !== state.project,
  );
  privacy.push({
    project_id: state.project,
    allow_cloud: allow,
    local_only: local,
    allowed_providers: allow ? ["xai"] : [],
    revision: (old?.revision || 0) + (changed ? 1 : 0),
  });
  await api("/api/settings", {
    method: "PATCH",
    body: {
      expected_revision: state.settings.revision,
      models: readProfiles(),
      privacy,
      telegram: {
        enabled: $("telegram-enabled").checked,
        connection_id: $("telegram-connection").value.trim(),
        project_id: $("telegram-project").value,
      },
    },
  });
  await loadSettings();
  announce(
    "설정을 저장했습니다. 모델 자격은 별도의 실제 검증 결과를 따릅니다.",
  );
}
async function loadMemories() {
  const data = await api(
    "/api/memories?" +
      query({
        project_id: state.project,
        query: $("memory-query").value.trim(),
      }),
  );
  $("memory-list").replaceChildren(
    ...((data.memories || []).length
      ? data.memories.map((item) =>
          el(
            "article",
            { class: "memory-item" },
            badge(item.category),
            el("p", {}, item.text),
            el(
              "small",
              {},
              "수정 " +
                date(item.updated_at) +
                (item.expires_at ? " · 만료 " + date(item.expires_at) : ""),
            ),
            el(
              "div",
              { class: "actions" },
              button("수정", () => {
                state.editingMemory = item;
                $("memory-category").value = item.category;
                $("memory-text").value = item.text;
                $("memory-expires").value = localDate(item.expires_at);
                text("memory-save", "기억 수정 저장");
                $("memory-text").focus();
              }),
              button(
                "삭제",
                async () => {
                  await api("/api/memories/" + encodeURIComponent(item.id), {
                    method: "DELETE",
                    body: {
                      project_id: state.project,
                      expected_revision: item.revision,
                      purge_original: false,
                    },
                  });
                  if (state.editingMemory?.id === item.id) resetMemory();
                  await loadMemories();
                  announce("선택한 기억을 삭제했습니다.");
                },
                "quiet danger",
              ),
              button(
                "원문도 제거",
                async () => {
                  if (
                    !window.confirm(
                      "이 기억의 모든 저장 버전 파일까지 제거할까요? 참고한 업무 자료는 유지됩니다. 진행 중인 업무가 참조하면 파일 제거는 보류됩니다.",
                    )
                  )
                    return;
                  const result = await api(
                    "/api/memories/" + encodeURIComponent(item.id),
                    {
                      method: "DELETE",
                      body: {
                        project_id: state.project,
                        expected_revision: item.revision,
                        purge_original: true,
                      },
                    },
                  );
                  if (state.editingMemory?.id === item.id) resetMemory();
                  await loadMemories();
                  announce(
                    result.purge_pending
                      ? "기억을 검색에서 제외했습니다. 진행 중인 업무가 참조하는 파일은 제거 대기 중입니다."
                      : "기억과 저장된 모든 버전 파일을 제거했습니다.",
                  );
                },
                "quiet danger",
              ),
            ),
          ),
        )
      : [empty("명시적으로 저장한 기억이 없습니다.")]),
  );
}
function resetMemory() {
  state.editingMemory = null;
  $("memory-form").reset();
  text("memory-save", "기억 저장");
}
async function saveMemory() {
  const old = state.editingMemory,
    args = {
      text: $("memory-text").value.trim(),
      category: $("memory-category").value,
      evidence_refs: old?.evidence_refs || [],
      expires_at: $("memory-expires").value
        ? iso($("memory-expires").value)
        : null,
    };
  if (old)
    await api("/api/memories/" + encodeURIComponent(old.id), {
      method: "PATCH",
      body: {
        project_id: state.project,
        expected_revision: old.revision,
        args,
      },
    });
  else
    await api("/api/memories", {
      method: "POST",
      body: { project_id: state.project, args },
    });
  resetMemory();
  await loadMemories();
  announce("요청한 내용을 기억에 저장했습니다.");
}
async function refreshAll() {
  const settled = await Promise.allSettled([
    loadSources(),
    loadHistory(),
    loadAutomations(),
    loadFolders(),
  ]);
  const errors = settled.filter((item) => item.status === "rejected");
  if (errors.length) announce(errors[0].reason.message, true);
}
async function enter(session) {
  state.actor = session.actor;
  state.csrf = session.csrf_token;
  $("project").replaceChildren(
    ...session.actor.project_ids.map((id) => el("option", { value: id }, id)),
  );
  $("telegram-project").replaceChildren(
    ...session.actor.project_ids.map((id) => el("option", { value: id }, id)),
  );
  state.project = session.actor.project_ids[0];
  $("login").hidden = true;
  $("app").hidden = false;
  $("bootstrap-code").value = "";
  text("connection-state", "로컬 연결됨");
  await refreshAll();
  $("request").focus();
}
for (const node of document.querySelectorAll("[data-view]"))
  node.addEventListener("click", () => switchView(node.dataset.view, true));
document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault();
  switchView("conversation", true);
});
$("settings-dialog").addEventListener("close", () =>
  $("open-settings").focus(),
);
$("preview-dialog").addEventListener("close", () =>
  $("new-automation").focus(),
);
for (const node of document.querySelectorAll("[data-close]"))
  node.addEventListener("click", () => $(node.dataset.close).close());
for (const [day, name] of ["월", "화", "수", "목", "금", "토", "일"].entries())
  $("auto-weekdays").append(
    el(
      "label",
      { class: "check" },
      el("input", { type: "checkbox", value: day, checked: true }),
      name,
    ),
  );
for (const id of ["auto-trigger", "auto-source-mode", "auto-delivery"])
  $(id).addEventListener("change", autoFields);
$("source-choices").addEventListener("change", attachmentCount);
$("project").addEventListener("change", () =>
  action(null, async () => {
    state.project = $("project").value;
    state.runId = null;
    state.detail = null;
    state.selected.clear();
    state.pendingRun = null;
    state.inputBase = null;
    $("run-detail").hidden = true;
    $("run-empty").hidden = false;
    $("automation-editor").open = false;
    state.editingBot = null;
    resetMemory();
    await refreshAll();
  }),
);
$("upload-files").addEventListener("change", () =>
  action($("run-submit"), async () => {
    state.uploading = true;
    try {
      const files = [...$("upload-files").files];
      if (state.selected.size + files.length > 20)
        throw new Error("한 번에 선택할 자료는 최대 20개입니다.");
      for (const file of files) await upload(file);
      $("upload-files").value = "";
      announce(files.length + "개 자료를 추가했습니다.");
    } finally {
      state.uploading = false;
    }
  }),
);
$("example").addEventListener("click", () =>
  action($("example"), async () => {
    state.uploading = true;
    try {
      const artifact = await upload(
        new File(
          ["항목,금액\n자료 정리,12000\n회의 준비,8000\n문서 작성,15000\n"],
          "예제-금액.csv",
          { type: "text/csv" },
        ),
      );
      state.selected = new Set([sourceId(artifact)]);
      sourceChoices("source-choices", [...state.selected]);
      attachmentCount();
      $("request").value = "금액 합계 내줘";
      $("output-format").value = "xlsx";
      $("quality").value = "balanced";
      announce(
        "합성 CSV를 준비했습니다. 예상 합계는 35,000입니다. 업무 시작을 누르면 계산합니다.",
      );
      $("request").focus();
    } finally {
      state.uploading = false;
    }
  }),
);
$("cancel-run").addEventListener("click", () =>
  action($("cancel-run"), async () => {
    await api("/api/runs/" + encodeURIComponent(state.runId) + "/cancel", {
      method: "POST",
      body: {},
    });
    await refreshCurrent();
    announce(
      "취소 요청을 저장했습니다. 진행 중인 작업의 종료 상태를 확인합니다.",
    );
  }),
);
$("retry-compose").addEventListener("click", () => {
  const request = state.detail?.run?.request_json;
  if (!request) return;
  $("request").value = request.request;
  $("output-format").value = request.output_format;
  $("quality").value = request.quality_mode;
  state.selected = new Set(request.artifact_ids);
  sourceChoices("source-choices", [...state.selected]);
  attachmentCount();
  state.pendingRun = null;
  $("request").focus();
  announce(
    "새 요청을 준비했습니다. 필요한 내용을 고친 뒤 업무 시작을 눌러 주세요.",
  );
});
$("new-automation").addEventListener("click", () => editAutomation());
$("cancel-auto-edit").addEventListener("click", () => {
  state.editingBot = null;
  $("automation-editor").open = false;
  $("new-automation").focus();
});
$("enable-automation").addEventListener("click", () =>
  action($("enable-automation"), async () => {
    const bot = state.preview;
    if (!bot?.proposal_hash) throw new Error("검토안을 다시 불러와 주세요.");
    await api("/api/automations/" + encodeURIComponent(bot.id), {
      method: "PATCH",
      body: {
        expected_revision: bot.revision,
        enabled: true,
        confirmed_hash: bot.proposal_hash,
      },
    });
    $("preview-dialog").close();
    await loadAutomations();
    announce("검토한 자동화를 활성화했습니다.");
  }),
);
$("refresh-history").addEventListener("click", () =>
  action($("refresh-history"), loadHistory),
);
$("open-settings").addEventListener("click", () =>
  action($("open-settings"), async () => {
    await loadSettings();
    const loaded = await Promise.allSettled([loadFolders(), loadMemories()]);
    $("settings-dialog").querySelector(".dialog-status").textContent = "";
    $("settings-dialog").showModal();
    for (const result of loaded)
      if (result.status === "rejected") announce(result.reason.message, true);
  }),
);
$("add-model").addEventListener("click", () => {
  $("model-profiles").append(profileCard());
  $("model-profiles").lastChild.querySelector("input").focus();
});
$("check-models").addEventListener("click", () =>
  action($("check-models"), async () => {
    const data = await api("/api/settings/check-models", {
      method: "POST",
      body: {},
    });
    modelStatus(data);
    keyStatus(data);
    announce(
      "모델 목록 조회를 마쳤습니다. 가용성과 품질 상태를 확인해 주세요.",
    );
  }),
);
$("privacy-local").addEventListener("change", () => {
  if ($("privacy-local").checked) $("privacy-cloud").checked = false;
});
$("privacy-cloud").addEventListener("change", () => {
  if ($("privacy-cloud").checked) $("privacy-local").checked = false;
});
$("create-pairing").addEventListener("click", () =>
  action($("create-pairing"), async () => {
    const pair = await api("/api/telegram/pairing", {
      method: "POST",
      body: {},
    });
    text("pairing-code", "/start " + pair.code);
    text(
      "pairing-expiry",
      "내 봇의 개인 대화에 위 명령을 보내세요. 만료: " +
        date(pair.expires_at) +
        " · 한 번만 사용할 수 있습니다.",
    );
  }),
);
$("memory-new").addEventListener("click", () => {
  resetMemory();
  $("memory-text").focus();
});
form("login-form", async () => {
  const code = $("bootstrap-code").value;
  try {
    const result = await api("/api/bootstrap", {
      method: "POST",
      body: { code },
    });
    state.csrf = result.csrf_token;
    await enter(await api("/api/session"));
  } finally {
    $("bootstrap-code").value = "";
  }
});
form("run-form", startRun);
form("input-form", saveInputs);
form("automation-form", saveAutomation);
form("settings-form", saveSettings);
form("memory-form", saveMemory);
form("memory-search-form", loadMemories);
form("secret-form", async () => {
  try {
    await api("/api/secrets", {
      method: "POST",
      body: {
        provider: $("secret-provider").value,
        value: $("secret-value").value,
      },
    });
    const data = await api("/api/settings");
    state.settings = data;
    keyStatus(data);
    announce("키 저장 요청을 처리했습니다. 입력한 값은 지웠습니다.");
  } finally {
    $("secret-value").value = "";
    $("secret-confirm").checked = false;
  }
});
form("folder-form", async () => {
  await api("/api/folders", {
    method: "POST",
    body: {
      project_id: state.project,
      path: $("folder-path").value.trim(),
      explicit_confirmed: $("folder-confirm").checked,
    },
  });
  $("folder-path").value = "";
  $("folder-confirm").checked = false;
  await loadFolders();
  announce("선택한 프로젝트의 자료 폴더를 등록했습니다.");
});
autoFields();
api("/api/session")
  .then(enter)
  .catch((error) => {
    if (error.status !== 401) announce(error.message, true);
  });
setInterval(async () => {
  if (!state.actor || state.polling || document.hidden) return;
  state.polling = true;
  try {
    await refreshCurrent();
    if (!$("view-history").hidden) await loadHistory();
    text("connection-state", "로컬 연결됨");
  } catch {
    text("connection-state", "연결 확인 필요");
  } finally {
    state.polling = false;
  }
}, 1000);
