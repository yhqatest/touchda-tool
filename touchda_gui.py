#!/usr/bin/env python3
"""用户触达 · 派发小工具（单窗口）。

贴 player id → 选优惠类型 → 派发。底层跑 touchda_run.py：CDP 接到你本机的专用 Chrome
（:9222，已登录 mgnt-webserver），在后台「创建触达任务」里填表；默认填完停在发布前，
由你在 Chrome 里自己点「发布任务」，勾了「直接发布」才会自动发。

Tk 规矩：子线程一律只往 self.q 投消息，所有控件操作在主线程 _drain 里做——
Tk 9.0 下子线程碰 Tk 不报错但静默失效，甚至直接崩。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText
from urllib.parse import quote, urlsplit, urlunsplit

import touchda_config

HERE = Path(__file__).resolve().parent
VERSION = (HERE / "VERSION").read_text(encoding="utf-8").strip() if (HERE / "VERSION").exists() else "dev"
LOG_PATH = HERE / "touchda_dispatch.log"
CDP = "http://localhost:9222"

ENV_DOMAINS = {"uat": "platform99.me", "qat": "platform88.me"}

REWARD_OPTIONS = (
    ("mud",         "派泥码（泥码彩金）",          "mud_event_keyword"),
    ("credit",      "派彩金（免费彩金）",          None),
    ("coupon",      "派优惠券 · 金额（优惠代码）",  "coupon_template"),
    ("coupon_pct",  "派优惠券 · 百分比（优惠代码）", "coupon_pct_template"),
    ("mud_deposit", "派泥码存送（泥码存送券）",     "mud_deposit_template"),
    ("fs",          "派 FS（FreeSpin）",          "fs_template"),
    ("freeplay",    "派 Freeplay", "freeplay_template"),
)
# (config 键, 标签, 宽度)
TEMPLATE_FIELDS = (
    ("mud_event_keyword",     "泥码 · 事件关键词",          36),
    ("mud_display_name",      "泥码 · 玩家端显示名(英文数字)", 36),
    ("credit_amount",         "彩金 · 单笔金额",            10),
    ("credit_validity_hours", "彩金 · 有效期(小时)",         10),
    ("coupon_template",       "优惠券(金额) · 模版名",       36),
    ("coupon_pct_template",   "优惠券(百分比) · 模版名",     36),
    ("mud_deposit_template",  "泥码存送 · 模版名",           36),
    ("fs_template",           "FS · 模版名",               36),
    ("freeplay_template",     "Freeplay · 模版名",          36),
)
POPUP_OPTIONS = (("general", "通用弹窗"), ("configured", "配置弹窗（纯图片）"), ("none", "无弹窗"))


def campaign_url(mode: str, lane: str, domain: str) -> str:
    """UAT/QAT 走主站；泳道拼成 https://<泳道>-mgnt-web.<域名>/campaign。
    泳道框里粘了整条链接也认。泳道名为空返回 ""（调用方据此拒跑）。"""
    if mode in ENV_DOMAINS:
        return f"https://mgnt-webserver.{ENV_DOMAINS[mode]}/campaign"
    lane = (lane or "").strip().lower()
    if not lane:
        return ""
    if "://" in lane or "." in lane:
        p = urlsplit(lane if "://" in lane else f"https://{lane}")
        return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/") or "/campaign", "", ""))
    return f"https://{lane}-mgnt-web.{domain or ENV_DOMAINS['uat']}/campaign"


def extract_player_ids(raw: str) -> list[str]:
    """从随便粘的文本里抠 player id：只要出现了 a 开头的就只认 a 开头的
    （免得表格序号、手机号被当成 id）；整段都没有才退回认纯数字。"""
    edge = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"
    toks = re.findall(edge.format(r"[aA]\d+"), raw) or re.findall(edge.format(r"\d+"), raw)
    out, seen = [], set()
    for t in toks:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.cfg = touchda_config.load()
        root.title(f"用户触达 · 派发小工具  v{VERSION}")
        root.geometry("900x900")

        outer = tk.Frame(root)
        outer.pack(fill="both", expand=True, padx=12, pady=10)

        # ---- 测试环境 + Chrome
        env = tk.LabelFrame(outer, text="测试环境")
        env.pack(fill="x")
        self.mode = tk.StringVar(value=self.cfg.get("env_mode", "uat"))
        self.lane = tk.StringVar(value=self.cfg.get("env_lane", ""))
        self.domain = tk.StringVar(value=self.cfg.get("env_domain", ENV_DOMAINS["uat"]))
        r = tk.Frame(env); r.pack(fill="x", padx=8, pady=4)
        for val, lbl in (("uat", "UAT（platform99）"), ("qat", "QAT（platform88）"), ("manual", "泳道")):
            tk.Radiobutton(r, text=lbl, value=val, variable=self.mode).pack(side="left")
        tk.Label(r, text="泳道名").pack(side="left", padx=(12, 2))
        tk.Entry(r, textvariable=self.lane, width=24).pack(side="left")
        tk.OptionMenu(r, self.domain, *ENV_DOMAINS.values()).pack(side="left", padx=4)
        self.url_var = tk.StringVar()
        tk.Label(env, textvariable=self.url_var, fg="#555", anchor="w").pack(fill="x", padx=8)
        for v in (self.mode, self.lane, self.domain):
            v.trace_add("write", lambda *_: self._env_changed())

        cr = tk.Frame(env); cr.pack(fill="x", padx=8, pady=(2, 6))
        self.chrome_var = tk.StringVar(value="● Chrome 检测中…")
        self.chrome_lbl = tk.Label(cr, textvariable=self.chrome_var, fg="#999")
        self.chrome_lbl.pack(side="left")
        tk.Button(cr, text="启动专用 Chrome", command=self.on_start_chrome).pack(side="left", padx=(12, 2))
        tk.Button(cr, text="在专用 Chrome 打开后台", command=self.on_open_backend).pack(side="left", padx=2)

        # ---- player id
        ids = tk.LabelFrame(outer, text="Player IDs（一行一个，或空格/逗号分隔，直接从表格粘也行）")
        ids.pack(fill="x", pady=(8, 0))
        self.ids_text = ScrolledText(ids, height=5, font=("Menlo", 12))
        self.ids_text.pack(fill="x", padx=8, pady=6)

        # ---- 类型 + 模版
        mid = tk.Frame(outer); mid.pack(fill="x", pady=(8, 0))
        tf = tk.LabelFrame(mid, text="优惠类型")
        tf.pack(side="left", fill="y")
        self.rtype = tk.StringVar(value=self.cfg.get("last_reward_type", "mud"))
        for code, lbl, _k in REWARD_OPTIONS:
            tk.Radiobutton(tf, text=lbl, value=code, variable=self.rtype, anchor="w").pack(anchor="w", padx=6)

        tpl = tk.LabelFrame(mid, text="我的模版（存在 config.json，改完自动保存）")
        tpl.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.tpl_vars: dict[str, tk.StringVar] = {}
        for key, lbl, w in TEMPLATE_FIELDS:
            row = tk.Frame(tpl); row.pack(fill="x", padx=6, pady=1)
            tk.Label(row, text=lbl, width=24, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(self.cfg.get(key, "") or ""))
            var.trace_add("write", lambda *_: self._save_cfg())
            tk.Entry(row, textvariable=var, width=w).pack(side="left")
            self.tpl_vars[key] = var

        # ---- 弹窗 / 内容 / 发布方式
        opt = tk.Frame(outer); opt.pack(fill="x", pady=(8, 0))
        pf = tk.LabelFrame(opt, text="弹窗配置"); pf.pack(side="left")
        self.popup = tk.StringVar(value=self.cfg.get("popup_type", "general"))
        for val, lbl in POPUP_OPTIONS:
            tk.Radiobutton(pf, text=lbl, value=val, variable=self.popup).pack(side="left", padx=4)
        cf = tk.LabelFrame(opt, text="触达内容（全不勾=未配置）"); cf.pack(side="left", padx=(8, 0))
        saved_ch = set(self.cfg.get("content_channels", []))
        self.ch_vars = {}
        for code, lbl in (("sms", "短信"), ("inapp", "站内信"), ("email", "邮件")):
            v = tk.BooleanVar(value=code in saved_ch)
            tk.Checkbutton(cf, text=lbl, variable=v).pack(side="left", padx=4)
            self.ch_vars[code] = v

        act = tk.Frame(outer); act.pack(fill="x", pady=(10, 0))
        self.send_btn = tk.Button(act, text="派发", width=10, command=self.on_dispatch)
        self.send_btn.pack(side="left")
        # 默认不勾：填完停在发布前，人去 Chrome 里看一眼再点「发布任务」
        self.auto_publish = tk.BooleanVar(value=False)
        tk.Checkbutton(act, text="直接发布（不勾＝填完停在发布前，你在 Chrome 里自己点「发布任务」）",
                       variable=self.auto_publish, fg="#b00020").pack(side="left", padx=8)

        tools = tk.Frame(outer); tools.pack(fill="x", pady=(6, 0))
        tk.Button(tools, text="验证当前模版", command=self.on_verify).pack(side="left")
        tk.Button(tools, text="跑全部类型（只建不发）", command=self.on_matrix).pack(side="left", padx=6)
        tk.Button(tools, text="全派（直接发布）", command=self.on_dispatch_all).pack(side="left", padx=(0, 6))
        tk.Button(tools, text="检查更新", command=self.on_update).pack(side="left")
        self.status = tk.StringVar(value="就绪")
        tk.Label(tools, textvariable=self.status, fg="#2e7d32").pack(side="left", padx=12)

        lf = tk.LabelFrame(outer, text="日志（同时写进 touchda_dispatch.log）")
        lf.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = ScrolledText(lf, font=("Menlo", 11), state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

        for v in (self.mode, self.lane, self.domain, self.rtype, self.popup, *self.ch_vars.values()):
            v.trace_add("write", lambda *_: self._save_cfg())
        self._env_changed()
        self._drain()
        self._poll_chrome()
        if not touchda_config.CONFIG_PATH.exists():
            self._save_cfg()
            self.log("第一次用：先在右边「我的模版」填你自己在后台建的模版名，再派发。")

    # ------------------------------------------------------------ 基础
    def log(self, text: str) -> None:
        """线程安全：子线程也能调。"""
        self.q.put(("log", text))
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}\n")
        except Exception:
            pass

    def _drain(self) -> None:
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", rest[0] + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status.set(rest[0])
                elif kind == "done":
                    self.busy = False
                    self.send_btn.configure(state="normal")
                    self.status.set(rest[0])
                elif kind == "chrome":
                    self._show_chrome(*rest)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _env_changed(self) -> None:
        url = self.url()
        self.url_var.set(f"后台地址：{url}" if url else "后台地址：（先填泳道名）")

    def url(self) -> str:
        return campaign_url(self.mode.get(), self.lane.get(), self.domain.get())

    def _save_cfg(self) -> None:
        cfg = touchda_config.load()
        cfg.update({k: v.get().strip() for k, v in self.tpl_vars.items()} if hasattr(self, "tpl_vars") else {})
        if hasattr(self, "ch_vars"):
            cfg.update({
                "env_mode": self.mode.get(), "env_lane": self.lane.get().strip(),
                "env_domain": self.domain.get(), "last_reward_type": self.rtype.get(),
                "popup_type": self.popup.get(),
                "content_channels": [c for c, v in self.ch_vars.items() if v.get()],
            })
        try:
            touchda_config.save(cfg)
        except Exception as e:
            self.status.set(f"配置保存失败：{e}")

    def env_overrides(self) -> dict:
        """主线程快照：子线程不许读 Tk 变量。"""
        env = {touchda_config.ENV_KEYS[k]: v.get().strip()
               for k, v in self.tpl_vars.items() if v.get().strip()}
        env.update({
            "TOUCHDA_CAMPAIGN_URL": self.url(),
            "TOUCHDA_POPUP_TYPE": self.popup.get(),
            "TOUCHDA_CONTENT_CHANNELS": ",".join(c for c, v in self.ch_vars.items() if v.get()),
            "PYTHONUNBUFFERED": "1",
        })
        return env

    # ------------------------------------------------------------ Chrome
    def _poll_chrome(self) -> None:
        def probe():
            try:
                with urllib.request.urlopen(f"{CDP}/json/list", timeout=3) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.q.put(("chrome", True, [t.get("url", "") for t in data if t.get("type") == "page"]))
            except Exception:
                self.q.put(("chrome", False, []))
        threading.Thread(target=probe, daemon=True).start()
        self.root.after(10000, self._poll_chrome)

    def _show_chrome(self, alive: bool, urls: list) -> None:
        host = urlsplit(self.url()).netloc
        if not alive:
            text, color = "● 专用 Chrome 没开（点「启动专用 Chrome」）", "#b00020"
        elif host and any(urlsplit(u).netloc == host for u in urls):
            text, color = f"● 就绪 · 已打开 {host}", "#1a7f37"
        else:
            text, color = "● Chrome 在，但没打开这个环境的后台（点「在专用 Chrome 打开后台」并登录）", "#9a6700"
        self.chrome_var.set(text)
        self.chrome_lbl.configure(fg=color)

    def on_start_chrome(self) -> None:
        def work():
            p = subprocess.run(["/bin/zsh", str(HERE / "touchda_chrome.sh")], capture_output=True, text=True)
            self.log((p.stdout or p.stderr).strip() or "已启动")
            self.q.put(("status", "专用 Chrome 已启动，记得在里面登录后台" if p.returncode == 0 else "Chrome 启动失败"))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(3000, self._poll_chrome)

    def on_open_backend(self) -> None:
        url = self.url()
        if not url:
            messagebox.showerror("缺泳道名", "选了「泳道」就要填泳道名。")
            return

        def work():
            try:
                req = urllib.request.Request(f"{CDP}/json/new?" + quote(url, safe=""), method="PUT")
                urllib.request.urlopen(req, timeout=6).read()
                self.log(f"已在专用 Chrome 打开 {url}（没登录的话先登录）")
            except Exception as e:
                self.log(f"打开失败（专用 Chrome 开了吗？）：{e}")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 派发
    def _check_template(self, rtype: str) -> bool:
        key = dict((c, k) for c, _l, k in REWARD_OPTIONS).get(rtype)
        if key and not self.tpl_vars[key].get().strip():
            messagebox.showerror("模版没填", f"这个类型要先在「我的模版」里填：{dict((k, l) for k, l, _w in TEMPLATE_FIELDS)[key]}")
            return False
        return True

    def on_dispatch(self) -> None:
        if self.busy:
            return
        if not self.url():
            messagebox.showerror("缺泳道名", "选了「泳道」就要填泳道名。")
            return
        ids = extract_player_ids(self.ids_text.get("1.0", "end"))
        if not ids:
            messagebox.showerror("没有 player id", "没识别出 player id（a 开头 + 数字，或纯数字）。")
            return
        rtype = self.rtype.get()
        if not self._check_template(rtype):
            return
        label = dict((c, l) for c, l, _k in REWARD_OPTIONS)[rtype]
        publish = self.auto_publish.get()
        preview = "\n".join(ids[:15]) + (f"\n…（另 {len(ids) - 15} 个）" if len(ids) > 15 else "")
        how = "⚠️ 会直接发布，奖励会真的发给这些玩家" if publish else "填完停在发布前，由你在 Chrome 里点「发布任务」"
        if not messagebox.askyesno("确认派发", f"环境：{self.url()}\n类型：{label}\n{len(ids)} 个玩家：\n{preview}\n\n{how}。确认？"):
            return
        self.busy = True
        self.send_btn.configure(state="disabled")
        self.status.set(f"派发中…（{label}，{len(ids)} 个）")
        self.log("=" * 60)
        self.log(f"派发：{label} · {len(ids)} 个 · {'直接发布' if publish else '停在发布前'} · {self.url()}")
        env = self.env_overrides()
        env.update({"TOUCHDA_BRIDGE": "1", "TOUCHDA_REWARD_TYPE": rtype,
                    "TOUCHDA_AUTO_PUBLISH": "1" if publish else "0"})
        threading.Thread(target=self._dispatch_bg, args=(ids, env, label), daemon=True).start()

    # 「全派」覆盖的类型（Voucher / Credit Voucher / Reward Credit / Freespin / Freeplay）。
    # 免费彩金、百分比优惠券不在里面；没填模版的由 touchda_matrix_run.py 跳过。
    DISPATCH_ALL = ("coupon", "mud_deposit", "mud", "fs", "freeplay")

    def on_dispatch_all(self) -> None:
        if self.busy:
            return
        if not self.url():
            messagebox.showerror("缺泳道名", "选了「泳道」就要填泳道名。")
            return
        ids = extract_player_ids(self.ids_text.get("1.0", "end"))
        if not ids:
            messagebox.showerror("没有 player id", "没识别出 player id（a 开头 + 数字，或纯数字）。")
            return
        names = dict((c, l) for c, l, _k in REWARD_OPTIONS)
        kinds = " / ".join(names.get(c, c) for c in self.DISPATCH_ALL)
        preview = "\n".join(ids[:15]) + (f"\n…（另 {len(ids) - 15} 个）" if len(ids) > 15 else "")
        if not messagebox.askyesno("确认全派", f"环境：{self.url()}\n依次派：{kinds}\n{len(ids)} 个玩家：\n{preview}\n\n"
                                              "⚠️ 每种都直接发布，奖励会真的发给这些玩家。确认？"):
            return
        self.busy = True
        self.send_btn.configure(state="disabled")
        self.status.set(f"全派中…（{len(ids)} 个）")
        self.log("=" * 60)
        self.log(f"全派：{kinds} · {len(ids)} 个 · {self.url()}")
        args = [str(HERE / "touchda_matrix_run.py"), *self.DISPATCH_ALL, "--publish"]
        threading.Thread(target=self._dispatch_bg, args=(ids, self.env_overrides(), "全派", args),
                         daemon=True).start()

    def _dispatch_bg(self, ids: list[str], extra_env: dict, label: str,
                     args: list[str] | None = None) -> None:
        status = "派发线程异常退出"
        try:
            up = touchda_config.upload_dir()
            up.mkdir(parents=True, exist_ok=True)
            others = [p for p in up.glob("*.xlsx") if p.name != "player_id.xlsx"]
            if others:
                status = f"{up} 里还有别的 xlsx（{others[0].name}），脚本只认一个，先挪走"
                self.log("✗ " + status)
                return
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.cell(row=1, column=1, value="player_id")
            for i, pid in enumerate(ids, start=2):
                ws.cell(row=i, column=1, value=pid)
            wb.save(up / "player_id.xlsx")
            self.log(f"✓ 名单已写入 {up / 'player_id.xlsx'}")

            env = os.environ.copy()
            env.update(extra_env)
            proc = subprocess.Popen([sys.executable, *(args or [str(HERE / "touchda_run.py")])],
                                    cwd=str(HERE), env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            ok, err, info, ready = False, "", None, False
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                self.log(line)
                if line.startswith("OK:READY_FOR_PUBLISH"):
                    ready = True
                    self.q.put(("status", "表已填好 → 去专用 Chrome 里检查并点「发布任务」（最多等 15 分钟）"))
                elif line.startswith("OK:"):
                    ok = True
                elif line.startswith("ERR:"):
                    err = line
                elif line.startswith("STATUS:"):
                    try:
                        info = json.loads(line[7:])
                    except Exception:
                        pass
            proc.wait()
            if args:   # 全派走矩阵脚本，逐类结果已在日志汇总表里
                status = ("🎉 全派完成" if proc.returncode == 0
                          else f"全派有失败项（code={proc.returncode}），看日志汇总")
            elif ok and info:
                status = (f"🎉 已发布（{label}）· 上传 {info.get('hitCount', '?')} · "
                          f"验证 {info.get('actualHitCount', '?')} · 送达 {info.get('deliverySuccess', '?')}")
            elif ok:
                status = f"🎉 已发布（{label}）"
            elif ready:
                status = "等你点发布超时了（任务没发布）"
            else:
                status = err or f"❌ 脚本退出（code={proc.returncode}），看日志"
        except Exception as e:
            status = f"派发出错：{e}"
            self.log("✗ " + status)
        finally:
            self.q.put(("done", status))

    # ------------------------------------------------------------ 工具
    def _run_tool(self, args: list[str], label: str, extra_env: dict | None = None) -> None:
        env = os.environ.copy()
        env.update(extra_env or {})

        def work():
            self.log(f"— {label} —")
            p = subprocess.run([sys.executable, *args], cwd=str(HERE), env=env, capture_output=True, text=True)
            for line in ((p.stdout or "") + (p.stderr or "")).splitlines():
                self.log("  " + line)
            self.q.put(("status", f"{label}：{'通过 ✓' if p.returncode == 0 else '没通过 ✗（看日志）'}"))
        threading.Thread(target=work, daemon=True).start()

    def on_verify(self) -> None:
        rtype = self.rtype.get()
        key = dict((c, k) for c, _l, k in REWARD_OPTIONS).get(rtype)
        if not key:
            messagebox.showinfo("不用验证", "免费彩金填的是金额，没有模版可验证。")
            return
        if not self._check_template(rtype):
            return
        self._run_tool([str(HERE / "touchda_verify_template.py"), rtype, self.tpl_vars[key].get().strip()],
                       f"验证模版 {rtype}", self.env_overrides())

    def on_matrix(self) -> None:
        if not messagebox.askyesno("只建不发", "会依次把每种（填了模版的）优惠类型填一遍表，"
                                              "停在发布前、不发布。需要几分钟，继续？"):
            return
        self._run_tool([str(HERE / "touchda_matrix_run.py")], "全部类型（只建不发）", self.env_overrides())

    def on_update(self) -> None:
        def work():
            def git(*a):
                return subprocess.run(["git", *a], cwd=str(HERE), capture_output=True, text=True)
            if git("fetch", "--quiet").returncode != 0:
                self.q.put(("status", "检查更新失败（网络？还是不是用 git clone 装的？）"))
                return
            behind = git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
            if behind in ("", "0"):
                self.q.put(("status", f"已经是最新版 v{VERSION}"))
                return
            p = git("pull", "--ff-only")
            self.log(p.stdout + p.stderr)
            self.q.put(("status", "已更新，关掉窗口重开就是新版" if p.returncode == 0
                        else "自动更新失败（你改过代码？），看日志"))
        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
