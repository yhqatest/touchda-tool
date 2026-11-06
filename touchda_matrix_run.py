#!/usr/bin/env python3
"""按优惠类型批量跑用户触达建单 —— 回归矩阵「6 种优惠 x N」那一维的自动化入口。

逐个调用 touchda_run.py，每种优惠类型一轮，收集结果并输出汇总。

默认 **直接发布**（2026-09-30 用户定：所有触达脚本都直接发布）。
只想建到发布前一步看看表单，加 --no-publish。

用法
    python3 touchda_matrix_run.py                     # 全部 7 种，只建不发（默认）
    python3 touchda_matrix_run.py --publish           # 全部 7 种，真发布
    python3 touchda_matrix_run.py fs freeplay         # 只跑指定类型
    python3 touchda_matrix_run.py --skip coupon_pct   # 排除某几种
    python3 touchda_matrix_run.py --tag " R2"         # 任务名加后缀，便于区分批次

模板类环境变量（没配的类型会被跳过并在汇总里标出来；没传的从 config.json 取）
    TOUCHDA_COUPON_TEMPLATE       优惠代码（金额）模板名
    TOUCHDA_COUPON_PCT_TEMPLATE   优惠代码（百分比）模板名
    TOUCHDA_FS_TEMPLATE           FreeSpin 模板名
    TOUCHDA_FREEPLAY_TEMPLATE     Freeplay 模板名
    TOUCHDA_MUD_DEPOSIT_TEMPLATE  泥码存送券模板名
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import touchda_config

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "touchda_run.py"

# 顺序刻意这样排：不依赖外部模板的排前面，先跑完能立刻看出环境通不通。
ORDER = [
    ("mud",         "泥码彩金",          None),
    ("credit",      "免费彩金",          None),
    ("coupon",      "优惠代码（金额）",   "TOUCHDA_COUPON_TEMPLATE"),
    ("coupon_pct",  "优惠代码（百分比）", "TOUCHDA_COUPON_PCT_TEMPLATE"),
    ("fs",          "FreeSpin 彩金",     "TOUCHDA_FS_TEMPLATE"),
    ("freeplay",    "Freeplay 彩金",     "TOUCHDA_FREEPLAY_TEMPLATE"),
    ("mud_deposit", "泥码存送券",        "TOUCHDA_MUD_DEPOSIT_TEMPLATE"),
]
# 个人模版名不写在代码里：没传环境变量就从 config.json 取
touchda_config.apply_env_defaults()
DEFAULTS: dict = {}


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("types", nargs="*", help="只跑这几种；留空则跑全部 7 种")
    # 默认只建不发：以前默认真发布，GUI 的「只建不发」按钮不带参数调用，结果真发了
    ap.add_argument("--publish", dest="publish", action="store_true", default=False,
                    help="真发布（默认只建到发布前一步）")
    ap.add_argument("--no-publish", dest="publish", action="store_false", help=argparse.SUPPRESS)  # 兼容旧用法
    ap.add_argument("--skip", action="append", default=[], help="排除某个类型，可重复")
    ap.add_argument("--tag", default="", help="任务名后缀，例如 ' R2'")
    ap.add_argument("--stop-on-fail", action="store_true", help="第一个失败就停")
    args = ap.parse_args()

    known = {t for t, _, _ in ORDER}
    for t in list(args.types) + list(args.skip):
        if t not in known:
            print(f"ERR: 未知优惠类型 {t!r}；可选：{', '.join(sorted(known))}", file=sys.stderr)
            return 2

    plan = [r for r in ORDER if (not args.types or r[0] in args.types) and r[0] not in args.skip]
    if not plan:
        print("没有要跑的类型", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"用户触达 · 优惠类型矩阵  |  {len(plan)} 种  |  "
          f"{'真发布' if args.publish else '只建不发'}  |  {stamp}\n")

    results = []
    for i, (key, cn, env_key) in enumerate(plan, 1):
        tpl = os.environ.get(env_key, DEFAULTS.get(env_key, "")) if env_key else ""
        if env_key and not tpl:
            print(f"[{i}/{len(plan)}] {cn:<18} SKIP — 未设置 {env_key}")
            results.append((key, cn, "SKIP", 0, f"未设置 {env_key}"))
            continue

        env = dict(os.environ)
        env["TOUCHDA_REWARD_TYPE"] = key
        # 只建不发 = 填完 1-7 步截图就收工（STOP_BEFORE_PUBLISH）。别用 AUTO_PUBLISH=0：
        # 那是「留标签页等人手点发布」，每种类型都会干等 15 分钟。
        env["TOUCHDA_AUTO_PUBLISH"] = "1"
        if not args.publish:
            env["TOUCHDA_STOP_BEFORE_PUBLISH"] = "1"
        if args.tag:
            env["TOUCHDA_TASK_SUFFIX"] = args.tag
        if env_key:
            env[env_key] = tpl

        print(f"[{i}/{len(plan)}] {cn:<18} 开始"
              + (f"  (模板 {tpl!r})" if tpl else ""))
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, str(RUNNER)],
            env=env, cwd=str(HERE),
            capture_output=True, text=True,
        )
        dur = time.time() - t0
        ok = proc.returncode == 0
        if ok:
            # PASS 时备注放任务名 + 回查到的任务号/状态，省得再去列表里翻
            out_lines = (proc.stdout or "").splitlines()
            name = next((l[3:] for l in out_lines if l.startswith("OK:")), "")
            st = next((l[7:] for l in out_lines if l.startswith("STATUS:")), "")
            try:
                st = json.loads(st) if st else {}
            except ValueError:
                st = {}
            note = name + (f"  {st.get('taskId')} {st.get('status')}" if st else "  (列表未回查到)")
        else:
            # 优先取 ERR: 行；以前取最后一行，常常只拿到一条 ==== 分隔线
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()
            errs = [l for l in tail if l.startswith("ERR:")]
            note = (errs[-1] if errs else (tail[-1] if tail else ""))[:150]
        results.append((key, cn, "PASS" if ok else "FAIL", dur, note))
        print(f"      {'PASS' if ok else 'FAIL'}  {dur:.0f}s  {note}")
        if not ok and args.stop_on_fail:
            print("\n--stop-on-fail：中断")
            break

    print("\n" + "=" * 74)
    print(f"{'优惠类型':<20}{'结果':<8}{'耗时':>7}   备注")
    print("-" * 74)
    for _, cn, st, dur, note in results:
        print(f"{cn:<20}{st:<8}{dur:>6.0f}s   {note[:60]}")
    n_pass = sum(1 for r in results if r[2] == "PASS")
    n_fail = sum(1 for r in results if r[2] == "FAIL")
    n_skip = sum(1 for r in results if r[2] == "SKIP")
    print("-" * 74)
    print(f"PASS {n_pass}   FAIL {n_fail}   SKIP {n_skip}   合计 {len(results)}")

    out = HERE / f"touchda_matrix_{stamp}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# 用户触达 · 优惠类型矩阵跑批 {stamp}\n\n")
        f.write(f"- 模式：{'真发布' if args.publish else '只建不发'}\n")
        f.write(f"- 结果：PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}\n\n")
        f.write("| 优惠类型 | 结果 | 耗时 | 备注 |\n|---|---|---|---|\n")
        for _, cn, st, dur, note in results:
            f.write(f"| {cn} | {st} | {dur:.0f}s | {note.replace('|', '/')[:90]} |\n")
    print(f"\n汇总已写入 {out.name}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
