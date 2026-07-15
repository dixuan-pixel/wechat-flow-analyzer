#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信流水风险分析器 (WeChat Flow Risk Analyzer)
==============================================

面向个人 / 合规 / 风控场景的微信支付流水风险扫描工具。

核心能力
--------
1. 固定金额高频分析      —— 同一金额反复出现（疑似会费 / 固定还款 / 套路贷）
2. 周期规律分析          —— 日 / 周（星期几）/ 月（几号）/ 固定间隔的规律性
3. 深夜交易分析          —— 00:00 以后（可配置）的异常时点往来
4. 万整大额入账分析      —— ≥1万且百/十/个位均为 0 的入账（如 10000/25000/50000）
5. 民间借贷关键词扫描    —— 借 / 贷 / 周转 / 押 / 典 / 息 / 小贷 / 典当 等强信号
6. 借贷相关关键词关注    —— 还款 / 银行 / 小贷 / 信息咨询 等定向关注词
7. 对手方风险画像        —— 双向往来（对敲 / 走账）
8. 大额交易              —— 单笔大额支出（可配 --large 阈值）
9. 快进快出（分层）      —— 转入后短时间内相似金额转出（可用 --skip-layering 关闭）
10. 综合风险评分          —— 加权汇总为 低 / 中 / 高 风险等级

两个“大条件”开关（研究前先过滤）
-------------------------------
--global-min 300   全局过滤低于该金额的所有交易（默认 0，本次研究设为 300）
--skip-layering    彻底跳过“快进快出”维度的研究
--ignore           全局忽略的交易类型（默认 充值,提现,信用卡还款），
                   研究前直接剔除，不进入任何维度统计（避免充值/提现/信用卡还款
                   淹没真实风险信号；可按需增减，逗号分隔）

用法
----
    # 分析单个文件
    python wechat_flow_risk_analyzer.py 账单.csv

    # 分析整个目录下的所有流水文件
    python wechat_flow_risk_analyzer.py ./我的流水/

    # 本次研究的口径：过滤<300、不研究快进快出、忽略充值/提现/信用卡还款
    python wechat_flow_risk_analyzer.py 账单.pdf --global-min 300 --skip-layering

    # 自定义阈值
    python wechat_flow_risk_analyzer.py 账单.csv --large 50000 --late-start 0 --min-freq 3

输出
----
    output/risk_report.md      人类可读风险报告（含数据表格）
    output/risk_findings.csv   机器可读的命中明细（可导入 Excel 复查）
"""

import argparse
import csv
import glob
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

# ---------------------------------------------------------------------------
# 列名模糊匹配（兼容微信支付 CSV 与各类 Excel 导出）
# ---------------------------------------------------------------------------
COLUMN_ALIASES = {
    "time":   ["交易时间", "时间", "日期", "记账时间", "交易日期", "发生时间", "下单时间"],
    "counterparty": ["交易对方", "对方", "交易对象", "收款方", "付款方", "商户", "对方昵称", "交易商户"],
    "direction": ["收/支", "收付款", "收支类型", "方向", "收支", "交易方向"],
    "amount": ["金额(元)", "金额", "交易金额", "数额", "交易数额", "人民币", "流水金额"],
    "type":   ["交易类型", "类型", "业务类型", "消费类型"],
    "remark": ["备注", "商品", "说明", "交易说明", "摘要", "交易备注", "用途"],
    "status": ["当前状态", "状态", "交易状态"],
}


def _find_col(columns, aliases):
    for cand in aliases:
        for col in columns:
            if cand in str(col):
                return col
    return None


def _is_header_line(line):
    """判断一行是否为数据表头：认准微信标准列名，避免误命中‘起始时间’等说明行。"""
    s = str(line)
    if "交易时间" in s or "金额(元)" in s:
        return True
    # 兜底：同时含‘时间’与‘金额/对方/收’的说明行才算表头
    return ("时间" in s) and (("金额" in s) or ("对方" in s) or ("收" in s))


def _parse_amount(x):
    """把 '¥1,234.50' / '1234.50' / '收入' 等解析为浮点金额（恒为正）。"""
    if pd.isna(x):
        return 0.0
    s = str(x).replace(",", "").replace("¥", "").replace("￥", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    return float(m.group())


def _direction_sign(x):
    """返回 +1 收入 / -1 支出 / 0 未知。"""
    s = str(x)
    if any(k in s for k in ["收", "转入", "退款", "充值", "领取", "入", "退"]):
        return 1
    if any(k in s for k in ["支", "转出", "出", "付", "消费", "还款", "花"]):
        return -1
    return 0


def _parse_time(x):
    if pd.isna(x):
        return pd.NaT
    s = str(x).strip()
    # 统一分隔符：2025/04/13、2025.04.13 -> 2025-04-13；并清洗「年/月/日」
    s = (s.replace("年", "-").replace("月", "-").replace("日", " ")
            .replace("/", "-").replace(".", "-"))
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y-%m-%d %H:%M:%S.%f", "%m-%d %H:%M", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 兜底：用 pandas 宽松解析（兼容更多区域格式）
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        return pd.NaT


# ---------------------------------------------------------------------------
# 载入与归一化
# ---------------------------------------------------------------------------
def load_bill(path):
    """读取 CSV / Excel，返回归一化后的 DataFrame。"""
    ext = os.path.splitext(path)[1].lower()
    records = []
    cols = []

    if ext in (".xlsx", ".xls"):
        raw = pd.read_excel(path, dtype=str, header=None, keep_default_na=False)
        header_idx = None
        for i, row in raw.iterrows():
            joined = " ".join(str(v) for v in row.tolist())
            if _is_header_line(joined):
                header_idx = i
                break
        if header_idx is None:
            header_idx = 0
        cols = [str(c).strip() for c in raw.iloc[header_idx].tolist()]
        for _, row in raw.iloc[header_idx + 1:].iterrows():
            vals = [str(v).strip() for v in row.tolist()]
            if not any(vals) or all("---" in v for v in vals):
                continue
            records.append(dict(zip(cols, vals)))
    else:
        # 微信 CSV 含表头脏行，逐行用 csv 解析更稳健
        with open(path, encoding="utf-8-sig", newline="") as fh:
            lines = fh.read().splitlines()
        header_idx = None
        for i, line in enumerate(lines):
            if _is_header_line(line):
                header_idx = i
                break
        if header_idx is None:
            header_idx = 0
        reader = csv.reader(lines[header_idx:])
        cols = [c.strip() for c in next(reader)]
        for row in reader:
            if not row or all("---" in c for c in row):
                continue
            vals = [c.strip() for c in row]
            if not any(vals):
                continue
            records.append(dict(zip(cols, vals)))

    if not records:
        raise ValueError("未解析到任何交易行，请检查文件格式。")

    mapped = {}
    for key, aliases in COLUMN_ALIASES.items():
        mapped[key] = _find_col(cols, aliases)

    if not mapped["time"] or not mapped["amount"]:
        raise ValueError(f"无法识别关键列（时间/金额）。文件列名：{cols}")

    df = pd.DataFrame()
    df["time"] = pd.Series([r.get(mapped["time"], "") for r in records]).map(_parse_time)
    df["counterparty"] = pd.Series([r.get(mapped["counterparty"], "") for r in records]).map(
        lambda v: str(v).strip())
    if mapped["direction"]:
        df["raw_dir"] = pd.Series([r.get(mapped["direction"], "") for r in records])
        df["dir"] = df["raw_dir"].map(_direction_sign)
    else:
        df["raw_dir"] = ""
        df["dir"] = 0
    df["amount"] = pd.Series([r.get(mapped["amount"], "") for r in records]).map(_parse_amount)
    df["type"] = pd.Series([r.get(mapped["type"], "") for r in records]).map(
        lambda v: str(v).strip())
    df["remark"] = pd.Series([r.get(mapped["remark"], "") for r in records]).map(
        lambda v: str(v).strip())
    df["status"] = pd.Series([r.get(mapped["status"], "") for r in records]).map(
        lambda v: str(v).strip())

    # 若方向未知，尝试从类型 / 金额符号推断
    dir_series = df["dir"].copy()
    mask0 = dir_series == 0
    if mask0.any():
        dir_series.loc[mask0] = df.loc[mask0, "type"].map(_direction_sign)
    mask0 = dir_series == 0
    if mask0.any():
        dir_series.loc[mask0] = df.loc[mask0, "amount"].map(
            lambda a: -1 if str(a).startswith("-") else 1)
    df["dir"] = dir_series.astype(int)
    df["amount"] = df["amount"].abs()

    df = df.dropna(subset=["time"])
    df = df[df["amount"] > 0]
    df = df.sort_values("time").reset_index(drop=True)
    # 完整对手方文本（用于关键词扫描）：对方 + 商品/备注
    df["cp_text"] = (df["counterparty"].fillna("") + " " +
                     df["remark"].fillna("") + " " + df["type"].fillna(""))
    return df


def _parse_pdf_row(cells):
    """从 PDF 表格的一行中抽取一条交易；非数据行返回 None。

    注意：微信证明 PDF 的标准列为
        单号 / 时间 / 类型 / 收·支 / 交易方式(银行卡) / 金额 / 交易对方 / 商户单号
    但 pdfplumber 在不同页可能把“交易方式”与“交易对方”的列序打乱，
    因此不能写死列索引。这里改为**按单元格内容**识别各字段，稳健抗错位。
    """
    rec = _remap_pdf_cells(cells)
    if not rec["time"] or not rec["amount"]:
        return None
    return rec


# 微信交易类型白名单（用于把该单元格识别为“交易类型”而非对手方）
_KNOWN_TYPES = {
    "转账", "转账-退款", "退款", "二维码收款", "二维码付款", "扫二维码付款",
    "商户消费", "群收款", "微信红包", "红包", "零钱充值", "零钱提现", "提现",
    "充值", "理财", "信用卡还款", "冻结", "解冻", "面对面收款", "面对面付款",
    "转入零钱", "零钱转账", "分付", "微粒贷", "信贷还款", "经营收款", "经营付款",
}


def _remap_pdf_cells(cells):
    cells = [("" if c is None else str(c)) for c in cells]

    def norm(c):
        return c.replace("\n", "").replace(" ", "").strip()

    def clean(c):
        if not c:
            return ""
        s = c.replace("\n", " ").replace("/", " ")
        s = re.sub(r"\s+", " ", s).strip()
        return s

    time_c = dir_c = amt_c = type_c = method_c = cp_c = None
    remain = []
    for c in cells:
        cs = norm(c)
        if time_c is None and re.search(r"\d{4}-\d{2}-\d{2}", c):
            time_c = c
        elif dir_c is None and cs in ("收入", "支出", "其他"):
            dir_c = c
        elif amt_c is None and re.fullmatch(r"\d[\d,]*\.\d{2}", cs):
            amt_c = c
        elif len(cs) >= 14 and re.fullmatch(r"\d+", cs):
            pass  # 交易单号 / 商户单号，跳过
        else:
            remain.append(c)
    # 第二遍：在剩余单元格中识别 交易类型 / 交易方式 / 交易对方
    for c in remain:
        cs = norm(c)
        if type_c is None and cs in _KNOWN_TYPES:
            type_c = c
        elif method_c is None and (re.search(r"储蓄卡|信用卡|银行卡|零钱通", cs)
                                   or cs in ("零钱", "零钱通", "微信零钱")):
            method_c = c
        elif cp_c is None:
            cp_c = c
        else:
            cp_c = cp_c + " " + c  # 罕见的多候选，拼接
    if cp_c is None:
        for c in remain:
            if c not in (type_c, method_c):
                cp_c = c
                break
    return {"time": clean(time_c), "counterparty": clean(cp_c),
            "raw_dir": clean(dir_c), "amount": clean(amt_c),
            "type": clean(type_c), "method": clean(method_c),
            "remark": "", "status": ""}


def load_pdf_bill(path):
    """解析微信支付交易明细证明 PDF（官方证书格式）。

    优先使用 pdfplumber（表格探测最稳，微信证明这种「单号+日期挤列、日期/时间跨两行」
    的紧凑中文表格，只有 pdfplumber 能正确还原 8 列）；
    若 pdfplumber 不可用（如浏览器 Pyodide 未安装），或 pdfplumber 抽到 0 行
    （个别 PDF 表格探测失败），回退到纯 Python 的 pdfminer.six 坐标重建。
    两条路径产出结构完全一致的 DataFrame。
    """
    try:
        import pdfplumber  # noqa: F401
        df = _load_pdf_pdfplumber(path)
        if len(df):
            return df
        # 抽到 0 行，交给 pdfminer 兜底
    except Exception:
        pass
    try:
        import pdfminer  # noqa: F401
        return _load_pdf_pdfminer(path)
    except ImportError:
        raise ImportError(
            "解析 PDF 需要 pdfplumber 或 pdfminer.six。"
            "本地环境请 pip install pdfplumber；浏览器端请确保 pdfplumber / pdfminer.six 已加载。")


def _load_pdf_pdfplumber(path):
    """pdfplumber 路径（表格探测最稳，本地与浏览器 Pyodide 均可用）。"""
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("解析 PDF 需要 pdfplumber，请先 pip install pdfplumber")
    records = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for tbl in page.extract_tables():
                for row in tbl:
                    if not row:
                        continue
                    cells = [("" if c is None else str(c)) for c in row]
                    rec = _parse_pdf_row(cells)
                    if rec:
                        records.append(rec)
    if not records:
        raise ValueError("PDF 未解析到交易行，可能是扫描件或格式异常。")
    return _pdf_records_to_df(records)


def _iter_pdf_chars(el):
    """递归收集 pdfminer 布局树里的字符叶子（LTChar）。"""
    from pdfminer.layout import LTChar
    if isinstance(el, LTChar):
        yield el
    else:
        objs = getattr(el, "_objs", None)
        if objs is not None:
            for c in objs:
                yield from _iter_pdf_chars(c)


def _pdf_cells_per_page(page):
    """按字符坐标重建每一行的单元格（列），供 _remap_pdf_cells 复用。

    处理要点：按 y 坐标归组为行；行内按 x 坐标排序，水平间隔超过阈值即视为新列；
    把被拆成「日期」「时间」两格的情况合并回一个时间戳。
    """
    chars = list(_iter_pdf_chars(page))
    lines = {}
    for ch in chars:
        lines.setdefault(round(ch.y0), []).append(ch)
    out = []
    for key in sorted(lines, reverse=True):  # PDF y 轴向上，逆序即自上而下
        lc = sorted(lines[key], key=lambda c: c.x0)
        if not lc:
            continue
        widths = sorted((c.x1 - c.x0) for c in lc if c.x1 > c.x0)
        thr = (widths[len(widths) // 2] if widths else 5) * 1.8 + 1
        words, cur = [], [lc[0]]
        for c in lc[1:]:
            if c.x0 - cur[-1].x1 > thr:
                words.append(cur)
                cur = [c]
            else:
                cur.append(c)
        words.append(cur)
        cells = []
        for w in words:
            txt = "".join(ch.get_text() for ch in w).strip()
            if txt:
                cells.append(txt)
        # 合并「日期」与「时间」被拆成相邻两格的情况
        merged, i = [], 0
        while i < len(cells):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[i]) and i + 1 < len(cells) \
                    and re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", cells[i + 1]):
                merged.append(cells[i] + " " + cells[i + 1])
                i += 2
            else:
                merged.append(cells[i])
                i += 1
        out.append(merged)
    return out


def _load_pdf_pdfminer(path):
    """pdfminer.six 路径（纯 Python 兜底，无原生轮子依赖；仅在 pdfplumber 抽到 0 行时用）。"""
    try:
        from pdfminer.high_level import extract_pages
    except ImportError:
        raise ImportError("解析 PDF 需要 pdfminer.six，请先 pip install pdfminer.six")
    records = []
    for page in extract_pages(path):
        for cells in _pdf_cells_per_page(page):
            rec = _remap_pdf_cells(cells)
            if rec["time"] and rec["amount"]:
                records.append(rec)
    if not records:
        raise ValueError("PDF 未解析到交易行，可能是扫描件或格式异常。")
    return _pdf_records_to_df(records)


def _pdf_records_to_df(records):
    """把 _remap_pdf_cells 产出的记录列表统一成 DataFrame（两路径共用）。"""
    df = pd.DataFrame()
    df["time"] = pd.Series([r["time"] for r in records]).map(_parse_time)
    df["counterparty"] = pd.Series([r["counterparty"] for r in records])
    df["raw_dir"] = pd.Series([r["raw_dir"] for r in records])
    df["dir"] = df["raw_dir"].map(_direction_sign)
    df["amount"] = pd.Series([r["amount"] for r in records]).map(_parse_amount)
    df["type"] = pd.Series([r["type"] for r in records])
    df["method"] = pd.Series([r["method"] for r in records])
    df["remark"] = ""
    df["status"] = ""
    df["amount"] = df["amount"].abs()
    df = df.dropna(subset=["time"])
    df = df[df["amount"] > 0]
    df = df.sort_values("time").reset_index(drop=True)
    # 关键词扫描文本：对手方 + 交易类型 + 备注（不含“交易方式/银行卡”，
    # 否则每笔银行卡付款都会被“银行”命中，淹没真正的银行借贷往来）
    df["cp_text"] = (df["counterparty"].fillna("") + " " +
                     df["type"].fillna("") + " " +
                     df["remark"].fillna(""))
    return df


# ---------------------------------------------------------------------------
# 民间借贷 / 高风险关键词
# ---------------------------------------------------------------------------
STRONG_KEYWORDS = [
    "借款", "贷款", "放贷", "出借", "欠款", "欠条", "借条",
    "周转", "调头", "过桥", "押", "抵押", "质押", "车抵", "房抵", "典", "典当",
    "利息", "本息", "月息", "日息", "砍头息", "垫付", "垫资",
    "小贷", "网贷", "现金贷", "信用贷", "高利", "催收", "代偿", "担保",
    "租机", "手机租", "以租代购",
]
WEAK_KEYWORDS = [
    "财务", "商务", "咨询", "金融", "财富", "金服", "普惠",
    "捷信", "宜信", "恒昌", "玖富", "拍拍", "分期", "白条", "花呗", "借呗",
]

# 定向关注词：还款 / 银行 / 小贷 / 信息咨询（用户明确要求关注）
WATCH_KEYWORDS = ["还款", "银行", "小贷", "信息咨询"]


def _scan_keywords(text):
    strong = [k for k in STRONG_KEYWORDS if k in text]
    weak = [k for k in WEAK_KEYWORDS if k in text]
    return strong, weak


def _scan_watch(text):
    return [k for k in WATCH_KEYWORDS if k in text]


# ---------------------------------------------------------------------------
# 分析引擎
# ---------------------------------------------------------------------------
class Findings:
    def __init__(self):
        self.items = []  # (category, severity, title, detail, count, examples)

    def add(self, category, severity, title, detail, count=0, examples="", detail_full=None, rows=None, time_color_after=None):
        self.items.append({
            "category": category, "severity": severity, "title": title,
            "detail": detail, "count": count, "examples": examples,
            "detail_full": detail_full,
            "rows": list(rows) if rows else [],
            "time_color_after": time_color_after,
        })


def _dir_label(d):
    """方向标签：进 / 出 / 未知。d 为 +1 / -1 / 0 或 None。"""
    if d is None:
        return "未知"
    try:
        d = int(d)
    except (TypeError, ValueError):
        return "未知"
    return "进" if d > 0 else ("出" if d < 0 else "未知")


def _after_1500(t):
    """判断交易时间是否晚于 15:00（用于固定金额打款时间高亮）。"""
    return t.hour > 15 or (t.hour == 15 and t.minute > 0)


def _fmt_row(t, cp, a, note="", dir_label=""):
    """把一笔交易格式化为明细行：日期时间 + 对手方 + 金额（+ 方向 + 备注）。"""
    cp = str(cp).strip() or "(未知对方)"
    if t is None or (hasattr(t, "isna") and bool(t.isna())) or str(t) == "NaT":
        ts = "（时间缺失）"
    else:
        try:
            ts = f"{t:%Y-%m-%d %H:%M}"
        except Exception:
            ts = "（时间缺失）"
    s = f"{ts}　{cp}　¥{a:,.2f}"
    if dir_label:
        s += f"　[{dir_label}]"
    if note:
        s += f"　{note}"
    return s


def _fmt_gap(t, prev):
    """计算当前笔与同一对手方上一笔的间隔，始终以「日历天数」表示，不细化到小时/分钟。"""
    if prev is None:
        return "首笔"
    try:
        cal_days = (t.date() - prev.date()).days
    except Exception:
        return ""
    return f"距上笔{cal_days}天"


def _cap_rows(rows, n=50):
    """明细行截断保护，避免报告过长。"""
    rows = list(rows)
    if len(rows) > n:
        return rows[:n] + [f"（仅显示前 {n} 笔，共 {len(rows)} 笔，完整见 risk_findings.csv）"]
    return rows


def _normalize_for_analysis(df):
    """分析前的最后一道保险：保证关键列存在，方向缺失时尽力推断，避免 KeyError / 误判。

    正常文件（加载器已建好 dir）不受影响；仅当 dir 列缺失或全为 0（罕见边角/旧版）
    时才兜底：先按交易类型、再按金额符号推断，仍无果则默认支出（微信流水多为支出），
    确保报告一定能出、且支出侧维度可用。
    """
    for c in ("time", "amount", "counterparty", "type", "remark", "method"):
        if c not in df.columns:
            df[c] = "" if c != "amount" else 0.0
    # 关键：time 列必须统一为 datetime，丢弃解析失败的行。
    # 否则含 NaN/字符串的时间会让整列退化为 float，后续 .min()/.days 直接崩溃。
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).reset_index(drop=True)
    if df.empty:
        df["dir"] = pd.Series(dtype="int64")
        df["amount"] = pd.Series(dtype="float64")
        df["cp_text"] = pd.Series(dtype="object")
        df["counterparty"] = pd.Series(dtype="object")
        df["type"] = pd.Series(dtype="object")
        df["remark"] = pd.Series(dtype="object")
        df["method"] = pd.Series(dtype="object")
        return df
    # 关键词扫描文本（对手方 + 备注 + 类型），analyze 按行读取，必须存在
    if "cp_text" not in df.columns:
        df["cp_text"] = (df["counterparty"].fillna("") + " " +
                         df["remark"].fillna("") + " " +
                         df["type"].fillna("")).str.strip()
    if "dir" not in df.columns:
        df["dir"] = 0
    dir_series = pd.to_numeric(df["dir"], errors="coerce").fillna(0)
    if (dir_series == 0).all():
        # 先按交易类型推断（收入/支出关键词）
        if "type" in df.columns and df["type"].astype(str).str.strip().ne("").any():
            dir_series = dir_series.where(
                dir_series != 0, df["type"].map(_direction_sign).fillna(0))
        # 再按金额符号推断（负数=支出）
        if (dir_series == 0).all():
            dir_series = dir_series.where(
                dir_series != 0,
                df["amount"].map(
                    lambda a: -1 if (isinstance(a, str) and str(a).lstrip().startswith("-")) else 0))
        # 仍全 0：默认支出，保证不崩
        if (dir_series == 0).all():
            dir_series = pd.Series(-1, index=df.index)
    df["dir"] = dir_series.astype(int)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    return df


def analyze(df, args):
    f = Findings()
    df = _normalize_for_analysis(df)
    total_in = df.loc[df["dir"] > 0, "amount"].sum()
    total_out = df.loc[df["dir"] < 0, "amount"].sum()
    n = len(df)
    date_min, date_max = df["time"].min(), df["time"].max()
    if pd.isna(date_min) or pd.isna(date_max):
        span_days = 1
    else:
        span_days = max((date_max - date_min).days, 1)
    cp_count = df["counterparty"].nunique()

    # ---- 1. 固定金额高频（定向：同一对手方 + 同一金额 反复）----
    exp = df[df["dir"] < 0].copy()
    exp["amt"] = exp["amount"].round(2)
    pair = exp.groupby(["counterparty", "amt"]).size().reset_index(name="cnt")
    pair = pair[pair["cnt"] >= args.min_freq].sort_values("cnt", ascending=False)
    if len(pair):
        top = pair.head(15)
        ex_parts = []
        for _, row in top.iterrows():
            cp = row["counterparty"] if str(row["counterparty"]).strip() else "(未知对方)"
            ex_parts.append(f"{cp} ¥{row['amt']:.0f}×{int(row['cnt'])}笔")
        ex = "；".join(ex_parts)
        n_high = int((pair["cnt"] >= 6).sum())
        rows_fixed = []
        for _, row in pair.iterrows():
            cp = row["counterparty"] if str(row["counterparty"]).strip() else "(未知对方)"
            sub = exp[(exp["counterparty"] == row["counterparty"]) &
                      (exp["amt"] == row["amt"])].sort_values("time")
            prev = None
            for i, t in enumerate(sub["time"]):
                if i >= 15:  # 每组最多展示 15 笔，避免单一对手方挤占明细、掩盖其他组的高亮时间
                    rows_fixed.append(f"（「{cp}」共 {len(sub)} 笔，本组仅显示前 15 笔）")
                    break
                gap = _fmt_gap(t, prev)
                note = gap
                if _after_1500(t):
                    note = (note + " ⚠>15:00") if note else "⚠>15:00"
                rows_fixed.append(_fmt_row(t, cp, row["amt"], note))
                prev = t
        f.add("固定金额定向打款", "高" if n_high else "中",
              f"{len(pair)} 组(对手方+金额)反复出现≥{args.min_freq}次",
              "同一对手方反复收取相同金额，疑似固定还款/会费/套路贷扣款/固定薪资；逐笔日期与距上笔间隔见下方明细。",
              len(pair), ex, rows=_cap_rows(rows_fixed, 120), time_color_after=15)

    # ---- 2. 周期规律 ----
    # 星期几
    df["weekday"] = df["time"].dt.weekday
    df["day"] = df["time"].dt.day
    df["hour"] = df["time"].dt.hour
    wd = df["weekday"].value_counts()
    if not wd.empty:
        wd_share = wd.max() / n
        if wd_share >= 0.35 and wd.max() >= 5:
            names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            f.add("周期规律(周)", "中", "交易高度集中在特定星期",
                  f"{(wd_share*100):.0f}% 的交易落在 {names[wd.idxmax()]}。",
                  int(wd.max()), names[wd.idxmax()])
    # 每月几号
    d = df["day"].value_counts()
    if not d.empty:
        d_share = d.max() / n
        if d_share >= 0.25 and d.max() >= 3:
            f.add("周期规律(月)", "中", "交易高度集中在每月固定日期",
                  f"{(d_share*100):.0f}% 的交易发生在每月 {d.idxmax()} 号，疑似按月还款/缴费。",
                  int(d.max()), f"每月{d.idxmax()}号")
    # 固定间隔（按对手方）
    interval_hits = 0
    interval_examples = []
    for cp, g in df.sort_values("time").groupby("counterparty"):
        if len(g) < 3:
            continue
        diffs = g["time"].diff().dropna().dt.days
        if diffs.empty:
            continue
        vc = diffs.round().astype(int).value_counts()
        if vc.empty:
            continue
        common, cnt = vc.idxmax(), vc.max()
        if cnt >= 3 and common in (7, 14, 30, 31, 15, 10):
            interval_hits += 1
            if len(interval_examples) < 8:
                interval_examples.append(f"{cp}（每{common}天，{cnt}次）")
    if interval_hits:
        f.add("周期规律(间隔)", "中", "存在等间隔规律性打款",
              f"发现 {interval_hits} 个对手方呈现 7/14/30 天等固定间隔打款，极可能为周期性还款。",
              interval_hits, "；".join(interval_examples), rows=interval_examples)

    # ---- 3. 深夜交易 ----
    late = df[df["hour"] >= args.late_start]
    if args.late_end is not None:
        late = late[late["hour"] < args.late_end]
    else:
        late = late[late["hour"] < 6]  # 默认统计 0~6 点
    if len(late):
        late_out_amt = late.loc[late["dir"] < 0, "amount"].sum()
        ex = "；".join(
            f"{t.strftime('%m-%d %H:%M')} {cp} ¥{a:.0f}[{_dir_label(d)}]"
            for t, cp, a, d in zip(late["time"].head(8), late["counterparty"].head(8), late["amount"].head(8), late["dir"].head(8)))
        rows_late = [_fmt_row(t, cp, a, dir_label=_dir_label(d))
                     for t, cp, a, d in zip(late["time"], late["counterparty"], late["amount"], late["dir"])]
        f.add("深夜交易", "中" if len(late) >= 5 else "低",
              f"{len(late)} 笔发生在 {args.late_start}:00 之后的交易",
              f"深夜支出合计 ¥{late_out_amt:,.0f}，需关注是否与异常资金往来相关；本栏逐笔标注【进】(收入)/【出】(支出)。",
              len(late), ex, rows=_cap_rows(rows_late))

    # ---- 4. 民间借贷关键词 ----
    strong_rows, weak_rows = [], []
    for _, r in df.iterrows():
        s, w = _scan_keywords(r["cp_text"])
        if s:
            strong_rows.append((r, s))
        elif w:
            weak_rows.append((r, w))
    if strong_rows:
        cp_strong = Counter(r["counterparty"] for r, _ in strong_rows)
        ex = "；".join(f"{cp}({c}笔)" for cp, c in cp_strong.most_common(8))
        rows_strong = [_fmt_row(r["time"], r["counterparty"], r["amount"], f"（命中：{','.join(s)}）")
                       for r, s in strong_rows]
        f.add("民间借贷(强信号)", "高",
              f"{len(strong_rows)} 笔命中民间借贷强关键词",
              "对手方/备注含 借/贷/周转/押/典/息/小贷/典当 等，疑似民间放贷或借贷往来；逐笔明细见下方说明。",
              len(strong_rows), ex, rows=_cap_rows(rows_strong))
    if weak_rows:
        cp_weak = Counter(r["counterparty"] for r, _ in weak_rows)
        ex = "；".join(f"{cp}({c}笔)" for cp, c in cp_weak.most_common(6))
        f.add("民间借贷(弱信号)", "低",
              f"{len(weak_rows)} 笔命中金融类弱关键词",
              "对手方/备注含 财务/商务/金融/财富 等，建议人工复核是否涉及借贷中介。",
              len(weak_rows), ex)
    if not strong_rows:
        f.add("民间借贷(强信号)", "低",
              "未检出民间借贷强信号关键词",
              "对手方/备注中未发现 借/贷/周转/押/典/息/小贷/典当 等强信号词（已排除‘信用卡还款’等银行还款误报）。",
              0, "")

    # ---- 4.5 万整大额入账（≥1万且百/十/个位均为 0）----
    wan = df[(df["dir"] > 0) & (df["amount"] >= 10000) & (df["amount"].mod(1000).eq(0))].copy()
    if len(wan):
        wan = wan.sort_values("amount", ascending=False)
        ex = "；".join(
            f"{t.strftime('%Y-%m-%d')} {cp} ¥{a:,.0f}"
            for t, cp, a in zip(wan["time"].head(10), wan["counterparty"].head(10), wan["amount"].head(10)))
        rows_wan = [_fmt_row(t, cp, a) for t, cp, a in zip(wan["time"], wan["counterparty"], wan["amount"])]
        f.add("万整大额入账", "中" if len(wan) >= 3 else "低",
              f"{len(wan)} 笔入账为 ≥1万且百/十/个位均为0的整额",
              "入账金额恰为万元整数（如 10000/20000/50000），常关联特定结算、还款或资金归集，需关注来源与对手方关系。",
              len(wan), ex, rows=_cap_rows(rows_wan))

    # ---- 4.6 借贷相关关键词关注（还款 / 银行 / 小贷 / 信息咨询）----
    watch_rows = []
    for _, r in df.iterrows():
        hits = _scan_watch(r["cp_text"])
        if hits:
            watch_rows.append((r, hits))
    if watch_rows:
        cp_watch = Counter(r["counterparty"] for r, _ in watch_rows)
        ex = "；".join(f"{cp}({c}笔)" for cp, c in cp_watch.most_common(8))
        rows_watch = [_fmt_row(r["time"], r["counterparty"], r["amount"], f"（{','.join(hits)}）")
                      for r, hits in watch_rows]
        f.add("借贷相关关键词关注", "中",
              f"{len(watch_rows)} 笔命中 还款/银行/小贷/信息咨询 关注词",
              "对手方/备注含 还款/银行/小贷/信息咨询，疑似银行还款、小贷或咨询类借贷中介往来，建议人工复核。",
              len(watch_rows), ex, rows=_cap_rows(rows_watch))

    # ---- 5. 对手方风险画像：双向往来（对敲）----
    net = df.groupby("counterparty")["amount"].apply(
        lambda s: df.loc[s.index, "dir"].mul(s).sum())
    both = net[(net > 0) & (net < 0)].index  # 同时有收有支
    two_way = []
    for cp in df["counterparty"].unique():
        g = df[df["counterparty"] == cp]
        if (g["dir"] > 0).any() and (g["dir"] < 0).any():
            in_amt = g.loc[g["dir"] > 0, "amount"].sum()
            out_amt = g.loc[g["dir"] < 0, "amount"].sum()
            diff = abs(in_amt - out_amt)
            ratio = diff / max(in_amt + out_amt, 1)
            if ratio < 0.15 and min(in_amt, out_amt) > 1000:
                two_way.append((cp, in_amt, out_amt, ratio))
    if two_way:
        two_way.sort(key=lambda x: x[3])
        ex = "；".join(f"{cp}(收¥{i:,.0f}/支¥{o:,.0f})" for cp, i, o, _ in two_way[:8])
        rows_two = [f"{cp}　收¥{i:,.2f} / 支¥{o:,.2f}（净额近零）" for cp, i, o, _ in two_way]
        f.add("双向往来(对敲)", "高",
              f"{len(two_way)} 个对手方收支净额极低",
              "同一对手方收款与付款金额几乎相抵，疑似走账/对敲/资金过渡。",
              len(two_way), ex, rows=_cap_rows(rows_two, 40))

    # ---- 6. 大额交易 ----
    large = df[df["amount"] >= args.large].copy()
    if len(large):
        n_in = int((large["dir"] > 0).sum())
        n_out = int((large["dir"] < 0).sum())
        ex = "；".join(f"{t.strftime('%m-%d')} {cp} ¥{a:,.0f}[{_dir_label(d)}]"
                       for t, cp, a, d in zip(large["time"].head(8), large["counterparty"].head(8), large["amount"].head(8), large["dir"].head(8)))
        rows_large = [_fmt_row(t, cp, a, dir_label=_dir_label(d))
                      for t, cp, a, d in zip(large["time"], large["counterparty"], large["amount"], large["dir"])]
        f.add("大额交易", "中", f"{len(large)} 笔金额 ≥ ¥{args.large:,.0f}（进{n_in}/出{n_out}）",
              "单笔大额交易需关注资金来源与用途合规性；本栏逐笔标注【进】(收入)/【出】(支出)。",
              len(large), ex, rows=_cap_rows(rows_large))

    # ---- 7. 快进快出（分层）----（可用 --skip-layering 关闭）
    if not args.skip_layering:
        # 仅当「收入方 ≠ 支出方」且金额较大时视为可疑分层，避免小商户正常收付款误报
        df_sorted = df.sort_values("time").reset_index(drop=True)
        layering = []
        for i, r in df_sorted.iterrows():
            if r["dir"] <= 0 or r["amount"] < args.layering_min:
                continue
            window = df_sorted[(df_sorted["time"] > r["time"]) &
                               (df_sorted["time"] <= r["time"] + timedelta(hours=72)) &
                               (df_sorted["dir"] < 0) &
                               (df_sorted["counterparty"] != r["counterparty"])]
            for _, o in window.iterrows():
                if 0.8 <= o["amount"] / r["amount"] <= 1.25:
                    layering.append((r, o))
                    break
        if len(layering) >= 2:
            ex = "；".join(
                f"{ri['time'].strftime('%m-%d')}收¥{ri['amount']:,.0f}({ri['counterparty']})→{oi['counterparty']}支¥{oi['amount']:,.0f}"
                for ri, oi in layering[:8])
            f.add("快进快出(分层)", "高" if len(layering) >= 5 else "中",
                  f"{len(layering)} 次大额转入后 72h 内相似金额转给不同对手方",
                  f"收¥{args.layering_min:,.0f}以上、转入后 72h 内以 0.8~1.25 倍金额转给不同对手方，疑似过渡账户/洗钱分层。",
                  len(layering), ex)
    else:
        print("[跳过] 快进快出维度已按 --skip-layering 关闭")

    # ---- 8. 综合风险评分 ----
    score = 0
    score += sum({"高": 25, "中": 12, "低": 4}.get(it["severity"], 0)
                 for it in f.items if it["category"].startswith(("固定金额", "民间借贷(强", "双向往来", "快进快出", "借贷相关关键词关注")))
    score += sum({"高": 10, "中": 5, "低": 2}.get(it["severity"], 0)
                 for it in f.items if not it["category"].startswith(("固定金额", "民间借贷(强", "双向往来", "快进快出", "借贷相关关键词关注")))
    score = min(score, 100)
    level = "高" if score >= 60 else ("中" if score >= 30 else "低")

    summary = {
        "n": n, "total_in": total_in, "total_out": total_out,
        "net": total_in - total_out, "date_min": date_min, "date_max": date_max,
        "span_days": span_days, "cp_count": cp_count, "score": score, "level": level,
    }
    return f, summary


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------
def build_markdown(f, summary, sources, filters=""):
    L = []
    L.append("# 微信流水风险分析报告\n")
    L.append(f"- 分析文件：{', '.join(os.path.basename(s) for s in sources)}")
    if filters:
        L.append(f"- 应用口径（大条件）：{filters}")
    L.append(f"- 时间范围：{summary['date_min']:%Y-%m-%d} ~ {summary['date_max']:%Y-%m-%d}（{summary['span_days']}天）")
    L.append(f"- 交易笔数：**{summary['n']}**　对手方：**{summary['cp_count']}**")
    L.append(f"- 收入合计：¥{summary['total_in']:,.2f}　支出合计：¥{summary['total_out']:,.2f}　净流：¥{summary['net']:,.2f}")
    L.append(f"\n## 综合风险评级：{'🔴 高' if summary['level']=='高' else ('🟡 中' if summary['level']=='中' else '🟢 低')}（评分 {summary['score']}/100）\n")
    L.append("| 类别 | 风险等级 | 命中数 | 明细 |")
    L.append("|------|---------|-------|------|")
    order = {"高": 0, "中": 1, "低": 2}
    for it in sorted(f.items, key=lambda x: order.get(x["severity"], 3)):
        L.append(f"| {it['category']} | {it['severity']} | {it['count']} | {it['examples']} |")
    L.append("\n## 各维度明细\n")
    for it in sorted(f.items, key=lambda x: order.get(x["severity"], 3)):
        L.append(f"### [{it['severity']}] {it['title']}")
        L.append(f"- 类别：{it['category']}")
        L.append(f"- 命中数：{it['count']}")
        L.append(f"- 说明：{it['detail']}")
        if it["rows"]:
            L.append("- 明细：")
            for r in it["rows"]:
                L.append(f"  · {r}")
        elif it["examples"]:
            L.append(f"- 明细：{it['examples']}")
        L.append("")
    L.append("\n> 本报告由脚本自动生成，结果为风险线索提示，不构成法律/审计结论。请结合原始凭证人工复核。")
    return "\n".join(L)


def build_findings_csv(f):
    out = pd.DataFrame(f.items)[["category", "severity", "title", "count", "detail", "examples"]]
    out["明细"] = [" | ".join(it["rows"]) if it["rows"] else "" for it in f.items]
    return out


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_date_safe(x):
    """安全格式化日期：NaT / None / 空值统一显示『—』，避免 NaTType does not support strftime。"""
    try:
        if x is None or (hasattr(x, "isna") and bool(x.isna())) or str(x) == "NaT":
            return "—"
        return f"{x:%Y-%m-%d}"
    except Exception:
        return "—"


# 匹配明细行开头的日期时间，用于「固定金额定向打款」>15:00 红色高亮
_DT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} (\d{2}):(\d{2}))")


def _color_time(raw, threshold_hour):
    """把明细行开头的日期时间按阈值染色（晚于 threshold_hour 的时点显示为红色）。"""
    m = _DT_RE.match(raw)
    if not m:
        return _esc(raw)
    hh, mm = int(m.group(2)), int(m.group(3))
    after = (hh > threshold_hour) or (hh == threshold_hour and mm > 0)
    dt_str, rest = m.group(1), raw[m.end():]
    if after:
        return f'<span class="time-red">{_esc(dt_str)}</span>{_esc(rest)}'
    return _esc(raw)


def _render_row_html(r, it):
    """渲染单条明细：若该维度启用了时间高亮，则对 >15:00 的时间染色。"""
    if it.get("time_color_after") is not None:
        return _color_time(r, it["time_color_after"])
    return _esc(r)


def build_html(f, summary, sources, filters=""):
    """生成带排版的 HTML 报告，便于浏览器直接打开阅读。"""
    sev_cls = {"高": "sev-high", "中": "sev-mid", "低": "sev-low"}
    rate_cls = {"高": "rate-high", "中": "rate-mid", "低": "rate-low"}[summary["level"]]
    order = {"高": 0, "中": 1, "低": 2}
    items = sorted(f.items, key=lambda x: order.get(x["severity"], 3))

    rows = ""
    for it in items:
        sc = sev_cls.get(it["severity"], "")
        if it["rows"]:
            shown = it["rows"][:30]
            ex_cell = "<br>".join(_render_row_html(r, it) for r in shown)
            if len(it["rows"]) > 30:
                ex_cell += f"<br>…共 {len(it['rows'])} 笔"
        else:
            ex_cell = _esc(it["examples"])
        rows += (f"<tr><td>{_esc(it['category'])}</td>"
                 f"<td class='{sc}'>{_esc(it['severity'])}</td>"
                 f"<td class='num'>{it['count']}</td>"
                 f"<td class='ex'>{ex_cell}</td></tr>\n")

    details = ""
    for it in items:
        sc = sev_cls.get(it["severity"], "")
        detail_html = _esc(it["detail"]).replace("\n", "<br>\n")
        if it["rows"]:
            body_rows = "<br>".join(_render_row_html(r, it) for r in it["rows"][:50])
            if len(it["rows"]) > 50:
                body_rows += f"<br>…共 {len(it['rows'])} 笔"
            ex_html = f"<div class='card-ex'>明细：<br>{body_rows}</div>"
        elif it["examples"]:
            ex_html = f"<div class='card-ex'>明细：{_esc(it['examples'])}</div>"
        else:
            ex_html = ""
        details += (f"<div class='card'>\n"
                    f"  <div class='card-head'>"
                    f"<span class='badge {sc}'>{_esc(it['severity'])}</span>"
                    f"<span class='card-title'>{_esc(it['title'])}</span>"
                    f"<span class='card-count'>命中 {it['count']}</span></div>\n"
                    f"  <div class='card-body'>{detail_html}</div>\n"
                    f"  {ex_html}</div>\n")

    css = """
    :root{ --bg:#f5f7fa; --card:#ffffff; --ink:#1f2933; --muted:#6b7785;
           --line:#e3e8ef; --hi:#e5484d; --mid:#f08c00; --lo:#12a150; --accent:#2f6fed; }
    *{ box-sizing:border-box; }
    body{ margin:0; background:var(--bg); color:var(--ink);
          font-family:-apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,Roboto,sans-serif;
          line-height:1.7; }
    .wrap{ max-width:980px; margin:0 auto; padding:32px 24px 64px; }
    h1{ font-size:26px; margin:0 0 4px; }
    .meta{ color:var(--muted); font-size:14px; margin:2px 0; }
    .meta b{ color:var(--ink); }
    .rate{ display:inline-block; margin:18px 0 6px; padding:10px 22px; border-radius:12px;
           font-size:20px; font-weight:700; color:#fff; }
    .rate-high{ background:var(--hi); } .rate-mid{ background:var(--mid); } .rate-low{ background:var(--lo); }
    table{ width:100%; border-collapse:collapse; margin:18px 0 8px; background:var(--card);
           border:1px solid var(--line); border-radius:12px; overflow:hidden; font-size:14px; }
    th,td{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
    th{ background:#eef2f7; font-weight:600; }
    td.num{ text-align:center; font-variant-numeric:tabular-nums; }
    td.ex{ color:var(--muted); font-size:13px; }
    .sev-high{ color:var(--hi); font-weight:700; }
    .sev-mid{ color:var(--mid); font-weight:700; }
    .sev-low{ color:var(--lo); font-weight:700; }
    h2{ font-size:19px; margin:34px 0 8px; border-left:4px solid var(--accent); padding-left:10px; }
    .card{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:14px 16px; margin:12px 0; box-shadow:0 1px 3px rgba(16,24,40,.04); }
    .card-head{ display:flex; align-items:center; gap:10px; margin-bottom:6px; }
    .badge{ padding:2px 10px; border-radius:999px; color:#fff; font-size:13px; font-weight:700; }
    .badge.sev-high{ background:var(--hi); } .badge.sev-mid{ background:var(--mid); }
    .badge.sev-low{ background:var(--lo); }
    .card-title{ font-weight:600; font-size:15px; }
    .card-count{ margin-left:auto; color:var(--muted); font-size:13px; }
    .card-body{ white-space:pre-wrap; word-break:break-word; font-size:14px; }
    .card-ex{ margin-top:8px; color:var(--muted); font-size:13px; border-top:1px dashed var(--line); padding-top:6px; }
    .time-red{ color:#e5484d; font-weight:700; }
    .foot{ margin-top:36px; color:var(--muted); font-size:13px; border-top:1px solid var(--line); padding-top:12px; }
    """

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信流水风险分析报告</title><style>{css}</style></head>
<body><div class="wrap">
<h1>微信流水风险分析报告</h1>
<div class="meta">分析文件：{_esc(', '.join(os.path.basename(s) for s in sources))}</div>
<div class="meta">时间范围：{_fmt_date_safe(summary['date_min'])} ~ {_fmt_date_safe(summary['date_max'])}（{summary['span_days']}天）</div>
<div class="meta">交易笔数：<b>{summary['n']}</b>　对手方：<b>{summary['cp_count']}</b></div>
<div class="meta">收入合计：¥{summary['total_in']:,.2f}　支出合计：¥{summary['total_out']:,.2f}　净流：¥{summary['net']:,.2f}</div>
<div class="meta">应用口径（大条件）：{_esc(filters)}</div>
<div class="rate {rate_cls}">综合风险评级：{summary['level']}（{summary['score']}/100）</div>
{_empty_notice(summary)}

<h2>风险总览</h2>
<table><thead><tr><th>类别</th><th>风险等级</th><th>命中数</th><th>明细</th></tr></thead>
<tbody>
{rows}</tbody></table>

<h2>各维度明细</h2>
{details}

<div class="foot">本报告由脚本自动生成，结果为风险线索提示，不构成法律 / 审计结论。请结合原始凭证人工复核。</div>
</div></body></html>"""
    return html


def _empty_notice(summary):
    """当有效交易/时间为 0 时，给出友好提示而非空白报告。"""
    if summary.get("n", 0) and not (summary.get("date_min") is None
            or (hasattr(summary.get("date_min"), "isna") and bool(summary["date_min"].isna()))
            or str(summary.get("date_min")) == "NaT"):
        return ""
    return ('<div class="card" style="border-color:#e0a800;background:#fff8e6">'
            '⚠️ 未能解析到有效交易时间或交易记录。常见原因：① PDF 为扫描件 / 图片型，'
            '文字层缺失，浏览器端 pdfminer 无法抽取；② 时间格式非常规。'
            '建议改用微信导出的 <b>CSV / Excel</b>（微信 → 账单 → 下载账单 → 选 CSV/Excel 格式），'
            '解析成功率更高。</div>')


# ---------------------------------------------------------------------------
# 无子进程 / 内存字节入口（供前端 Pyodide 调用，也便于本地测试）
# ---------------------------------------------------------------------------
def make_args(d=None):
    """根据字典构造与命令行一致的参数对象；缺省即本次研究的默认口径。"""
    d = d or {}
    return argparse.Namespace(
        global_min=float(d.get("global_min", 300)),
        skip_layering=bool(d.get("skip_layering", True)),
        large=float(d.get("large", 50000)),
        late_start=int(d.get("late_start", 0)),
        late_end=int(d.get("late_end", 6)),
        min_freq=int(d.get("min_freq", 3)),
        min_amount=float(d.get("min_amount", 100)),
        layering_min=float(d.get("layering_min", 1000)),
        ignore=str(d.get("ignore", "充值,提现,信用卡还款")),
        top_n=int(d.get("top_n", 10)),
    )


def run_bytes_full(data, filename, args=None):
    """直接传入文件字节，返回 (HTML 报告, 完整明细 DataFrame)。

    完整明细为「过滤前」的全部解析行（含被 global_min / ignore 剔除的交易），
    便于服务端落地收集用户上传的流水。HTML 报告仍基于过滤后的子集生成。
    全流程在内存完成，临时文件用后即删。
    """
    ext = os.path.splitext(filename)[1].lower() or ".csv"
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        tmp.write(data)
        tmp.flush()
        a = make_args(args if isinstance(args, dict) else None)
        df_full = load_pdf_bill(tmp.name) if ext == ".pdf" else load_bill(tmp.name)
        # 用于报告生成的副本：应用 global_min 与 ignore 过滤
        df = df_full.copy()
        if a.global_min > 0:
            df = df[df["amount"] >= a.global_min].reset_index(drop=True)
        ignore_kws = [k.strip() for k in a.ignore.split(",") if k.strip()]
        if ignore_kws:
            text_cols = [c for c in ("type", "counterparty", "method", "remark") if c in df.columns]
            if text_cols:
                mask = ~df[text_cols].apply(
                    lambda row: any(k in " ".join(str(v) for v in row) for k in ignore_kws), axis=1)
                df = df[mask].reset_index(drop=True)
        f, summary = analyze(df, a)
        filters_str = (f"已剔除金额<¥{a.global_min:.0f}的交易；"
                       f"已关闭快进快出维度；"
                       f"已忽略充值/提现/信用卡还款等干扰交易")
        html = build_html(f, summary, [filename], filters=filters_str)
        return html, df_full
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def run_bytes(data, filename, args=None):
    """兼容入口：仅返回 HTML 报告（服务端请改用 run_bytes_full 以获取明细）。"""
    html, _ = run_bytes_full(data, filename, args)
    return html


def run_file(path, filename=None, args=None):
    """从（临时）文件路径读取并分析，返回 HTML 报告。"""
    filename = filename or os.path.basename(path)
    with open(path, "rb") as fh:
        data = fh.read()
    return run_bytes(data, filename, args)


def main():
    ap = argparse.ArgumentParser(description="微信流水风险分析器")
    ap.add_argument("path", help="流水文件或目录（.csv/.xlsx/.xls）")
    ap.add_argument("--out", default="output", help="报告输出目录")
    ap.add_argument("--large", type=float, default=50000, help="大额阈值（元）")
    ap.add_argument("--late-start", type=int, default=0, help="深夜交易起始小时")
    ap.add_argument("--late-end", type=int, default=6, help="深夜交易结束小时（默认6）")
    ap.add_argument("--min-freq", type=int, default=3, help="固定金额最小出现次数")
    ap.add_argument("--min-amount", type=float, default=100, help="固定金额检测的最小金额（过滤小额噪声）")
    ap.add_argument("--layering-min", type=float, default=1000, help="快进快出判定中‘大额转入’的最小金额")
    ap.add_argument("--global-min", type=float, default=0.0, help="全局最小金额过滤（研究前剔除低于此金额的所有交易）")
    ap.add_argument("--ignore", default="充值,提现,信用卡还款", help="全局忽略的交易类型关键词（研究前剔除，不进入任何维度），逗号分隔")
    ap.add_argument("--skip-layering", action="store_true", help="关闭‘快进快出’维度研究")
    ap.add_argument("--top-n", type=int, default=10, help="对手方 Top N")
    args = ap.parse_args()

    files = []
    if os.path.isdir(args.path):
        for ext in ("*.csv", "*.CSV", "*.xlsx", "*.xls", "*.pdf", "*.PDF"):
            files += glob.glob(os.path.join(args.path, ext))
    else:
        files = [args.path]
    files = [x for x in files if "sample" not in os.path.basename(x).lower() or True]
    if not files:
        print("未找到流水文件。", file=sys.stderr)
        sys.exit(1)

    dfs = []
    for fp in files:
        print(f"[载入] {fp}")
        if fp.lower().endswith(".pdf"):
            dfs.append(load_pdf_bill(fp))
        else:
            dfs.append(load_bill(fp))
    df = pd.concat(dfs, ignore_index=True)
    print(f"[汇总] 共 {len(df)} 条交易，时间 {df['time'].min():%Y-%m-%d} ~ {df['time'].max():%Y-%m-%d}")

    # 大条件：研究前先过滤低于 --global-min 的交易
    if args.global_min > 0:
        before = len(df)
        df = df[df["amount"] >= args.global_min].reset_index(drop=True)
        print(f"[过滤] 剔除金额 < ¥{args.global_min:.0f} 的交易 {before - len(df)} 条，剩余 {len(df)} 条")

    # 大条件：研究前剔除 充值/提现/信用卡还款 等干扰交易
    ignore_kws = [k.strip() for k in args.ignore.split(",") if k.strip()]
    if ignore_kws:
        text_cols = [c for c in ("type", "counterparty", "method", "remark") if c in df.columns]
        before = len(df)
        mask = ~df[text_cols].apply(
            lambda row: any(k in " ".join(str(v) for v in row) for k in ignore_kws), axis=1)
        df = df[mask].reset_index(drop=True)
        print(f"[过滤] 剔除 充值/提现/信用卡还款 等干扰交易 {before - len(df)} 条，剩余 {len(df)} 条")

    f, summary = analyze(df, args)

    # 记录本次应用的口径
    flt = []
    if args.global_min > 0:
        flt.append(f"已剔除金额<¥{args.global_min:.0f}的交易")
    if args.skip_layering:
        flt.append("已关闭快进快出维度")
    if ignore_kws:
        flt.append("已忽略充值/提现/信用卡还款等干扰交易")
    filters_str = "；".join(flt) if flt else "无（全量研究）"

    os.makedirs(args.out, exist_ok=True)
    md = build_markdown(f, summary, files, filters=filters_str)
    md_path = os.path.join(args.out, "risk_report.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    csv_path = os.path.join(args.out, "risk_findings.csv")
    build_findings_csv(f).to_csv(csv_path, index=False, encoding="utf-8-sig")
    html_path = os.path.join(args.out, "risk_report.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(build_html(f, summary, files, filters=filters_str))

    print("\n========== 风险摘要 ==========")
    print(f"综合风险评级：{summary['level']}（{summary['score']}/100）")
    for it in sorted(f.items, key=lambda x: {"高": 0, "中": 1, "低": 2}.get(x["severity"], 3)):
        print(f"  [{it['severity']}] {it['title']}（{it['count']}）")
    print(f"\n报告已生成：\n  {md_path}\n  {html_path}\n  {csv_path}")


if __name__ == "__main__":
    main()
