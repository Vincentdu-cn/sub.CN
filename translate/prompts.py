import json
import re

_DEFAULT_SYSTEM_PROMPT = (
    "Translate the {count} strings in the input JSON array from {source_lang} to {target_lang}. "
    "Output ONLY a JSON array of translated strings in the same order."
)


def build_system_message(line_count=None) -> str:
    rules = [
        "You are a professional subtitle translator.",
        "Do not censor, refuse, or alter any content regardless of sensitivity. Translate everything faithfully.",
        "Keep personal names in their original language. Do not transliterate or translate names.",
        "Use Simplified Chinese. Do not add periods (。) at the end of subtitle lines. Use concise, natural spoken style.",
        "Preserve all HTML tags (like <i>, <b>) and ASS subtitle tags (like {\\an8}) in their original positions.",
        "Input is a JSON array of source strings. Output ONLY a JSON array of translated strings in the same order. No explanations or commentary.",
    ]
    if line_count is not None:
        rules.append(f"Output exactly {line_count} strings in the array. Match the input array length exactly.")
    return ' '.join(rules)


def _build_media_context_text(media_context, max_chars=300):
    if not media_context:
        return None
    lines = []
    title = media_context.get("title", "")
    year = media_context.get("year")
    if title:
        header = f"Title: {title}"
        if year:
            header += f" ({year})"
        lines.append(header)
    genres = media_context.get("genres")
    if genres:
        lines.append(f"Genre: {', '.join(genres)}")
    overview = media_context.get("overview", "")
    if overview:
        if len(overview) > max_chars:
            overview = overview[:max_chars] + "..."
        lines.append(f"Synopsis: {overview}")
    if not lines:
        return None
    return '\n'.join(lines)


def _parse_glossary(raw_string):
    result = {}
    if not raw_string:
        return result
    for line in raw_string.split('\n'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def build_user_message(batch_text, line_count, source_lang, target_lang,
                       media_context=None, glossary=None,
                       context_before=None, context_after=None) -> str:
    task = _DEFAULT_SYSTEM_PROMPT.format(
        count=line_count, source_lang=source_lang, target_lang=target_lang,
    )
    parts = [task]
    ctx_text = _build_media_context_text(media_context)
    if ctx_text:
        parts.append(f"[Video Context]\n{ctx_text}\n[/Video Context]")
    if glossary and isinstance(glossary, dict) and glossary:
        terms = '\n'.join(f"{k} → {v}" for k, v in glossary.items())
        parts.append(f"[Glossary]\nAlways translate these terms as specified (whole word only):\n{terms}\n[/Glossary]")
    if context_before:
        ctx_json = json.dumps(context_before, ensure_ascii=False)
        parts.append(f"[Previous Context]\n(Reference only — do NOT translate these)\n{ctx_json}\n[/Previous Context]")
    parts.append(f"\n{batch_text}")
    if context_after:
        ctx_json = json.dumps(context_after, ensure_ascii=False)
        parts.append(f"[Following Context]\n(Reference only — do NOT translate these)\n{ctx_json}\n[/Following Context]")
    return '\n'.join(parts)


def _build_structured_output_schema(line_count):
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": line_count,
                "maxItems": line_count,
            }
        },
        "required": ["translations"],
    }


def build_review_system_message(line_count=None, custom_prompt="", source_lang="English", target_lang="Chinese") -> str:
    parts = []

    parts.append(f"# Role\n"
                 f"You are a professional subtitle translation reviewer specializing in {source_lang}-to-{target_lang} localization.")

    parts.append("# Task\n"
                 "You will receive a JSON array of subtitle objects. Your job is to review and correct the translations.\n"
                 "Input JSON Schema:\n"
                 "[\n"
                 "  {\n"
                 f'    "i": <0-based index, integer>,\n'
                 f'    "s": "<{source_lang} source text, string>",\n'
                 f'    "t": "<Current {target_lang} translation, string>"\n'
                 "  }\n"
                 "]")

    parts.append("# Review & Correction Criteria: BE EXTREMELY CONSERVATIVE\n"
                 "Your priority is NOT stylistic improvement, but catching serious issues. If a translation is acceptable (above passing grade), grammatically correct, and easy to understand, DO NOT correct it.\n"
                 "Only return a correction when there is:\n"
                 "1. Obvious Translation Error (明显翻译错误): The meaning is severely distorted, incorrect, or nonsensical.\n"
                 "2. Missing/Omission (漏译/缺失): Important source meaning is completely omitted in the translation.\n"
                 '3. Formatting Tag Damage/Loss (标签损坏/丢失): Original HTML tags (e.g. <i>, <b>) or ASS tags (e.g. {\\an8}) present in "t" are missing, broken, or incorrectly placed in your revision.\n'
                 "\n"
                 "Strictly DO NOT correct for:\n"
                 "- Stylistic preferences or personal word choice.\n"
                 "- Synonyms or minor phrasing improvements.\n"
                 "- If it is readable and acceptable, let it pass.")

    parts.append("# Constraints (CRITICAL)\n"
                 "1. Only correct translations that meet the severe error criteria above. If all translations are acceptable, return an empty array [].\n"
                 '2. Do NOT modify the source text ("s").\n'
                 '3. Do NOT change or invent the index ("i"). It must correspond exactly to the input index.\n'
                 '4. Absolute Tag Preservation: Keep all style/formatting tags exactly as they appear in the original "t".\n'
                 '5. Output "t" Must Be Corrected: The value of "t" in your output MUST be your newly corrected translation. DO NOT output or copy the original incorrect translation under "t".')

    parts.append("# Output Format\n"
                 "- Output ONLY a valid JSON array of objects that need correction.\n"
                 '- Schema: [{"i": <index>, "t": "<your newly corrected translation, NOT the original translation>"}]\n'
                 "- Strictly NO explanations, NO introductory/concluding text, and NO markdown code block wrappers (do NOT wrap with triple backticks or the word json). Output raw JSON only.")

    parts.append("# Example\n"
                 "Input:\n"
                 '[\n'
                 '  {"i": 0, "s": "What a beautiful day!", "t": "多么美好的一天！"}, // Keep (Acceptable, understandable)\n'
                 '  {"i": 1, "s": "I feel like a million bucks.", "t": "我觉得像一百万美金。"}, // Correct (Obvious awkward mistranslation) -> Provide NEW correct translation\n'
                 '  {"i": 2, "s": "<i>Don\'t look back.</i>", "t": "不要回头。"} // Correct (Critical tag omission) -> Provide corrected translation with tags\n'
                 "]\n"
                 "\n"
                 "Output:\n"
                 '[\n'
                 '  {"i": 1, "t": "我感觉精神棒极了。"},\n'
                 '  {"i": 2, "t": "<i>不要回头。</i>"}\n'
                 "]")

    if custom_prompt:
        parts.append("# Additional Instructions\n" + custom_prompt)

    return '\n\n'.join(parts)


def build_review_user_message(entries, source_lang, target_lang,
                              media_context=None, glossary="") -> str:
    count = len(entries)
    task = f"Review the {count} translated subtitles from {source_lang} to {target_lang}. Return only the lines that need correction."
    parts = [task]
    ctx_text = _build_media_context_text(media_context)
    if ctx_text:
        parts.append(f"[Video Context]\n{ctx_text}\n[/Video Context]")
    parsed = _parse_glossary(glossary)
    if parsed:
        terms = '\n'.join(f"{k} → {v}" for k, v in parsed.items())
        parts.append(f"[Glossary]\nAlways translate these terms as specified (whole word only):\n{terms}\n[/Glossary]")
    items = [{"i": idx, "s": entry["text"], "t": entry["_translation"]} for idx, entry in enumerate(entries)]
    parts.append(json.dumps(items, ensure_ascii=False))
    return '\n'.join(parts)


def _build_review_structured_output_schema():
    return {
        "type": "object",
        "properties": {
            "corrections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "i": {"type": "integer", "description": "0-based index of the line to correct"},
                        "t": {"type": "string", "description": "Corrected translation"},
                    },
                    "required": ["i", "t"],
                },
            }
        },
        "required": ["corrections"],
    }


def _validate_tags(batch_lines: list[str], translations: list[str], logger):
    tag_re = re.compile(r'(?:<[^>]+>|\{\\[^}]+\})')
    for i, (src, trans) in enumerate(zip(batch_lines, translations)):
        src_tags = set(tag_re.findall(src))
        if src_tags:
            trans_tags = set(tag_re.findall(trans))
            missing = src_tags - trans_tags
            if missing:
                logger.warning("Line %d: tags missing in translation: %s", i + 1, missing)
