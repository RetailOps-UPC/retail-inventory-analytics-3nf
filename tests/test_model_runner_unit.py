# -*- coding: utf-8 -*-
"""
Unit tests for the model runner's parsing and dependency resolution.

No database needed: these cover the logic that decides WHAT runs and in WHAT
ORDER, which is where a wrong dependency would silently build a fact before the
dimension it points at.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import model_runner as mr
from model_runner import Model, ModelError

REAL_MODELS = mr.DEFAULT_MODELS_DIR


def make(name, depends=(), kind="table", target=None):
    return Model(name=name, kind=kind, target=target or f"mart.{name}",
                 depends_on=list(depends), description="", sql="SELECT 1",
                 path=Path(f"{name}.sql"))


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

def test_parse_model_reads_the_header(tmp_path):
    path = tmp_path / "10_x.sql"
    path.write_text("-- model: x\n-- kind: incremental\n-- target: mart.x\n"
                    "-- depends_on: a, b\n-- description: hello\n\nSELECT 1;\n",
                    encoding="utf-8")
    model = mr.parse_model(path)
    assert (model.name, model.kind, model.target) == ("x", "incremental", "mart.x")
    assert model.depends_on == ["a", "b"]
    assert model.description == "hello"


def test_parse_model_treats_an_empty_depends_on_as_no_dependencies(tmp_path):
    path = tmp_path / "10_x.sql"
    path.write_text("-- model: x\n-- kind: table\n-- target: mart.x\n"
                    "-- depends_on:\nSELECT 1;\n", encoding="utf-8")
    assert mr.parse_model(path).depends_on == []


@pytest.mark.parametrize("missing", ["model", "kind", "target"])
def test_parse_model_rejects_a_missing_required_header(tmp_path, missing):
    lines = {"model": "-- model: x", "kind": "-- kind: table",
             "target": "-- target: mart.x"}
    del lines[missing]
    path = tmp_path / "bad.sql"
    path.write_text("\n".join(lines.values()) + "\nSELECT 1;\n", encoding="utf-8")
    with pytest.raises(ModelError, match=missing):
        mr.parse_model(path)


def test_parse_model_rejects_an_unknown_kind(tmp_path):
    path = tmp_path / "bad.sql"
    path.write_text("-- model: x\n-- kind: materialised\n-- target: mart.x\nSELECT 1;\n",
                    encoding="utf-8")
    with pytest.raises(ModelError, match="kind"):
        mr.parse_model(path)


def test_header_comments_inside_the_sql_body_are_not_re_read(tmp_path):
    """A 'model:' mentioned in a later comment must not override the header."""
    path = tmp_path / "x.sql"
    path.write_text("-- model: real\n-- kind: table\n-- target: mart.real\n"
                    "SELECT 1;\n-- model: impostor\n", encoding="utf-8")
    assert mr.parse_model(path).name == "real"


def test_duplicate_model_names_are_rejected(tmp_path):
    for fname in ("10_a.sql", "20_b.sql"):
        (tmp_path / fname).write_text(
            "-- model: same\n-- kind: table\n-- target: mart.same\nSELECT 1;\n",
            encoding="utf-8")
    with pytest.raises(ModelError, match="duplicate"):
        mr.load_models(tmp_path)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_topological_order_respects_dependencies():
    models = {m.name: m for m in (make("fact", ["dim_a", "dim_b"]),
                                  make("dim_a"), make("dim_b"),
                                  make("view", ["fact"]))}
    order = mr.topological_order(models)
    assert order.index("dim_a") < order.index("fact")
    assert order.index("dim_b") < order.index("fact")
    assert order.index("fact") < order.index("view")


def test_topological_order_is_deterministic():
    models = {m.name: m for m in (make("b"), make("a"), make("c", ["a"]))}
    assert mr.topological_order(models) == mr.topological_order(dict(reversed(models.items())))


def test_a_dependency_cycle_is_rejected():
    models = {m.name: m for m in (make("a", ["b"]), make("b", ["a"]))}
    with pytest.raises(ModelError, match="cycle"):
        mr.topological_order(models)


def test_an_unknown_dependency_is_rejected():
    with pytest.raises(ModelError, match="unknown"):
        mr.topological_order({"a": make("a", ["ghost"])})


def test_select_with_plus_includes_downstream_models():
    models = {m.name: m for m in (make("dim"), make("fact", ["dim"]),
                                  make("view", ["fact"]), make("other"))}
    assert mr.select_models(models, ["dim+"]) == ["dim", "fact", "view"]
    assert mr.select_models(models, ["fact"]) == ["fact"]
    assert mr.select_models(models, None) == mr.topological_order(models)


def test_select_rejects_an_unknown_model():
    with pytest.raises(ModelError, match="unknown"):
        mr.select_models({"a": make("a")}, ["nope"])


# ---------------------------------------------------------------------------
# Tests file
# ---------------------------------------------------------------------------

def test_parse_tests_splits_on_markers_and_strips_the_trailing_semicolon(tmp_path):
    path = tmp_path / "tests.sql"
    path.write_text(
        "-- header comment\n"
        "-- test: first | model=m1 | severity=error\n"
        "SELECT 1 WHERE FALSE;\n\n"
        "-- test: second | model=m2 | severity=warn\n"
        "-- a comment inside the test\n"
        "SELECT 2;\n", encoding="utf-8")
    first, second = mr.parse_tests(path)
    assert (first.name, first.model, first.severity) == ("first", "m1", "error")
    assert not first.sql.endswith(";")
    assert (second.name, second.severity) == ("second", "warn")


def test_a_test_without_a_select_is_rejected(tmp_path):
    path = tmp_path / "tests.sql"
    path.write_text("-- test: broken | model=m | severity=error\n-- nothing here\n",
                    encoding="utf-8")
    with pytest.raises(ModelError, match="SELECT"):
        mr.parse_tests(path)


# ---------------------------------------------------------------------------
# The project's real models
# ---------------------------------------------------------------------------

def test_the_real_models_load_and_form_a_valid_graph():
    models = mr.load_models(REAL_MODELS)
    order = mr.topological_order(models)
    for name, model in models.items():
        if model.kind in ("incremental", "snapshot"):
            for dim in ("dim_date", "dim_store", "dim_product"):
                assert order.index(dim) < order.index(name), \
                    f"{name} would be built before {dim}"


def _transitive_dependencies(models, name):
    seen, stack = set(), list(models[name].depends_on)
    while stack:
        dep = stack.pop()
        if dep not in seen:
            seen.add(dep)
            stack.extend(models[dep].depends_on)
    return seen


def test_every_dimension_a_model_reads_is_built_before_it():
    """If a model reads mart.dim_x, dim_x must be guaranteed to build first.

    This is the check that stops the classic failure: it works today because
    alphabetical order happens to be right, then breaks when a file is renamed.
    A TRANSITIVE dependency is enough to guarantee the order, so a view over
    facts need not re-declare the dimensions those facts already depend on.
    """
    models = mr.load_models(REAL_MODELS)
    for name, model in models.items():
        referenced = set(re.findall(r"mart\.(dim_\w+)", model.sql))
        referenced.discard(model.target.replace("mart.", ""))
        unguaranteed = referenced - _transitive_dependencies(models, name)
        assert not unguaranteed, (
            f"{name} reads {sorted(unguaranteed)} but nothing guarantees it builds first")


def test_every_test_targets_a_model_that_exists():
    models = mr.load_models(REAL_MODELS)
    tests = mr.parse_tests(REAL_MODELS / mr.TESTS_FILE)
    assert len(tests) >= 10
    assert all(t.model in models for t in tests)
    assert len({t.name for t in tests}) == len(tests), "duplicate test names"
