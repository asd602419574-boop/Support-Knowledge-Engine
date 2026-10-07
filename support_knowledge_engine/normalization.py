from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .governance import normalize_alias


DEFAULT_RULES_PATH = Path(__file__).resolve().parents[1] / "config" / "query_rules.json"


@dataclass(frozen=True)
class NormalizedQuery:
    original: str
    normalized: str
    retrieval_query: str
    applied_rules: tuple[str, ...]
    product_ids: tuple[int, ...]
    product_names: tuple[str, ...]
    product_terms: tuple[str, ...]
    fault_terms: tuple[str, ...]
    ambiguous: bool


_ASCII_ALNUM = "0-9A-Za-z"


def alias_regex(alias: str) -> re.Pattern[str]:
    """Compile a pattern that finds ``alias`` as a whole token.

    A boundary is enforced only on edges that are ASCII letters or digits, so
    ``ACM2`` does not match inside ``ACM25`` or ``xACM2y``. Edges that are CJK
    characters or punctuation need no boundary, because Chinese text has no
    spaces and ``航拍迷你二代故障`` must still match ``航拍迷你二代``.
    """
    first, last = alias[:1], alias[-1:]
    prefix = rf"(?<![{_ASCII_ALNUM}])" if first.isascii() and first.isalnum() else ""
    suffix = rf"(?![{_ASCII_ALNUM}])" if last.isascii() and last.isalnum() else ""
    return re.compile(prefix + re.escape(alias) + suffix, re.IGNORECASE)


def load_rules(path: str | Path = DEFAULT_RULES_PATH) -> dict:
    with Path(path).open("r", encoding="utf-8") as source:
        rules = json.load(source)
    if not isinstance(rules.get("symptom_synonyms"), dict) or not isinstance(
        rules.get("spelling_corrections"), dict
    ):
        raise ValueError("查询规则配置缺少 symptom_synonyms 或 spelling_corrections。")
    return rules


def _replace_phrases(value: str, mapping: dict[str, str], prefix: str, applied: list[str]) -> str:
    for source, target in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        pattern = re.compile(re.escape(source), re.IGNORECASE)
        if pattern.search(value):
            value = pattern.sub(target, value)
            applied.append(f"{prefix}:{source}->{target}")
    return value


def normalize_query(connection, query: str, rules_path: str | Path = DEFAULT_RULES_PATH) -> NormalizedQuery:
    original = query
    value = query.strip()
    applied: list[str] = []
    nfkc = unicodedata.normalize("NFKC", value)
    if nfkc != value:
        applied.append("unicode:nfkc")
    value = nfkc
    lowered = value.lower()
    if lowered != value:
        applied.append("case:latin-lower")
    value = lowered

    rules = load_rules(rules_path)
    value = _replace_phrases(value, rules["spelling_corrections"], "spelling", applied)
    value = _replace_phrases(value, rules["symptom_synonyms"], "synonym", applied)
    punctuation = str(rules.get("stop_punctuation", ""))
    cleaned = re.sub(f"[{re.escape(punctuation)}]", " ", value) if punctuation else value
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned != value:
        applied.append("text:punctuation-whitespace")
    value = cleaned

    alias_rows = connection.execute(
        """SELECT a.product_id, a.alias_text, a.normalized_alias, p.standard_name
           FROM product_aliases a JOIN products p ON p.id = a.product_id
           WHERE a.is_enabled = 1 ORDER BY length(a.alias_text) DESC, a.id"""
    ).fetchall()
    normalized_value = normalize_alias(value)
    matches: dict[int, tuple[str, str]] = {}
    for row in alias_rows:
        alias = row["normalized_alias"]
        if alias and alias_regex(alias).search(normalized_value):
            matches[int(row["product_id"])] = (str(row["standard_name"]), str(row["alias_text"]))

    product_ids = tuple(sorted(matches))
    product_names = tuple(matches[item][0] for item in product_ids)
    product_terms = tuple(matches[item][1] for item in product_ids)
    ambiguous = len(product_ids) > 1
    retrieval = value
    if len(product_ids) == 1:
        alias_text = matches[product_ids[0]][1]
        retrieval = alias_regex(alias_text).sub(" ", retrieval)
        retrieval = re.sub(r"\s+", " ", retrieval).strip()
        applied.append(f"product:{alias_text}->{matches[product_ids[0]][0]}")
    fault_terms = tuple(term for term in retrieval.split() if term)
    if not retrieval:
        retrieval = value
    return NormalizedQuery(
        original=original, normalized=value, retrieval_query=retrieval,
        applied_rules=tuple(dict.fromkeys(applied)), product_ids=product_ids,
        product_names=product_names, product_terms=product_terms,
        fault_terms=fault_terms, ambiguous=ambiguous,
    )
