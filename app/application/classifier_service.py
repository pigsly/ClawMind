from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

try:
    from flashtext import KeywordProcessor as FlashTextKeywordProcessor
except ImportError:
    FlashTextKeywordProcessor = None

from app.domain.enums import AnalysisMode, ExecutorType, TaskType
from app.domain.models import InstructionBundle, Task

DETERMINISTIC_TASK_TYPES = {
    TaskType.MARKDOWN_APPEND,
    TaskType.METADATA_UPDATE,
    TaskType.FILE_NAMING,
    TaskType.LINK_APPEND,
}

DETERMINISTIC_KEYWORD_RULES: list[tuple[tuple[str, ...], TaskType]] = [
    (("append", "追加", "補上", "補寫", "寫入 markdown"), TaskType.MARKDOWN_APPEND),
    (("metadata", "屬性", "property", "properties", "更新欄位"), TaskType.METADATA_UPDATE),
    (("append link", "journal link", "追加連結", "補 journal link", "補頁面連結"), TaskType.LINK_APPEND),
]

LEGACY_KEYWORD_RULES: list[tuple[tuple[str, ...], TaskType]] = [
    (("spec", "規格", "需求草稿", "draft"), TaskType.SPEC_DRAFT),
    (("code", "程式碼", "修改程式", "修 code", "patch"), TaskType.CODE_CHANGE),
]

ANALYSIS_CUES = (
    "分析",
    "比較",
    "比较",
    "研究",
    "整理結論",
    "為什麼",
    "为什么",
    "怎麼選",
    "怎么选",
    "差異",
    "差异",
    "why is",
    "why does",
    "how should we choose",
    "what is the difference",
    "compare a and b",
    "what are the tradeoffs",
)
UNCERTAINTY_CUE_WEIGHTS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("為什麼", "为什么", "怎麼選", "怎么选", "差異", "差异", "why is", "why does", "how should we choose", "what is the difference"), 2),
    (("建議", "建议", "規劃", "规划", "腦力激盪", "脑力激荡", "優化", "优化", "寫文章", "写文章", "what do you recommend", "how should we plan", "help me brainstorm", "how can we optimize", "help me write an article"), 3),
    (("註解", "注解", "提取", "annotate this", "extract the key points"), 1),
    (("比較", "比较", "取捨", "取舍", "檢查", "检查", "compare a and b", "what are the tradeoffs", "please check whether"), 2),
)
COMPLEXITY_CUES = ("子任務", "workflow", "系統設計", "structured", "結構化", "整合")
COMPARISON_DECISION_CUES = (
    "請比較",
    "比較",
    "比较",
    "取捨",
    "取舍",
    "更適合",
    "更适合",
    "哪一種更適合",
    "哪一种更适合",
    "哪個比較適合",
    "哪个比较适合",
    "建議我怎麼選",
    "建议我怎么选",
    "怎麼選",
    "怎么选",
    "recommend",
    "tradeoffs",
    "which is better",
    "which is more suitable",
)
MULTI_DIMENSION_PATTERNS = (
    re.compile(r"(在|從|从).{0,40}(、|，|與|与|和|及).{0,40}(、|，|與|与|和|及)"),
    re.compile(r"(個性|个性|優缺點|优缺点|風險|风险|成本|速度|維護性|维护性|temperament|pros and cons|cost|risk|performance|maintainability).{0,30}(、|，|與|与|和|及|,| and ).{0,30}"),
    re.compile(r"(in terms of|across|for).{0,80}(,| and ).{0,80}(,| and )"),
)
FAST_MODEL = "gpt-5.4-mini"
DEEP_MODEL = "gpt-5.4"


class _SubstringKeywordProcessor:
    def __init__(self) -> None:
        self._entries: list[tuple[str, str]] = []

    def add_keyword(self, keyword: str, clean_name: str | None = None) -> None:
        self._entries.append((keyword.lower(), clean_name or keyword.lower()))

    def extract_keywords(self, text: str) -> list[str]:
        matches: list[str] = []
        seen: set[str] = set()
        lowered = text.lower()
        for keyword, clean_name in self._entries:
            if keyword in lowered and clean_name not in seen:
                matches.append(clean_name)
                seen.add(clean_name)
        return matches


class PhraseMatcher:
    def __init__(self, phrases: tuple[str, ...]) -> None:
        self.backend_name = "flashtext" if FlashTextKeywordProcessor is not None else "substring_fallback"
        processor_cls: Any = FlashTextKeywordProcessor or _SubstringKeywordProcessor
        self._processor = processor_cls(case_sensitive=False) if FlashTextKeywordProcessor is not None else processor_cls()
        for phrase in phrases:
            self._processor.add_keyword(phrase.lower(), phrase.lower())

    def contains_any(self, text: str) -> bool:
        return bool(self.extract(text))

    def extract(self, text: str) -> list[str]:
        return list(self._processor.extract_keywords(text.lower()))


@dataclass(slots=True)
class ExecutionPlan:
    executor_type: ExecutorType
    model: str | None


class ExecutionPolicy:
    def resolve(
        self,
        *,
        task_type: TaskType,
        analysis_mode: AnalysisMode,
        execution_mode: str,
        explicit_deterministic: bool,
    ) -> ExecutionPlan:
        if execution_mode == "deterministic":
            return ExecutionPlan(ExecutorType.DETERMINISTIC, None)
        if execution_mode == "mixed":
            return ExecutionPlan(ExecutorType.MIXED, self._resolve_model(analysis_mode))
        if execution_mode == "codex":
            return ExecutionPlan(ExecutorType.CODEX, self._resolve_model(analysis_mode))
        if analysis_mode != AnalysisMode.NORMAL:
            return ExecutionPlan(ExecutorType.CODEX, self._resolve_model(analysis_mode))
        if explicit_deterministic and task_type in DETERMINISTIC_TASK_TYPES:
            return ExecutionPlan(ExecutorType.DETERMINISTIC, None)
        return ExecutionPlan(ExecutorType.CODEX, self._resolve_model(analysis_mode))

    def _resolve_model(self, analysis_mode: AnalysisMode) -> str:
        if analysis_mode == AnalysisMode.NORMAL:
            return FAST_MODEL
        return DEEP_MODEL


class ClassifierService:
    def __init__(self, execution_policy: ExecutionPolicy | None = None) -> None:
        self.execution_policy = execution_policy or ExecutionPolicy()
        self._analysis_matcher = PhraseMatcher(ANALYSIS_CUES)
        self._comparison_decision_matcher = PhraseMatcher(COMPARISON_DECISION_CUES)
        self._uncertainty_matchers = tuple(
            (PhraseMatcher(cues), weight)
            for cues, weight in UNCERTAINTY_CUE_WEIGHTS
        )

    def classify(self, task: Task) -> InstructionBundle:
        task_type = self._resolve_task_type(task)
        explicit_deterministic = self._is_explicit_deterministic_task(task, task_type)
        analysis_mode = self._resolve_analysis_mode(task, task_type)
        execution_mode = task.properties.get("execution_mode", "").lower()
        plan = self.execution_policy.resolve(
            task_type=task_type,
            analysis_mode=analysis_mode,
            execution_mode=execution_mode,
            explicit_deterministic=explicit_deterministic,
        )
        return InstructionBundle(
            task_type=task_type,
            analysis_mode=analysis_mode,
            executor_type=plan.executor_type,
            model=plan.model,
            template_id=task.properties.get("template_id"),
            instruction_patch=task.properties.get("instruction_patch"),
            expected_output_type=task.properties.get("expected_output_type", "markdown"),
            validation_rules=self._parse_validation_rules(task.properties.get("validation_rules", "")),
        )

    def _resolve_task_type(self, task: Task) -> TaskType:
        raw_task_type = task.properties.get("task_type")
        if raw_task_type:
            return TaskType(raw_task_type)

        execution_mode = task.properties.get("execution_mode", "").lower()
        if execution_mode == "deterministic":
            return TaskType.MARKDOWN_APPEND

        task_sentence = task.raw_block_text.splitlines()[0].lower()
        for keywords, task_type in DETERMINISTIC_KEYWORD_RULES + LEGACY_KEYWORD_RULES:
            if any(keyword.lower() in task_sentence for keyword in keywords):
                return task_type
        return TaskType.MARKDOWN_APPEND

    def _resolve_analysis_mode(self, task: Task, task_type: TaskType) -> AnalysisMode:
        raw_analysis_mode = task.properties.get("analysis_mode")
        if raw_analysis_mode:
            return AnalysisMode(raw_analysis_mode)

        if task_type == TaskType.CROSS_PAGE_SYNTHESIS:
            return AnalysisMode.CROSS_PAGE_SYNTHESIS
        if task_type == TaskType.REASONING_ANALYSIS:
            return AnalysisMode.REASONING_ANALYSIS
        if self._is_explicit_deterministic_task(task, task_type):
            return AnalysisMode.NORMAL

        page_count = len(task.page_links)
        if page_count >= 2:
            return AnalysisMode.CROSS_PAGE_SYNTHESIS

        task_sentence = task.raw_block_text.lower()
        complexity_score = sum(2 for cue in COMPLEXITY_CUES if cue in task_sentence)
        uncertainty_score = sum(
            weight
            for matcher, weight in self._uncertainty_matchers
            if matcher.contains_any(task_sentence)
        )
        comparison_decision_score = len(self._comparison_decision_matcher.extract(task_sentence))
        multi_dimension_structure_score = self._multi_dimension_structure_score(task_sentence)
        if page_count > 0:
            complexity_score += 1
        if self._analysis_matcher.contains_any(task_sentence):
            uncertainty_score += 1

        if (
            complexity_score >= 4
            or uncertainty_score >= 4
            or comparison_decision_score >= 3
            or (comparison_decision_score >= 2 and multi_dimension_structure_score >= 1)
        ):
            return AnalysisMode.REASONING_ANALYSIS
        return AnalysisMode.NORMAL

    def _multi_dimension_structure_score(self, task_sentence: str) -> int:
        return sum(1 for pattern in MULTI_DIMENSION_PATTERNS if pattern.search(task_sentence))

    def _parse_validation_rules(self, raw_rules: str) -> list[str]:
        if not raw_rules.strip():
            return []
        return [rule.strip() for rule in raw_rules.split(",") if rule.strip()]

    def _is_explicit_deterministic_task(self, task: Task, task_type: TaskType) -> bool:
        if task.properties.get("execution_mode", "").lower() == "deterministic":
            return True
        if task.properties.get("task_type") in {item.value for item in DETERMINISTIC_TASK_TYPES}:
            return True
        task_sentence = task.raw_block_text.splitlines()[0].lower()
        return any(
            any(keyword.lower() in task_sentence for keyword in keywords)
            for keywords, _ in DETERMINISTIC_KEYWORD_RULES
        ) and task_type in DETERMINISTIC_TASK_TYPES
