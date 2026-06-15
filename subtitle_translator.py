import json
import logging
import re
import time
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from translate import (
    build_system_message,
    build_user_message,
    _parse_glossary,
    _build_structured_output_schema,
    _validate_tags,
    _parse_translations,
    ProgressTracker,
    check_repetition,
    effective_repetition_threshold,
)
from translate.ass_parser import _parse_ass, _format_ass

logger = logging.getLogger(__name__)


class RepetitionDetected(Exception):
    """Raised when streaming repetition is detected mid-response."""
    pass


def _parse_srt(content: str) -> list[dict]:
    entries = []
    blocks = re.split(r'\n\s*\n', content.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
        timestamp = lines[1].strip()
        if '-->' not in timestamp:
            continue
        text = '\n'.join(lines[2:]).strip()
        entries.append({"index": index, "timestamp": timestamp, "text": text})
    return entries


def _format_srt(entries: list[dict]) -> str:
    parts = []
    for entry in entries:
        parts.append(str(entry["index"]))
        parts.append(entry["timestamp"])
        parts.append(entry["text"])
        parts.append('')
    return '\n'.join(parts)


def _build_batches(entries, batch_size=50):
    return [entries[i:i + batch_size] for i in range(0, len(entries), batch_size)]


def _stream_response(url, headers, payload, input_text="") -> str:
    threshold = effective_repetition_threshold(input_text)
    resp = requests.post(url, headers=headers, json=payload, timeout=120, stream=True)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    chunks = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk_data = json.loads(data_str)
            delta = chunk_data.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                chunks.append(content)
                if check_repetition(chunks, threshold=threshold):
                    logger.warning("Streaming repetition detected, aborting mid-stream")
                    raise RepetitionDetected("Streaming repetition detected")
        except (json.JSONDecodeError, IndexError, KeyError):
            continue
    return "".join(chunks).strip()


def _call_ai(batch_text, source_lang, target_lang, bilingual, line_count,
             api_url, api_key, model, temperature, max_output_tokens,
             system_prompt, bilingual_prompt,
             max_retries, retry_delay, media_context=None, glossary=None,
             context_before=None, context_after=None, output_mode="text",
             streaming=True):
    sys_msg = system_prompt if system_prompt else build_system_message(line_count)
    if output_mode in ("structured", "auto"):
        sys_msg += " Respond with a JSON object containing a 'translations' array."
    user_msg = build_user_message(batch_text, line_count, source_lang, target_lang,
                                  media_context=media_context, glossary=glossary,
                                  context_before=context_before, context_after=context_after)

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
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "stream": streaming,
    }
    if output_mode in ("structured", "auto"):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "translations",
                "strict": True,
                "schema": _build_structured_output_schema(line_count),
            }
        }

    url = f"{api_url.rstrip('/')}/chat/completions"

    last_error = None
    _repetition_retried = False
    for attempt in range(max_retries):
        try:
            if streaming:
                return _stream_response(url, headers, payload, input_text=batch_text)
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data['choices'][0]['message']['content'].strip()
        except RepetitionDetected:
            if not _repetition_retried:
                _repetition_retried = True
                logger.warning("Repetition detected, retrying batch once")
                continue
            logger.warning("Repetition persists after retry, falling back to non-streaming")
            payload["stream"] = False
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            return data['choices'][0]['message']['content'].strip()
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    raise last_error


def translate_srt_file(
    sub_path,
    source_lang="English",
    target_lang="Chinese",
    bilingual=True,
    api_url="https://api.openai.com/v1",
    api_key="",
    model="gpt-4o-mini",
    batch_size=50,
    max_retries=3,
    retry_delay=2.0,
    temperature=0.3,
    max_output_tokens=4096,
    system_prompt=None,
    bilingual_prompt=None,
    media_context=None,
    glossary=None,
    context_lines=2,
    output_mode="text",
    streaming=True,
    concurrency=2,
    ai_review_enabled=True,
    ai_review_model="",
    ai_review_timeout=300,
    ai_review_prompt="",
) -> Path:
    sub_path = Path(sub_path)
    content = sub_path.read_text(encoding='utf-8-sig')

    is_ass = sub_path.suffix.lower() in ('.ass', '.ssa') or '[Events]' in content[:2000]
    if is_ass:
        entries, ass_header = _parse_ass(content)
    else:
        entries = _parse_srt(content)
        ass_header = None

    if not entries:
        raise ValueError(f"No valid subtitle entries found in {sub_path}")

    batches = _build_batches(entries, batch_size)
    all_texts = [entry['text'] for entry in entries]
    lang_tag = "ai"
    output_ext = '.ass' if is_ass else '.srt'
    output_path = sub_path.with_suffix(f'.{lang_tag}{output_ext}')

    progress = ProgressTracker(output_path)
    completed_batches: set[int] = set()
    translations_by_batch: dict[int, list[str]] = {}

    saved = progress.load()
    if saved is not None:
        completed_batches, saved_by_batch = saved
        translations_by_batch = saved_by_batch
        logger.info("Resuming translation: %d batches already done", len(completed_batches))

    _structured_fallback = False
    _fallback_lock = threading.Lock()
    _translation_cache: dict[str, str] = {}
    _cache_lock = threading.Lock()

    for bi, trans_list in translations_by_batch.items():
        batch = batches[bi]
        for entry, trans in zip(batch, trans_list):
            if entry.get("dup_of") is None and trans:
                _translation_cache[entry["text"]] = trans

    pending = [i for i in range(len(batches)) if i not in completed_batches]

    def _translate_batch(batch_idx: int) -> tuple[int, list[str]]:
        nonlocal _structured_fallback
        batch = batches[batch_idx]
        batch_start = batch_idx * batch_size

        unique_indices = []
        unique_texts = []
        for i, entry in enumerate(batch):
            if entry.get("dup_of") is not None:
                continue
            with _cache_lock:
                if entry["text"] in _translation_cache:
                    continue
            unique_indices.append(i)
            unique_texts.append(entry["text"])

        results = [''] * len(batch)

        for i, entry in enumerate(batch):
            if entry.get("dup_of") is not None:
                continue
            with _cache_lock:
                if entry["text"] in _translation_cache:
                    results[i] = _translation_cache[entry["text"]]

        if unique_texts:
            batch_text = json.dumps(unique_texts, ensure_ascii=False)

            ctx_before_start = max(0, batch_start - context_lines)
            context_before = all_texts[ctx_before_start:batch_start] if batch_start > 0 else []
            after_end = batch_start + len(batch) + context_lines
            context_after = all_texts[batch_start + len(batch):after_end] if batch_start + len(batch) < len(entries) else []

            with _fallback_lock:
                effective_mode = output_mode
                if output_mode == "auto" and _structured_fallback:
                    effective_mode = "text"

            response = _call_ai(
                batch_text, source_lang, target_lang, bilingual, len(unique_texts),
                api_url, api_key, model, temperature, max_output_tokens,
                system_prompt, bilingual_prompt,
                max_retries, retry_delay,
                media_context=media_context, glossary=glossary,
                context_before=context_before or None,
                context_after=context_after or None,
                output_mode=effective_mode, streaming=streaming,
            )

            translations, json_ok = _parse_translations(response, len(unique_texts), effective_mode)

            # Detect batch-level repetition: if many translations duplicate
            # already-cached values (from different source texts), the AI is stuck
            non_empty = [t for t in translations if t]
            if non_empty and _translation_cache:
                dup_count = sum(1 for t in non_empty if t in _translation_cache.values())
                if dup_count > len(non_empty) * 0.4:
                    logger.warning("Batch %d: %d/%d translations repeat earlier batches — AI stuck, retrying",
                                   batch_idx, dup_count, len(non_empty))
                    # Retry once without context to break the repetition loop
                    response = _call_ai(
                        batch_text, source_lang, target_lang, bilingual, len(unique_texts),
                        api_url, api_key, model, temperature, max_output_tokens,
                        system_prompt, bilingual_prompt,
                        max_retries, retry_delay,
                        media_context=media_context, glossary=glossary,
                        context_before=None, context_after=None,
                        output_mode=effective_mode, streaming=streaming,
                    )
                    translations, json_ok = _parse_translations(response, len(unique_texts), effective_mode)

            if effective_mode in ("structured", "auto") and not json_ok:
                with _fallback_lock:
                    if output_mode == "auto" and not _structured_fallback:
                        _structured_fallback = True
                response = _call_ai(
                    batch_text, source_lang, target_lang, bilingual, len(unique_texts),
                    api_url, api_key, model, temperature, max_output_tokens,
                    system_prompt, bilingual_prompt,
                    max_retries, retry_delay,
                    media_context=media_context, glossary=glossary,
                    context_before=context_before or None,
                    context_after=context_after or None,
                    output_mode="text", streaming=streaming,
                )
                translations, _ = _parse_translations(response, len(unique_texts), "text")

            _validate_tags(unique_texts, translations, logger)

            actual_count = len([t for t in translations if t])
            if actual_count < len(unique_texts):
                logger.warning("Batch %d: AI returned %d/%d non-empty translations",
                               batch_idx, actual_count, len(unique_texts))

            with _cache_lock:
                for idx, text, trans in zip(unique_indices, unique_texts, translations):
                    results[idx] = trans
                    if trans:
                        _translation_cache[text] = trans

        for i, entry in enumerate(batch):
            if entry.get("dup_of") is not None:
                source_idx = entry["dup_of"]
                if source_idx < len(results):
                    results[i] = results[source_idx]
                else:
                    with _cache_lock:
                        results[i] = _translation_cache.get(entry["text"], entry["text"])

        return (batch_idx, results)

    try:
        if concurrency <= 1 or len(pending) <= 1:
            for batch_idx in pending:
                _, translations = _translate_batch(batch_idx)
                translations_by_batch[batch_idx] = translations
                completed_batches.add(batch_idx)
                if len(batches) > 1:
                    progress.save(sorted(completed_batches), translations_by_batch)
        else:
            effective_concurrency = min(concurrency, len(pending))
            with ThreadPoolExecutor(max_workers=effective_concurrency) as executor:
                futures = {}
                pending_iter = iter(pending)
                for _ in range(effective_concurrency):
                    batch_idx = next(pending_iter, None)
                    if batch_idx is not None:
                        futures[executor.submit(_translate_batch, batch_idx)] = batch_idx

                while futures:
                    done = next(as_completed(futures))
                    del futures[done]
                    batch_idx, translations = done.result()
                    translations_by_batch[batch_idx] = translations
                    completed_batches.add(batch_idx)
                    if len(batches) > 1:
                        progress.save(sorted(completed_batches), translations_by_batch)

                    batch_idx = next(pending_iter, None)
                    if batch_idx is not None:
                        futures[executor.submit(_translate_batch, batch_idx)] = batch_idx

        accumulated_translations = []
        for i in range(len(batches)):
            accumulated_translations.extend(translations_by_batch[i])

        # AI Review: post-translation quality review
        if ai_review_enabled and any(t for t in accumulated_translations):
            from translate.review import review_translations
            review_model = ai_review_model or model
            # Set _translation on all entries for review.py dedup and tag validation
            for entry, translation in zip(entries, accumulated_translations):
                entry["_translation"] = translation
            logger.info("Starting AI review of %d translations with model %s...", len(accumulated_translations), review_model)
            try:
                reviewed = review_translations(
                    api_url=api_url,
                    api_key=api_key,
                    model=review_model,
                    entries=entries,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    timeout=ai_review_timeout,
                    media_context=media_context,
                    glossary=glossary or "",
                    custom_prompt=ai_review_prompt,
                )
                accumulated_translations = reviewed
                logger.info("AI review completed successfully.")
            except Exception as e:
                logger.error("AI review failed, using original translations: %s", e)

        if is_ass and ass_header is not None:
            for entry, translation in zip(entries, accumulated_translations):
                entry["_translation"] = translation
            output_content = _format_ass(entries, ass_header, bilingual)
            output_path.write_text(output_content, encoding='utf-8')
        else:
            output_entries = []
            for entry, translation in zip(entries, accumulated_translations):
                if bilingual:
                    new_text = f"{entry['text']}\n\n{translation}"
                else:
                    new_text = translation if translation else entry['text']
                output_entries.append({
                    "index": entry["index"],
                    "timestamp": entry["timestamp"],
                    "text": new_text,
                })
            output_path.write_text(_format_srt(output_entries), encoding='utf-8')

        progress.clear()
        return output_path

    except Exception:
        if output_path and output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        raise


def find_subtitle_for_video(video_path, prefer_lang="en") -> Optional[Path]:
    video_path = Path(video_path)
    video_stem = video_path.stem
    video_dir = video_path.parent

    preferred = []
    fallback = []

    for ext in ('.srt', '.ass', '.ssa', '.sub'):
        for sub_file in video_dir.glob(f"{video_stem}*{ext}"):
            if sub_file == video_path:
                continue
            lang_tag = _extract_lang_tag(sub_file.name, video_stem)
            if prefer_lang and lang_tag == prefer_lang:
                preferred.append(sub_file)
            else:
                fallback.append(sub_file)

    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]

    for sub_file in video_dir.glob("*.srt"):
        if sub_file == video_path:
            continue
        try:
            head = sub_file.read_text(encoding='utf-8-sig', errors='ignore')[:2000]
            if prefer_lang == "en" and _detect_lang_from_text(head) in ("en", "zh+en"):
                return sub_file
            if prefer_lang == "zh" and _detect_lang_from_text(head) in ("zh", "zh+en"):
                return sub_file
        except OSError:
            continue

    return None


def list_subtitles_for_video(video_path) -> list[dict]:
    video_path = Path(video_path)
    video_stem = video_path.stem
    video_dir = video_path.parent
    results = []

    for ext in ('.srt', '.ass', '.ssa', '.sub'):
        for sub_file in video_dir.glob(f"{video_stem}*{ext}"):
            if sub_file == video_path:
                continue
            results.append({
                "path": str(sub_file),
                "filename": sub_file.name,
            })

    return results


def _extract_lang_tag(filename: str, video_stem: str) -> str:
    base = filename
    for ext in ('.srt', '.ass', '.ssa', '.sub'):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    suffix = base[len(video_stem):]
    if suffix.startswith('.'):
        suffix = suffix[1:]
    if not suffix:
        return ""
    if re.match(r'^[a-z]{2,3}(\+[a-z]{2,3})?$', suffix, re.IGNORECASE):
        return suffix.lower()
    return ""


def _detect_lang_from_text(text: str) -> str:
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    if cjk > 0 and latin > 0:
        return "zh+en"
    if cjk > 0:
        return "zh"
    if latin > 0:
        return "en"
    return "unknown"
