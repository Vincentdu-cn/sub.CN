"""AI review orchestration for subtitle translations.

Sends translated entries to an LLM for quality review, applies index-based
corrections, and returns the updated translations list.  Lines the model
does not mention are never touched.
"""

import json
import logging
import re

import requests

from translate.prompts import _validate_tags
from translate.prompts import build_review_system_message, build_review_user_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------

_REFUSAL_PATTERNS = re.compile(
    r"I can't|I'm unable|As an AI|I apologize|I must decline|I will not|I cannot",
    re.IGNORECASE,
)


def _is_refusal(text: str) -> bool:
    return bool(_REFUSAL_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# 3-layer response parsing (adapted for review {"i","t"} format)
# ---------------------------------------------------------------------------

def _parse_review_response(response_text: str) -> list[dict]:
    """Parse the LLM review response into a list of {"i": int, "t": str} objects.

    Three layers:
      1. Direct json.loads()
      2. Strip markdown fences then json.loads()
      3. Regex extract individual {"i": N, "t": "..."} objects from raw text
    """

    # Layer 1: direct JSON parse
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, list):
            return parsed
        logger.warning("Layer 1: parsed JSON is not a list (type=%s)", type(parsed).__name__)
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug("Layer 1 JSON parse failed: %s", e)

    # Layer 2: strip markdown fences then parse
    fence_match = re.match(r'^```(?:json)?\s*\n?(.*?)\n?\s*```$', response_text, re.DOTALL)
    if fence_match:
        fenced_content = fence_match.group(1)
        logger.warning("Stripped markdown code fence, retrying JSON parse")
        try:
            parsed = json.loads(fenced_content)
            if isinstance(parsed, list):
                return parsed
            logger.warning("Layer 2: parsed JSON is not a list (type=%s)", type(parsed).__name__)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Layer 2 JSON parse failed: %s", e)

    # Layer 3: regex extract individual {"i": N, "t": "..."} objects
    corrections: list[dict] = []
    obj_pattern = re.compile(r'\{\s*"i"\s*:\s*(\d+)\s*,\s*"t"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}')
    for m in obj_pattern.finditer(response_text):
        idx = int(m.group(1))
        text = m.group(2)
        # Unescape JSON string escapes
        text = text.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n").replace("\\t", "\t")
        corrections.append({"i": idx, "t": text})
    if corrections:
        logger.info("Layer 3 regex extracted %d corrections", len(corrections))
    else:
        logger.warning("All 3 parsing layers failed for review response")
    return corrections


def _collect_streaming_response(resp: "requests.Response", chunk_timeout: int = 60) -> str:
    """Collect full text from an OpenAI-compatible streaming response.

    Iterates SSE chunks, concatenates delta content, returns the full
    response text once the stream ends or [DONE] is received.
    """
    content_parts: list[str] = []
    for line in resp.iter_lines(chunk_size=None, decode_unicode=True):
        if not line:
            continue
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            text = delta.get("content", "")
            if text:
                content_parts.append(text)
        except (json.JSONDecodeError, IndexError, KeyError):
            continue
    return "".join(content_parts).strip()


def _validate_correction_objects(corrections: list, expected_count: int) -> list[dict]:
    """Filter correction objects: must be dicts with int 'i' in range and non-empty str 't'."""
    valid: list[dict] = []
    for obj in corrections:
        if not isinstance(obj, dict):
            continue
        if "i" not in obj or "t" not in obj:
            continue
        idx = obj["i"]
        text = obj["t"]
        if not isinstance(idx, int) or not isinstance(text, str):
            continue
        if idx < 0 or idx >= expected_count:
            logger.warning("Correction index %d out of range (0-%d), discarding", idx, expected_count - 1)
            continue
        if not text.strip():
            continue
        valid.append({"i": idx, "t": text})
    return valid


# ---------------------------------------------------------------------------
# Main review function
# ---------------------------------------------------------------------------

def review_translations(
    api_url: str,
    api_key: str,
    model: str,
    entries: list[dict],
    source_lang: str,
    target_lang: str,
    timeout: int = 300,
    media_context=None,
    glossary: str = "",
    custom_prompt: str = "",
) -> list[str]:
    """Send translations for AI review and return corrected translations list.

    Parameters
    ----------
    api_url : str
        OpenAI-compatible API base URL.
    api_key : str
        API key for authentication.
    model : str
        Model name to use for review.
    entries : list[dict]
        Each entry must have ``text`` (source) and ``_translation`` (current translation).
        May optionally have ``dup_of`` (int) pointing to the first occurrence index.
    source_lang : str
        Source language name.
    target_lang : str
        Target language name.
    timeout : int
        Request timeout in seconds.
    media_context : dict or None
        Optional media context (title, year, genres, overview).
    glossary : str
        Raw glossary string (key=value per line).
    custom_prompt : str
        Additional instructions appended to the system prompt.

    Returns
    -------
    list[str]
        Full translations list with only corrected lines replaced.
        On any error, returns the original translations untouched.
    """
    try:
        # 1. Empty guard
        if not entries:
            return []

        # 2. Extract original translations
        original_translations = [e.get("_translation", "") for e in entries]
        result_translations = list(original_translations)

        # 3. Deduplicate — skip entries with dup_of, track index mapping
        unique_entries: list[dict] = []
        unique_to_full: list[int] = []  # unique_idx -> full entries index
        full_to_unique: dict[int, int] = {}  # full entries index -> unique_idx (for dup propagation)
        for i, entry in enumerate(entries):
            if "dup_of" in entry:
                continue
            full_to_unique[i] = len(unique_entries)
            unique_to_full.append(i)
            unique_entries.append(entry)

        if not unique_entries:
            return result_translations

        # 4. Build API messages using prompt builders (they handle payload formatting)
        sys_msg = build_review_system_message(line_count=len(unique_entries),
                                              custom_prompt=custom_prompt,
                                              source_lang=source_lang,
                                              target_lang=target_lang)
        user_msg = build_review_user_message(unique_entries, source_lang, target_lang,
                                             media_context=media_context, glossary=glossary)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.3,
            "stream": True,
        }

        url = f"{api_url.rstrip('/')}/chat/completions"

        # 6. Call AI API with streaming (with at most 1 retry on connection error)
        # Streaming avoids losing the entire response on long-thinking models:
        # as long as chunks keep flowing within chunk_timeout, we keep collecting.
        chunk_timeout = min(timeout, 60)  # per-chunk timeout, capped at 60s
        response_text: str | None = None
        for attempt in range(2):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     stream=True, timeout=chunk_timeout)
                resp.raise_for_status()
                response_text = _collect_streaming_response(resp, chunk_timeout=chunk_timeout)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 0:
                    logger.warning("Review API connection error, retrying once: %s", e)
                    continue
                logger.error("Review API connection failed after retry: %s", e)
                return result_translations
            except Exception as e:
                logger.error("Review API call failed: %s", e)
                return result_translations

        if response_text is None:
            return result_translations

        # 7. Check for refusal
        if _is_refusal(response_text):
            logger.info("Review model refused — skipping review")
            return result_translations

        # 8. Parse response
        corrections = _parse_review_response(response_text)

        # 9. Validate: must be a list of dicts with "i" and "t"
        if not isinstance(corrections, list):
            logger.warning("Review response is not a list — skipping review")
            return result_translations

        # Validate format: all items must be dicts with i/t
        corrections = _validate_correction_objects(corrections, len(unique_entries))
        if not corrections:
            logger.info("No valid corrections from review")
            return result_translations

        # 10. Apply corrections
        for corr in corrections:
            unique_idx = corr["i"]
            new_text = corr["t"]
            full_idx = unique_to_full[unique_idx]
            old_text = result_translations[full_idx]

            # Skip if text unchanged
            if new_text == old_text:
                continue

            # Tag validation: discard correction if tags are broken
            source_text = unique_entries[unique_idx]["text"]
            _validate_tags([source_text], [new_text], logger)
            # Re-extract tags to check — _validate_tags only logs warnings,
            # so we do an explicit check and discard if tags are missing
            tag_re = re.compile(r'(?:<[^>]+>|\{\\[^}]+\})')
            src_tags = set(tag_re.findall(source_text))
            if src_tags:
                new_tags = set(tag_re.findall(new_text))
                missing = src_tags - new_tags
                if missing:
                    logger.warning("Correction for line %d discarded — tags missing: %s", full_idx, missing)
                    continue

            result_translations[full_idx] = new_text
            logger.info("Review corrected line %d: '%s' → '%s'", full_idx, old_text, new_text)

        # 11. Re-expand dedup: propagate corrections to dup_of entries
        for i, entry in enumerate(entries):
            if "dup_of" in entry:
                dup_of_idx = entry["dup_of"]
                if result_translations[i] != result_translations[dup_of_idx]:
                    old = result_translations[i]
                    result_translations[i] = result_translations[dup_of_idx]
                    logger.info("Review propagated correction to dup line %d: '%s' → '%s'",
                                i, old, result_translations[dup_of_idx])

        return result_translations

    except Exception as e:
        logger.error("Review failed, returning original translations: %s", e)
        # On any error return original translations
        try:
            return [e.get("_translation", "") for e in entries]
        except Exception:
            return []
