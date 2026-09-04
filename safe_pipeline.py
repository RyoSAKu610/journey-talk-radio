from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence
from urllib.parse import urlsplit

import feedparser
import requests


PROJECT_DIR = Path(__file__).resolve().parent
CATALOG_PATH = PROJECT_DIR / "safe_catalog.json"
SAFE_SCHEMA_VERSION = 1
SAFE_CONTRACT_VERSION = 1
REVIEWED_CATALOG_SHA256 = "ced2da2c79186cbf202fa761bf9900d2380a98289b2df734d8ae5a5a4842abbb"
SUPPORTED_LANGUAGES = (
    "en-US", "de-DE", "es-ES", "ru-RU", "zh-CN", "ko-KR",
)
SOURCE_LANGUAGES = frozenset({"ja-JP", "en-US"})
EXPECTED_SOURCE_LANGUAGES = {
    "NHK NEWS WEB": "ja-JP",
    "BBC News World": "en-US",
    "Reuters via Google News": "en-US",
}
EPISODE_MINIMUM_SECONDS = 600.0
EPISODE_TARGET_SECONDS = 720.0
EPISODE_MAXIMUM_SECONDS = 900.0
MAX_RSS_BYTES = 5 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CHUNK_BYTES = 32 * 1024
MAX_EPISODE_BYTES = 512 * 1024
MAX_DONE_BYTES = 16 * 1024
_EPSILON = 1e-6
_ARTICLE_LIMITS = {
    "feed": 200,
    "title": 500,
    "published": 200,
    "summary": 600,
}
_UTTERANCE_KEYS = {
    "slot_id", "speaker", "language", "content_kind", "content_ref",
    "text", "pause_after_ms", "repeat_of_slot_id",
}
_EPISODE_KEYS = {
    "schema_version", "episode_date", "generated_at", "run_id",
    "contract_hash", "config_hash", "source_digest", "model", "plan",
    "program", "catalog_sha256", "selected_articles", "source_quotes",
    "utterances", "episode_sha256",
}
_PLAN_VARIANT_KEYS = frozenset({"opening", "news", "expressions", "closing"})
_ARTICLE_FIELDS = {
    "source_id", "feed", "source_language", "title", "url", "published",
    "summary",
}
_QUOTE_FIELDS = {
    "quote_id", "source_id", "field", "start", "end", "text", "sha256",
    "source_language",
}


class SafePipelineError(RuntimeError):
    """A fail-closed error in the publishable catalog pipeline."""


_MAX_CONTRACT_MODULE_BYTES = 512 * 1024
with Path(__file__).resolve().open("rb") as _module_handle:
    _MODULE_SOURCE_BYTES = _module_handle.read(_MAX_CONTRACT_MODULE_BYTES + 1)
if len(_MODULE_SOURCE_BYTES) > _MAX_CONTRACT_MODULE_BYTES:
    raise RuntimeError("safe pipeline module exceeds the contract hash read limit")
_MODULE_SOURCE_SHA256 = hashlib.sha256(_MODULE_SOURCE_BYTES).digest()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class SafeArticle:
    source_id: str
    feed: str
    source_language: str
    title: str
    url: str
    published: str
    summary: str

    def snapshot(self) -> Dict[str, str]:
        return {
            "source_id": self.source_id,
            "feed": self.feed,
            "source_language": self.source_language,
            "title": self.title,
            "url": self.url,
            "published": self.published,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ExpectedSlot:
    slot_id: str
    speaker: str
    language: str
    content_kind: str
    content_ref: str
    text: str
    base_pause_ms: int


@dataclass(frozen=True)
class SelectorRuntime:
    model_name: str
    model_digest: str


@dataclass(frozen=True)
class LoadedCatalog(Mapping[str, Any]):
    value: Mapping[str, Any]
    canonical_bytes: bytes
    sha256: str

    def __getitem__(self, key: str) -> Any:
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self) -> int:
        return len(self.value)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _require_loaded_catalog(catalog: Any) -> LoadedCatalog:
    if type(catalog) is not LoadedCatalog:
        raise SafePipelineError("a bound LoadedCatalog is required")
    if (
        len(catalog.canonical_bytes) > MAX_MANIFEST_BYTES
        or sha256_bytes(catalog.canonical_bytes) != catalog.sha256
    ):
        raise SafePipelineError("LoadedCatalog bytes/hash binding is invalid")
    try:
        parsed = strict_json_loads(catalog.canonical_bytes, MAX_MANIFEST_BYTES)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SafePipelineError(f"LoadedCatalog canonical bytes are invalid: {exc}") from exc
    if canonical_json_bytes(parsed) != catalog.canonical_bytes:
        raise SafePipelineError("LoadedCatalog bytes are not canonical JSON")
    if _thaw_json(catalog.value) != parsed:
        raise SafePipelineError("LoadedCatalog value does not match its canonical bytes")
    _validate_catalog_value(parsed)
    return catalog


def _strict_object(pairs: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON value: {value}")


def strict_json_loads(data: bytes, maximum_bytes: int) -> Any:
    if len(data) > maximum_bytes:
        raise ValueError(f"JSON exceeds {maximum_bytes} bytes")
    return json.loads(
        data.decode("utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )


def read_bounded_json(path: Path, maximum_bytes: int) -> Any:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise SafePipelineError(f"Could not read {path}: {exc}") from exc
    try:
        return strict_json_loads(data, maximum_bytes)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SafePipelineError(f"Invalid strict JSON in {path}: {exc}") from exc


def read_bounded_bytes(path: Path, maximum_bytes: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise SafePipelineError(f"Could not read {path}: {exc}") from exc
    if len(data) > maximum_bytes:
        raise SafePipelineError(f"File exceeds {maximum_bytes} bytes: {path}")
    return data


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in text)


def _has_url(text: str) -> bool:
    return any(
        pattern.search(text) is not None
        for pattern in (
            re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}:[^\s]+"),
            re.compile(r"(?i)\bwww\."),
            re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b"),
            re.compile(r"(?i)\b(?:[A-Z0-9-]+\.)+[A-Z]{2,63}(?:/[^\s]*)?\b"),
            re.compile(
                r"(?iu)(?<![\w@])(?:[\w-]+\.)+(?:[^\W_\d]|-){2,63}(?:/[^\s]*)?"
            ),
            re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        )
    )


def _has_markdown_structure(text: str) -> bool:
    return (
        any(char in text for char in "[]<>`*_#")
        or re.search(r"(?m)^\s*(?:[-+>]\s|\d+[.)]\s)", text) is not None
    )


def _script_is_pure(language: str, text: str) -> bool:
    has_latin = bool(re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", text))
    has_cyrillic = bool(re.search(r"[\u0400-\u052f]", text))
    has_han = bool(re.search(r"[\u3400-\u9fff]", text))
    has_japanese_kana = bool(re.search(r"[\u3040-\u30ff]", text))
    has_hangul = bool(re.search(r"[\uac00-\ud7af]", text))
    if language in {"en-US", "de-DE", "es-ES"}:
        return has_latin and not (has_cyrillic or has_han or has_japanese_kana or has_hangul)
    if language == "ru-RU":
        return has_cyrillic and not (has_latin or has_han or has_japanese_kana or has_hangul)
    if language == "zh-CN":
        return has_han and not (has_latin or has_cyrillic or has_japanese_kana or has_hangul)
    if language == "ko-KR":
        return has_hangul and not (has_latin or has_cyrillic or has_japanese_kana)
    if language == "ja-JP":
        return (has_japanese_kana or has_han) and not (has_cyrillic or has_hangul)
    return False


def _validate_catalog_value(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise SafePipelineError("safe catalog root must be an object")
    required = {
        "schema_version", "supported_languages", "variant_ids", "pause_profiles",
        "base_pauses_ms", "opening", "news", "expressions", "closing", "languages",
    }
    if set(value) != required or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise SafePipelineError("safe catalog schema is invalid")
    if value.get("supported_languages") != list(SUPPORTED_LANGUAGES):
        raise SafePipelineError("safe catalog language list is invalid")
    variants = value.get("variant_ids")
    if not isinstance(variants, Mapping) or set(variants) != _PLAN_VARIANT_KEYS:
        raise SafePipelineError("safe catalog variant ids are invalid")
    if any(variants[key] != ["reviewed-v1"] for key in _PLAN_VARIANT_KEYS):
        raise SafePipelineError("safe catalog contains an unreviewed variant")
    profiles = value.get("pause_profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != {"brisk", "balanced", "reflective"}:
        raise SafePipelineError("safe catalog pause profiles are invalid")
    for name, offsets in profiles.items():
        if (
            not isinstance(offsets, list)
            or not offsets
            or any(type(item) is not int or item < 0 or item > 6000 for item in offsets)
            or offsets != sorted(set(offsets))
            or offsets[0] != 0
        ):
            raise SafePipelineError(f"invalid pause profile: {name}")
    base_pauses = value.get("base_pauses_ms")
    expected_lengths = {"opening": 6, "news": 14, "expressions": 14, "closing": 6}
    if not isinstance(base_pauses, Mapping) or set(base_pauses) != set(expected_lengths):
        raise SafePipelineError("safe catalog base pauses are invalid")
    for section, length in expected_lengths.items():
        pauses = base_pauses[section]
        if (
            not isinstance(pauses, list)
            or len(pauses) != length
            or any(type(item) is not int or not 0 <= item <= 2000 for item in pauses)
        ):
            raise SafePipelineError(f"safe catalog base pauses are invalid for {section}")
    languages = value.get("languages")
    if not isinstance(languages, Mapping) or set(languages) != set(SUPPORTED_LANGUAGES):
        raise SafePipelineError("safe catalog language records are invalid")
    expected_language_fields = {
        "name", "P1", "P2", "P3", "P4", "P5", "P6", "opening_welcome",
        "opening_focus", "expressions_review", "closing_thanks",
    }
    foreign_texts: List[str] = []
    for code in SUPPORTED_LANGUAGES:
        record = languages[code]
        if not isinstance(record, Mapping) or set(record) != expected_language_fields:
            raise SafePipelineError(f"safe catalog record is invalid for {code}")
        for key, text in record.items():
            if not isinstance(text, str) or not text or unicodedata.normalize("NFKC", text) != text:
                raise SafePipelineError(f"safe catalog text is not canonical: {code}/{key}")
            if key != "name":
                if (
                    _has_control(text)
                    or "\n" in text
                    or _has_url(text)
                    or _has_markdown_structure(text)
                    or any(char.isdigit() for char in text)
                    or not _script_is_pure(code, text)
                ):
                    raise SafePipelineError(f"unsafe catalog text: {code}/{key}")
                foreign_texts.append(text.casefold())
    if len(foreign_texts) != len(set(foreign_texts)):
        raise SafePipelineError("foreign safe catalog contains duplicate speech")
    for text in _walk_strings(value):
        if unicodedata.normalize("NFKC", text) != text or _has_control(text) or "\n" in text:
            raise SafePipelineError("safe catalog contains non-canonical text")
    if sha256_bytes(canonical_json_bytes(value)) != REVIEWED_CATALOG_SHA256:
        raise SafePipelineError("safe catalog content is not the exact reviewed catalog")


def load_catalog(path: Path = CATALOG_PATH) -> LoadedCatalog:
    raw_bytes = read_bounded_bytes(path, MAX_MANIFEST_BYTES)
    try:
        value = strict_json_loads(raw_bytes, MAX_MANIFEST_BYTES)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SafePipelineError(f"Invalid strict catalog JSON in {path}: {exc}") from exc
    _validate_catalog_value(value)
    canonical = canonical_json_bytes(value)
    loaded = LoadedCatalog(
        value=_freeze_json(value),
        canonical_bytes=canonical,
        sha256=sha256_bytes(canonical),
    )
    return _require_loaded_catalog(loaded)


def contract_hash(catalog: LoadedCatalog) -> str:
    catalog = _require_loaded_catalog(catalog)
    payload = (
        b"journey-talk-safe-contract-v1\0"
        + _MODULE_SOURCE_SHA256
        + b"\0catalog\0"
        + catalog.canonical_bytes
    )
    return sha256_bytes(payload)


def config_hash(config: Mapping[str, Any]) -> str:
    try:
        return sha256_bytes(canonical_json_bytes(config))
    except (TypeError, ValueError) as exc:
        raise SafePipelineError(f"Config cannot be canonicalized: {exc}") from exc


def safe_content_config_hash(config: Mapping[str, Any]) -> str:
    record = {
        "program": config["program"],
        "languages": config["languages"],
        "script": {
            key: config["script"][key]
            for key in (
                "news_items", "minimum_utterances",
                "minimum_estimated_minutes", "maximum_estimated_minutes",
            )
        },
        "selector_model": config["ollama"]["model"],
    }
    return sha256_bytes(canonical_json_bytes(record))


def safe_source_config_hash(config: Mapping[str, Any]) -> str:
    rss = config["rss"]
    record = {
        "user_agent": rss.get("user_agent"),
        "items_per_source": rss["items_per_source"],
        "max_items": rss["max_items"],
        "summary_max_characters": rss["summary_max_characters"],
        "sources": [
            {
                "name": source["name"],
                "url": source["url"],
                "source_language": source["source_language"],
                "enabled": source.get("enabled", True),
            }
            for source in rss["sources"]
        ],
    }
    return sha256_bytes(canonical_json_bytes(record))


def selector_runtime(config: Mapping[str, Any]) -> SelectorRuntime:
    """Describe the configured enum selector without making a metadata request."""
    model_name = str(config["ollama"]["model"])
    identity = sha256_bytes(b"journey-talk-enum-selector\0" + model_name.encode("utf-8"))
    return SelectorRuntime(model_name=model_name, model_digest=identity)


def validate_safe_config(config: Mapping[str, Any]) -> None:
    navigation = config.get("program", {}).get("navigation_language", {})
    if navigation.get("code") != "ja-JP":
        raise SafePipelineError("safe mode requires navigation language ja-JP")
    hosts = config.get("program", {}).get("hosts")
    if not isinstance(hosts, list) or [item.get("id") for item in hosts if isinstance(item, Mapping)] != ["MC_F", "MC_M"]:
        raise SafePipelineError("safe mode requires hosts MC_F then MC_M")
    languages = config.get("languages")
    if not isinstance(languages, list) or [item.get("code") for item in languages if isinstance(item, Mapping)] != list(SUPPORTED_LANGUAGES):
        raise SafePipelineError("safe mode supports exactly en/de/es/ru/zh/ko in catalog order")
    script = config.get("script", {})
    if (
        script.get("news_items") != 3
        or script.get("minimum_utterances") != 110
        or script.get("minimum_estimated_minutes") != 10
        or script.get("maximum_estimated_minutes") != 15
    ):
        raise SafePipelineError("safe mode requires three sources, 110 slots, and 10-15 minutes")
    sources = config.get("rss", {}).get("sources")
    enabled = [item for item in sources or [] if isinstance(item, Mapping) and item.get("enabled", True)]
    if len(enabled) != 3:
        raise SafePipelineError("safe mode requires exactly three enabled RSS sources")
    actual = [
        (item.get("name"), item.get("source_language"))
        for item in enabled
    ]
    if actual != list(EXPECTED_SOURCE_LANGUAGES.items()):
        raise SafePipelineError("safe RSS source_language mapping is not the reviewed mapping")
    if any(item.get("source_language") not in SOURCE_LANGUAGES for item in enabled):
        raise SafePipelineError("safe RSS source language is unsupported")


def _clean_article_text(value: Any) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def canonical_article_text(
    value: Any, field: str, *, allow_empty: bool,
) -> str:
    if field not in _ARTICLE_LIMITS:
        raise ValueError(f"unknown article field: {field}")
    normalized = unicodedata.normalize("NFKC", unescape(str(value or "")))
    canonical = unicodedata.normalize("NFKC", _clean_article_text(normalized)).strip()
    canonical = re.sub(r"\s+", " ", canonical).strip()
    if _has_control(canonical) or "<" in canonical or ">" in canonical:
        raise ValueError(f"unsafe article {field}")
    if not allow_empty and not canonical:
        raise ValueError(f"empty article {field}")
    if len(canonical) > _ARTICLE_LIMITS[field]:
        raise ValueError(f"article {field} exceeds {_ARTICLE_LIMITS[field]} characters")
    return canonical


def canonical_article_url(value: Any) -> str:
    if type(value) is not str:
        value = str(value or "")
    if (
        not value
        or value != value.strip()
        or len(value) > 2048
        or any(
            char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            for char in value
        )
        or any(char in value for char in "<>")
    ):
        raise ValueError("unsafe article URL")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("article URL must be absolute HTTP(S)")
    return value


def _latin_letter_count(text: str) -> int:
    return sum(
        1 for char in text
        if char.isalpha() and unicodedata.name(char, "").startswith("LATIN ")
    )


def _contains_any(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


def _validate_source_text_script(source_language: str, text: str, *, quote: bool) -> None:
    han_count = len(re.findall(r"[\u3400-\u9fff]", text))
    kana_count = len(re.findall(r"[\u3040-\u30ff]", text))
    has_han = han_count > 0
    has_kana = kana_count > 0
    has_hangul = _contains_any(text, r"[\uac00-\ud7af]")
    has_cyrillic = _contains_any(text, r"[\u0400-\u052f]")
    if source_language == "en-US":
        alphabetic = sum(1 for char in text if char.isalpha())
        latin = _latin_letter_count(text)
        if (
            latin == 0
            or alphabetic == 0
            or latin / alphabetic < 0.8
            or has_han
            or has_kana
            or has_hangul
            or has_cyrillic
        ):
            raise SafePipelineError("en-US source text is not predominantly Latin")
    elif source_language == "ja-JP":
        if has_hangul or has_cyrillic:
            raise SafePipelineError("ja-JP source text contains a competing script")
        alphabetic = sum(1 for char in text if char.isalpha())
        japanese = han_count + kana_count
        if (
            alphabetic == 0
            or japanese / alphabetic < 0.8
            or not (kana_count >= 2 or han_count >= 2)
            or (not quote and kana_count == 0)
        ):
            layer = "quote" if quote else "snapshot field"
            raise SafePipelineError(
                f"ja-JP source {layer} is not predominantly Japanese script"
            )
    else:
        raise SafePipelineError("source_language is not supported by the safe source contract")


def validate_article_source_scripts(article: SafeArticle) -> None:
    _validate_source_text_script(article.source_language, article.title, quote=False)
    _validate_source_text_script(article.source_language, article.summary, quote=False)


def validate_article_quote_scripts(article: SafeArticle) -> None:
    """Validate the exact segments that could become speech, not only metadata."""
    for field, finder in (("title", title_segment), ("summary", summary_segment)):
        _, _, text = finder(getattr(article, field))
        _validate_source_text_script(article.source_language, text, quote=True)


def _segment_is_safe(text: str, minimum: int, maximum: int) -> bool:
    return (
        minimum <= len(text) <= maximum
        and text == text.strip()
        and unicodedata.normalize("NFKC", text) == text
        and not _has_control(text)
        and "\n" not in text
        and not _has_url(text)
        and not _has_markdown_structure(text)
    )


def title_segment(title: str) -> tuple[int, int, str]:
    if not _segment_is_safe(title, 1, 150):
        raise SafePipelineError("article title has no safe whole-title quote")
    return 0, len(title), title


_SENTENCE_END = frozenset("。！？.!?")
_CLOSING_QUOTES = frozenset("\"'”’」』）)")


def summary_segments(summary: str) -> List[tuple[int, int, str]]:
    segments: List[tuple[int, int, str]] = []
    start = 0
    index = 0
    while index < len(summary):
        if summary[index] not in _SENTENCE_END:
            index += 1
            continue
        end = index + 1
        while end < len(summary) and summary[end] in _CLOSING_QUOTES:
            end += 1
        if end < len(summary) and not summary[end].isspace() and summary[index] == ".":
            index += 1
            continue
        while start < end and summary[start].isspace():
            start += 1
        candidate_end = end
        while candidate_end > start and summary[candidate_end - 1].isspace():
            candidate_end -= 1
        candidate = summary[start:candidate_end]
        if _segment_is_safe(candidate, 20, 150):
            segments.append((start, candidate_end, candidate))
        start = end
        while start < len(summary) and summary[start].isspace():
            start += 1
        index = start
    return segments


def summary_segment(summary: str) -> tuple[int, int, str]:
    segments = summary_segments(summary)
    if not segments:
        raise SafePipelineError("article summary has no safe complete 20-150 character sentence")
    return segments[0]


def _read_stream_limited(
    response: Any, remaining: int, *, deadline_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    length_header = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if length_header not in (None, ""):
        try:
            advertised = int(length_header)
        except (TypeError, ValueError) as exc:
            raise SafePipelineError("RSS Content-Length is invalid") from exc
        if advertised < 0 or advertised > remaining:
            raise SafePipelineError("RSS Content-Length exceeds the remaining 5 MiB limit")
    chunks: List[bytes] = []
    total = 0
    started = clock()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if deadline_seconds is not None and clock() - started > deadline_seconds:
            raise SafePipelineError("stream response exceeded its total time limit")
        if not chunk:
            continue
        total += len(chunk)
        if total > remaining:
            raise SafePipelineError("decompressed RSS data exceeds the cumulative 5 MiB limit")
        chunks.append(bytes(chunk))
    if deadline_seconds is not None and clock() - started > deadline_seconds:
        raise SafePipelineError("stream response exceeded its total time limit")
    return b"".join(chunks)


def fetch_safe_articles(config: Mapping[str, Any], session: requests.Session) -> List[SafeArticle]:
    validate_safe_config(config)
    rss = config["rss"]
    timeout = int(rss["request_timeout_seconds"])
    per_source = int(rss["items_per_source"])
    headers = {"User-Agent": str(rss.get("user_agent") or "JourneyTalk-safe/1")}
    candidates: List[SafeArticle] = []
    consumed = 0
    for source in rss["sources"]:
        if not source.get("enabled", True):
            continue
        response = None
        try:
            feed_name = canonical_article_text(source["name"], "feed", allow_empty=False)
            source_language = str(source["source_language"])
            response = session.get(
                str(source["url"]), headers=headers,
                timeout=(min(timeout, 10), timeout), stream=True,
            )
            response.raise_for_status()
            payload = _read_stream_limited(
                response, MAX_RSS_BYTES - consumed,
                deadline_seconds=float(timeout),
            )
            consumed += len(payload)
            parsed = feedparser.parse(payload)
            entries = list(parsed.entries)
            if not entries:
                raise SafePipelineError("RSS feed has no entries")
        except (requests.RequestException, SafePipelineError, ValueError, TypeError):
            if response is not None:
                response.close()
            continue
        finally:
            if response is not None:
                response.close()
        added = 0
        for entry in entries:
            if added >= per_source:
                break
            try:
                title = canonical_article_text(entry.get("title"), "title", allow_empty=False)
                summary = canonical_article_text(
                    entry.get("summary") or entry.get("description"),
                    "summary", allow_empty=False,
                )
                published = canonical_article_text(
                    entry.get("published") or entry.get("updated") or "",
                    "published", allow_empty=True,
                )
                url = canonical_article_url(entry.get("link") or "")
                candidate_article = SafeArticle(
                    "", feed_name, source_language, title, url, published, summary,
                )
                validate_article_source_scripts(candidate_article)
                validate_article_quote_scripts(candidate_article)
            except (ValueError, SafePipelineError):
                continue
            candidates.append(candidate_article)
            added += 1
    if consumed > MAX_RSS_BYTES:
        raise SafePipelineError("cumulative RSS byte limit was exceeded")
    return candidates


def select_safe_articles(
    candidates: Sequence[SafeArticle], config: Mapping[str, Any],
) -> List[SafeArticle]:
    selected: List[SafeArticle] = []
    seen_urls: set[str] = set()
    enabled = [source for source in config["rss"]["sources"] if source.get("enabled", True)]
    for index, source in enumerate(enabled, start=1):
        feed = canonical_article_text(source["name"], "feed", allow_empty=False)
        source_language = str(source["source_language"])
        match = None
        for candidate in candidates:
            if (
                candidate.feed == feed
                and candidate.source_language == source_language
                and candidate.url.casefold() not in seen_urls
            ):
                try:
                    validate_article_source_scripts(candidate)
                    validate_article_quote_scripts(candidate)
                except SafePipelineError:
                    continue
                match = candidate
                break
        if match is None:
            raise SafePipelineError(f"No safe title-and-summary candidate for RSS source {feed}")
        seen_urls.add(match.url.casefold())
        selected.append(
            SafeArticle(
                f"S{index:02d}", match.feed, match.source_language, match.title,
                match.url, match.published, match.summary,
            )
        )
    if len(selected) != 3:
        raise SafePipelineError("safe selection did not produce exactly three articles")
    return selected


def decode_article_snapshots(value: Any, config: Mapping[str, Any]) -> List[SafeArticle]:
    if not isinstance(value, list) or len(value) != 3:
        raise SafePipelineError("selected_articles must contain exactly three snapshots")
    enabled = [source for source in config["rss"]["sources"] if source.get("enabled", True)]
    articles: List[SafeArticle] = []
    for index, (record, source) in enumerate(zip(value, enabled), start=1):
        if not isinstance(record, Mapping) or set(record) != _ARTICLE_FIELDS:
            raise SafePipelineError("selected article fields are invalid")
        if any(type(record[field]) is not str for field in _ARTICLE_FIELDS):
            raise SafePipelineError("selected article fields must be strings")
        expected_id = f"S{index:02d}"
        if (
            record["source_id"] != expected_id
            or record["feed"] != source["name"]
            or record["source_language"] != source["source_language"]
        ):
            raise SafePipelineError("selected article order/feed/language is invalid")
        for field in ("feed", "title", "published", "summary"):
            canonical = canonical_article_text(
                record[field], field, allow_empty=field == "published",
            )
            if canonical != record[field]:
                raise SafePipelineError(f"selected article {field} is not canonical")
        if canonical_article_url(record["url"]) != record["url"]:
            raise SafePipelineError("selected article URL is not canonical")
        article = SafeArticle(**record)
        validate_article_source_scripts(article)
        validate_article_quote_scripts(article)
        articles.append(article)
    return articles


def article_snapshots(articles: Sequence[SafeArticle]) -> List[Dict[str, str]]:
    return [article.snapshot() for article in articles]


def source_digest(articles: Sequence[SafeArticle]) -> str:
    return sha256_bytes(canonical_json_bytes(article_snapshots(articles)))


def build_quote_records(articles: Sequence[SafeArticle]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for article in articles:
        for section, field, finder in (
            ("A", "title", title_segment),
            ("B", "summary", summary_segment),
        ):
            start, end, text = finder(getattr(article, field))
            _validate_source_text_script(article.source_language, text, quote=True)
            records.append(
                {
                    "quote_id": f"quote:{article.source_id}:{section}",
                    "source_id": article.source_id,
                    "field": field,
                    "start": start,
                    "end": end,
                    "text": text,
                    "sha256": sha256_bytes(text.encode("utf-8")),
                    "source_language": article.source_language,
                }
            )
    return records


def _fallback_seed(episode_date: date, digest: str, contract: str) -> str:
    return sha256_bytes(
        episode_date.isoformat().encode("ascii")
        + digest.encode("ascii")
        + contract.encode("ascii")
    )


def fallback_plan(
    episode_date: date, digest: str, contract: str,
    catalog: LoadedCatalog, status: str = "fallback",
) -> Dict[str, Any]:
    catalog = _require_loaded_catalog(catalog)
    seed = _fallback_seed(episode_date, digest, contract)
    start = int(seed[:16], 16) % len(SUPPORTED_LANGUAGES)
    rotation = list(SUPPORTED_LANGUAGES[start:] + SUPPORTED_LANGUAGES[:start])
    profiles = sorted(catalog["pause_profiles"])
    pace = profiles[int(seed[16:32], 16) % len(profiles)]
    return {
        "language_rotation": rotation,
        "pace_profile": pace,
        "variant_ids": {key: "reviewed-v1" for key in sorted(_PLAN_VARIANT_KEYS)},
        "selector": {
            "mode": "fallback",
            "status": status,
            "seed_sha256": seed,
        },
    }


def _validate_plan_core(value: Any, catalog: LoadedCatalog) -> Dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"language_rotation", "pace_profile", "variant_ids"}:
        raise ValueError("selector plan fields are invalid")
    rotation = value["language_rotation"]
    if (
        not isinstance(rotation, list)
        or len(rotation) != 6
        or set(rotation) != set(SUPPORTED_LANGUAGES)
        or any(type(code) is not str for code in rotation)
    ):
        raise ValueError("selector language rotation is invalid")
    pace = value["pace_profile"]
    if type(pace) is not str or pace not in catalog["pause_profiles"]:
        raise ValueError("selector pace profile is invalid")
    variants = value["variant_ids"]
    if not isinstance(variants, Mapping) or set(variants) != _PLAN_VARIANT_KEYS:
        raise ValueError("selector variant map is invalid")
    for key in _PLAN_VARIANT_KEYS:
        if type(variants[key]) is not str or variants[key] not in catalog["variant_ids"][key]:
            raise ValueError("selector chose an unreviewed catalog variant")
    return {
        "language_rotation": list(rotation),
        "pace_profile": pace,
        "variant_ids": dict(variants),
    }


def select_plan(
    config: Mapping[str, Any], session: requests.Session, runtime: Any,
    episode_date: date, digest: str, contract: str,
    catalog: LoadedCatalog,
) -> Dict[str, Any]:
    catalog = _require_loaded_catalog(catalog)
    fallback = fallback_plan(episode_date, digest, contract, catalog)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["language_rotation", "pace_profile", "variant_ids"],
        "properties": {
            "language_rotation": {
                "type": "array", "minItems": 6, "maxItems": 6,
                "uniqueItems": True,
                "items": {"type": "string", "enum": list(SUPPORTED_LANGUAGES)},
            },
            "pace_profile": {
                "type": "string", "enum": sorted(catalog["pause_profiles"]),
            },
            "variant_ids": {
                "type": "object", "additionalProperties": False,
                "required": sorted(_PLAN_VARIANT_KEYS),
                "properties": {
                    key: {"type": "string", "enum": list(catalog["variant_ids"][key])}
                    for key in sorted(_PLAN_VARIANT_KEYS)
                },
            },
        },
    }
    prompt = (
        "Choose only one permitted enum plan for a deterministic reviewed catalog episode. "
        "Return the JSON object only. Do not write or translate any episode text. "
        f"Episode date: {episode_date.isoformat()}. Permitted languages: "
        + ", ".join(SUPPORTED_LANGUAGES)
        + "."
    )
    ollama = config["ollama"]
    connect_timeout = min(int(ollama["connection_timeout_seconds"]), 5)
    read_timeout = min(int(ollama["generation_timeout_seconds"]), 45)
    response = None
    try:
        response = session.post(
            f"{str(ollama['base_url']).rstrip('/')}/api/generate",
            json={
                "model": runtime.model_name,
                "prompt": prompt,
                "stream": False,
                "format": schema,
                "keep_alive": ollama["keep_alive"],
                "options": {
                    "temperature": 0,
                    "num_ctx": min(int(ollama["context_tokens"]), 2048),
                    "num_predict": 128,
                },
            },
            timeout=(connect_timeout, read_timeout),
            stream=True,
        )
        response.raise_for_status()
        outer_bytes = _read_stream_limited(
            response, MAX_MANIFEST_BYTES, deadline_seconds=float(read_timeout),
        )
        payload = strict_json_loads(outer_bytes, MAX_MANIFEST_BYTES)
        if (
            not isinstance(payload, Mapping)
            or payload.get("done") is not True
            or payload.get("done_reason") == "length"
        ):
            raise ValueError("selector response is incomplete")
        raw = payload.get("response") if isinstance(payload, Mapping) else None
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
            raise ValueError("selector response is missing or oversized")
        parsed = strict_json_loads(raw.encode("utf-8"), 4096)
        selected = _validate_plan_core(parsed, catalog)
        selected["selector"] = {
            "mode": "ollama",
            "status": "valid",
            "seed_sha256": fallback["selector"]["seed_sha256"],
        }
        return selected
    except requests.Timeout:
        return fallback_plan(episode_date, digest, contract, catalog, "timeout")
    except (
        requests.RequestException, SafePipelineError, ValueError, TypeError,
        KeyError, UnicodeError, RecursionError,
    ):
        return fallback_plan(episode_date, digest, contract, catalog, "invalid")
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()


def _validate_plan(
    value: Any, episode_date: date, digest: str, contract: str,
    catalog: LoadedCatalog,
) -> Dict[str, Any]:
    catalog = _require_loaded_catalog(catalog)
    if not isinstance(value, Mapping) or set(value) != {
        "language_rotation", "pace_profile", "variant_ids", "selector",
    }:
        raise SafePipelineError("episode plan fields are invalid")
    try:
        core = _validate_plan_core(
            {key: value[key] for key in ("language_rotation", "pace_profile", "variant_ids")},
            catalog,
        )
    except ValueError as exc:
        raise SafePipelineError(str(exc)) from exc
    selector = value["selector"]
    if not isinstance(selector, Mapping) or set(selector) != {"mode", "status", "seed_sha256"}:
        raise SafePipelineError("selector metadata is invalid")
    seed = _fallback_seed(episode_date, digest, contract)
    if selector.get("seed_sha256") != seed:
        raise SafePipelineError("selector fallback seed is invalid")
    mode = selector.get("mode")
    status = selector.get("status")
    if mode == "ollama":
        if status != "valid":
            raise SafePipelineError("Ollama selector metadata is invalid")
    elif mode == "fallback":
        if status not in {"fallback", "invalid", "timeout"}:
            raise SafePipelineError("fallback selector status is invalid")
        expected = fallback_plan(episode_date, digest, contract, catalog, str(status))
        if core != {key: expected[key] for key in core}:
            raise SafePipelineError("fallback plan is not deterministic")
    else:
        raise SafePipelineError("selector mode is invalid")
    core["selector"] = dict(selector)
    return core


def _format_catalog_template(template: Any, cue: str) -> str:
    if type(template) is not str or set(re.findall(r"{([^{}]+)}", template)) - {"cue"}:
        raise SafePipelineError("catalog template contains an unsupported placeholder")
    text = template.format(cue=cue)
    if unicodedata.normalize("NFKC", text) != text:
        raise SafePipelineError("resolved catalog text is not NFKC")
    return text


def build_expected_slots(
    articles: Sequence[SafeArticle], quote_records: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any], catalog: LoadedCatalog,
) -> List[ExpectedSlot]:
    catalog = _require_loaded_catalog(catalog)
    if len(articles) != 3:
        raise SafePipelineError("the safe schedule requires exactly three articles")
    rotation = list(plan["language_rotation"])
    quote_by_id = {record["quote_id"]: record for record in quote_records}
    slots: List[ExpectedSlot] = []

    def append(
        slot_id: str, language: str, kind: str, ref: str, text: str,
        base_pause_ms: int,
    ) -> None:
        slots.append(
            ExpectedSlot(
                slot_id=slot_id,
                speaker="MC_F" if len(slots) % 2 == 0 else "MC_M",
                language=language,
                content_kind=kind,
                content_ref=ref,
                text=text,
                base_pause_ms=base_pause_ms,
            )
        )

    opening_code = rotation[0]
    opening_pauses = catalog["base_pauses_ms"]["opening"]
    opening = catalog["opening"]
    language_catalog = catalog["languages"]
    opening_records = (
        ("ja-JP", "catalog:opening:ja_welcome", opening["ja_welcome"]),
        (opening_code, f"catalog:language:{opening_code}:opening_welcome", language_catalog[opening_code]["opening_welcome"]),
        ("ja-JP", "catalog:opening:ja_focus_gloss", opening["ja_focus_gloss"]),
        (opening_code, f"catalog:language:{opening_code}:opening_focus", language_catalog[opening_code]["opening_focus"]),
        ("ja-JP", "catalog:opening:ja_source_policy", opening["ja_source_policy"]),
        ("ja-JP", "catalog:opening:ja_transition", opening["ja_transition"]),
    )
    for index, (language, ref, text) in enumerate(opening_records, start=1):
        append(f"opening.{index:02d}", language, "catalog", ref, text, opening_pauses[index - 1])

    news_pauses = catalog["base_pauses_ms"]["news"]
    for article_index, article in enumerate(articles):
        cue = catalog["news"]["cues"][article.source_id]
        for section_index, section in enumerate(("A", "B")):
            learning_code = rotation[article_index * 2 + section_index]
            prefix = f"news.{article.source_id}.{section}"
            nav = catalog["news"][section]
            append(
                f"{prefix}.01", "ja-JP", "catalog",
                f"catalog:news:{article.source_id}:{section}:intro",
                _format_catalog_template(nav["intro"], cue), news_pauses[0],
            )
            quote_id = f"quote:{article.source_id}:{section}"
            quote = quote_by_id.get(quote_id)
            if quote is None:
                raise SafePipelineError(f"missing source quote {quote_id}")
            append(
                f"{prefix}.02", article.source_language, "source_quote", quote_id,
                str(quote["text"]), news_pauses[1],
            )
            turn = 3
            for phrase_index in range(1, 7):
                phrase = f"P{phrase_index}"
                append(
                    f"{prefix}.{turn:02d}", "ja-JP", "catalog",
                    f"catalog:news:{article.source_id}:{section}:{phrase}:gloss",
                    _format_catalog_template(nav["glosses"][phrase], cue),
                    news_pauses[turn - 1],
                )
                turn += 1
                append(
                    f"{prefix}.{turn:02d}", learning_code, "catalog",
                    f"catalog:language:{learning_code}:{phrase}",
                    language_catalog[learning_code][phrase], news_pauses[turn - 1],
                )
                turn += 1
            if turn != 15:
                raise AssertionError("news safe schedule must contain fourteen slots")

    expression_pauses = catalog["base_pauses_ms"]["expressions"]
    expression = catalog["expressions"]
    expression_turn = 1
    for code in rotation:
        append(
            f"expressions.{expression_turn:02d}", "ja-JP", "catalog",
            f"catalog:expressions:{code}:ja_gloss",
            expression["ja_glosses"][code], expression_pauses[expression_turn - 1],
        )
        expression_turn += 1
        append(
            f"expressions.{expression_turn:02d}", code, "catalog",
            f"catalog:language:{code}:expressions_review",
            language_catalog[code]["expressions_review"],
            expression_pauses[expression_turn - 1],
        )
        expression_turn += 1
    for key in ("ja_policy", "ja_close"):
        append(
            f"expressions.{expression_turn:02d}", "ja-JP", "catalog",
            f"catalog:expressions:{key}", expression[key],
            expression_pauses[expression_turn - 1],
        )
        expression_turn += 1

    closing_pauses = catalog["base_pauses_ms"]["closing"]
    closing = catalog["closing"]
    closing_records = (
        ("ja-JP", "catalog:closing:ja_review", closing["ja_review"]),
        (opening_code, f"catalog:language:{opening_code}:closing_thanks", language_catalog[opening_code]["closing_thanks"]),
        ("ja-JP", "catalog:closing:ja_thanks_gloss", closing["ja_thanks_gloss"]),
        ("ja-JP", "catalog:closing:ja_safety", closing["ja_safety"]),
        ("ja-JP", "catalog:closing:ja_encourage", closing["ja_encourage"]),
        ("ja-JP", "catalog:closing:ja_end", closing["ja_end"]),
    )
    for index, (language, ref, text) in enumerate(closing_records, start=1):
        append(f"closing.{index:02d}", language, "catalog", ref, text, closing_pauses[index - 1])

    if len(slots) != 110:
        raise AssertionError(f"safe schedule resolved {len(slots)} slots instead of 110")
    if any(previous.speaker == following.speaker for previous, following in zip(slots, slots[1:])):
        raise AssertionError("safe schedule does not alternate speakers")
    return slots


def speech_duration_seconds(language: str, text: str) -> float:
    if language in {"ja-JP", "zh-CN"}:
        units = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
        seconds = units / 4.3
    elif language == "ko-KR":
        units = len(re.findall(r"[\uac00-\ud7af]", text))
        seconds = units / 4.0
    else:
        words = len(re.findall(r"[\wÀ-ÖØ-öø-ÿА-Яа-яЁё]+", text))
        seconds = words / 2.3
    return seconds + 0.22 * len(re.findall(r"[。！？.!?;；]", text)) + 0.2


def estimate_duration_seconds(utterances: Sequence[Mapping[str, Any]]) -> float:
    seconds = 0.0
    for utterance in utterances:
        seconds += speech_duration_seconds(str(utterance["language"]), str(utterance["text"]))
        pause = utterance.get("pause_after_ms")
        if type(pause) is not int or pause < 0:
            raise SafePipelineError("pause_after_ms must be a non-negative integer")
        seconds += pause / 1000.0
    return seconds


def pack_pause_options(
    speech_seconds: float, options_by_slot: Sequence[Sequence[int]],
    minimum_seconds: float = EPISODE_MINIMUM_SECONDS,
    target_seconds: float = EPISODE_TARGET_SECONDS,
    maximum_seconds: float = EPISODE_MAXIMUM_SECONDS,
) -> List[int]:
    if not math.isfinite(speech_seconds) or speech_seconds < 0:
        raise SafePipelineError("speech duration is invalid")
    normalized: List[tuple[int, ...]] = []
    for options in options_by_slot:
        values = tuple(sorted(set(options)))
        if not values or any(type(item) is not int or item < 0 or item > 7000 for item in values):
            raise SafePipelineError("pause option set is invalid")
        normalized.append(values)
    minimum_pause = math.ceil((minimum_seconds - speech_seconds - _EPSILON) * 1000)
    maximum_pause = math.floor((maximum_seconds - speech_seconds + _EPSILON) * 1000)
    target_pause = round((target_seconds - speech_seconds) * 1000)
    if maximum_pause < 0:
        raise SafePipelineError("speech alone exceeds the episode maximum")
    states: Dict[int, tuple[int, ...]] = {0: ()}
    for options in normalized:
        following: Dict[int, tuple[int, ...]] = {}
        for total, chosen in states.items():
            for pause in options:
                candidate = total + pause
                if candidate <= maximum_pause and candidate not in following:
                    following[candidate] = chosen + (pause,)
        states = following
        if not states:
            raise SafePipelineError("no duration-feasible pause packing exists")
    feasible = [total for total in states if minimum_pause <= total <= maximum_pause]
    if not feasible:
        raise SafePipelineError("no duration-feasible pause packing exists")
    best = min(feasible, key=lambda total: (abs(total - target_pause), total))
    return list(states[best])


def pack_episode_pauses(
    slots: Sequence[ExpectedSlot], catalog: LoadedCatalog, pace_profile: str,
) -> List[int]:
    catalog = _require_loaded_catalog(catalog)
    offsets = catalog["pause_profiles"].get(pace_profile)
    if not isinstance(offsets, Sequence) or isinstance(offsets, (str, bytes)):
        raise SafePipelineError("unknown pace profile")
    speech = sum(speech_duration_seconds(slot.language, slot.text) for slot in slots)
    options = [tuple(slot.base_pause_ms + offset for offset in offsets) for slot in slots]
    return pack_pause_options(speech, options)


def episode_hash(episode: Mapping[str, Any]) -> str:
    value = dict(episode)
    value.pop("episode_sha256", None)
    return sha256_bytes(canonical_json_bytes(value))


def build_episode(
    config: Mapping[str, Any], articles: Sequence[SafeArticle], plan: Mapping[str, Any],
    runtime: Any, episode_date: date, generated_at: datetime,
    catalog: LoadedCatalog, run_id: str | None = None,
) -> Dict[str, Any]:
    catalog = _require_loaded_catalog(catalog)
    validate_safe_config(config)
    contract = contract_hash(catalog)
    digest = source_digest(articles)
    checked_plan = _validate_plan(plan, episode_date, digest, contract, catalog)
    snapshots = article_snapshots(articles)
    decoded = decode_article_snapshots(snapshots, config)
    quotes = build_quote_records(decoded)
    slots = build_expected_slots(decoded, quotes, checked_plan, catalog)
    pauses = pack_episode_pauses(slots, catalog, checked_plan["pace_profile"])
    utterances = [
        {
            "slot_id": slot.slot_id,
            "speaker": slot.speaker,
            "language": slot.language,
            "content_kind": slot.content_kind,
            "content_ref": slot.content_ref,
            "text": slot.text,
            "pause_after_ms": pause,
            "repeat_of_slot_id": None,
        }
        for slot, pause in zip(slots, pauses)
    ]
    value: Dict[str, Any] = {
        "schema_version": SAFE_SCHEMA_VERSION,
        "episode_date": episode_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "run_id": run_id or secrets.token_hex(16),
        "contract_hash": contract,
        "config_hash": config_hash(config),
        "source_digest": digest,
        "catalog_sha256": catalog.sha256,
        "model": {
            "name": str(runtime.model_name),
            "digest": str(runtime.model_digest),
        },
        "plan": checked_plan,
        "program": {
            "name": str(config["program"]["name"]),
            "hosts": [
                {"id": str(host["id"]), "name": str(host["name"])}
                for host in config["program"]["hosts"]
            ],
        },
        "selected_articles": snapshots,
        "source_quotes": quotes,
        "utterances": utterances,
    }
    value["episode_sha256"] = episode_hash(value)
    validate_episode(value, config, catalog)
    return value


def _article_specific_terms(
    articles: Sequence[SafeArticle], catalog_texts: Sequence[str],
) -> set[str]:
    token_pattern = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё]{5,}|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]{4,}")
    catalog_vocabulary = {
        match.group(0).casefold()
        for text in catalog_texts
        for match in token_pattern.finditer(text)
    }
    article_vocabulary = {
        match.group(0).casefold()
        for article in articles
        for value in (article.title, article.summary)
        for match in token_pattern.finditer(value)
    }
    return article_vocabulary - catalog_vocabulary


def _specific_term_tokens(text: str) -> set[str]:
    return {
        match.group(0).casefold()
        for match in re.finditer(
            r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё]{5,}|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]{4,}",
            text,
        )
    }


def _validate_quote_record_types(records: Any) -> None:
    if not isinstance(records, list) or len(records) != 6:
        raise SafePipelineError("source_quotes must contain exactly six records")
    for record in records:
        if not isinstance(record, Mapping) or set(record) != _QUOTE_FIELDS:
            raise SafePipelineError("source quote fields are invalid")
        for field in _QUOTE_FIELDS - {"start", "end"}:
            if type(record[field]) is not str or not record[field]:
                raise SafePipelineError("source quote string field is invalid")
        if type(record["start"]) is not int or type(record["end"]) is not int:
            raise SafePipelineError("source quote range must use exact integers")


def validate_episode(
    episode: Mapping[str, Any], config: Mapping[str, Any],
    catalog: LoadedCatalog,
) -> None:
    catalog = _require_loaded_catalog(catalog)
    validate_safe_config(config)
    if not isinstance(episode, Mapping) or set(episode) != _EPISODE_KEYS:
        raise SafePipelineError("typed episode fields are invalid")
    if type(episode.get("schema_version")) is not int or episode["schema_version"] != SAFE_SCHEMA_VERSION:
        raise SafePipelineError("typed episode schema_version is invalid")
    if type(episode.get("episode_date")) is not str:
        raise SafePipelineError("typed episode date is invalid")
    try:
        parsed_date = date.fromisoformat(episode["episode_date"])
    except ValueError as exc:
        raise SafePipelineError("typed episode date is invalid") from exc
    if parsed_date.isoformat() != episode["episode_date"]:
        raise SafePipelineError("typed episode date is not canonical ISO format")
    if type(episode.get("generated_at")) is not str:
        raise SafePipelineError("typed episode generated_at is invalid")
    try:
        parsed_generated_at = datetime.fromisoformat(episode["generated_at"])
    except ValueError as exc:
        raise SafePipelineError("typed episode generated_at is invalid") from exc
    if (
        parsed_generated_at.tzinfo is None
        or parsed_generated_at.isoformat() != episode["generated_at"]
    ):
        raise SafePipelineError("typed episode generated_at must include a timezone")
    if type(episode.get("run_id")) is not str or re.fullmatch(r"[0-9a-f]{32}", episode["run_id"]) is None:
        raise SafePipelineError("typed episode run_id is invalid")
    current_contract = contract_hash(catalog)
    current_config = config_hash(config)
    if episode.get("contract_hash") != current_contract:
        raise SafePipelineError("typed episode contract_hash is stale")
    if episode.get("config_hash") != current_config:
        raise SafePipelineError("typed episode config_hash is stale")
    if episode.get("catalog_sha256") != catalog.sha256:
        raise SafePipelineError("typed episode catalog_sha256 is stale")

    program = episode.get("program")
    expected_program = {
        "name": str(config["program"]["name"]),
        "hosts": [
            {"id": str(host["id"]), "name": str(host["name"])}
            for host in config["program"]["hosts"]
        ],
    }
    if program != expected_program:
        raise SafePipelineError("typed episode program metadata is invalid")
    if any(
        type(value) is not str
        or not value
        or unicodedata.normalize("NFKC", value) != value
        or _has_control(value)
        for value in [program["name"]]
        + [item[key] for item in program["hosts"] for key in ("id", "name")]
    ):
        raise SafePipelineError("typed episode program strings are unsafe")

    model = episode.get("model")
    expected_selector_runtime = selector_runtime(config)
    if (
        not isinstance(model, Mapping)
        or set(model) != {"name", "digest"}
        or type(model.get("name")) is not str
        or type(model.get("digest")) is not str
        or not model["digest"]
        or model != {
            "name": expected_selector_runtime.model_name,
            "digest": expected_selector_runtime.model_digest,
        }
    ):
        raise SafePipelineError("typed episode model metadata is invalid")

    articles = decode_article_snapshots(episode.get("selected_articles"), config)
    digest = source_digest(articles)
    if episode.get("source_digest") != digest:
        raise SafePipelineError("typed episode source_digest is invalid")
    checked_plan = _validate_plan(episode.get("plan"), parsed_date, digest, current_contract, catalog)
    if checked_plan != episode["plan"]:
        raise SafePipelineError("typed episode plan is not canonical")

    quote_records = episode.get("source_quotes")
    _validate_quote_record_types(quote_records)
    expected_quotes = build_quote_records(articles)
    if quote_records != expected_quotes:
        raise SafePipelineError("source quote provenance or hash does not match the pinned field slice")
    expected_slots = build_expected_slots(articles, expected_quotes, checked_plan, catalog)
    expected_pauses = pack_episode_pauses(expected_slots, catalog, checked_plan["pace_profile"])

    utterances = episode.get("utterances")
    if not isinstance(utterances, list) or len(utterances) != 110:
        raise SafePipelineError("typed episode must contain exactly 110 utterances")
    catalog_texts = [slot.text for slot in expected_slots if slot.content_kind == "catalog"]
    article_terms = _article_specific_terms(articles, catalog_texts)
    seen_texts: set[str] = set()
    actual_languages: set[str] = set()
    for index, (utterance, slot, pause) in enumerate(
        zip(utterances, expected_slots, expected_pauses)
    ):
        if not isinstance(utterance, Mapping) or set(utterance) != _UTTERANCE_KEYS:
            raise SafePipelineError(f"utterance {index + 1} fields are invalid")
        for field in (
            "slot_id", "speaker", "language", "content_kind", "content_ref", "text",
        ):
            if type(utterance[field]) is not str:
                raise SafePipelineError(f"utterance {index + 1} {field} must be a string")
        if utterance["repeat_of_slot_id"] is not None:
            raise SafePipelineError("repeat_of_slot_id must be null in the reviewed schedule")
        if type(utterance["pause_after_ms"]) is not int:
            raise SafePipelineError("pause_after_ms must use an exact integer")
        expected_fields = {
            "slot_id": slot.slot_id,
            "speaker": slot.speaker,
            "language": slot.language,
            "content_kind": slot.content_kind,
            "content_ref": slot.content_ref,
            "text": slot.text,
            "pause_after_ms": pause,
            "repeat_of_slot_id": None,
        }
        if dict(utterance) != expected_fields:
            raise SafePipelineError(f"utterance {index + 1} does not resolve from its declared slot")
        text = utterance["text"]
        if (
            not text
            or unicodedata.normalize("NFKC", text) != text
            or _has_control(text)
            or "\n" in text
        ):
            raise SafePipelineError(f"utterance {index + 1} text is not canonical")
        identity = text.casefold()
        if identity in seen_texts:
            raise SafePipelineError("resolved episode contains duplicate speech text")
        seen_texts.add(identity)
        actual_languages.add(utterance["language"])
        if slot.content_kind == "catalog":
            if not slot.content_ref.startswith("catalog:"):
                raise SafePipelineError("catalog slot has a non-catalog reference")
            if (
                any(char.isdigit() for char in text)
                or _has_url(text)
                or _has_markdown_structure(text)
                or re.search(r"\bS0[1-3]\b", text)
                or not _script_is_pure(slot.language, text)
            ):
                raise SafePipelineError(f"unsafe or cross-script catalog text at {slot.slot_id}")
            if article_terms & _specific_term_tokens(text):
                raise SafePipelineError(f"article-specific term leaked into catalog slot {slot.slot_id}")
        elif slot.content_kind == "source_quote":
            if not slot.content_ref.startswith("quote:"):
                raise SafePipelineError("source quote slot has an invalid reference")
        else:
            raise SafePipelineError("free_text and unknown content kinds are not publishable")

    if any(
        previous["speaker"] == following["speaker"]
        for previous, following in zip(utterances, utterances[1:])
    ):
        raise SafePipelineError("global MC alternation is invalid")
    if actual_languages != {"ja-JP", *SUPPORTED_LANGUAGES}:
        raise SafePipelineError("typed episode does not contain the complete seven-language set")
    seconds = estimate_duration_seconds(utterances)
    if not EPISODE_MINIMUM_SECONDS - _EPSILON <= seconds <= EPISODE_MAXIMUM_SECONDS + _EPSILON:
        raise SafePipelineError(f"typed episode duration is outside 600-900 seconds: {seconds:.3f}")
    if abs(seconds - EPISODE_TARGET_SECONDS) > 181:
        raise SafePipelineError("duration packing did not select a reasonable target-nearest result")
    if type(episode.get("episode_sha256")) is not str or episode["episode_sha256"] != episode_hash(episode):
        raise SafePipelineError("canonical episode_sha256 is invalid")
    try:
        episode_bytes = episode_document_bytes(episode)
    except (TypeError, ValueError) as exc:
        raise SafePipelineError(f"typed episode cannot be serialized: {exc}") from exc
    if len(episode_bytes) > MAX_EPISODE_BYTES:
        raise SafePipelineError("typed episode exceeds 512 KiB")
    markdown = render_markdown(episode)
    if len(markdown.encode("utf-8")) > MAX_EPISODE_BYTES:
        raise SafePipelineError("rendered Markdown exceeds 512 KiB")
    if len(re.findall(r"^### \[S0[1-3]\] ", markdown, flags=re.MULTILINE)) != 3:
        raise SafePipelineError("rendered Markdown does not contain exactly three source headings")
    if len(re.findall(r"^- \[S0[1-3]\] ", markdown, flags=re.MULTILINE)) != 3:
        raise SafePipelineError("rendered Markdown does not contain exactly three source references")


def episode_document_bytes(episode: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            episode, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _markdown_title(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("#", "\\#")
    )


def _utterance_markdown(utterance: Mapping[str, Any]) -> str:
    return f"[{utterance['speaker']}|{utterance['language']}] {utterance['text']}"


def render_markdown(episode: Mapping[str, Any]) -> str:
    """Render only from the authoritative typed episode JSON object."""
    utterances = episode["utterances"]
    articles = episode["selected_articles"]
    plan = episode["plan"]
    lines = [
        "---",
        f"schema_version: {episode['schema_version']}",
        f"episode_date: {json.dumps(episode['episode_date'])}",
        f"generated_at: {json.dumps(episode['generated_at'])}",
        f"run_id: {json.dumps(episode['run_id'])}",
        f"episode_sha256: {json.dumps(episode['episode_sha256'])}",
        f"contract_hash: {json.dumps(episode['contract_hash'])}",
        f"catalog_sha256: {json.dumps(episode['catalog_sha256'])}",
        f"config_hash: {json.dumps(episode['config_hash'])}",
        f"source_digest: {json.dumps(episode['source_digest'])}",
        "typed_episode: true",
        "publishable: true",
        "---",
        f"# {_markdown_title(episode['program']['name'])} — {episode['episode_date']}",
        "",
        "## オープニング",
    ]
    cursor = 0
    lines.extend(_utterance_markdown(item) for item in utterances[cursor:cursor + 6])
    cursor += 6
    lines.extend(["", "## ニュース本編"])
    for article_index, article in enumerate(articles):
        lines.extend(["", f"### [{article['source_id']}] {_markdown_title(article['title'])}"])
        for section_index, section in enumerate(("A", "B")):
            learning_code = plan["language_rotation"][article_index * 2 + section_index]
            lines.extend(["", f"#### {section} — {learning_code}"])
            lines.extend(_utterance_markdown(item) for item in utterances[cursor:cursor + 14])
            cursor += 14
    lines.extend(["", "## 今日の表現・復習"])
    lines.extend(_utterance_markdown(item) for item in utterances[cursor:cursor + 14])
    cursor += 14
    lines.extend(["", "## エンディング"])
    lines.extend(_utterance_markdown(item) for item in utterances[cursor:cursor + 6])
    cursor += 6
    if cursor != 110:
        raise SafePipelineError("renderer did not consume exactly 110 utterances")
    lines.extend(["", "## 参照記事", ""])
    for article in articles:
        published = f" / {article['published']}" if article["published"] else ""
        lines.append(
            f"- [{article['source_id']}] [{_markdown_title(article['title'])}](<{article['url']}>)"
            f" — {_markdown_title(article['feed'])}{_markdown_title(published)}"
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class PublishedPaths:
    done: Path
    run_json: Path
    run_markdown: Path
    fixed_json: Path
    fixed_markdown: Path


_DONE_KEYS = {
    "schema_version", "episode_date", "run_id", "episode_sha256",
    "contract_hash", "config_hash", "source_digest", "catalog_sha256", "model",
    "json_path", "json_sha256", "markdown_path", "markdown_sha256",
    "fixed_json_path", "fixed_markdown_path",
}


def _atomic_write_bytes(
    path: Path, data: bytes,
    io_hook: Callable[[str, Path], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        )
        temporary = Path(handle.name)
        with handle:
            if io_hook is not None:
                io_hook("before_write", temporary)
            handle.write(data)
            if io_hook is not None:
                io_hook("before_fsync", temporary)
            handle.flush()
            os.fsync(handle.fileno())
        if io_hook is not None:
            io_hook("before_replace", temporary)
        os.replace(temporary, path)
        temporary = None
    except BaseException as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise SafePipelineError(f"Could not atomically write {path}: {exc}") from exc
        raise


def _write_immutable_bytes(
    path: Path, data: bytes, maximum_bytes: int,
    io_hook: Callable[[str, Path], None] | None = None,
) -> None:
    if path.exists():
        existing = read_bounded_bytes(path, maximum_bytes)
        if existing != data:
            raise SafePipelineError(f"immutable run artifact already exists with different bytes: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        )
        temporary = Path(handle.name)
        with handle:
            if io_hook is not None:
                io_hook("before_write", temporary)
            handle.write(data)
            if io_hook is not None:
                io_hook("before_fsync", temporary)
            handle.flush()
            os.fsync(handle.fileno())
        if io_hook is not None:
            io_hook("before_replace", temporary)
        try:
            # A hard-link create is atomic and fails when the destination already
            # exists. Never fall back to replace: run artifacts are immutable.
            os.link(temporary, path)
        except FileExistsError:
            if read_bounded_bytes(path, maximum_bytes) != data:
                raise SafePipelineError(
                    f"immutable run artifact already exists with different bytes: {path}"
                )
        temporary.unlink()
        temporary = None
    except BaseException as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, OSError):
            raise SafePipelineError(
                f"Could not atomically create immutable artifact {path}: {exc}"
            ) from exc
        raise


def _assert_immutable_compatible(
    path: Path, data: bytes, maximum_bytes: int,
) -> None:
    """Preflight an immutable artifact without changing the filesystem."""
    if path.exists() and read_bounded_bytes(path, maximum_bytes) != data:
        raise SafePipelineError(
            f"immutable run artifact already exists with different bytes: {path}"
        )


_SOURCE_MANIFEST_KEYS = {
    "schema_version", "kind", "pin_id", "episode_date", "contract_hash",
    "catalog_sha256", "content_config_hash", "source_config_hash",
    "source_digest", "selected_articles",
}


def _source_manifest_document_bytes(value: Mapping[str, Any]) -> bytes:
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise SafePipelineError("safe source manifest exceeds 64 KiB")
    return data


def load_source_manifest_or_none(
    work_dir: Path, episode_date: date, config: Mapping[str, Any],
    catalog: LoadedCatalog,
) -> tuple[List[SafeArticle] | None, str]:
    catalog = _require_loaded_catalog(catalog)
    path = work_dir / "manifest.json"
    if not path.is_file():
        return None, "missing"
    try:
        value = read_bounded_json(path, MAX_MANIFEST_BYTES)
        if not isinstance(value, Mapping) or set(value) != _SOURCE_MANIFEST_KEYS:
            raise SafePipelineError("safe source manifest fields are invalid")
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != SAFE_SCHEMA_VERSION
            or value.get("kind") != "safe-source-pin"
            or type(value.get("pin_id")) is not str
            or re.fullmatch(r"[0-9a-f]{32}", value["pin_id"]) is None
            or type(value.get("episode_date")) is not str
        ):
            raise SafePipelineError("safe source manifest metadata is invalid")
        parsed_date = date.fromisoformat(value["episode_date"])
        if parsed_date.isoformat() != value["episode_date"]:
            raise SafePipelineError("safe source manifest date is not canonical")
        if value["episode_date"] != episode_date.isoformat():
            return None, "stale"
        expected_hashes = {
            "contract_hash": contract_hash(catalog),
            "catalog_sha256": catalog.sha256,
            "content_config_hash": safe_content_config_hash(config),
            "source_config_hash": safe_source_config_hash(config),
        }
        if any(value.get(key) != expected for key, expected in expected_hashes.items()):
            return None, "stale"
        articles = decode_article_snapshots(value.get("selected_articles"), config)
        if value.get("source_digest") != source_digest(articles):
            raise SafePipelineError("safe source manifest digest is invalid")
        build_quote_records(articles)
        return articles, "valid"
    except (SafePipelineError, ValueError, TypeError, UnicodeError, RecursionError, OSError):
        return None, "invalid"


def save_source_manifest(
    work_dir: Path, episode_date: date, config: Mapping[str, Any],
    catalog: LoadedCatalog, articles: Sequence[SafeArticle],
    io_hook: Callable[[str, Path], None] | None = None,
) -> Path:
    catalog = _require_loaded_catalog(catalog)
    snapshots = article_snapshots(articles)
    decoded = decode_article_snapshots(snapshots, config)
    build_quote_records(decoded)
    value = {
        "schema_version": SAFE_SCHEMA_VERSION,
        "kind": "safe-source-pin",
        "pin_id": secrets.token_hex(16),
        "episode_date": episode_date.isoformat(),
        "contract_hash": contract_hash(catalog),
        "catalog_sha256": catalog.sha256,
        "content_config_hash": safe_content_config_hash(config),
        "source_config_hash": safe_source_config_hash(config),
        "source_digest": source_digest(decoded),
        "selected_articles": snapshots,
    }
    data = _source_manifest_document_bytes(value)
    destination = work_dir / "manifest.json"
    _atomic_write_bytes(destination, data, io_hook)
    return destination


def _done_document_bytes(value: Mapping[str, Any]) -> bytes:
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_DONE_BYTES:
        raise SafePipelineError("done pointer exceeds 16 KiB")
    return data


def _publication_paths(output_dir: Path, episode_date: str, run_id: str) -> PublishedPaths:
    run_stem = f"{episode_date}_{run_id}_podcast"
    return PublishedPaths(
        done=output_dir / f"{episode_date}_podcast.done.json",
        run_json=output_dir / "runs" / f"{run_stem}.json",
        run_markdown=output_dir / "runs" / f"{run_stem}.md",
        fixed_json=output_dir / f"{episode_date}_podcast.json",
        fixed_markdown=output_dir / f"{episode_date}_podcast.md",
    )


def _relative_output_path(output_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError as exc:
        raise SafePipelineError("publication path escapes the output directory") from exc


def publish_episode(
    episode: Mapping[str, Any], config: Mapping[str, Any], output_dir: Path,
    catalog: LoadedCatalog,
    stage_hook: Callable[[str], None] | None = None,
    io_hook: Callable[[str, Path], None] | None = None,
) -> PublishedPaths:
    if episode.get("draft") is True or episode.get("publishable") is False:
        raise SafePipelineError("draft artifacts cannot enter the publish path")
    catalog = _require_loaded_catalog(catalog)
    validate_episode(episode, config, catalog)
    json_bytes = episode_document_bytes(episode)
    markdown_bytes = render_markdown(episode).encode("utf-8")
    if len(json_bytes) > MAX_EPISODE_BYTES or len(markdown_bytes) > MAX_EPISODE_BYTES:
        raise SafePipelineError("publishable episode exceeds 512 KiB")
    output_dir = output_dir.resolve()
    paths = _publication_paths(output_dir, episode["episode_date"], episode["run_id"])
    json_hash = sha256_bytes(json_bytes)
    markdown_hash = sha256_bytes(markdown_bytes)
    done = {
        "schema_version": SAFE_SCHEMA_VERSION,
        "episode_date": episode["episode_date"],
        "run_id": episode["run_id"],
        "episode_sha256": episode["episode_sha256"],
        "contract_hash": episode["contract_hash"],
        "config_hash": episode["config_hash"],
        "source_digest": episode["source_digest"],
        "catalog_sha256": episode["catalog_sha256"],
        "model": dict(episode["model"]),
        "json_path": _relative_output_path(output_dir, paths.run_json),
        "json_sha256": json_hash,
        "markdown_path": _relative_output_path(output_dir, paths.run_markdown),
        "markdown_sha256": markdown_hash,
        "fixed_json_path": _relative_output_path(output_dir, paths.fixed_json),
        "fixed_markdown_path": _relative_output_path(output_dir, paths.fixed_markdown),
    }
    # Finish every serialization and cap check before creating any artifact.
    done_bytes = _done_document_bytes(done)
    _assert_immutable_compatible(paths.run_json, json_bytes, MAX_EPISODE_BYTES)
    _assert_immutable_compatible(paths.run_markdown, markdown_bytes, MAX_EPISODE_BYTES)

    def wrote(stage: str) -> None:
        if stage_hook is not None:
            stage_hook(stage)

    _write_immutable_bytes(paths.run_json, json_bytes, MAX_EPISODE_BYTES, io_hook)
    wrote("run_json")
    _write_immutable_bytes(
        paths.run_markdown, markdown_bytes, MAX_EPISODE_BYTES, io_hook,
    )
    wrote("run_markdown")
    _atomic_write_bytes(paths.fixed_json, json_bytes, io_hook)
    wrote("fixed_json")
    _atomic_write_bytes(paths.fixed_markdown, markdown_bytes, io_hook)
    wrote("fixed_markdown")
    if (
        sha256_bytes(read_bounded_bytes(paths.fixed_json, MAX_EPISODE_BYTES)) != json_hash
        or sha256_bytes(read_bounded_bytes(paths.fixed_markdown, MAX_EPISODE_BYTES)) != markdown_hash
    ):
        raise SafePipelineError("fixed publication copies do not match the committed hashes")
    _atomic_write_bytes(paths.done, done_bytes, io_hook)
    wrote("done")
    return paths


def _resolve_done_relative(output_dir: Path, value: Any, suffix: str) -> Path:
    if type(value) is not str or not value or "\\" in value:
        raise SafePipelineError("done pointer path is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or not value.endswith(suffix):
        raise SafePipelineError("done pointer path is invalid")
    candidate = (output_dir / Path(*parts)).resolve()
    try:
        candidate.relative_to(output_dir.resolve())
    except ValueError as exc:
        raise SafePipelineError("done pointer path escapes output") from exc
    return candidate


def _validate_done_shape(done: Any) -> Mapping[str, Any]:
    if not isinstance(done, Mapping) or set(done) != _DONE_KEYS:
        raise SafePipelineError("done pointer fields are invalid")
    if type(done.get("schema_version")) is not int or done["schema_version"] != SAFE_SCHEMA_VERSION:
        raise SafePipelineError("done pointer schema is invalid")
    for key in _DONE_KEYS - {"schema_version", "model"}:
        if type(done.get(key)) is not str or not done[key]:
            raise SafePipelineError(f"done pointer {key} is invalid")
    model = done.get("model")
    if (
        not isinstance(model, Mapping)
        or set(model) != {"name", "digest"}
        or any(type(model.get(key)) is not str or not model[key] for key in ("name", "digest"))
    ):
        raise SafePipelineError("done pointer model is invalid")
    for key in ("episode_sha256", "contract_hash", "config_hash", "source_digest", "catalog_sha256", "json_sha256", "markdown_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", done[key]) is None:
            raise SafePipelineError(f"done pointer {key} is not SHA-256")
    if re.fullmatch(r"[0-9a-f]{32}", done["run_id"]) is None:
        raise SafePipelineError("done pointer run_id is invalid")
    try:
        parsed_date = date.fromisoformat(done["episode_date"])
    except ValueError as exc:
        raise SafePipelineError("done pointer date is invalid") from exc
    if parsed_date.isoformat() != done["episode_date"]:
        raise SafePipelineError("done pointer date is not canonical")
    return done


def verify_done_or_none(
    config: Mapping[str, Any], episode_date: date, output_dir: Path,
    catalog: LoadedCatalog,
    repair_fixed_copies: bool = True,
) -> PublishedPaths | None:
    """Verify a committed pair before any Ollama or RSS request."""
    catalog = _require_loaded_catalog(catalog)
    validate_safe_config(config)
    output_dir = output_dir.resolve()
    expected_done = output_dir / f"{episode_date.isoformat()}_podcast.done.json"
    if not expected_done.is_file():
        return None
    done = _validate_done_shape(read_bounded_json(expected_done, MAX_DONE_BYTES))
    if done["episode_date"] != episode_date.isoformat():
        raise SafePipelineError("done pointer date does not match its filename")
    if done["contract_hash"] != contract_hash(catalog) or done["config_hash"] != config_hash(config):
        raise SafePipelineError("done pointer contract/config is stale; use --force")
    if done["model"]["name"] != str(config["ollama"]["model"]):
        raise SafePipelineError("done pointer model name is stale; use --force")
    run_json = _resolve_done_relative(output_dir, done["json_path"], ".json")
    run_markdown = _resolve_done_relative(output_dir, done["markdown_path"], ".md")
    fixed_json = _resolve_done_relative(output_dir, done["fixed_json_path"], ".json")
    fixed_markdown = _resolve_done_relative(output_dir, done["fixed_markdown_path"], ".md")
    expected_paths = _publication_paths(output_dir, done["episode_date"], done["run_id"])
    if (
        run_json != expected_paths.run_json
        or run_markdown != expected_paths.run_markdown
        or fixed_json != expected_paths.fixed_json
        or fixed_markdown != expected_paths.fixed_markdown
        or expected_done != expected_paths.done
    ):
        raise SafePipelineError("done pointer publication paths are not canonical")
    json_bytes = read_bounded_bytes(run_json, MAX_EPISODE_BYTES)
    markdown_bytes = read_bounded_bytes(run_markdown, MAX_EPISODE_BYTES)
    if sha256_bytes(json_bytes) != done["json_sha256"]:
        raise SafePipelineError("committed JSON file hash is invalid")
    if sha256_bytes(markdown_bytes) != done["markdown_sha256"]:
        raise SafePipelineError("committed Markdown file hash is invalid")
    try:
        episode = strict_json_loads(json_bytes, MAX_EPISODE_BYTES)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SafePipelineError(f"committed episode JSON is invalid: {exc}") from exc
    if not isinstance(episode, Mapping):
        raise SafePipelineError("committed episode JSON root is invalid")
    validate_episode(episode, config, catalog)
    if done["catalog_sha256"] != catalog.sha256:
        raise SafePipelineError("done pointer catalog hash is stale; use --force")
    for key in ("run_id", "episode_sha256", "contract_hash", "config_hash", "source_digest", "catalog_sha256"):
        if episode[key] != done[key]:
            raise SafePipelineError(f"done pointer {key} does not match episode JSON")
    if episode["model"] != done["model"]:
        raise SafePipelineError("done pointer model does not match episode JSON")
    expected_markdown = render_markdown(episode).encode("utf-8")
    if expected_markdown != markdown_bytes:
        raise SafePipelineError("committed Markdown is not the rendering of committed JSON")
    for path, expected in ((fixed_json, json_bytes), (fixed_markdown, markdown_bytes)):
        current = None
        try:
            current = read_bounded_bytes(path, MAX_EPISODE_BYTES)
        except SafePipelineError:
            pass
        if current != expected:
            if not repair_fixed_copies:
                raise SafePipelineError("fixed publication copy does not match committed run")
            _atomic_write_bytes(path, expected)
    return expected_paths


def save_draft_artifact(
    draft_dir: Path, episode_date: date, markdown: str, model: Mapping[str, str],
    generated_at: datetime,
) -> Path:
    """Store legacy freeform output in a non-publishable envelope."""
    value = {
        "schema_version": 1,
        "draft": True,
        "publishable": False,
        "episode_date": episode_date.isoformat(),
        "generated_at": generated_at.isoformat(),
        "run_id": uuid.uuid4().hex,
        "model": dict(model),
        "format": "legacy-freeform-markdown",
        "markdown": markdown,
    }
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_EPISODE_BYTES:
        raise SafePipelineError("draft artifact exceeds 512 KiB")
    destination = draft_dir / f"{episode_date.isoformat()}_{value['run_id']}.draft.json"
    _atomic_write_bytes(destination, data)
    return destination
