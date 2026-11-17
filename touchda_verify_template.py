#!/usr/bin/env python3
"""校验某个优惠模板在后台是否真的存在 —— 免得填错名字要等一整轮跑到 modal 才失败。

只读：打开创建触达任务页、选优惠类型、开选择弹窗、查列表里有没有这个名字，然后关掉标签页。
不建单、不发布、不保存。

用法
    python3 touchda_verify_template.py <reward_type> <template_name>
    python3 touchda_verify_template.py coupon <你的优惠代码模版名>

退出码
    0 = 找到     1 = 没找到（会打印列表里实际有什么）    2 = 参数/环境错误
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

from chrome_cdp import connect_cdp
import touchda_config

touchda_config.apply_env_defaults()

CDP_URL = os.environ.get("TOUCHDA_CDP_URL", "http://localhost:9222")
CAMPAIGN_URL = os.environ.get(
    "TOUCHDA_CAMPAIGN_URL", "https://mgnt-webserver.platform99.me/campaign"
)

# reward_type -> (下拉候选文案, 选择按钮候选文案)
PICKERS = {
    "coupon":      (["优惠代码"], ["选择优惠代码"]),
    "coupon_pct":  (["优惠代码"], ["选择优惠代码"]),
    "fs":          (["FreeSpin"], ["选择FreeSpin"]),
    "mud":         (["泥码彩金"], ["选择泥码"]),
    "mud_deposit": (["泥码存送券"], ["选择泥码存送券"]),
    "freeplay":    (["Freeplay彩金", "Freeplay", "FreePlay", "Free Play", "免费游戏"],
                    ["选择Freeplay", "选择FreePlay", "选择Free Play", "选择免费游戏"]),
    # 免费彩金是填金额不是选模板，没有可校验的模板
}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    rtype, name = sys.argv[1], sys.argv[2].strip()
    if rtype == "credit":
        print("credit（免费彩金）是填金额，没有模板可校验")
        return 0
    if rtype not in PICKERS:
        print(f"未知类型 {rtype!r}；可选：{', '.join(sorted(PICKERS))}", file=sys.stderr)
        return 2
    if not name:
        print("模板名为空", file=sys.stderr)
        return 2
    type_labels, btn_labels = PICKERS[rtype]
    # touchda_run 在 import 时按 TOUCHDA_REWARD_TYPE 校验类型，先垫好再 import
    os.environ["TOUCHDA_REWARD_TYPE"] = rtype
    import touchda_run as TR

    with sync_playwright() as p:
        try:
            browser = connect_cdp(p, CDP_URL, timeout=20000, allow_restart=False,
                                  log=lambda *a: None)
        except Exception as exc:
            print(f"连不上 Chrome {CDP_URL}：{exc}", file=sys.stderr)
            return 2
        page = browser.contexts[0].new_page()
        try:
            page.goto(CAMPAIGN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            if TR._looks_like_login_page(page) or "loginRequired=yes" in page.url:
                print("看起来没登录 mgnt-webserver", file=sys.stderr)
                return 2

            page.evaluate(
                """() => {const el = Array.from(document.querySelectorAll('a,button,li,span'))
                    .filter(e => e.offsetParent !== null)
                    .find(e => (e.textContent||'').trim() === '创建触达任务'); if (el) el.click();}"""
            )
            page.wait_for_timeout(3500)
            # 填到能露出分组行为止
            page.evaluate(
                """() => {
                    const setv = (s, v) => {const e = document.querySelector(s); if (!e) return;
                        e.value = v; e.dispatchEvent(new Event('input', {bubbles:true}));
                        e.dispatchEvent(new Event('change', {bubbles:true}));
                        try { angular.element(e).scope().$apply(); } catch (x) {}};
                    setv('input[ng-model="vm.newCampaign.name"]', 'verify-template-readonly');
                    setv('input[ng-model="vm.newCampaign.remark"]', 'verify');
                    setv('input[ng-model="vm.newCampaign.statisticDateNumber"]', '1');
                    const t = document.querySelector('select[ng-model="vm.newCampaign.type"]');
                    if (t) {const o = Array.from(t.options).find(o => o.textContent.trim() === '流失召回');
                        if (o) {t.value = o.value; t.dispatchEvent(new Event('change', {bubbles:true}));
                            try { angular.element(t).scope().$apply(); } catch (x) {}}}
                    const sm = document.querySelector('input[ng-model="vm.newCampaign.sendStrategyMode"]');
                    if (sm) sm.click();
                }"""
            )
            page.wait_for_timeout(1400)
            page.evaluate(
                """() => {const r = document.querySelector(
                    'input[ng-model="vm.newCampaign.oneTimePushTimeMode"]'); if (r) r.click();}"""
            )
            page.wait_for_timeout(900)
            page.evaluate(
                """() => {const rs = Array.from(document.querySelectorAll(
                    'input[ng-model="vm.newCampaign.targetGroupConfig.strategy"]'));
                    const pick = rs.find(r => ((r.closest('label')||{}).innerText||'').includes('纯回访'))
                                 || rs[1] || rs[0]; if (pick) pick.click();}"""
            )
            page.wait_for_timeout(2000)

            # 下拉 / 选择按钮 / 读模板行全部复用 touchda_run，别再各写一份（旧写法停在 8 月改版前，误报过「列表是空的」）
            try:
                TR.select_by_label_any(page, 'select[ng-model="group.rewardType"]', type_labels)
                page.wait_for_timeout(2000)
                btn = TR._open_reward_modal(page, btn_labels)
                names = TR._reward_rows(page, btn)
            except Exception as exc:   # RuntimeError（没这类型/没模板）或 Playwright 超时
                print(exc, file=sys.stderr)
                return 1
            hit = any(name in n for n in names)
            print(f"{'✓ 找到' if hit else '✗ 没找到'}  {name!r}   （{btn} 列表共 {len(names)} 项）")
            if not hit:
                print("列表里实际有：")
                for n in names[:40]:
                    print(f"    {n}")
                if len(names) > 40:
                    print(f"    …… 还有 {len(names)-40} 项")
            return 0 if hit else 1
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
