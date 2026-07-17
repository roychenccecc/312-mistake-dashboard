"use strict";

const state = {
  summary: null,
  chapters: [],
  catalogChapters: [],
  knowledge: [],
  selectedChapterId: "",
  selectedKnowledgeId: "",
  activeFilters: {
    subject: "",
    chapter_id: "",
    date_from: "",
    date_to: "",
  },
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  loadDashboard();
});

function cacheElements() {
  const ids = [
    "freshness-label",
    "filter-form",
    "subject-filter",
    "chapter-filter",
    "date-from-filter",
    "date-to-filter",
    "reset-filters",
    "filter-error",
    "page-status",
    "page-error",
    "page-error-message",
    "retry-button",
    "summary-section",
    "summary-range",
    "overall-error-rate",
    "overall-error-context",
    "exam-error-rate",
    "exam-error-context",
    "eligible-attempt-count",
    "question-count-context",
    "mapping-coverage-rate",
    "exam-coverage-rate",
    "coverage-context",
    "chapters-section",
    "chapter-empty",
    "chapter-chart",
    "knowledge-section",
    "knowledge-title",
    "knowledge-summary",
    "knowledge-loading",
    "knowledge-empty",
    "knowledge-content",
    "knowledge-table-body",
    "comparison-count",
    "comparison-chart",
    "comparison-fallback",
    "comparison-fallback-note",
    "comparison-table-body",
    "unknown-frequency-section",
    "unknown-count",
    "unknown-frequency-list",
    "questions-section",
    "questions-title",
    "questions-summary",
    "questions-loading",
    "questions-empty",
    "question-list",
  ];

  ids.forEach((id) => {
    elements[toCamelCase(id)] = document.getElementById(id);
  });
}

function bindEvents() {
  elements.filterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    applyFilters();
  });

  elements.resetFilters.addEventListener("click", () => {
    elements.subjectFilter.value = "";
    elements.chapterFilter.value = "";
    elements.dateFromFilter.value = "";
    elements.dateToFilter.value = "";
    applyFilters();
  });

  elements.subjectFilter.addEventListener("change", () => {
    populateChapterFilter(state.catalogChapters, elements.subjectFilter.value, "");
  });

  elements.retryButton.addEventListener("click", loadDashboard);
}

async function loadDashboard() {
  setPageLoading(true);
  hidePageError();

  try {
    const params = filterParams(state.activeFilters, { includeChapter: true });
    const catalogParams = new URLSearchParams();
    if (state.activeFilters.date_from) catalogParams.set("date_from", state.activeFilters.date_from);
    if (state.activeFilters.date_to) catalogParams.set("date_to", state.activeFilters.date_to);
    const [summaryPayload, chaptersPayload, catalogPayload] = await Promise.all([
      getJson("/api/summary", params),
      getJson("/api/chapters", params),
      getJson("/api/chapters", catalogParams),
    ]);

    state.summary = normalizeSummary(summaryPayload);
    state.chapters = normalizeChapters(chaptersPayload);
    state.catalogChapters = normalizeChapters(catalogPayload);
    renderSummary(state.summary);
    populateSubjectFilter(state.catalogChapters);
    populateChapterFilter(
      state.catalogChapters,
      state.activeFilters.subject,
      state.activeFilters.chapter_id,
    );
    renderChapters(state.chapters);

    elements.summarySection.hidden = false;
    elements.chaptersSection.hidden = false;
    elements.knowledgeSection.hidden = false;

    const requestedChapter = state.activeFilters.chapter_id;
    const stillAvailable = state.chapters.some((chapter) => chapter.id === requestedChapter);
    const nextChapter = stillAvailable
      ? requestedChapter
      : state.chapters.find((chapter) => chapter.errorRate !== null)?.id || state.chapters[0]?.id || "";

    if (nextChapter) {
      await selectChapter(nextChapter, { scroll: false });
    } else {
      clearKnowledgeView();
    }
  } catch (error) {
    showPageError(error);
  } finally {
    setPageLoading(false);
  }
}

function applyFilters() {
  const next = {
    subject: elements.subjectFilter.value,
    chapter_id: elements.chapterFilter.value,
    date_from: elements.dateFromFilter.value,
    date_to: elements.dateToFilter.value,
  };

  if (next.date_from && next.date_to && next.date_from > next.date_to) {
    elements.filterError.textContent = "开始日期不能晚于结束日期。";
    elements.filterError.hidden = false;
    elements.dateFromFilter.focus();
    return;
  }

  elements.filterError.hidden = true;
  state.activeFilters = next;
  state.selectedChapterId = "";
  state.selectedKnowledgeId = "";
  elements.questionsSection.hidden = true;
  loadDashboard();
}

async function selectChapter(chapterId, options = {}) {
  const chapter = state.chapters.find((item) => item.id === chapterId);
  if (!chapter) return;

  state.selectedChapterId = chapterId;
  state.selectedKnowledgeId = "";
  elements.questionsSection.hidden = true;
  updateSelectedChapterRows();
  elements.knowledgeTitle.textContent = `${chapter.subject ? `${chapter.subject} · ` : ""}${chapter.name}`;
  elements.knowledgeSummary.textContent = "按个人综合错误率排序；考频只展示已完成语义核验的证据。";
  elements.knowledgeLoading.hidden = false;
  elements.knowledgeEmpty.hidden = true;
  elements.knowledgeContent.hidden = true;

  try {
    const params = filterParams(state.activeFilters, { includeChapter: false });
    params.set("chapter_id", chapterId);
    const payload = await getJson("/api/knowledge", params);
    state.knowledge = normalizeKnowledge(payload);
    renderKnowledge(state.knowledge);

    if (options.scroll) {
      elements.knowledgeSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    state.knowledge = [];
    elements.knowledgeEmpty.textContent = friendlyError(error, "知识点读取失败，请稍后重试。");
    elements.knowledgeEmpty.hidden = false;
  } finally {
    elements.knowledgeLoading.hidden = true;
  }
}

async function selectKnowledge(item, options = {}) {
  if (!item?.id) return;

  state.selectedKnowledgeId = item.id;
  elements.questionsSection.hidden = false;
  elements.questionsLoading.hidden = false;
  elements.questionsEmpty.hidden = true;
  elements.questionList.replaceChildren();
  elements.questionsTitle.textContent = item.name;
  elements.questionsSummary.textContent = "题目按真题、教材题、辅导书题、改写题、AI/自编题排列。";

  try {
    const params = filterParams(state.activeFilters, { includeChapter: false });
    params.set("mastery_unit_id", item.id);
    const payload = await getJson("/api/questions", params);
    const questions = normalizeQuestions(payload);
    renderQuestions(questions);
    if (options.scroll !== false) {
      // The trigger may live inside a horizontally scrollable table. Moving
      // focus to the revealed result keeps keyboard focus meaningful and
      // prevents a hidden table button from widening the page after resize.
      elements.questionsTitle.focus({ preventScroll: true });
      elements.questionsSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  } catch (error) {
    elements.questionsEmpty.textContent = friendlyError(error, "题目读取失败，请稍后重试。");
    elements.questionsEmpty.hidden = false;
  } finally {
    elements.questionsLoading.hidden = true;
  }
}

async function getJson(path, params) {
  const query = params && [...params].length ? `?${params.toString()}` : "";
  const response = await fetch(`${path}${query}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });

  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body.error || body.message || "";
    } catch (_error) {
      detail = "";
    }
    throw new Error(detail || `请求失败（HTTP ${response.status}）`);
  }

  return response.json();
}

function filterParams(filters, options = {}) {
  const params = new URLSearchParams();
  if (filters.subject) params.set("subject", filters.subject);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  if (options.includeChapter && filters.chapter_id) {
    params.set("chapter_id", filters.chapter_id);
  }
  return params;
}

function normalizeSummary(payload) {
  const raw = unwrapObject(payload, ["summary", "data"]);
  const overall = objectValue(raw.overall) || {};
  const realExam = objectValue(raw.real_exam) || {};
  const coverage = objectValue(raw.coverage) || {};
  const freshness = objectValue(raw.freshness) || {};
  const mapping = objectValue(raw.mapping_coverage) || {};
  const exam = objectValue(raw.exam_frequency_coverage) || objectValue(raw.exam_coverage) || {};

  return {
    errorRate: rateValue(firstDefined(
      raw.error_rate,
      raw.overall_error_rate,
      raw.weighted_error_rate,
      overall.error_rate,
    )),
    examErrorRate: rateValue(firstDefined(
      raw.exam_error_rate,
      raw.real_exam_error_rate,
      realExam.error_rate,
    )),
    eligibleAttempts: integerValue(firstDefined(
      raw.eligible_attempt_count,
      raw.eligible_attempts,
      raw.attempt_count,
      raw.sample_count,
      overall.eligible_attempts,
    )),
    scoredAttempts: integerValue(firstDefined(raw.scored_attempt_count, overall.scored_attempts)),
    distinctQuestions: integerValue(firstDefined(
      raw.distinct_question_count,
      raw.question_count,
      raw.eligible_question_count,
      overall.question_count,
    )),
    examEligibleAttempts: integerValue(firstDefined(
      raw.exam_eligible_attempt_count,
      raw.exam_attempt_count,
      raw.real_exam_sample_count,
      realExam.eligible_attempts,
    )),
    examScoredAttempts: integerValue(firstDefined(
      raw.exam_scored_attempt_count,
      realExam.scored_attempts,
    )),
    mappingRate: rateValue(firstDefined(
      raw.mapping_coverage_rate,
      coverage.question_mapping_rate,
      coverage.mapping_rate,
      mapping.rate,
      mapping.coverage_rate,
      mapping.percent,
    )),
    mappedQuestions: integerValue(firstDefined(
      raw.mapped_question_count,
      coverage.mapped_questions,
      mapping.mapped,
      mapping.mapped_count,
    )),
    mappableQuestions: integerValue(firstDefined(
      raw.mappable_question_count,
      coverage.question_count,
      overall.question_count,
      mapping.total,
      mapping.total_count,
    )),
    examCoverageRate: rateValue(firstDefined(
      raw.exam_frequency_coverage_rate,
      raw.exam_coverage_rate,
      coverage.exam_audit_rate,
      exam.rate,
      exam.coverage_rate,
      exam.percent,
    )),
    examAuditKnownRate: rateValue(coverage.exam_audit_known_rate),
    verifiedKnowledge: integerValue(firstDefined(
      raw.verified_knowledge_count,
      coverage.audited_units,
      exam.verified,
      exam.complete_count,
    )),
    auditKnownKnowledge: integerValue(coverage.audit_known_units),
    eligibleKnowledge: integerValue(firstDefined(
      raw.eligible_knowledge_count,
      coverage.attempted_units,
      exam.total,
      exam.total_count,
    )),
    scoringRate: rateValue(coverage.scoring_rate),
    dataThrough: textValue(firstDefined(
      raw.data_through,
      raw.latest_date,
      raw.as_of,
      raw.freshness_date,
      freshness.data_cutoff_date,
      freshness.end_date,
    )),
    dateFrom: textValue(firstDefined(raw.date_from, raw.range_start, freshness.start_date)),
    dateTo: textValue(firstDefined(raw.date_to, raw.range_end, freshness.end_date)),
    excludedScoreCount: integerValue(firstValue(raw, ["excluded_score_count", "invalid_score_count"])),
  };
}

function normalizeChapters(payload) {
  return unwrapArray(payload, ["items", "chapters", "data"])
    .map((raw) => {
      const metrics = objectValue(raw.metrics) || {};
      const realExam = objectValue(raw.real_exam) || {};
      return {
        id: textValue(firstValue(raw, ["chapter_id", "id"])),
        subject: textValue(firstValue(raw, ["subject", "subject_name"])),
        name: textValue(firstValue(raw, ["chapter_name", "name", "canonical_name", "label"])) || "未命名章节",
        errorRate: rateValue(firstDefined(
          raw.error_rate,
          raw.overall_error_rate,
          raw.weighted_error_rate,
          metrics.error_rate,
        )),
        examErrorRate: rateValue(firstDefined(raw.exam_error_rate, raw.real_exam_error_rate, realExam.error_rate)),
        eligibleAttempts: integerValue(firstDefined(
          raw.eligible_attempt_count,
          raw.eligible_attempts,
          raw.attempt_count,
          raw.sample_count,
          metrics.eligible_attempts,
        )),
        scoredAttempts: integerValue(firstDefined(raw.scored_attempt_count, metrics.scored_attempts)),
        distinctQuestions: integerValue(firstDefined(raw.distinct_question_count, raw.question_count, metrics.question_count)),
      };
    })
    .filter((item) => item.id)
    .sort(compareByErrorRate);
}

function normalizeKnowledge(payload) {
  return unwrapArray(payload, ["items", "knowledge", "knowledge_points", "data"])
    .map((raw) => {
      const metrics = objectValue(raw.metrics) || {};
      const realExam = objectValue(raw.real_exam) || {};
      const frequency = objectValue(raw.exam_frequency) || objectValue(raw.frequency) || {};
      const audit = objectValue(frequency.audit) || objectValue(raw.exam_frequency_audit) || {};
      const status = normalizeFrequencyStatus(firstDefined(
        raw.exam_frequency_status,
        raw.audit_status,
        raw.verification_status,
        frequency.status,
        frequency.audit_status,
        frequency.verification_status,
        audit.status,
      ));

      const recentOccurrenceCount = integerValue(firstDefined(
        raw.recent_occurrence_count,
        frequency.recent_occurrence_count,
      ));
      const recentYearCount = integerValue(firstDefined(raw.recent_year_count, frequency.recent_year_count));
      const recentByYear = Array.isArray(frequency.recent_by_year)
        ? frequency.recent_by_year
          .map((entry) => ({
            year: integerValue(entry?.year),
            count: integerValue(firstDefined(entry?.occurrence_count, entry?.count)),
          }))
          .filter((entry) => entry.year !== null && entry.count !== null)
        : [];
      const explicitTrend = textValue(firstDefined(
        raw.exam_recent_trend,
        raw.recent_trend,
        frequency.recent_trend,
        frequency.trend_2024_2026,
      ));
      const frequencyIsComplete = status === "complete" || status === "verified";
      const recentSummary = frequencyIsComplete
        ? explicitTrend || (
          recentByYear.length
            ? recentByYear.map((entry) => `${entry.year}：${entry.count} 次`).join(" · ")
            : recentOccurrenceCount !== null
            ? `${recentOccurrenceCount} 次${recentYearCount === null ? "" : ` / ${recentYearCount} 年`}`
            : ""
        )
        : recentByYear.length
        ? `${recentByYear.map((entry) => `${entry.year}：≥ ${entry.count} 次`).join(" · ")}；其余年份未知`
        : "";

      const auditYearStart = integerValue(firstDefined(audit.year_start, frequency.year_start));
      const auditYearEnd = integerValue(firstDefined(audit.year_end, frequency.year_end));
      const explicitYearsVerified = textValue(firstDefined(
        raw.years_verified,
        frequency.years_verified,
        frequency.verified_range,
        audit.years_verified,
        audit.verified_range,
      ));

      return {
        id: textValue(firstValue(raw, ["mastery_unit_id", "knowledge_id", "id"])),
        name: textValue(firstValue(raw, ["name", "knowledge_name", "label"])) || "未命名知识点",
        moduleName: textValue(firstValue(raw, ["module_name", "module", "section_name"])),
        errorRate: rateValue(firstDefined(
          raw.error_rate,
          raw.overall_error_rate,
          raw.weighted_error_rate,
          metrics.error_rate,
        )),
        examErrorRate: rateValue(firstDefined(raw.exam_error_rate, raw.real_exam_error_rate, realExam.error_rate)),
        eligibleAttempts: integerValue(firstDefined(
          raw.eligible_attempt_count,
          raw.eligible_attempts,
          raw.attempt_count,
          raw.sample_count,
          metrics.eligible_attempts,
        )),
        scoredAttempts: integerValue(firstDefined(raw.scored_attempt_count, metrics.scored_attempts)),
        distinctQuestions: integerValue(firstDefined(raw.distinct_question_count, raw.question_count, metrics.question_count)),
        frequency: {
          status,
          occurrenceCount: integerValue(firstDefined(
            raw.exam_occurrence_count,
            raw.occurrence_count,
            frequency.occurrence_count,
            frequency.count,
          )),
          verifiedOccurrenceLowerBound: integerValue(firstDefined(
            raw.verified_occurrence_lower_bound,
            frequency.verified_occurrence_lower_bound,
          )),
          distinctYearCount: integerValue(firstDefined(
            raw.exam_distinct_year_count,
            raw.distinct_year_count,
            frequency.distinct_year_count,
            frequency.year_count,
          )),
          verifiedDistinctYearLowerBound: integerValue(firstDefined(
            raw.verified_distinct_year_lower_bound,
            frequency.verified_distinct_year_lower_bound,
          )),
          recentTrend: recentSummary,
          recentByYear,
          questionTypes: arrayOrText(firstDefined(raw.exam_question_types, raw.question_types, frequency.question_types)),
          points: arrayOrText(firstDefined(raw.exam_points, raw.points, frequency.points)),
          yearsVerified: explicitYearsVerified || (
            auditYearStart !== null && auditYearEnd !== null
              ? `${auditYearStart}–${auditYearEnd}`
              : ""
          ),
          blockedReason: textValue(firstDefined(
            raw.exam_blocked_reason,
            raw.exam_blocker_reason,
            raw.blocked_reason,
            raw.blocker_reason,
            frequency.blocked_reason,
            frequency.blocker_reason,
            audit.blocked_reason,
            audit.blocker_reason,
          )),
        },
      };
    })
    .filter((item) => item.id)
    .sort(compareByErrorRate);
}

function normalizeQuestions(payload) {
  return unwrapArray(payload, ["items", "questions", "data"])
    .map((raw) => {
      const metrics = objectValue(raw.metrics) || {};
      const stem = textValue(firstValue(raw, ["stem", "question_text", "prompt", "title"])) || "题干暂缺";
      const questionFamily = textValue(firstValue(raw, ["question_family", "family"]));
      const questionType = textValue(firstValue(raw, ["question_type", "type"]));
      const options = normalizeQuestionOptions(firstDefined(raw.options, raw.choices));
      const failureRecords = [raw.failures, raw.failure_records, raw.attempts, raw.records]
        .find((value) => Array.isArray(value));
      const failureCount = Array.isArray(raw.failures)
        ? raw.failures.length
        : integerValue(firstDefined(
          raw.failure_count,
          raw.incorrect_count,
          raw.wrong_count,
          raw.failures,
          metrics.failure_count,
        ));
      return {
        id: textValue(firstValue(raw, ["question_id", "id"])),
        stem,
        answer: textValue(firstValue(raw, ["answer", "reference_answer", "correct_answer"])),
        explanation: textValue(firstValue(raw, ["explanation", "analysis", "rationale"])),
        sourceType: textValue(firstValue(raw, ["source_type", "source", "origin"])) || "来源未知",
        questionType,
        questionFamily,
        options,
        optionsStatus: normalizeQuestionOptionsStatus(
          firstDefined(raw.options_status, raw.option_status),
          questionFamily,
          questionType,
          stem,
          options,
        ),
        sourceYear: textValue(firstValue(raw, ["source_year", "year", "exam_year"])),
        errorRate: rateValue(firstDefined(raw.error_rate, raw.weighted_error_rate, metrics.error_rate)),
        eligibleAttempts: integerValue(firstDefined(
          raw.eligible_attempt_count,
          raw.attempt_count,
          raw.sample_count,
          metrics.eligible_attempts,
        )),
        scoredAttempts: integerValue(firstDefined(raw.scored_attempt_count, metrics.scored_attempts)),
        failureCount,
        attempts: normalizeAttempts(failureRecords),
      };
    })
    .filter((item) => item.id || item.stem)
    .sort((a, b) => {
      const sourceDiff = sourceRank(a.sourceType) - sourceRank(b.sourceType);
      if (sourceDiff) return sourceDiff;
      const rateDiff = nullableNumber(b.errorRate, -1) - nullableNumber(a.errorRate, -1);
      if (rateDiff) return rateDiff;
      return a.stem.localeCompare(b.stem, "zh-CN");
    });
}

function normalizeQuestionOptions(value) {
  if (!Array.isArray(value)) return [];
  return value
    .map((raw, index) => ({
      key: textValue(firstValue(raw, ["key", "option_key", "label"])),
      text: textValue(firstValue(raw, ["text", "option_text", "content"])),
      position: integerValue(firstDefined(raw?.position, raw?.display_order, index + 1)),
    }))
    .filter((option) => option.key && option.text)
    .sort((a, b) => nullableNumber(a.position, Number.MAX_SAFE_INTEGER)
      - nullableNumber(b.position, Number.MAX_SAFE_INTEGER));
}

function normalizeQuestionOptionsStatus(value, family, questionType, stem, options) {
  const normalized = textValue(value).toLowerCase();
  if (["structured", "legacy_inline", "missing", "not_applicable"].includes(normalized)) {
    return normalized;
  }
  if (options.length) return "structured";
  const objective = ["single_choice", "multiple_choice", "other_objective"].includes(family)
    || /(选择|单选|多选|匹配|判断|排序|分类)/.test(questionType);
  if (!objective) return "not_applicable";
  if (/(^|\s)[A-H]\s*[.．、:：。)）]\s*\S/i.test(stem)) return "legacy_inline";
  return "missing";
}

function normalizeAttempts(value) {
  if (!Array.isArray(value)) return [];
  return value.map((raw) => ({
    date: textValue(firstValue(raw, ["review_date", "attempt_date", "date", "created_at"])),
    result: textValue(firstValue(raw, ["result", "result_label", "outcome", "status"])),
    score: numberValue(firstValue(raw, ["score", "earned_score"])),
    maxScore: numberValue(firstValue(raw, ["max_score", "possible_score"])),
    errorType: textValue(firstValue(raw, ["error_type", "mistake_type"])),
    errorNotes: textValue(firstValue(raw, ["error_notes", "mistake_notes", "notes"])),
    answer: textValue(firstValue(raw, ["answer_text", "user_answer", "answer"])),
  }));
}

function renderSummary(summary) {
  elements.overallErrorRate.textContent = formatRate(summary.errorRate);
  elements.examErrorRate.textContent = formatRate(summary.examErrorRate);
  elements.eligibleAttemptCount.textContent = formatInteger(summary.eligibleAttempts);
  elements.mappingCoverageRate.textContent = formatRate(summary.mappingRate);
  elements.examCoverageRate.textContent = formatRate(summary.examCoverageRate);

  elements.overallErrorContext.textContent = scoredSampleLabel(
    summary.scoredAttempts,
    summary.eligibleAttempts,
  );
  elements.examErrorContext.textContent = summary.examEligibleAttempts === null
    ? "只统计来源为“真题”的作答"
    : `真题：${scoredSampleLabel(summary.examScoredAttempts, summary.examEligibleAttempts)}`;
  elements.questionCountContext.textContent = summary.distinctQuestions === null
    ? "独立、无提示、有效分数"
    : `涉及 ${formatInteger(summary.distinctQuestions)} 道不同题目`;

  const mappingDetail = coverageDetail(summary.mappedQuestions, summary.mappableQuestions, "题");
  const examDetail = coverageDetail(summary.verifiedKnowledge, summary.eligibleKnowledge, "个知识点");
  const auditStateDetail = coverageDetail(
    summary.auditKnownKnowledge,
    summary.eligibleKnowledge,
    "个知识点",
  );
  const details = [
    mappingDetail && `映射 ${mappingDetail}`,
    auditStateDetail && `考频状态已审计 ${auditStateDetail}`,
    examDetail && `完整核验 ${examDetail}`,
  ].filter(Boolean);
  const excluded = summary.excludedScoreCount ?? (
    summary.eligibleAttempts !== null && summary.scoredAttempts !== null
      ? Math.max(0, summary.eligibleAttempts - summary.scoredAttempts)
      : null
  );
  if (excluded) details.push(`另有 ${excluded} 次合格作答缺少有效分数`);
  if (summary.scoringRate !== null && summary.scoringRate < 1) {
    details.push(`有效分数覆盖 ${formatRate(summary.scoringRate)}`);
  }
  elements.coverageContext.textContent = details.join("；") || "未知不会按 0 处理";

  const range = dateRangeLabel(summary.dateFrom || state.activeFilters.date_from, summary.dateTo || state.activeFilters.date_to);
  elements.summaryRange.textContent = range;
  elements.freshnessLabel.textContent = summary.dataThrough
    ? `数据截至 ${formatDate(summary.dataThrough)}`
    : "数据截止日期未知";
}

function renderChapters(chapters) {
  elements.chapterChart.replaceChildren();
  elements.chapterEmpty.hidden = chapters.length > 0;

  chapters.forEach((chapter) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chapter-row";
    button.dataset.chapterId = chapter.id;
    button.setAttribute("role", "listitem");
    button.setAttribute(
      "aria-label",
      `${chapter.subject ? `${chapter.subject}，` : ""}${chapter.name}，综合错误率${formatRateSpeech(chapter.errorRate)}，${scoredSampleLabel(chapter.scoredAttempts, chapter.eligibleAttempts)}`,
    );
    button.addEventListener("click", () => selectChapter(chapter.id, { scroll: true }));

    const label = document.createElement("span");
    label.className = "chapter-label";
    label.append(
      textElement("span", chapter.name, "chapter-name"),
      textElement("span", chapter.subject || "科目未知", "chapter-subject"),
    );

    const track = document.createElement("span");
    track.className = "bar-track";
    const fill = document.createElement("span");
    fill.className = "bar-fill";
    if (chapter.errorRate === null) {
      fill.style.width = "0";
      fill.classList.add("low-label");
      fill.textContent = "暂无数据";
    } else {
      const percent = Math.max(0, Math.min(100, chapter.errorRate * 100));
      fill.style.width = `${percent}%`;
      fill.textContent = formatRate(chapter.errorRate);
      if (percent < 16) fill.classList.add("low-label");
    }
    track.append(fill);

    const sample = document.createElement("span");
    sample.className = "chapter-sample";
    sample.append(
      textElement("strong", formatInteger(chapter.scoredAttempts)),
      textElement(
        "span",
        chapter.eligibleAttempts !== null && chapter.scoredAttempts !== chapter.eligibleAttempts
          ? ` / ${formatInteger(chapter.eligibleAttempts)} 合格`
          : " 次计分",
        "bar-sample",
      ),
    );

    button.append(label, track, sample);
    elements.chapterChart.append(button);
  });
}

function renderKnowledge(items) {
  elements.knowledgeTableBody.replaceChildren();
  elements.knowledgeEmpty.hidden = items.length > 0;
  elements.knowledgeContent.hidden = items.length === 0;

  if (!items.length) return;

  items.forEach((item) => {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.className = "knowledge-name";
    nameCell.append(
      textElement("strong", item.name),
      textElement("span", item.moduleName || "模块未标注"),
    );

    const errorCell = metricCell(formatRate(item.errorRate), item.errorRate === null);
    const examErrorCell = metricCell(formatRate(item.examErrorRate), item.examErrorRate === null);
    const sampleCell = metricCell(
      compactScoredSample(item.scoredAttempts, item.eligibleAttempts),
      item.scoredAttempts === null,
    );
    const frequencyCell = document.createElement("td");
    frequencyCell.append(renderFrequencyBadge(item.frequency));
    const frequencyEvidence = [
      item.frequency.questionTypes.length
        ? `题型：${item.frequency.questionTypes.join("、")}`
        : "",
      item.frequency.points.length
        ? `分值：${item.frequency.points.join("、")}`
        : "",
      item.frequency.recentTrend
        ? `近三年：${item.frequency.recentTrend}`
        : "",
    ].filter(Boolean).join("；");
    if (frequencyEvidence) {
      frequencyCell.append(textElement("span", frequencyEvidence, "frequency-evidence"));
    }

    const actionCell = document.createElement("td");
    const action = document.createElement("button");
    action.type = "button";
    action.className = "text-button";
    action.textContent = "查看题目";
    action.setAttribute("aria-label", `查看“${item.name}”的相关题目`);
    action.addEventListener("click", () => selectKnowledge(item));
    actionCell.append(action);

    row.append(nameCell, errorCell, examErrorCell, sampleCell, frequencyCell, actionCell);
    elements.knowledgeTableBody.append(row);
  });

  renderComparison(items);
  renderUnknownFrequency(items);
}

function renderComparison(items) {
  const comparable = items.filter(isComparableFrequency);
  elements.comparisonCount.textContent = `${comparable.length} 个可比知识点`;
  elements.comparisonChart.replaceChildren();
  elements.comparisonTableBody.replaceChildren();

  if (comparable.length >= 1) {
    elements.comparisonChart.hidden = false;
    elements.comparisonFallback.hidden = true;
    renderScatter(comparable);
    return;
  }

  elements.comparisonChart.hidden = true;
  elements.comparisonFallback.hidden = false;
  elements.comparisonFallback.querySelector(".table-wrap").hidden = comparable.length === 0;
  elements.comparisonFallbackNote.textContent =
    "目前没有同时具备个人错误率和完整考频核验的知识点，暂不绘制散点图。";

  comparable.forEach((item) => {
    const row = document.createElement("tr");
    row.append(
      textCell(item.name),
      metricCell(formatRate(item.errorRate)),
      metricCell(formatInteger(item.frequency.occurrenceCount)),
      metricCell(formatInteger(item.frequency.distinctYearCount)),
      textCell(formatTrend(item.frequency.recentTrend)),
    );
    elements.comparisonTableBody.append(row);
  });
}

function renderScatter(items) {
  const wrapper = document.createElement("div");
  wrapper.className = "scatter-plot";
  wrapper.setAttribute("role", "img");
  wrapper.setAttribute(
    "aria-label",
    `${items.length} 个知识点的个人错误率与已核验不同年份数散点图。横轴为不同年份数，纵轴为个人错误率。`,
  );

  const maxYears = Math.max(4, ...items.map((item) => item.frequency.distinctYearCount || 0));
  const maxSample = Math.max(1, ...items.map((item) => item.scoredAttempts || 1));

  [0, 0.25, 0.5, 0.75, 1].forEach((rate) => {
    const tick = textElement("span", formatRate(rate), "axis-tick y-tick");
    tick.style.bottom = `${rate * 100}%`;
    wrapper.append(tick);
  });

  [0, 0.25, 0.5, 0.75, 1].forEach((ratio) => {
    const years = Math.round(maxYears * ratio);
    const tick = textElement("span", String(years), "axis-tick x-tick");
    tick.style.left = `${ratio * 100}%`;
    wrapper.append(tick);
  });

  items.forEach((item) => {
    const point = document.createElement("button");
    point.type = "button";
    point.className = "scatter-point";
    point.style.left = `${(item.frequency.distinctYearCount / maxYears) * 100}%`;
    point.style.bottom = `${item.errorRate * 100}%`;
    const size = 12 + Math.sqrt((item.scoredAttempts || 1) / maxSample) * 12;
    point.style.setProperty("--point-size", `${Math.round(size)}px`);
    point.setAttribute(
      "aria-label",
      `${item.name}：个人错误率 ${formatRate(item.errorRate)}，考过 ${item.frequency.occurrenceCount} 次，涉及 ${item.frequency.distinctYearCount} 个不同年份，${scoredSampleLabel(item.scoredAttempts, item.eligibleAttempts)}`,
    );
    point.addEventListener("focus", () => showScatterTooltip(wrapper, point, item));
    point.addEventListener("mouseenter", () => showScatterTooltip(wrapper, point, item));
    point.addEventListener("blur", () => hideScatterTooltip(wrapper));
    point.addEventListener("mouseleave", () => hideScatterTooltip(wrapper));
    point.addEventListener("click", () => selectKnowledge(item));
    wrapper.append(point);
  });

  elements.comparisonChart.append(
    wrapper,
    textElement("span", "个人综合错误率", "axis-label y-axis-label"),
    textElement("span", "已核验不同年份数", "axis-label x-axis-label"),
  );
}

function showScatterTooltip(wrapper, point, item) {
  hideScatterTooltip(wrapper);
  const tooltip = document.createElement("div");
  tooltip.className = "scatter-tooltip";
  tooltip.setAttribute("aria-hidden", "true");
  tooltip.append(
    textElement("strong", item.name),
    textElement(
      "span",
      `错误率 ${formatRate(item.errorRate)} · ${item.frequency.distinctYearCount} 年 / ${item.frequency.occurrenceCount} 次 · ${scoredSampleLabel(item.scoredAttempts, item.eligibleAttempts)}`,
    ),
    textElement("span", `近三年：${formatTrend(item.frequency.recentTrend)}`),
  );
  wrapper.append(tooltip);

  const pointRect = point.getBoundingClientRect();
  const wrapperRect = wrapper.getBoundingClientRect();
  const maxLeft = Math.max(8, wrapperRect.width - Math.min(230, wrapperRect.width - 16) - 8);
  const left = clamp(pointRect.left - wrapperRect.left + pointRect.width / 2 - 70, 8, maxLeft);
  const above = pointRect.top - wrapperRect.top - 76;
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${above > 8 ? above : pointRect.bottom - wrapperRect.top + 8}px`;
}

function hideScatterTooltip(wrapper) {
  wrapper.querySelector(".scatter-tooltip")?.remove();
}

function renderUnknownFrequency(items) {
  const unknown = items.filter((item) => !isComparableFrequency(item));
  elements.unknownFrequencyList.replaceChildren();
  elements.unknownFrequencySection.hidden = unknown.length === 0;
  elements.unknownCount.textContent = `${unknown.length} 个`;

  unknown.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "unknown-item";
    button.setAttribute("aria-label", `${item.name}，考频${frequencyStatusLabel(item.frequency.status)}，查看相关题目`);
    button.append(
      textElement("strong", item.name),
      textElement("span", unknownReason(item)),
    );
    button.addEventListener("click", () => selectKnowledge(item));
    elements.unknownFrequencyList.append(button);
  });
}

function renderQuestions(questions) {
  elements.questionList.replaceChildren();
  elements.questionsEmpty.hidden = questions.length > 0;

  questions.forEach((question, index) => {
    const details = document.createElement("details");
    details.className = "question-card";
    if (index === 0) details.open = true;

    const summary = document.createElement("summary");
    const badge = textElement("span", question.sourceType, `source-badge ${sourceBadgeClass(question.sourceType)}`);
    const stem = textElement("span", question.stem, "question-stem");
    const metric = textElement(
      "span",
      question.errorRate === null
        ? scoredSampleLabel(question.scoredAttempts, question.eligibleAttempts)
        : `错误率 ${formatRate(question.errorRate)} · ${scoredSampleLabel(question.scoredAttempts, question.eligibleAttempts)}`,
      "question-summary-metric",
    );
    summary.append(badge, stem, metric);

    const body = document.createElement("div");
    body.className = "question-body";
    body.append(textElement("p", question.stem, "question-full-stem"));

    const options = renderQuestionOptions(question);
    if (options) body.append(options);

    const meta = document.createElement("div");
    meta.className = "question-meta";
    [
      question.id && `题目 ID：${question.id}`,
      question.questionType && `题型：${question.questionType}`,
      question.sourceYear && `年份：${question.sourceYear}`,
      question.failureCount !== null && `失败记录：${question.failureCount} 次`,
    ].filter(Boolean).forEach((label) => meta.append(textElement("span", label)));
    body.append(meta);

    const referenceContent = document.createElement("div");
    referenceContent.className = "question-reference-grid";
    referenceContent.append(
      renderQuestionReferenceBlock("参考答案", question.answer, "参考答案待补"),
      renderQuestionReferenceBlock("解析", question.explanation, "解析待补"),
    );
    body.append(referenceContent);

    if (question.attempts.length) {
      body.append(textElement("h3", "失败与部分得分记录"));
      const list = document.createElement("ul");
      list.className = "attempt-list";
      question.attempts.forEach((attempt) => {
        const row = document.createElement("li");
        row.className = "attempt-item";
        const score = attempt.score !== null && attempt.maxScore
          ? `${formatNumber(attempt.score)} / ${formatNumber(attempt.maxScore)}`
          : attempt.result || "结果未标注";
        row.append(
          textElement("span", formatDate(attempt.date) || "日期未知"),
          textElement(
            "span",
            [attempt.errorType, attempt.errorNotes, attempt.answer].filter(Boolean).join(" · ") || "未记录错因",
          ),
          textElement("span", score, "attempt-result"),
        );
        list.append(row);
      });
      body.append(list);
    } else {
      body.append(textElement("p", "暂无可展示的失败明细。", "section-note"));
    }

    details.append(summary, body);
    elements.questionList.append(details);
  });
}

function renderQuestionOptions(question) {
  if (question.optionsStatus === "not_applicable") return null;

  const section = document.createElement("section");
  section.className = "question-options-panel";
  section.append(textElement("h3", "选项", "question-options-title"));

  if (question.options.length) {
    const list = document.createElement("ul");
    list.className = "question-options-list";
    question.options.forEach((option) => {
      const item = document.createElement("li");
      item.className = "question-option-item";
      item.append(
        textElement("span", option.key, "question-option-key"),
        textElement("span", option.text, "question-option-text"),
      );
      list.append(item);
    });
    section.append(list);
    return section;
  }

  const isLegacyInline = question.optionsStatus === "legacy_inline";
  section.append(textElement(
    "p",
    isLegacyInline ? "选项沿用旧题干内嵌格式。" : "选项未记录。",
    `question-options-note${isLegacyInline ? "" : " is-missing"}`,
  ));
  return section;
}

function renderQuestionReferenceBlock(label, content, missingText) {
  const block = document.createElement("section");
  block.className = "question-reference-card";
  block.append(
    textElement("h3", label, "question-reference-title"),
    textElement(
      "p",
      content || missingText,
      `question-reference-text${content ? "" : " is-missing"}`,
    ),
  );
  return block;
}

function renderFrequencyBadge(frequency) {
  const badge = document.createElement("span");
  badge.className = `status-badge ${frequency.status}`;
  if (isCompleteFrequency(frequency)) {
    badge.textContent = `${formatInteger(frequency.occurrenceCount)} 次 · ${formatInteger(frequency.distinctYearCount)} 年`;
  } else if (frequency.status === "partial") {
    const count = frequency.verifiedOccurrenceLowerBound > 0
      ? `≥ ${frequency.verifiedOccurrenceLowerBound} 次`
      : "已核验部分年份";
    badge.textContent = `部分核验 · ${count}`;
  } else if (frequency.status === "blocked") {
    const lowerBound = frequency.verifiedOccurrenceLowerBound > 0
      ? ` · ≥ ${frequency.verifiedOccurrenceLowerBound} 次`
      : "";
    badge.textContent = `核验受阻${lowerBound}`;
  } else {
    badge.textContent = "考频未知";
  }
  const details = [
    frequency.yearsVerified && `核验范围：${frequency.yearsVerified}`,
    frequency.recentTrend && `2024–2026：${frequency.recentTrend}`,
    frequency.questionTypes.length && `题型：${frequency.questionTypes.join("、")}`,
    frequency.points.length && `分值：${frequency.points.join("、")}`,
    frequency.blockedReason && `阻塞原因：${frequency.blockedReason}`,
  ].filter(Boolean);
  if (details.length) {
    badge.title = details.join("；");
    badge.setAttribute("aria-label", `${badge.textContent}；${badge.title}`);
  }
  return badge;
}

function updateSelectedChapterRows() {
  elements.chapterChart.querySelectorAll(".chapter-row").forEach((row) => {
    row.setAttribute("aria-current", String(row.dataset.chapterId === state.selectedChapterId));
  });
}

function clearKnowledgeView() {
  state.knowledge = [];
  state.selectedChapterId = "";
  elements.knowledgeTitle.textContent = "选择一个章节";
  elements.knowledgeSummary.textContent = "当前筛选范围内没有可展开的章节。";
  elements.knowledgeContent.hidden = true;
  elements.knowledgeEmpty.hidden = false;
  elements.knowledgeEmpty.textContent = "当前筛选范围内没有知识点作答。";
  elements.questionsSection.hidden = true;
}

function populateSubjectFilter(chapters) {
  const current = state.activeFilters.subject || elements.subjectFilter.value;
  const subjects = [...new Set(chapters.map((chapter) => chapter.subject).filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
  replaceSelectOptions(elements.subjectFilter, [{ value: "", label: "全部科目" }], subjects.map((subject) => ({
    value: subject,
    label: subject,
  })));
  if ([...elements.subjectFilter.options].some((option) => option.value === current)) {
    elements.subjectFilter.value = current;
  }
}

function populateChapterFilter(chapters, subject, selected) {
  const options = chapters
    .filter((chapter) => !subject || chapter.subject === subject)
    .map((chapter) => ({
      value: chapter.id,
      label: chapter.subject && !subject ? `${chapter.subject} · ${chapter.name}` : chapter.name,
    }));
  replaceSelectOptions(elements.chapterFilter, [{ value: "", label: "全部章节" }], options);
  if (options.some((option) => option.value === selected)) {
    elements.chapterFilter.value = selected;
  }
}

function replaceSelectOptions(select, ...groups) {
  select.replaceChildren();
  groups.flat().forEach(({ value, label }) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  });
}

function setPageLoading(loading) {
  elements.pageStatus.hidden = !loading;
  elements.filterForm.querySelectorAll("button, select, input").forEach((control) => {
    control.disabled = loading;
  });
}

function showPageError(error) {
  elements.pageErrorMessage.textContent = friendlyError(
    error,
    "请确认已运行 python3 scripts/mistake_dashboard_server.py --port 4174。",
  );
  elements.pageError.hidden = false;
  elements.freshnessLabel.textContent = "本地数据连接失败";
}

function hidePageError() {
  elements.pageError.hidden = true;
}

function compareByErrorRate(a, b) {
  if (a.errorRate === null && b.errorRate === null) {
    return a.name.localeCompare(b.name, "zh-CN");
  }
  if (a.errorRate === null) return 1;
  if (b.errorRate === null) return -1;
  if (b.errorRate !== a.errorRate) return b.errorRate - a.errorRate;
  if (nullableNumber(b.eligibleAttempts, 0) !== nullableNumber(a.eligibleAttempts, 0)) {
    return nullableNumber(b.eligibleAttempts, 0) - nullableNumber(a.eligibleAttempts, 0);
  }
  return a.name.localeCompare(b.name, "zh-CN");
}

function isComparableFrequency(item) {
  return item.errorRate !== null
    && isCompleteFrequency(item.frequency)
    && item.frequency.distinctYearCount !== null
    && item.frequency.occurrenceCount !== null;
}

function isCompleteFrequency(frequency) {
  return frequency.status === "complete" || frequency.status === "verified";
}

function normalizeFrequencyStatus(value) {
  const status = textValue(value).toLowerCase();
  if (["complete", "completed", "verified", "fully_verified"].includes(status)) return "complete";
  if (["partial", "partially_verified", "in_progress"].includes(status)) return "partial";
  if (["blocked", "unavailable"].includes(status)) return "blocked";
  return "unknown";
}

function frequencyStatusLabel(status) {
  if (status === "partial") return "部分核验";
  if (status === "blocked") return "核验受阻";
  if (status === "complete") return "已完整核验";
  return "未知";
}

function unknownReason(item) {
  const lowerBound = item.frequency.verifiedOccurrenceLowerBound > 0
    ? `已核验下限 ${item.frequency.verifiedOccurrenceLowerBound} 次`
    : "";
  if (item.frequency.status === "partial") return ["部分核验", lowerBound].filter(Boolean).join(" · ");
  if (item.frequency.status === "blocked") {
    return ["核验受阻", lowerBound, item.frequency.blockedReason].filter(Boolean).join(" · ");
  }
  if (item.errorRate === null && isCompleteFrequency(item.frequency)) return "暂无个人错误率";
  return "考频未知";
}

function sourceRank(source) {
  const normalized = String(source || "").toLowerCase();
  if (normalized.includes("ai") || normalized.includes("生成")) return 4;
  if (normalized.includes("自编")) return 5;
  if (
    normalized.includes("真题")
    && (normalized.includes("改写") || normalized.includes("变式") || normalized.includes("汇编"))
  ) return 3;
  if (normalized.includes("真题") || normalized === "past_paper" || normalized === "real_exam") return 0;
  if (normalized.includes("教材") || normalized.includes("课后") || normalized === "textbook") return 1;
  if (normalized.includes("辅导") || normalized.includes("练习册") || normalized === "workbook") return 2;
  if (normalized.includes("改写") || normalized.includes("变式")) return 4;
  return 6;
}

function sourceBadgeClass(source) {
  const rank = sourceRank(source);
  if (rank === 1 || rank === 2) return "book";
  if (rank >= 4) return "generated";
  return "";
}

function unwrapObject(payload, keys) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
  for (const key of keys) {
    if (payload[key] && typeof payload[key] === "object" && !Array.isArray(payload[key])) {
      return payload[key];
    }
  }
  return payload;
}

function unwrapArray(payload, keys) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  for (const key of keys) {
    if (Array.isArray(payload[key])) return payload[key];
  }
  return [];
}

function firstValue(object, keys) {
  if (!object || typeof object !== "object") return undefined;
  for (const key of keys) {
    if (object[key] !== undefined && object[key] !== null) return object[key];
  }
  return undefined;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null);
}

function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function arrayOrText(value) {
  if (Array.isArray(value)) return value.map(textValue).filter(Boolean);
  const text = textValue(value);
  return text ? [text] : [];
}

function rateValue(value) {
  const number = numberValue(value);
  if (number === null || number < 0) return null;
  return number > 1 ? number / 100 : number;
}

function numberValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function integerValue(value) {
  const number = numberValue(value);
  return number === null ? null : Math.round(number);
}

function textValue(value) {
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function nullableNumber(value, fallback) {
  return value === null || value === undefined ? fallback : value;
}

function formatRate(rate) {
  if (rate === null || !Number.isFinite(rate)) return "—";
  return `${(rate * 100).toLocaleString("zh-CN", { maximumFractionDigits: 1, minimumFractionDigits: 0 })}%`;
}

function formatRateSpeech(rate) {
  return rate === null ? "暂无数据" : formatRate(rate);
}

function formatInteger(value) {
  return value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function formatNumber(value) {
  return value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function formatDate(value) {
  const text = textValue(value);
  if (!text) return "";
  const match = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : text;
}

function formatTrend(value) {
  const text = textValue(value);
  if (!text) return "未知";
  const map = {
    rising: "上升",
    up: "上升",
    stable: "持平",
    flat: "持平",
    falling: "下降",
    down: "下降",
    none: "近三年未出现",
    unknown: "未知",
  };
  return map[text.toLowerCase()] || text;
}

function scoredSampleLabel(scored, eligible) {
  if (scored === null) return "计分样本未知";
  if (eligible !== null && scored !== eligible) {
    return `${formatInteger(scored)} 次计分样本 / ${formatInteger(eligible)} 次合格作答`;
  }
  return `${formatInteger(scored)} 次计分样本`;
}

function compactScoredSample(scored, eligible) {
  if (scored === null) return "—";
  if (eligible !== null && scored !== eligible) {
    return `${formatInteger(scored)} / ${formatInteger(eligible)}`;
  }
  return formatInteger(scored);
}

function coverageDetail(numerator, denominator, unit) {
  if (numerator === null && denominator === null) return "";
  if (denominator === null) return `${formatInteger(numerator)} ${unit}`;
  return `${formatInteger(numerator)} / ${formatInteger(denominator)} ${unit}`;
}

function dateRangeLabel(from, to) {
  if (from && to) return `${formatDate(from)} 至 ${formatDate(to)}`;
  if (from) return `${formatDate(from)} 起`;
  if (to) return `截至 ${formatDate(to)}`;
  return "全部已记录日期";
}

function metricCell(text, unknown = false) {
  const cell = document.createElement("td");
  cell.className = unknown ? "metric-value metric-unknown" : "metric-value";
  cell.textContent = text;
  return cell;
}

function textCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function textElement(tag, text, className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function friendlyError(error, fallback) {
  const message = textValue(error?.message);
  if (message === "Failed to fetch" || message.toLowerCase().includes("networkerror")) {
    return fallback;
  }
  return message || fallback;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function toCamelCase(value) {
  return value.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}
