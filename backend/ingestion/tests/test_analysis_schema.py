import json

import pytest

import limits
from analysis_schema import (
    AnalysisInvalid,
    analysis_from_dict,
    default_analysis,
    extract_json_object,
    validate_analysis,
)


def stats(**overrides):
    base = {
        "source_type": "md", "chars": 20000, "tokens": 5000, "pages": None,
        "blocks": {"heading": 0, "paragraph": 40, "list": 0, "table": 0, "code": 0},
        "heading_depth": 0, "avg_paragraph_tokens": 100, "max_paragraph_tokens": 300,
        "list_ratio": 0.0, "has_hierarchy": False,
    }
    base.update(overrides)
    return base


def answer(**overrides):
    data = {
        "document_type": "report",
        "document_structure": "unstructured",
        "document_characteristics": ["long prose"],
        "recommended_strategy": "recursive",
        "reasoning": "flat prose",
        "chunking_parameters": {
            "chunk_size": 350, "chunk_overlap": 50, "unit": "tokens",
            "min_chunk_size": 60, "max_chunk_size": 440,
            "table_chunk_size": 400, "code_chunk_size": 400,
        },
        "preprocessing_requirements": [],
        "additional_recommendations": [],
    }
    data.update(overrides)
    return json.dumps(data)


def reason(raw, st=None):
    with pytest.raises(AnalysisInvalid) as error:
        validate_analysis(raw, st or stats())
    return error.value.reason


# ---------- valid answers ----------

def test_a_valid_answer_passes():
    result = validate_analysis(answer(), stats())

    assert result.strategy == "recursive"
    assert result.source == "llm"
    assert result.fallback_reason == ""
    assert result.clamped is False
    assert result.params["chunk_size"] == 350
    assert result.params["table_chunk_size"] == 400


def test_a_fenced_answer_is_accepted():
    fenced = "```json\n" + answer() + "\n```"

    assert validate_analysis(fenced, stats()).strategy == "recursive"


def test_unknown_keys_are_ignored_and_strings_are_cut():
    raw = json.loads(answer())
    raw["secret_instruction"] = "do something"
    raw["reasoning"] = "x" * 2000
    raw["document_type"] = "y" * 999
    raw["document_characteristics"] = [f"c{i}" for i in range(30)]

    result = validate_analysis(json.dumps(raw), stats())

    assert len(result.reasoning) == 600
    assert len(result.document_type) == 200
    assert len(result.characteristics) == 10
    assert not hasattr(result, "secret_instruction")


# ---------- invalid answers ----------

def test_not_json_fails():
    assert reason("I think you should use paragraphs.") == "invalid_json"
    assert reason("[1, 2, 3]") == "invalid_json"


@pytest.mark.parametrize("missing, expected", [
    ("document_structure", "schema_missing:document_structure"),
    ("recommended_strategy", "schema_missing:recommended_strategy"),
    ("chunking_parameters", "schema_missing:chunking_parameters.chunk_size"),
])
def test_missing_required_keys_fail(missing, expected):
    data = json.loads(answer())
    del data[missing]

    assert reason(json.dumps(data)) == expected


def test_missing_chunk_size_or_unit_fails():
    data = json.loads(answer())
    del data["chunking_parameters"]["chunk_size"]
    assert reason(json.dumps(data)) == "schema_missing:chunking_parameters.chunk_size"

    data = json.loads(answer())
    del data["chunking_parameters"]["unit"]
    assert reason(json.dumps(data)) == "schema_missing:chunking_parameters.unit"


def test_bad_structure_value_fails():
    assert reason(answer(document_structure="messy")) == "schema_invalid:document_structure"


def test_unit_must_be_tokens():
    data = json.loads(answer())
    data["chunking_parameters"]["unit"] = "characters"

    assert reason(json.dumps(data)) == "schema_invalid:chunking_parameters.unit"


def test_chunk_size_that_is_not_a_number_fails():
    data = json.loads(answer())
    data["chunking_parameters"]["chunk_size"] = "big"

    assert reason(json.dumps(data)) == "schema_invalid:chunking_parameters.chunk_size"


# ---------- clamping ----------

def test_out_of_range_numbers_are_clamped_and_flagged():
    data = json.loads(answer())
    data["chunking_parameters"].update(
        chunk_size=9000, chunk_overlap=9000, min_chunk_size=1, max_chunk_size=9999,
        table_chunk_size=5, code_chunk_size=9999,
    )

    result = validate_analysis(json.dumps(data), stats())

    assert result.clamped is True
    assert result.params == {
        "chunk_size": 450, "chunk_overlap": 112, "min_chunk_size": 40,
        "max_chunk_size": 480, "table_chunk_size": 100, "code_chunk_size": 480,
    }


def test_numbers_given_as_text_are_coerced():
    data = json.loads(answer())
    data["chunking_parameters"]["chunk_size"] = "300"

    assert validate_analysis(json.dumps(data), stats()).params["chunk_size"] == 300


def test_missing_optional_numbers_get_defaults_without_clamped_flag():
    data = json.loads(answer())
    data["chunking_parameters"] = {"chunk_size": 400, "unit": "tokens"}

    result = validate_analysis(json.dumps(data), stats())

    assert result.clamped is False
    assert result.params["chunk_overlap"] == 50
    assert result.params["min_chunk_size"] == 60
    assert result.params["max_chunk_size"] == 480  # min(480, 400 * 1.25 = 500)
    assert result.params["table_chunk_size"] == 480


def test_default_overlap_is_zero_for_paragraph_and_heading_aware():
    data = json.loads(answer(recommended_strategy="paragraph"))
    data["chunking_parameters"] = {"chunk_size": 350, "unit": "tokens"}

    assert validate_analysis(json.dumps(data), stats()).params["chunk_overlap"] == 0


# ---------- unknown strategy and guard rails ----------

def test_unknown_strategy_becomes_recursive_with_a_reason():
    result = validate_analysis(answer(recommended_strategy="agentic"), stats())

    assert result.strategy == "recursive"
    assert result.fallback_reason == "unsupported_strategy:agentic"
    assert result.params["chunk_overlap"] == 50


def test_the_unsupported_name_is_cut_to_40_characters():
    result = validate_analysis(answer(recommended_strategy="z" * 100), stats())

    assert result.fallback_reason == "unsupported_strategy:" + "z" * 40


def test_heading_aware_without_headings_is_blocked():
    result = validate_analysis(answer(recommended_strategy="heading_aware"), stats())

    assert result.strategy == "recursive"
    assert result.fallback_reason == "guard:no_headings"


def test_heading_aware_with_headings_is_allowed():
    st = stats(blocks={"heading": 4, "paragraph": 10, "list": 0, "table": 0, "code": 0})

    result = validate_analysis(answer(recommended_strategy="heading_aware"), st)

    assert result.strategy == "heading_aware"


def test_hybrid_with_nothing_to_mix_is_blocked():
    result = validate_analysis(answer(recommended_strategy="hybrid"), stats())

    assert result.fallback_reason == "guard:nothing_to_mix"


def test_hybrid_with_a_table_is_allowed():
    st = stats(blocks={"heading": 0, "paragraph": 10, "list": 0, "table": 1, "code": 0})

    assert validate_analysis(answer(recommended_strategy="hybrid"), st).strategy == "hybrid"


def test_semantic_over_the_size_cap_is_blocked(monkeypatch):
    monkeypatch.setattr(limits, "SEMANTIC_MAX_CHARS", 1000)

    result = validate_analysis(answer(recommended_strategy="semantic"), stats(chars=5000))

    assert result.fallback_reason == "guard:semantic_too_large"


def test_semantic_under_the_cap_is_allowed():
    assert validate_analysis(answer(recommended_strategy="semantic"), stats()).strategy == "semantic"


def test_single_chunk_over_450_tokens_is_blocked():
    result = validate_analysis(answer(recommended_strategy="single_chunk"), stats(tokens=451))

    assert result.fallback_reason == "guard:too_long_for_single"


def test_fixed_size_on_a_clearly_structured_document_only_adds_a_note():
    st = stats(
        has_hierarchy=True,
        blocks={"heading": 6, "paragraph": 20, "list": 0, "table": 0, "code": 0},
    )

    result = validate_analysis(answer(recommended_strategy="fixed_size"), st)

    assert result.strategy == "fixed_size"
    assert result.fallback_reason == ""
    assert result.notes


# ---------- heuristic default ----------

def test_default_for_a_short_document_is_single_chunk():
    assert default_analysis(stats(tokens=300)).strategy == "single_chunk"


def test_default_for_headings_and_tables_is_hybrid():
    st = stats(blocks={"heading": 3, "paragraph": 5, "list": 0, "table": 1, "code": 0})

    result = default_analysis(st)

    assert (result.strategy, result.params["chunk_overlap"]) == ("hybrid", 40)


def test_default_for_a_hierarchy_is_heading_aware_without_overlap():
    st = stats(has_hierarchy=True, blocks={"heading": 5, "paragraph": 9, "list": 0, "table": 0, "code": 0})

    result = default_analysis(st)

    assert (result.strategy, result.params["chunk_overlap"]) == ("heading_aware", 0)


def test_default_for_long_paragraphs_without_headings_is_paragraph():
    result = default_analysis(stats(avg_paragraph_tokens=300))

    assert (result.strategy, result.params["chunk_overlap"]) == ("paragraph", 40)


def test_default_for_a_table_alone_is_hybrid():
    st = stats(blocks={"heading": 0, "paragraph": 5, "list": 0, "table": 2, "code": 0})

    assert default_analysis(st).strategy == "hybrid"


def test_default_otherwise_is_recursive_350_50():
    result = default_analysis(stats())

    assert (result.strategy, result.params["chunk_size"], result.params["chunk_overlap"]) == ("recursive", 350, 50)
    assert result.source == "heuristic"


# ---------- helpers ----------

def test_extract_json_object_finds_the_braces():
    assert json.loads(extract_json_object('Sure! {"a": 1} hope that helps')) == {"a": 1}
    assert extract_json_object("no braces") == "no braces"


def test_a_saved_analysis_round_trips_and_a_damaged_one_is_refused():
    saved = validate_analysis(answer(), stats()).to_dict()

    assert analysis_from_dict(json.loads(json.dumps(saved))).strategy == "recursive"

    with pytest.raises(ValueError):
        analysis_from_dict({"strategy": "recursive"})
    with pytest.raises(ValueError):
        analysis_from_dict({**saved, "strategy": "agentic"})
