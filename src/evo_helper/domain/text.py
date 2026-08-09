"""OCR 文本吸附用的编辑距离。

同一个函数原先在 `vision.parsers` 和 `game.pirate_ui` 各有一份私有实现。
两处的判据是同构的——「OCR 只会读错个别字，不会读成另一个词」——
所以判据本身也该只有一份：改了容差却只改一处，就会出现「舰种名吸得上、
任务类型吸不上」这种一半对的行为。
"""

from __future__ import annotations


def edit_distance(left: str, right: str) -> int:
    """Levenshtein 距离。短串在外层循环没有性能意义，这里按可读性写。"""
    previous = list(range(len(right) + 1))
    for i, lchar in enumerate(left, start=1):
        current = [i]
        for j, rchar in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (lchar != rchar),
                )
            )
        previous = current
    return previous[-1]


def snap_to_vocabulary(raw: str, vocabulary: tuple[str, ...], *, max_distance: int) -> str | None:
    """把 OCR 文本贴回封闭词表；贴不上或**有歧义**就返回 None。

    要求唯一命中：两个候选并列时宁可判不出来也不猜。吸附的意义在于
    「读错一个字仍认得出」，而不是「随便挑一个最像的」——后者会把一个
    真正没见过的词改写成已知词，而且不留痕迹。
    """
    text = raw.strip()
    if not text:
        return None
    hits = [word for word in vocabulary if edit_distance(text, word) <= max_distance]
    return hits[0] if len(hits) == 1 else None
