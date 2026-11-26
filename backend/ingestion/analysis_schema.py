"""What stage 1 decides, and how an untrusted answer is checked (plan 15.3).

The LLM only RECOMMENDS a strategy. Its answer is untrusted (a hostile document
can try to steer it), so it is parsed strictly, every number is clamped, and
the strategy is checked against the real statistics (guard rails). Anything
wrong means a safe fallback, never a failed upload (decision D6).

Everything here is pure: no network, no model, same input gives same output.
"""

import json
import re
from dataclasses import asdict, dataclass, field

import limits

ALLOWED_STRATEGIES = (
    "single_chunk", "fixed_size", "sentence", "paragraph", "recursive",
    "heading_aware", "sliding_window", "semantic", "hybrid",
)
STRUCTURES = ("structured", "unstructured", "semi-structured")

# Parameters used for the heuristic and for every fallback to recursive
DEFAULT_PARAMS = {
    "chunk_size": 350,
    "chunk_overlap": 50,
    "min_chunk_size": 60,
    "max_chunk_size": 440,
    "table_chunk_size": 440,
    "code_chunk_size": 440,
}


class AnalysisInvalid(Exception):
    """The answer cannot be used. `reason` is a short code such as invalid_json."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Analysis:
    strategy: str
    params: dict  # clamped ints: chunk_size, chunk_overlap, min/max, table/code sizes
    structure: str
    document_type: str
    characteristics: list[str]
    reasoning: str
    source: str  # single | llm | heuristic
    fallback_reason: str = ""
    clamped: bool = False
    # The extras below feed the chunking report (plan 15.6) and the cache
    requested_strategy: str = ""
    params_requested: dict = field(default_factory=dict)
    preprocessing_requirements: list[str] = field(default_factory=list)
    additional_recommendations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def analysis_from_dict(data: dict) -> Analysis:
    """Rebuild a saved analysis (the cache). Raises ValueError if it is damaged."""

    try:
        analysis = Analysis(**data)
    except TypeError as error:
        raise ValueError("saved analysis is not usable") from error

    if analysis.strategy not in ALLOWED_STRATEGIES or not isinstance(analysis.params, dict):
        raise ValueError("saved analysis is not usable")

    return analysis


# ---------- Parsing the raw answer ----------

_FENCE_START = re.compile(r"^```[a-zA-Z]*\s*\n?")
_FENCE_END = re.compile(r"\n?```\s*$")


def extract_json_object(text: str) -> str:
    """The first `{` to the last `}`. Used when the model ignored JSON mode."""

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return text
    return text[start : end + 1]


def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    text = _FENCE_START.sub("", text)
    text = _FENCE_END.sub("", text).strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise AnalysisInvalid("invalid_json") from None

    if not isinstance(data, dict):
        raise AnalysisInvalid("invalid_json")

    return data


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _text_list(value, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip()[:limit] for item in value if isinstance(item, str) and item.strip()][:count]


# ---------- Numbers ----------

def _clean_params(raw: dict, strategy: str) -> tuple[dict, bool]:
    """Coerce to int, clamp into range, and say whether anything was clamped."""

    clamped = False

    def read(key: str) -> int | None:
        return _to_int(raw.get(key))

    def fit(value: int, low: int, high: int) -> int:
        nonlocal clamped
        result = _clamp(value, low, high)
        if result != value:
            clamped = True
        return result

    size = fit(read("chunk_size"), 100, 450)

    overlap = read("chunk_overlap")
    if overlap is None:
        overlap = 0 if strategy in ("paragraph", "heading_aware") else 50
        overlap = min(overlap, size // 4)
    else:
        overlap = fit(overlap, 0, size // 4)

    minimum = read("min_chunk_size")
    if minimum is None:
        minimum = _clamp(60, 40, max(40, size // 2))
    else:
        minimum = fit(minimum, 40, max(40, size // 2))

    maximum = read("max_chunk_size")
    if maximum is None:
        maximum = min(480, int(size * 1.25))
    else:
        maximum = fit(maximum, size, 480)

    def side_size(key: str) -> int:
        value = read(key)
        return maximum if value is None else fit(value, 100, 480)

    params = {
        "chunk_size": size,
        "chunk_overlap": overlap,
        "min_chunk_size": minimum,
        "max_chunk_size": maximum,
        "table_chunk_size": side_size("table_chunk_size"),
        "code_chunk_size": side_size("code_chunk_size"),
    }
    return params, clamped


# ---------- The check ----------

def _guard_rail(strategy: str, stats: dict) -> tuple[str | None, str | None]:
    """Returns (rule that blocks the strategy, a note for strategies that pass)."""

    blocks = stats.get("blocks", {})
    headings = blocks.get("heading", 0)

    if strategy == "heading_aware" and headings == 0:
        return "no_headings", None

    if strategy == "hybrid" and (
        blocks.get("table", 0) == 0 and blocks.get("code", 0) == 0 and headings < 3
    ):
        return "nothing_to_mix", None

    if strategy == "semantic" and (
        stats.get("chars", 0) > limits.SEMANTIC_MAX_CHARS
        or stats.get("sentences", 0) > limits.SEMANTIC_MAX_SENTENCES
    ):
        return "semantic_too_large", None

    if strategy == "single_chunk" and stats.get("tokens", 0) > limits.SINGLE_CHUNK_MAX_TOKENS:
        return "too_long_for_single", None

    if strategy in ("fixed_size", "sliding_window") and stats.get("has_hierarchy") and headings >= 5:
        return None, "document has clear headings; a heading-based strategy may fit better"

    return None, None


def validate_analysis(raw: str, stats: dict) -> Analysis:
    """Turn the model's answer into an Analysis, or raise AnalysisInvalid."""

    data = _parse_json(raw)

    for key in ("document_structure", "recommended_strategy"):
        if key not in data:
            raise AnalysisInvalid(f"schema_missing:{key}")

    parameters = data.get("chunking_parameters")
    if not isinstance(parameters, dict) or "chunk_size" not in parameters:
        raise AnalysisInvalid("schema_missing:chunking_parameters.chunk_size")
    if "unit" not in parameters:
        raise AnalysisInvalid("schema_missing:chunking_parameters.unit")

    if data["document_structure"] not in STRUCTURES:
        raise AnalysisInvalid("schema_invalid:document_structure")
    if parameters["unit"] != "tokens":
        raise AnalysisInvalid("schema_invalid:chunking_parameters.unit")
    if _to_int(parameters["chunk_size"]) is None:
        raise AnalysisInvalid("schema_invalid:chunking_parameters.chunk_size")

    requested = str(data["recommended_strategy"]).strip().lower()
    params, clamped = _clean_params(parameters, requested)

    strategy = requested
    fallback_reason = ""
    notes: list[str] = []

    if requested not in ALLOWED_STRATEGIES:
        strategy = "recursive"
        fallback_reason = f"unsupported_strategy:{requested[:40]}"
    else:
        blocked, note = _guard_rail(requested, stats)
        if blocked:
            strategy = "recursive"
            fallback_reason = f"guard:{blocked}"
        if note:
            notes.append(note)

    if strategy == "recursive" and fallback_reason:
        params = dict(DEFAULT_PARAMS)

    return Analysis(
        strategy=strategy,
        params=params,
        structure=data["document_structure"],
        document_type=_text(data.get("document_type"), 200),
        characteristics=_text_list(data.get("document_characteristics"), 10, 200),
        reasoning=_text(data.get("reasoning"), 600),
        source="llm",
        fallback_reason=fallback_reason,
        clamped=clamped,
        requested_strategy=requested[:40],
        params_requested={
            "chunk_size": _to_int(parameters.get("chunk_size")),
            "chunk_overlap": _to_int(parameters.get("chunk_overlap")),
        },
        preprocessing_requirements=_text_list(data.get("preprocessing_requirements"), 10, 200),
        additional_recommendations=_text_list(data.get("additional_recommendations"), 10, 200),
        notes=notes,
    )


# ---------- The deterministic default ----------

def _preset(strategy: str, overlap: int, minimum: int = 60) -> dict:
    params = dict(DEFAULT_PARAMS)
    params.update(chunk_overlap=overlap, min_chunk_size=minimum)
    return params


def default_analysis(stats: dict) -> Analysis:
    """The heuristic used when the LLM is off, missing or failed. First match wins."""

    blocks = stats.get("blocks", {})
    headings = blocks.get("heading", 0)
    mixed = blocks.get("table", 0) + blocks.get("code", 0)

    if stats.get("tokens", 0) <= limits.SINGLE_CHUNK_SKIP_TOKENS:
        strategy, params, structure = "single_chunk", _preset("single_chunk", 0), "unstructured"
    elif headings >= 3 and mixed >= 1:
        strategy, params, structure = "hybrid", _preset("hybrid", 40), "structured"
    elif stats.get("has_hierarchy"):
        strategy, params, structure = "heading_aware", _preset("heading_aware", 0), "structured"
    elif stats.get("avg_paragraph_tokens", 0) > 250 and headings == 0:
        strategy, params, structure = "paragraph", _preset("paragraph", 40), "unstructured"
    elif mixed >= 1:
        strategy, params, structure = "hybrid", _preset("hybrid", 40), "semi-structured"
    else:
        strategy, params, structure = "recursive", _preset("recursive", 50), "unstructured"

    return Analysis(
        strategy=strategy,
        params=params,
        structure=structure,
        document_type="",
        characteristics=[],
        reasoning="Chosen by fixed rules from the document statistics.",
        source="heuristic",
        requested_strategy=strategy,
        params_requested={
            "chunk_size": params["chunk_size"],
            "chunk_overlap": params["chunk_overlap"],
        },
    )
