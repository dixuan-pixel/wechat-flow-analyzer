#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点管理脚本：设置访问密码、导出已收集流水、查看统计。

用法：
    python manage.py setpw <新密码>         # 设置/修改访问密码
    python manage.py export [out.csv]       # 导出已收集流水为 CSV
    python manage.py stats                  # 查看已收集量
"""
import os
import sys
import csv
import json
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "site_config.json")
DB_FILE = os.path.join(BASE, "data", "flow.db")

sys.path.insert(0, BASE)
from werkzeug.security import generate_password_hash


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


def cmd_setpw(pw):
    cfg = load_config()
    cfg["password_hash"] = generate_password_hash(pw)
    save_config(cfg)
    print("✅ 访问密码已更新（长度 %d）。" % len(pw))


def cmd_export(out):
    if not os.path.exists(DB_FILE):
        print("尚无收集数据（data/flow.db 不存在）。"); return
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        """SELECT r.upload_id, u.ts, r.seq, r.time, r.amount,
                  r.counterparty, r.type, r.remark, r.method, r.direction
           FROM records r JOIN uploads u ON u.id = r.upload_id
           ORDER BY r.upload_id, r.seq""").fetchall()
    conn.close()
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["upload_id", "upload_ts", "seq", "time", "amount",
                    "counterparty", "type", "remark", "method", "direction"])
        w.writerows(rows)
    print("✅ 已导出 %d 条明细 -> %s" % (len(rows), out))


def cmd_stats():
    if not os.path.exists(DB_FILE):
        print("尚无收集数据。"); return
    conn = sqlite3.connect(DB_FILE)
    up = conn.execute("SELECT COUNT(*), COALESCE(SUM(rows),0) FROM uploads").fetchone()
    rec = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    conn.close()
    print("上传次数 : %d" % up[0])
    print("上传行数 : %d" % up[1])
    print("收集明细 : %d 条" % rec)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "setpw" and len(sys.argv) >= 3:
        cmd_setpw(sys.argv[2])
    elif cmd == "export":
        cmd_export(sys.argv[2] if len(sys.argv) >= 3 else "collected_flow.csv")
    elif cmd == "stats":
        cmd_stats()
    else:
        print(__doc__)
