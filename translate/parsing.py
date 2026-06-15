import json
import logging
import re

logger = logging.getLogger(__name__)


def _parse_translations(response_text: str, expected_count: int, output_mode: str) -> tuple[list[str], bool]:
    # Layer 1: direct JSON parse (works for all modes — array mode returns plain arrays)
    try:
        parsed = json.loads(response_text)
        translations = _extract_translations_array(parsed)
        json_ok = output_mode in ("structured", "auto")
        if len(translations) == expected_count:
            return (translations, json_ok)
        logger.warning("Layer 1 JSON count mismatch: got %d, expected %d", len(translations), expected_count)
        return (_align_count(translations, expected_count), json_ok)
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        logger.debug("Layer 1 JSON parse failed: %s", e)

    # Layer 2: strip markdown fence then JSON parse
    fence_match = re.match(r'^```(?:json)?\s*\n?(.*?)\n?\s*```$', response_text, re.DOTALL)
    if fence_match:
        fenced_content = fence_match.group(1)
        logger.warning("Stripped markdown code fence, retrying JSON parse")
        try:
            parsed = json.loads(fenced_content)
            translations = _extract_translations_array(parsed)
            json_ok = output_mode in ("structured", "auto")
            if len(translations) == expected_count:
                return (translations, json_ok)
            logger.warning("Layer 2 JSON count mismatch: got %d, expected %d", len(translations), expected_count)
            return (_align_count(translations, expected_count), json_ok)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("Layer 2 JSON parse failed: %s", e)

    translations = [''] * expected_count
    for line in response_text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^(\d+)\s*[.、)\]:：。]\s*(.*)', line)
        if m:
            idx = int(m.group(1))
            text = m.group(2).strip()
            if 1 <= idx <= expected_count:
                translations[idx - 1] = text
        else:
            for i, t in enumerate(translations):
                if t == '':
                    translations[i] = line
                    break
    if len([t for t in translations if t]) != expected_count:
        logger.warning("Layer 3 line-split count mismatch: populated %d/%d",
                       len([t for t in translations if t]), expected_count)
    return (translations, False)


def _extract_translations_array(parsed) -> list[str]:
    """Extract translations list from JSON response.
    Handles both plain JSON array and {"translations": [...]} object."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "translations" in parsed:
        return parsed["translations"]
    raise KeyError("Response is neither a JSON array nor an object with 'translations' key")


def _align_count(translations: list[str], expected: int) -> list[str]:
    if len(translations) < expected:
        logger.warning("Padding %d translations to %d with empty strings", len(translations), expected)
        return translations + [''] * (expected - len(translations))
    if len(translations) > expected:
        logger.warning("Truncating %d translations to %d", len(translations), expected)
        return translations[:expected]
    return translations
