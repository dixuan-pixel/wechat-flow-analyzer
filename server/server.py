#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信流水风险分析器 —— 服务端版（线上 / 密码访问 / 流水收集）。

与「纯浏览器 Pyodide」版不同，本版本把分析放到服务器跑：
- 浏览器只负责上传文件 + 展示报告，分析器 Python 源码不下发，别人无法复制；
- 用「访问密码」做门槛：输入正确密码后由服务端签发会话 Cookie，之后才能分析；
- 每次成功分析后，会把用户上传的流水明细写入本地 SQLite（uploads + records），
  站点主人可用 /api/export 导出 CSV 汇总研究。

首次运行若 site_config.json 中未设置密码，会启用默认密码 wechat2026，
请务必用 `python manage.py setpw <你的密码>` 修改。

运行（本地）：
    pip install -r requirements.txt
    python server.py                      # http://127.0.0.1:8000

生产（国内云服务器）：
    gunicorn -w 2 -b 0.0.0.0:8000 server:app
"""
import os
import sys
import json
import sqlite3
import io
import datetime

from flask import (Flask, request, send_from_directory, jsonify,
                   session, Response, abort)
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import wechat_flow_risk_analyzer as m  # 同一份分析器

app = Flask(__name__, static_folder=os.path.join(BASE, "static"))

CONFIG_FILE = os.path.join(BASE, "site_config.json")
DB_FILE = os.path.join(BASE, "data", "flow.db")
MAX_CONTENT = 30 * 1024 * 1024  # 单文件上限 30MB

DEFAULT_PASSWORD = "wechat2026"


# ---------------- 配置 / 密码 ----------------
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


def _ensure_config():
    """保证 site_config.json 有 session_secret 与 password_hash（首次默认密码）。"""
    cfg = load_config()
    changed = False
    if not cfg.get("session_secret"):
        import secrets
        cfg["session_secret"] = secrets.token_hex(32)
        changed = True
    if not cfg.get("password_hash"):
        cfg["password_hash"] = generate_password_hash(DEFAULT_PASSWORD)
        changed = True
        print("[!] 未设置访问密码，已启用默认密码：%s —— 请尽快运行 `python manage.py setpw <新密码>` 修改！"
              % DEFAULT_PASSWORD)
    if changed:
        save_config(cfg)
    return cfg


def check_pw(pw):
    cfg = load_config()
    h = cfg.get("password_hash")
    if not h or not pw:
        return False
    return check_password_hash(h, pw)


cfg = _ensure_config()
app.secret_key = cfg["session_secret"]


# ---------------- 数据库（流水收集） ----------------
def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS uploads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        filename TEXT,
        rows INTEGER,
        src_ip TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS records(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        upload_id INTEGER,
        seq INTEGER,
        time TEXT,
        amount REAL,
        counterparty TEXT,
        type TEXT,
        remark TEXT,
        method TEXT,
        direction TEXT
    )""")
    conn.commit()
    conn.close()


def store_records(df_full, filename, src_ip):
    """把一次上传的明细写入 SQLite，返回 upload_id。"""
    conn = sqlite3.connect(DB_FILE)
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO uploads(ts, filename, rows, src_ip) VALUES(?,?,?,?)",
            (ts, filename, int(len(df_full)), src_ip))
        uid = cur.lastrowid
        cols = list(df_full.columns)
        rows = []
        for i, (_, r) in enumerate(df_full.iterrows()):
            t = r.get("time")
            try:
                tstr = t.strftime("%Y-%m-%d %H:%M:%S") if hasattr(t, "strftime") else str(t)
            except Exception:
                tstr = str(t)
            rows.append((
                uid, i, tstr,
                float(r.get("amount") or 0),
                str(r.get("counterparty") or ""),
                str(r.get("type") or ""),
                str(r.get("remark") or ""),
                str(r.get("method") or ""),
                str(r.get("dir") if "dir" in cols else ""),
            ))
        conn.executemany(
            "INSERT INTO records(upload_id,seq,time,amount,counterparty,type,remark,method,direction) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        return uid
    finally:
        conn.close()


init_db()


# ---------------- 鉴权辅助 ----------------
def _authed():
    return bool(session.get("authed"))


# ---------------- 路由 ----------------
@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE, "static"), "index.html")


@app.route("/api/me")
def me():
    return jsonify(authed=_authed())


@app.route("/api/login", methods=["POST"])
def login():
    pw = (request.form.get("password") or request.json.get("password")
          if request.is_json else request.form.get("password") or "").strip()
    if check_pw(pw):
        session["authed"] = True
        return jsonify(ok=True)
    return jsonify(ok=False, error="密码错误。"), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify(ok=True)


@app.route("/api/analyze", methods=["POST"])
def analyze():
    if not _authed():
        return jsonify(ok=False, error="请先输入访问密码。"), 401

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="请上传流水文件（PDF / CSV / XLSX）。"), 400
    data = f.read()
    if not data:
        return jsonify(ok=False, error="文件为空。"), 400
    if len(data) > MAX_CONTENT:
        return jsonify(ok=False, error="文件过大（上限 30MB）。"), 413

    args = {
        "global_min": float(request.form.get("globalMin", 300) or 300),
        "large": float(request.form.get("large", 50000) or 50000),
        "ignore": "充值,提现,信用卡还款" if request.form.get("ignoreChk") else "",
        "skip_layering": True,
    }
    try:
        # 分析在内存完成；df_full（过滤前完整明细）用于落库收集
        html, df_full = m.run_bytes_full(data, f.filename, args)
        if df_full is not None and len(df_full):
            store_records(df_full, f.filename, request.remote_addr or "")
    except Exception as e:
        return jsonify(ok=False, error="分析失败：" + str(e)), 500

    return jsonify(ok=True, html=html,
                   collected=len(df_full) if df_full is not None else 0)


@app.route("/api/stats")
def stats():
    if not _authed():
        return jsonify(ok=False), 401
    conn = sqlite3.connect(DB_FILE)
    try:
        up = conn.execute("SELECT COUNT(*), COALESCE(SUM(rows),0) FROM uploads").fetchone()
        rec = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return jsonify(ok=True, uploads=up[0], uploads_rows=up[1], records=rec)
    finally:
        conn.close()


@app.route("/api/export")
def export():
    if not _authed():
        return jsonify(ok=False), 401
    conn = sqlite3.connect(DB_FILE)
    try:
        out = io.StringIO()
        w = __import__("csv").writer(out)
        w.writerow(["upload_id", "upload_ts", "seq", "time", "amount",
                    "counterparty", "type", "remark", "method", "direction"])
        for row in conn.execute(
                """SELECT r.upload_id, u.ts, r.seq, r.time, r.amount,
                          r.counterparty, r.type, r.remark, r.method, r.direction
                   FROM records r JOIN uploads u ON u.id = r.upload_id
                   ORDER BY r.upload_id, r.seq"""):
            w.writerow(row)
        csv_text = out.getvalue()
    finally:
        conn.close()
    return Response(
        csv_text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=collected_flow.csv"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
