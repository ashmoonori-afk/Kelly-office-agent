"""TODO complexity and fixed recipes. Routing never receives prices or credit."""

import re

from pydantic import ValidationError

from .contracts import (
    OUTPUT_SCHEMA_KEYS,
    AppError,
    ArtifactInput,
    EvaluationRoute,
    ModelBinding,
    ModelExecutor,
    PlanV1,
    PrivacyPolicy,
    Requirement,
    RunRequest,
    StepInput,
    TaskFacts,
    TaskSpec,
    TodoV1,
    ToolExecutor,
    digest,
)
from .tools import AggregateArgs, ExtractArgs, SearchArgs

KIND_ROLE = {"simple": "L", "standard": "M", "complex": "H"}
COMPLEX_SIGNALS = {
    "conflicting_sources",
    "coupled_constraints",
    "causal_judgment",
    "alternative_evaluation",
}
RECIPES = {
    "aggregate",
    "summarize",
    "weekly_report",
    "meeting_actions",
    "draft_reply",
    "search_answer",
}
DYNAMIC_PATTERN = r"원인|인과|가설|대안|상충|충돌|모순|전략|제약|자동|매일|평일|정시|보내|전송|제외|우선순위|번역|이상|이하|기간"


def normalize_todo(todo: TodoV1, trusted_facts: TaskFacts) -> TodoV1:
    """Repair explicit contradictions without another model or changing authorization."""
    todo = TodoV1.model_validate(todo.model_dump())
    facts = TaskFacts.model_validate(trusted_facts.model_dump())
    if not facts.supported:
        raise AppError("UNSUPPORTED_CAPABILITY")
    if todo.usefulness == "optional":
        return todo.model_copy(update={"status": "skipped"})
    if not facts.requested_or_supporting:
        raise AppError("PLAN_INVALID")
    if not facts.required_inputs_present or todo.clarity == "missing_input":
        return todo.model_copy(update={"clarity": "missing_input", "status": "pending"})
    if facts.certified_deterministic:
        if not isinstance(todo.executor, ToolExecutor):
            raise AppError("PLAN_INVALID")
        return todo.model_copy(
            update={
                "kind": "mechanical",
                "signals": ["fixed_transform"],
                "reason": "서버가 확인한 고정 도구와 규칙으로 처리한다",
            }
        )
    if isinstance(todo.executor, ToolExecutor):
        # A label cannot turn an unverified operation into executable deterministic work.
        raise AppError("PLAN_INVALID")
    signals = list(dict.fromkeys(todo.signals))
    for name in sorted(COMPLEX_SIGNALS):
        if getattr(facts, name) and name not in signals:
            signals.append(name)
    kind, reason = todo.kind, todo.reason
    if COMPLEX_SIGNALS.intersection(signals):
        kind, reason = "complex", "상충 근거·결합 조건 또는 원인·대안 판단이 필요하다"
    elif kind == "mechanical":
        raise AppError("PLAN_INVALID")
    elif todo.clarity == "uncertain" and kind == "simple":
        kind, reason = "standard", "자료는 있지만 해석을 확인하고 내용을 종합해야 한다"
    if not facts.privacy_allows_candidate:
        raise AppError("POLICY_DENIED")
    if not facts.capable_model_available:
        raise AppError("NO_CAPABLE_MODEL")
    return todo.model_copy(
        update={
            "kind": kind,
            "signals": signals,
            "reason": reason,
            "executor": todo.executor.model_copy(update={"role": KIND_ROLE[kind]}),
        }
    )


def choose_model(todo: TodoV1, capability_catalog, privacy_policy: PrivacyPolicy):
    """Use configured candidate order, qualified role, task, availability and privacy only."""
    policy = PrivacyPolicy.model_validate(privacy_policy.model_dump())
    if todo.status == "skipped" or todo.usefulness == "optional" or todo.clarity == "missing_input":
        return None
    if isinstance(todo.executor, ToolExecutor):
        if todo.kind != "mechanical":
            raise AppError("PLAN_INVALID")
        return None
    role = KIND_ROLE.get(todo.kind)
    if role is None or todo.executor.role != role:
        raise AppError("PLAN_INVALID")
    return _choose_binding(role, todo.executor.task, capability_catalog, policy)


def choose_planner(capability_catalog, privacy_policy: PrivacyPolicy, *, role="M"):
    """Planning is one M call, not a synthetic user TODO or separate classifier."""
    policy = PrivacyPolicy.model_validate(privacy_policy.model_dump())
    if role not in {"M", "H"}:
        raise AppError("PLAN_INVALID")
    return _choose_binding(role, "planning", capability_catalog, policy)


def choose_evaluation_model(
    todo, capability_catalog, privacy_policy, *, arm, attempt, run, actor, snapshot
):
    """Keep the production classification; only the declared baseline execution role differs."""
    recommended = choose_model(todo, capability_catalog, privacy_policy)
    if arm not in {"A", "B"} or type(attempt) is not int or not 1 <= attempt <= 2:
        raise AppError("INVALID_INPUT")
    if recommended is None or todo.executor.task == "semantic_review":
        raise AppError("PLAN_INVALID")
    role = "H" if arm == "A" else "L" if attempt == 1 else "M"
    selected = _choose_binding(role, todo.executor.task, capability_catalog, privacy_policy)
    route = EvaluationRoute(
        arm=arm,
        actor_id=actor.id,
        project_id=run["project_id"],
        run_id=run["id"],
        run_revision=run["revision"],
        todo_id=todo.id,
        todo_digest=digest(todo),
        binding_digest=digest(selected),
        attempt=attempt,
        privacy=privacy_policy,
    )
    route.check(todo, selected, run, actor, snapshot=snapshot)
    return selected, route


def _choose_binding(role, task, capability_catalog, policy):
    denied = False
    for value in capability_catalog:
        candidate = ModelBinding.model_validate(
            value.model_dump() if isinstance(value, ModelBinding) else value
        )
        if (
            not candidate.enabled
            or not candidate.available
            or role not in candidate.roles
            or policy.project_id not in candidate.allowed_projects
            or candidate.quality_evidence_id is None
            or not {"structured_output", task} <= set(candidate.capabilities)
        ):
            continue
        local = candidate.metering_mode == "local"
        if candidate.provider not in policy.allowed_providers or (
            not local and (policy.local_only or not policy.allow_cloud)
        ):
            denied = True
            continue
        effort = {"L": "minimal", "M": "medium", "H": "high"}[role]
        if role == "L" and effort not in candidate.reasoning_efforts:
            effort = next(
                (value for value in ("none", "low") if value in candidate.reasoning_efforts), effort
            )
        return candidate.model_copy(
            update={
                "role": role,
                "reasoning_effort": effort if effort in candidate.reasoning_efforts else None,
            }
        )
    raise AppError("POLICY_DENIED" if denied else "NO_CAPABLE_MODEL")


def _recognize(text):
    if re.search(
        r"재계산|OCR|매크로\s*실행|(?:PPTX|HWPX?|파워포인트)\s*(?:생성|작성|만들)",
        text,
        re.IGNORECASE,
    ):
        raise AppError("UNSUPPORTED_CAPABILITY")
    if re.search(DYNAMIC_PATTERN, text):
        return None
    matches = set()
    if re.search(r"합계|합산|집계|합쳐", text):
        matches.add("aggregate")
    if re.search(r"(?:보고서|주간보고).*(?:만들|작성)|(?:만들|작성).*보고서", text):
        matches.add("weekly_report")
    if re.search(r"요약|축약", text):
        matches.add("summarize")
    if re.search(r"(?:회의|미팅).*(?:할\s*일|액션|담당자|해야\s*할|실행항목)", text):
        matches.add("meeting_actions")
    if re.search(r"(?:답장|회신).*(?:초안|작성|만들)", text):
        matches.add("draft_reply")
    if re.search(r"(?:결정|제안서|회의).*(?:뭐였|찾아|검색|알려)|(?:찾아|검색).*결정", text):
        matches.add("search_answer")
    if matches == {"aggregate", "weekly_report"}:
        return "weekly_report"
    return next(iter(matches)) if len(matches) == 1 else None


def _without_output_phrase(text, output_format):
    # This exact phrase only restates the already-confirmed output format.
    # Other format names and additional instructions remain for the planner.
    format_name = r"(?:text|txt)" if output_format == "text" else re.escape(output_format)
    return re.sub(
        rf"(?<!\w){format_name}(?:\s*파일)?(?:으)?로\s*"
        r"(?:만들어|작성해)\s*(?:주세요|줘)(?!\w)",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _fixed_scope(text, recipe, sources, output_format):
    """Conservative shortcuts: unexplained words go to the planner, never disappear."""
    text = _without_output_phrase(text, output_format)
    text = re.sub(r"[\"'‘“][^\"'’”]{1,500}[\"'’”]", "", text) if recipe == "search_answer" else text
    vocabulary = [
        "첨부한",
        "업로드한",
        "해주세요",
        "해 주세요",
        "해줘",
        "주세요",
        "줘요",
        "내줘",
        "자료",
        "문서",
        "파일",
        "내용",
        "매출표",
        "표",
        "이",
        "그",
        "를",
        "을",
        "에서",
        "으로",
        "로",
    ]
    vocabulary += {
        "aggregate": ["합계", "합산", "집계", "합쳐", "계산", "별로", "별", "열", "해", "줘", "내"],
        "weekly_report": [
            "합계",
            "합산",
            "집계",
            "해서",
            "별로",
            "별",
            "열",
            "보고서",
            "주간보고",
            "작성",
            "만들어",
            "만들",
            "해",
            "줘",
        ],
        "summarize": ["회의록", "요약", "축약", "짧게", "간단히", "해", "줘"],
        "meeting_actions": [
            "회의록",
            "회의",
            "미팅",
            "할 일",
            "할일",
            "담당자",
            "액션",
            "실행항목",
            "뽑아",
            "추출",
            "정리",
            "해",
            "줘",
        ],
        "draft_reply": ["이메일", "메일", "답장", "회신", "초안", "작성", "만들어", "해", "줘"],
        "search_answer": [
            "지난번",
            "제안서",
            "결정",
            "이",
            "뭐였지",
            "찾아",
            "검색",
            "알려",
            "해",
            "줘",
        ],
    }[recipe]
    if recipe in {"aggregate", "weekly_report"}:
        vocabulary += [
            h for source in sources for h in source.get("headers", []) if isinstance(h, str)
        ]
    text = re.sub(r"\s+", "", text)
    for token in sorted(set(vocabulary), key=len, reverse=True):
        text = text.replace(re.sub(r"\s+", "", token), "")
    return not text.strip(" .!?,'\"‘’“”`，。！？")


def _todo(
    todos,
    title,
    *,
    tool=None,
    args=None,
    task=None,
    kind="mechanical",
    schema,
    inputs=(),
    depends=(),
    checks=(),
    done=(),
    clarity="clear",
    reason=None,
):
    refs = list(inputs)
    dependencies = list(
        dict.fromkeys([*depends, *[ref.step_id for ref in refs if isinstance(ref, StepInput)]])
    )
    todo = TodoV1(
        id=f"t{len(todos) + 1}",
        title=title,
        requirement_ids=["r1"],
        depends_on=dependencies,
        inputs=refs,
        kind=kind,
        reason=reason
        or (
            "확정된 도구와 규칙으로 처리한다"
            if tool
            else "명시된 내용을 추출한다"
            if kind == "simple"
            else "근거를 종합해 초안을 구성한다"
        ),
        signals=["fixed_transform" if tool else "extraction" if kind == "simple" else "synthesis"],
        clarity=clarity,
        usefulness="required",
        executor=ToolExecutor(tool_name=tool, args=args or {})
        if tool
        else ModelExecutor(role=KIND_ROLE[kind], task=task),
        output_schema=schema,
        done_when=list(done) or [title + " 완료"],
        verify_with=list(checks),
    )
    todos.append(todo)
    return todo


def _ref(todo):
    return StepInput(step_id=todo.id, output_key=OUTPUT_SCHEMA_KEYS[todo.output_schema])


def _finish_recipe(todos, result, *, table=False):
    draft = _todo(
        todos,
        "검증된 결과로 파일 작성",
        tool="document.create",
        schema="ArtifactV1",
        args={"template_id": "table-v1" if table else "office-report-v1", "input_ref": result.id},
        inputs=[_ref(result)],
        checks=["artifact_structure", "source_grounding"],
        done=["요청한 형식으로 새 파일 생성", "원본 보존과 출처 기록"],
    )
    checked = _todo(
        todos,
        "문서 구조와 출처 확인",
        tool="artifact.validate",
        schema="ValidationResultV1",
        args={
            "input_ref": draft.id,
            "checks": ["preflight", "source_grounding", "report_structure"],
        },
        inputs=[_ref(draft)],
        done=["재열기와 출처 검증 통과", "시각 미검증 상태 표시"],
    )
    _todo(
        todos,
        "결과 파일 발행",
        tool="artifact.publish",
        schema="ReceiptV1",
        args={"input_ref": draft.id},
        inputs=[_ref(draft)],
        depends=[checked.id],
        done=["새 결과 파일과 hash 영수증 제공"],
    )


def _header_mentioned(header, text):
    return bool(
        re.search(
            r"(?<![\w])" + re.escape(header) + r"(?=$|[\s'\"`‘’“”,:;.)]|(?:열|을|를|의|별))", text
        )
    )


def _aggregate_policy(text, source, inputs):
    policy = inputs.get("policy", {})
    if (
        isinstance(policy, dict)
        and "thousands_separator" not in policy
        and source.get("format") == "csv"
        and re.search(r"따옴표로\s*감싼\s*천\s*단위", text)
    ):
        return {**policy, "thousands_separator": ","}
    return policy


def _aggregate_args(request, sources, inputs):
    allowed = set(AggregateArgs.model_fields)
    if set(inputs) - allowed:
        raise AppError("INVALID_INPUT")
    selected = inputs.get("artifact_id")
    candidates = [
        s
        for s in sources
        if s.get("format") in {"csv", "xlsx"} and (selected is None or s["artifact_id"] == selected)
    ]
    if len(candidates) != 1:
        return None, "집계할 표를 하나 선택해 주세요"
    source = candidates[0]
    if not source.get("headers_valid") or source.get("selection_required"):
        return None, "집계할 시트와 열을 확인해 주세요"
    headers = source.get("headers", [])
    values = inputs.get("value_columns")
    group = inputs.get("group_by")
    if group is None:
        groups = [h for h in headers if re.search(re.escape(h) + r"\s*별", request.request)]
        if len(groups) > 1:
            return None, "그룹 기준 열을 하나 선택해 주세요"
        group = groups[0] if groups else None
    if values is None:
        # Header names must occur explicitly. Five sampled numeric rows cannot define user intent.
        values = [h for h in headers if h != group and _header_mentioned(h, request.request)]
    if not values:
        return None, "합산할 값 열을 선택해 주세요"
    if (
        not isinstance(values, list)
        or any(v not in headers for v in values)
        or (group is not None and group not in headers)
    ):
        raise AppError("INVALID_INPUT")
    supplied = {
        "artifact_id": source["artifact_id"],
        "sheet": source.get("sheet"),
        "group_by": group,
        "value_columns": values,
        **inputs,
        "policy": _aggregate_policy(request.request, source, inputs),
    }
    try:
        return AggregateArgs.model_validate(supplied).model_dump(), None
    except ValidationError:
        raise AppError("INVALID_INPUT") from None


def _confirmed_aggregate_text(text, sources, inputs, output_format, request_text):
    """Remove only restatements of typed aggregate policies; never change execution args."""
    selected = inputs.get("artifact_id")
    candidates = [source for source in sources if selected in {None, source.get("artifact_id")}]
    if len(candidates) != 1:
        return text
    source = candidates[0]
    try:
        args = AggregateArgs.model_validate(
            {
                "artifact_id": source["artifact_id"],
                **inputs,
                "policy": _aggregate_policy(request_text, source, inputs),
            }
        )
    except ValidationError:
        return text
    policy = args.policy
    if source.get("headers_valid") and (
        not set(args.value_columns) <= set(source.get("headers", []))
        or args.group_by is not None
        and args.group_by not in source.get("headers", [])
    ):
        raise AppError("INVALID_INPUT")
    chosen_headers = {*args.value_columns, args.group_by}
    if any(
        header not in chosen_headers and _header_mentioned(header, text)
        for header in source.get("headers", [])
    ):
        return None
    text = re.sub(r"\s+", "", _without_output_phrase(text, output_format))

    def literal(value):
        return re.escape(re.sub(r"\s+", "", value))

    columns = "(?:" + "|".join(literal(name) for name in args.value_columns) + ")"
    rules = [(r"(?:환불)?음수를포함해", ""), (r"수식결과가확정되지않으면합계를중단해", "합계")]
    if source.get("format") == "csv" and policy.thousands_separator == ",":
        rules.append((r"따옴표로감싼천단위", ""))
    if args.sheet:
        rules.append((literal(args.sheet) + r"시트의", ""))
    if args.cell_range:
        rules.append((literal(args.cell_range) + r"범위의", ""))
    if policy.include_hidden_rows:
        rules.append((r"숨긴행(?:도|을)포함해서", ""))
    else:
        rules.append((r"숨긴행을제외해서", ""))
    if policy.blank_policy == "skip":
        rules.append((r"빈" + columns + r"셀은제외하고", ""))
    if policy.mixed_policy == "skip":
        rules.append((columns + r"의문자행을(?:명시적으로)?제외하고", ""))
    else:
        rules.append((columns + r"에문자가있으면합계하지말고입력보완을요청해", "합계"))
    ordinal = ("0", "첫째", "둘째", "셋째", "넷째", "다섯째", "여섯째")[policy.decimal_places]
    rules.extend(
        [
            (rf"소수(?:{ordinal}|{policy.decimal_places})자리까지", ""),
            (r"(?:ROUND_)?" + policy.rounding.removeprefix("ROUND_") + r"으로", ""),
        ]
    )
    if policy.ratio:
        numerator, denominator = literal(policy.ratio.numerator), literal(policy.ratio.denominator)
        rules.extend(
            [
                (rf"({columns})수(?=와|과|를|을)", r"\1"),
                (denominator + r"가0이면비율을미정으로남겨", ""),
                (numerator + r"비율을구해", ""),
            ]
        )
    if args.group_by and source.get("format") == "csv":
        rules.append((r"날짜문자열을보존해", ""))
    rules.append((rf"({columns})(?:열)?의", r"\1"))
    if len(args.value_columns) == 1:
        rules.append((rf"({columns})만", r"\1"))
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    # These grammatical forms carry no extra computation or filtering rule.
    for phrase in ("합산한뒤", "합산하고", "합계내고", "각각합계", "합계뒤"):
        text = text.replace(phrase, "합계")
    for phrase in ("작성해", "반올림해", "표시해"):
        text = text.replace(phrase, "해")
    for name in args.value_columns:
        text = re.sub(literal(name) + r"(?:과|와)", lambda _match, value=name: value, text)
    return text


def expand_recipe(request, inspected_sources, task_spec=None) -> PlanV1 | None:
    """Zero planning calls for six recognized recipes; unknown work goes to the one-shot planner."""
    request = RunRequest.model_validate(
        request.model_dump() if isinstance(request, RunRequest) else request
    )
    if len(request.request) > 500:
        return None  # The dynamic planner splits long requirements without truncating their text.
    if task_spec is not None:
        task_spec = TaskSpec.model_validate(
            task_spec.model_dump() if isinstance(task_spec, TaskSpec) else task_spec
        )
    sources = list(inspected_sources)
    scope_text = request.request
    aggregate_scope = (
        task_spec is not None and task_spec.intent == "aggregate" and task_spec.ambiguity == "clear"
    )
    currency_names = {
        "원": "KRW",
        "달러": "USD",
        "유로": "EUR",
        "엔": "JPY",
        "위안": "CNY",
        "파운드": "GBP",
    }
    currencies = {
        currency_names.get(value, value.upper())
        for value in re.findall(
            r"(?<![A-Za-z가-힣])(?:KRW|USD|EUR|JPY|CNY|GBP|원|달러|유로|엔|위안|파운드)(?=$|[\s,.]|[과와을를이가로화])",
            request.request,
            re.IGNORECASE,
        )
    }
    currency_missing = (
        aggregate_scope
        and len(currencies) > 1
        and re.search(r"통합|환산|환율", request.request)
        and re.search(r"합계|합산|집계", request.request)
    )
    # A target unit is not an exchange rate. The fixed aggregate contract never converts money.
    if aggregate_scope and not currency_missing:
        scope_text = _confirmed_aggregate_text(
            scope_text, sources, task_spec.inputs, request.output_format, request.request
        )
        if scope_text is None:
            return None
    recognized = _recognize(scope_text)
    recipe = task_spec.intent if task_spec else recognized
    if recipe not in RECIPES:
        return None
    if not currency_missing and (
        recognized != recipe or not _fixed_scope(scope_text, recipe, sources, request.output_format)
    ):
        return None
    conditions = task_spec.acceptance if task_spec else []
    if aggregate_scope and not currency_missing:
        conditions = [
            _confirmed_aggregate_text(
                condition, sources, task_spec.inputs, request.output_format, request.request
            )
            for condition in conditions
        ]
        if any(
            condition is None
            or condition != "업무완료"
            and not _fixed_scope(condition, recipe, sources, request.output_format)
            for condition in conditions
        ):
            return None
    if task_spec is not None and (
        task_spec.ambiguity == "uncertain"
        or task_spec.inputs.get("user_corrections")
        or any(re.search(DYNAMIC_PATTERN, condition) for condition in conditions)
    ):
        return None
    ids = [source.get("artifact_id") for source in sources]
    permitted = set(request.artifact_ids)
    if len(set(ids)) != len(ids) or not set(ids) <= permitted:
        raise AppError("POLICY_DENIED")
    if task_spec is not None:
        if not set(task_spec.source_ids) <= permitted:
            raise AppError("POLICY_DENIED")
        if task_spec.source_ids:
            sources = [s for s in sources if s["artifact_id"] in task_spec.source_ids]
    inputs = task_spec.inputs if task_spec else {}
    todos = []
    requirements = [Requirement(id="r1", text=request.request[:500])]
    missing = "통합 집계에 사용할 통화와 환율 기준을 확인해 주세요" if currency_missing else None
    if task_spec and task_spec.ambiguity == "missing_input":
        missing = "필수 입력을 확인해 주세요"
    if recipe in {"aggregate", "weekly_report"}:
        args, question = _aggregate_args(request, sources, inputs)
        missing = missing or question
        if not missing:
            result = _todo(
                todos,
                "표 수치 집계",
                tool="table.aggregate",
                args=args,
                schema="AggregateV1",
                inputs=[ArtifactInput(artifact_id=args["artifact_id"])],
                checks=["numeric_grounding"],
                done=["확정한 시트·범위·그룹·값 열로 집계", "포함·제외 행과 단위·반올림 기록"],
            )
            if recipe == "weekly_report":
                result = _todo(
                    todos,
                    "검증된 집계로 보고서 작성",
                    task="report_prose",
                    kind="standard",
                    schema="ReportSectionsV1",
                    inputs=[_ref(result)],
                    checks=["report_grounding"],
                    done=["요청한 비교와 보고서 항목 포함", "모든 숫자가 집계 결과와 일치"],
                )
            _finish_recipe(todos, result, table=recipe == "aggregate")
    elif not missing:
        if recipe == "search_answer":
            if set(inputs) - {"query", "limit"}:
                raise AppError("INVALID_INPUT")
            query = inputs.get("query")
            if query is None:
                quoted = re.findall(r"[\"'‘“]([^\"'’”]{1,500})[\"'’”]", request.request)
                query = quoted[0] if len(quoted) == 1 else None
            if query is None:
                missing = "검색할 결정·문구 또는 주제를 알려 주세요"
            else:
                args = SearchArgs(
                    query=query,
                    source_ids=[s["artifact_id"] for s in sources] or None,
                    limit=inputs.get("limit", 6),
                ).model_dump()
                extracted = [
                    _todo(
                        todos,
                        "프로젝트 근거 검색",
                        tool="source.search",
                        args=args,
                        schema="EvidenceListV1",
                        checks=["source_grounding"],
                    )
                ]
        else:
            if set(inputs) - {"projection", "limits"}:
                raise AppError("INVALID_INPUT")
            if not sources:
                missing = "처리할 자료를 추가해 주세요"
            elif len(sources) > 4:
                return None  # A dynamic bounded plan must choose an explicit scope or split.
            else:
                extracted = []
                for source in sources:
                    args = ExtractArgs(artifact_id=source["artifact_id"], **inputs).model_dump()
                    extracted.append(
                        _todo(
                            todos,
                            "자료 근거 추출",
                            tool="source.extract",
                            args=args,
                            schema="EvidenceListV1",
                            inputs=[ArtifactInput(artifact_id=source["artifact_id"])],
                            checks=["source_grounding"],
                            done=["범위와 위치가 있는 근거 확보", "근거 생략 없음"],
                        )
                    )
        if not missing:
            task, kind, schema, title, verifier = {
                "summarize": (
                    "summarize",
                    "simple" if len(sources) == 1 else "standard",
                    "ReportSectionsV1",
                    "근거 요약",
                    "report_grounding",
                ),
                "meeting_actions": (
                    "meeting_actions",
                    "simple",
                    "MeetingActionsV1",
                    "회의 할 일 추출",
                    "meeting_actions_grounding",
                ),
                "draft_reply": (
                    "draft_reply",
                    "standard",
                    "DraftReplyV1",
                    "답장 초안 작성",
                    "draft_reply_grounding",
                ),
                "search_answer": (
                    "search_answer",
                    "simple",
                    "SearchAnswerV1",
                    "검색 근거로 답변",
                    "search_answer_grounding",
                ),
            }[recipe]
            result = _todo(
                todos,
                title,
                task=task,
                kind=kind,
                schema=schema,
                inputs=[_ref(t) for t in extracted],
                checks=[verifier],
                done=["요청한 결과와 근거 위치 제공", "근거에 없는 값은 미정으로 표시"],
            )
            _finish_recipe(todos, result)
    if missing:
        todos = []
        _todo(
            todos,
            missing,
            task="analyze",
            kind="standard",
            schema="ReportSectionsV1",
            clarity="missing_input",
            reason="필수 입력이 없어 추측하지 않고 확인한다",
            done=[missing, "확정 입력으로 원래 요청을 완료한다"],
        )
    if task_spec is not None:
        # Confirmed acceptance conditions belong to the producing TODO, not only a display label.
        producing = next((t for t in reversed(todos) if t.executor.type == "model"), todos[0])
        for index, acceptance in enumerate(task_spec.acceptance, 2):
            if acceptance == request.request:
                continue
            requirement = Requirement(id=f"r{index}", text=acceptance)
            requirements.append(requirement)
            producing.requirement_ids.append(requirement.id)
            if acceptance not in producing.done_when:
                producing.done_when.append(acceptance)
        if len(requirements) > 20 or len(producing.done_when) > 20:
            return None
    return PlanV1(requirements=requirements, todos=todos)


def reclassify_pending(plan, new_facts, validation_errors, completed_ids=()) -> PlanV1:
    """Reconsider affected unfinished descendants; completed identities and outputs never move."""
    plan = PlanV1.model_validate(plan.model_dump())
    ids = {todo.id for todo in plan.todos}
    if not (set(new_facts) | set(validation_errors) | set(completed_ids)) <= ids:
        raise AppError("PLAN_INVALID")
    frozen = set(completed_ids) | {
        t.id
        for t in plan.todos
        if t.status in {"succeeded", "skipped", "cancelled", "outcome_unknown"}
    }
    affected = (set(new_facts) | set(validation_errors)) - frozen
    inherited = {}
    result = []
    mapping = {
        "CONFLICTING_SOURCES": "conflicting_sources",
        "CAUSAL_JUDGMENT": "causal_judgment",
        "COUPLED_CONSTRAINTS": "coupled_constraints",
        "ALTERNATIVE_EVALUATION": "alternative_evaluation",
    }
    for todo in plan.todos:
        parents = set(todo.depends_on) & affected
        if todo.id in frozen or (todo.id not in affected and not parents):
            result.append(todo)
            continue
        if todo.status in {"running", "checking", "awaiting_approval"}:
            raise AppError("CONFLICT", status=409)
        affected.add(todo.id)
        supplied = new_facts.get(todo.id)
        facts = TaskFacts.model_validate(
            supplied.model_dump()
            if isinstance(supplied, TaskFacts)
            else supplied or {"certified_deterministic": isinstance(todo.executor, ToolExecutor)}
        )
        updates = {}
        for parent in parents:
            parent_facts = inherited.get(parent)
            if parent_facts is not None:
                if not parent_facts.required_inputs_present:
                    updates["required_inputs_present"] = False
                if not facts.certified_deterministic:
                    for signal in COMPLEX_SIGNALS:
                        if getattr(parent_facts, signal):
                            updates[signal] = True
        for error in validation_errors.get(todo.id, []):
            if error in {
                "MISSING_INPUT",
                "EVIDENCE_MISSING",
                "FORMULA_UNVERIFIED",
                "CONTEXT_LIMIT",
            }:
                updates["required_inputs_present"] = False
            elif error in mapping:
                updates[mapping[error]] = True
            elif error == "UNSUPPORTED_CAPABILITY":
                updates["supported"] = False
        facts = facts.model_copy(update=updates)
        inherited[todo.id] = facts
        # New confirmed inputs can resume a previously unanswered TODO, without a role downgrade.
        current = todo
        if (
            supplied is not None
            and facts.required_inputs_present
            and todo.clarity == "missing_input"
        ):
            current = todo.model_copy(update={"clarity": "clear", "status": "pending"})
        result.append(normalize_todo(current, facts))
    return PlanV1(requirements=plan.requirements, todos=result)
