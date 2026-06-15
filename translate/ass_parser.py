import re


def _parse_ass(content: str) -> tuple[list[dict], str]:
    header_lines = []
    entries = []
    seen_keys = {}
    in_events = False
    for line in content.split('\n'):
        if not in_events and not line.startswith('['):
            header_lines.append(line)
            continue
        if line.strip() == '[Events]':
            in_events = True
            header_lines.append(line)
            continue
        if in_events and line.startswith('Format:'):
            header_lines.append(line)
            continue
        if not in_events:
            header_lines.append(line)
            continue
        if line.startswith('Dialogue:'):
            parts = line[9:].split(',', 9)
            if len(parts) < 10:
                continue
            start, end, style = parts[1], parts[2], parts[3]
            text = parts[9].strip()
            text = re.sub(r'\{[^}]*\}', '', text)
            cache_key = f"{start}|{end}|{text}"
            dup_index = seen_keys.get(cache_key)
            if dup_index is not None:
                entries.append({
                    "index": len(entries),
                    "timestamp": f"{start} --> {end}",
                    "text": text,
                    "is_ass": True,
                    "raw_line": line,
                    "dup_of": dup_index,
                })
            else:
                seen_keys[cache_key] = len(entries)
                entries.append({
                    "index": len(entries),
                    "timestamp": f"{start} --> {end}",
                    "text": text,
                    "is_ass": True,
                    "raw_line": line,
                    "dup_of": None,
                })
    return (entries, '\n'.join(header_lines) + '\n')


def _format_ass(entries: list[dict], header: str, bilingual: bool) -> str:
    lines = [header]
    for entry in entries:
        raw = entry.get("raw_line", "")
        if not raw:
            continue
        if entry.get("dup_of") is not None:
            source_entry = entries[entry["dup_of"]]
            translation = source_entry.get("_translation", "")
        else:
            translation = entry.get("_translation", "")
        parts = raw[9:].split(',', 9)
        if len(parts) < 10:
            continue
        if bilingual:
            new_text = f"{parts[9].strip()}\\N{translation}"
        else:
            new_text = translation if translation else parts[9].strip()
        parts[9] = new_text
        lines.append("Dialogue: " + ','.join(parts))
    return '\n'.join(lines)
