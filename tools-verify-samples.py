# -*- coding: utf-8 -*-
"""对着用户给的 5 张回放截图核对识别结果。

真值从截图里人工读出，重点核四个核心舰种：深空吞噬者 / 噬能截击者 / 钛能守卫者 / 收割者。
"""
import statistics
import sys

sys.path.insert(0, "src")
from PIL import Image
import pytesseract

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
from evo_helper.vision.optional.report_screens import locate_sections, number_column
from evo_helper.vision.report_layout import layout_for_viewport

L = layout_for_viewport(1920, 879)
CORE = ("深空吞噬者", "噬能截击者", "钛能守卫者", "收割者")

TRUTH = {
    "replay-1.png": ("bot_2_121_7", [
        ("轻型战斗机","5.36K"),("重型战斗机","1.59K"),("巡洋舰","517"),("战列舰","784"),
        ("无畏舰","1.09K"),("轰炸机","884"),("毁灭者","335"),("裂变者","12"),
        ("深空吞噬者","24"),("噬能截击者","11"),("钛能守卫者","13"),("收割者","2"),
        ("离子炮","281"),("火箭发射器","249"),("轻型激光炮","206"),("MK2 加农炮","276"),("等离子炮","56")]),
    "replay-2.png": ("bot_2_132_7", [
        ("轻型战斗机","2.92K"),("重型战斗机","2.69K"),("巡洋舰","1.03K"),("战列舰","789"),
        ("无畏舰","769"),("轰炸机","504"),("毁灭者","677"),("裂变者","25"),
        ("深空吞噬者","14"),("噬能截击者","3"),("钛能守卫者","4"),("收割者","1"),
        ("离子炮","168"),("火箭发射器","209"),("轻型激光炮","226"),("MK2 加农炮","132"),("等离子炮","82")]),
    "replay-3.png": ("bot_2_134_16", [
        ("轻型战斗机","1.11K"),("重型战斗机","666"),("巡洋舰","441"),("战列舰","259"),
        ("无畏舰","233"),("轰炸机","111"),("毁灭者","103"),("裂变者","11"),
        ("深空吞噬者","8"),("噬能截击者","6"),("钛能守卫者","4"),
        ("火箭发射器","85"),("轻型激光炮","105")]),
    "replay-4.png": ("bot_2_127_15", [
        ("轻型战斗机","1.97K"),("重型战斗机","1.73K"),("巡洋舰","260"),("战列舰","170"),
        ("无畏舰","587"),("轰炸机","278"),("毁灭者","547"),("裂变者","3"),
        ("深空吞噬者","4"),("噬能截击者","7"),("钛能守卫者","5"),("收割者","1"),
        ("离子炮","120"),("火箭发射器","52"),("轻型激光炮","106"),("MK2 加农炮","34")]),
    "replay-5.png": ("bot_2_146_11", [
        ("轻型战斗机","5.73K"),("重型战斗机","3.58K"),("巡洋舰","566"),("战列舰","670"),
        ("无畏舰","454"),("轰炸机","577"),("毁灭者","801"),("裂变者","28"),
        ("深空吞噬者","7"),("噬能截击者","12"),("钛能守卫者","11"),("收割者","2"),
        ("离子炮","139"),("火箭发射器","156"),("轻型激光炮","210"),("MK2 加农炮","173"),("等离子炮","74")]),
}

WHITELIST = "0123456789.K"


def rows_of(im, top, bot):
    """从名称列拿每行的顶端。"""
    up = 3
    nc = im.crop((975, top, 1075, bot)).convert("L")
    g = nc.resize((nc.width * up, nc.height * up), Image.LANCZOS)
    d = pytesseract.image_to_data(g, lang="chi_sim", config="--psm 6",
                                  output_type=pytesseract.Output.DICT)
    tops = {}
    for i, w in enumerate(d["text"]):
        if not w.strip():
            continue
        k = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        y = top + d["top"][i] // up
        tops[k] = min(tops.get(k, y), y)
    return sorted(tops.values())


def read_cell(im, y, pitch, col):
    """一格数字，多套配方投票。K 后缀一并收。"""
    base = im.crop((col[0], y - 3, col[1], y + pitch - 3)).convert("L")
    votes = {}
    for psm in (7, 6, 10):
        for thr in (None, 140, 170):
            for up in (4, 6):
                for f in (Image.NEAREST, Image.LANCZOS):
                    g = base if thr is None else base.point(lambda v, t=thr: 255 if v > t else 0)
                    g = g.resize((g.width * up, g.height * up), f)
                    s = pytesseract.image_to_string(
                        g, lang="eng", config=f"--psm {psm} -c tessedit_char_whitelist={WHITELIST}"
                    ).strip()
                    if s:
                        votes[s] = votes.get(s, 0) + 1
    return votes


def pick(votes):
    """选票。**后缀让位于更长的候选**——失败模式恒定是丢首位，从不凭空多字。"""
    if not votes:
        return ""
    folded = {}
    for text, count in votes.items():
        longer = [o for o in votes if o != text and o.endswith(text)]
        target = max(longer, key=len) if longer else text
        folded[target] = folded.get(target, 0) + count
    return max(sorted(folded), key=lambda v: folded[v])


core_ok = core_all = row_ok = row_all = 0
for name, (bot, truth) in TRUTH.items():
    im = Image.open(f"var/logs/samples/{name}")
    secs = locate_sections(im, L)
    if not secs:
        print(f"{name}: 定位不到分节"); continue
    top, bot_y = secs[0]
    ys = rows_of(im, top, bot_y)
    pitch = int(statistics.median(b - a for a, b in zip(ys, ys[1:]))) if len(ys) > 1 else 22
    # 行距很规整；逐行检测会漏行也会多出碎片（实测 17 行检出 18 行，
    # 钛能守卫者 被一个 sk 碎片顶替，之后整体错位）。改用等距网格。
    ys = [ys[0] + i * pitch for i in range(len(truth))]
    col = number_column(im, L.defender_column, top, bot_y)
    print(f"\n{name}  {bot}  数字列{col}  检出{len(ys)}行 / 真值{len(truth)}行")
    for i, (ship, want) in enumerate(truth):
        if i >= len(ys):
            print(f"   -- {ship:>6} 真{want:>6}  (无对应行)")
            row_all += 1
            if ship in CORE: core_all += 1
            continue
        votes = read_cell(im, ys[i], pitch, col)
        got = pick(votes)
        ok = got == want
        row_all += 1; row_ok += ok
        mark = ""
        if ship in CORE:
            core_all += 1; core_ok += ok
            mark = "  <核心>"
        print(f"   {'OK' if ok else 'XX'} {ship:>6} 真{want:>6} 读{got:>6}{mark}")

print(f"\n全部行 {row_ok}/{row_all} = {row_ok/row_all:.0%}")
print(f"核心舰 {core_ok}/{core_all} = {core_ok/core_all:.0%}")
