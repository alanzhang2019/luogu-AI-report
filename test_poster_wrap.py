"""Test the AI 定级 text sizing/breaking logic."""
def calc(ai_level):
    if ai_level:
        level_text = ai_level
        char_count = len(level_text)
        if char_count <= 12:
            level_fontsize = 28
        elif char_count <= 16:
            level_fontsize = 24
        elif char_count <= 20:
            level_fontsize = 21
        elif char_count <= 24:
            level_fontsize = 18
        elif char_count <= 30:
            level_fontsize = 16
        else:
            level_fontsize = 14
        if char_count > 16:
            break_chars = ["，", "、", "；", "：", " ", "（", ")", "(", ")", "·", "→", ">", "/"]
            target = char_count // 2
            best_split = -1
            for off in range(0, 6):
                for cand in (target + off, target - off):
                    if 4 <= cand < char_count and level_text[cand] in break_chars:
                        best_split = cand + 1
                        break
                if best_split != -1:
                    break
            if best_split == -1:
                best_split = char_count // 2
            level_text = level_text[:best_split].rstrip() + "\n" + level_text[best_split:].lstrip()
    else:
        level_text = "尚未生成报告"
        level_fontsize = 28
    return level_text, level_fontsize


# Test cases
cases = [
    "入门",                                  # 2 chars
    "CSP-S 入门",                            # 9 chars
    "CSP‑J 熟练 → CSP‑S 入门",                 # ~16 chars
    "CSP-S 入门者，尚未达到 CSP-S 合格水平",   # 22 chars
    "CSP-S 入门者，尚未达到 CSP-S 合格水平，可以从基础开始",  # 30+ chars
    "尚未生成报告",                          # None case
    "这个定级非常非常非常长非常非常非常长非常长",  # 22 chars no break chars
]
for c in cases:
    text, fs = calc(c) if c != "尚未生成报告" else (calc(None))
    if "\n" in text:
        for line in text.split("\n"):
            print(f"  [{fs}pt] '{line}' ({len(line)} chars)")
    else:
        print(f"  [{fs}pt] '{text}' ({len(text)} chars)")
    print()
