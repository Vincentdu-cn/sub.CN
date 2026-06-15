def check_repetition(chunks: list[str], threshold: int = 3) -> bool:
    text = "".join(chunks)
    if len(text) < 10:
        return False
    for seq_len in range(2, min(20, len(text) // threshold + 1)):
        seq = text[-seq_len:]
        count = 0
        pos = len(text)
        while pos >= seq_len:
            if text[pos - seq_len:pos] == seq:
                count += 1
                pos -= seq_len
            else:
                break
        if count >= threshold:
            return True
    return False


def effective_repetition_threshold(input_text: str, base: int = 3) -> int:
    if not input_text or len(input_text) < 20:
        return base
    for seq_len in range(2, min(10, len(input_text) // 3 + 1)):
        seen = {}
        for i in range(len(input_text) - seq_len + 1):
            gram = input_text[i:i + seq_len]
            seen[gram] = seen.get(gram, 0) + 1
            if seen[gram] >= 3:
                return base * 3
    return base
