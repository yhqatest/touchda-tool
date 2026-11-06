#!/usr/bin/env python3
"""Touchda task automation — Playwright replay of create-touchda-task skill.

Always attaches to the user's dedicated Chrome via CDP (localhost:9222).
With TOUCHDA_AUTO_PUBLISH=1 (default) it runs steps 1-9 end-to-end and
publishes. With =0 it stops after step 7 and holds the tab so the user
can click 发布任务 by hand; it posts the result back to Lark via lark-cli
when the task appears in the list.

默认在专用 Chrome 的可见标签页里跑（2026-10-06 起；无头太慢，还会出现 /campaign 白屏、
找不到「创建触达任务」）。TOUCHDA_HEADLESS=1 才借登录态（cookie + localStorage，见
chrome_cdp.headless_context）在无头 Chrome 里跑。发布前截一张表单图存
.snapshots/touchda_prepublish_<类型>_<时间>.png，bot 触发时一并发回群里留底。手动发布（AUTO_PUBLISH=0）必须有人点，
所以永远走可见标签页，无视 TOUCHDA_HEADLESS。
"""
import os
import json
import time
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from chrome_cdp import connect_cdp, headless_context, host_of
import touchda_config

# config.json 里的个人模版/目录先垫成环境变量默认值（调用方传的环境变量优先）
touchda_config.apply_env_defaults()

BRIDGE_MODE = os.environ.get("TOUCHDA_BRIDGE") == "1"
BRIDGE_HOLD_SECS = int(os.environ.get("TOUCHDA_BRIDGE_HOLD_SECS", "900"))  # 15 min default
CDP_URL = os.environ.get("TOUCHDA_CDP_URL", "http://localhost:9222")
REPLY_CHAT_ID = os.environ.get("TOUCHDA_REPLY_CHAT", "")
REPLY_MESSAGE_ID = os.environ.get("TOUCHDA_REPLY_MID", "")
AUTO_PUBLISH = os.environ.get("TOUCHDA_AUTO_PUBLISH", "1") == "1"
# 只有自动发布才能无头 —— 手动发布要人去点，标签页必须看得见
HEADLESS = AUTO_PUBLISH and os.environ.get("TOUCHDA_HEADLESS", "0") == "1"
REWARD_TYPE = os.environ.get("TOUCHDA_REWARD_TYPE", "mud").lower()  # 'mud' or 'credit'

# Per reward type: task naming and the reward dropdown label.
REWARD_CONFIGS = {
    "mud":         {"task_name": "Claude test Nima",        "reward_label": "泥码彩金"},
    "credit":      {"task_name": "Claude test credit",      "reward_label": "免费彩金"},
    "coupon":      {"task_name": "Claude test coupon",      "reward_label": "优惠代码"},
    # 百分比型优惠代码：与金额型走同一个下拉项，靠模板区分（模板本身是 percentage 类型）
    "coupon_pct":  {"task_name": "Claude test coupon pct",  "reward_label": "优惠代码"},
    "mud_deposit": {"task_name": "Claude test mud deposit", "reward_label": "泥码存送券"},
    "fs":          {"task_name": "Claude test fs",          "reward_label": "FreeSpin"},
    # 2026-08 新增的优惠类型。下拉项与选择按钮的确切文案尚未在真站确认，
    # 因此这里给候选列表，首跑会自动匹配并把实际文案打进日志。
    "freeplay":    {"task_name": "Claude test freeplay",
                    "reward_label": ["Freeplay彩金", "Freeplay", "FreePlay彩金",
                                     "FreePlay", "Free Play", "免费游戏"]},
}
if REWARD_TYPE not in REWARD_CONFIGS:
    print(f"ERR: unknown TOUCHDA_REWARD_TYPE={REWARD_TYPE!r}", file=sys.stderr)
    sys.exit(2)
_CFG = REWARD_CONFIGS[REWARD_TYPE]
CREDIT_AMOUNT = int(os.environ.get("TOUCHDA_CREDIT_AMOUNT", "1000"))
CREDIT_VALIDITY_HOURS = int(os.environ.get("TOUCHDA_CREDIT_VALIDITY_HOURS", "1"))
COUPON_TEMPLATE_NAME = os.environ.get("TOUCHDA_COUPON_TEMPLATE", "")
if REWARD_TYPE == "coupon_pct":
    COUPON_TEMPLATE_NAME = os.environ.get("TOUCHDA_COUPON_PCT_TEMPLATE", "")
FREEPLAY_TEMPLATE_NAME = os.environ.get("TOUCHDA_FREEPLAY_TEMPLATE", "")

# 弹窗配置：general=通用弹窗 / configured=配置弹窗（纯图片）/ none=无弹窗
POPUP_TYPE = os.environ.get("TOUCHDA_POPUP_TYPE", "general").strip().lower()
if POPUP_TYPE not in ("general", "configured", "none"):
    print(f"ERR: unknown TOUCHDA_POPUP_TYPE={POPUP_TYPE!r}", file=sys.stderr)
    sys.exit(2)
POPUP_ITEM_NAME = os.environ.get("TOUCHDA_POPUP_ITEM", "")  # configured 时按弹窗组件名称挑，留空取第一个

# 触达内容：逗号分隔的渠道，空=不配置（保持「未配置」）
CONTENT_CHANNELS = [c.strip().lower() for c in
                    os.environ.get("TOUCHDA_CONTENT_CHANNELS", "").split(",") if c.strip()]
CONTENT_SMS = os.environ.get("TOUCHDA_CONTENT_SMS", "Claude auto test sms")
CONTENT_INAPP_TITLE = os.environ.get("TOUCHDA_CONTENT_INAPP_TITLE", "Claude auto test")
CONTENT_INAPP = os.environ.get("TOUCHDA_CONTENT_INAPP", "Claude auto test inbox")
CONTENT_EMAIL_TITLE = os.environ.get("TOUCHDA_CONTENT_EMAIL_TITLE", "Claude auto test")
CONTENT_EMAIL = os.environ.get("TOUCHDA_CONTENT_EMAIL", "Claude auto test email")
MUD_DEPOSIT_TEMPLATE_NAME = os.environ.get("TOUCHDA_MUD_DEPOSIT_TEMPLATE", "")
FS_TEMPLATE_NAME = os.environ.get("TOUCHDA_FS_TEMPLATE", "")

BOT_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = touchda_config.upload_dir()
CAMPAIGN_URL = os.environ.get(
    "TOUCHDA_CAMPAIGN_URL",
    "https://mgnt-webserver.platform99.me/campaign",
)

# 名字末尾带时间戳：以前每个任务都叫「Claude test mud deposit」，列表里一堆同名的，
# 发布后按名字回查（step 9 / 手动发布的等待）可能认到上一条旧任务（2026-09-30 一天就 3 条同名）。
TASK_NAME = (_CFG["task_name"] + os.environ.get("TOUCHDA_TASK_SUFFIX", "")
             + time.strftime(" %m%d-%H%M%S"))
TASK_REMARK = TASK_NAME  # we never set them to different values
TASK_STAT_DAYS = 1
# 2026-08 后台改版：任务类型与防干扰规则拆成两个字段，推送策略从 select 变 radio。
# 旧值 "用户召回（遵循防干扰规则)" 这个合并选项已不存在。
TASK_TYPE_LABEL = os.environ.get("TOUCHDA_TASK_TYPE", "流失召回")   # 流失召回/用户促活/新用户注册/营销补发
FORCE_NOTIFY = os.environ.get("TOUCHDA_FORCE_NOTIFY", "0") == "1"  # 1=强制通知（无视防干扰规则）
PUSH_STRATEGY_MODE = os.environ.get("TOUCHDA_PUSH_MODE", "ONE_TIME")  # ONE_TIME / RECURRING
PUSH_TIME_LABEL = os.environ.get("TOUCHDA_PUSH_TIME", "创建任务后立即推送")
# 兼容旧后台（若某些环境还没升级，下面两个 label 仍会被用到）
PUSH_STRATEGY_LABEL = "单次定向发送"
PUSH_MODE_LABEL = "发布后立即推送"
_RL = _CFG["reward_label"]
REWARD_TYPE_LABELS = _RL if isinstance(_RL, list) else [_RL]
REWARD_TYPE_LABEL = REWARD_TYPE_LABELS[0]
MUD_EVENT_KEYWORD = os.environ.get("TOUCHDA_MUD_EVENT_KEYWORD", "")
MUD_DISPLAY_NAME = os.environ.get("TOUCHDA_MUD_DISPLAY_NAME", "") or "QA test"


def log(msg: str) -> None:
    print(msg, flush=True)


def lark_say(text: str) -> None:
    """Best-effort post-back to the lark chat that triggered this run."""
    if not REPLY_MESSAGE_ID and not REPLY_CHAT_ID:
        return
    try:
        if REPLY_MESSAGE_ID:
            cmd = ["lark-cli", "im", "+messages-reply",
                   "--message-id", REPLY_MESSAGE_ID, "--text", text, "--as", "bot"]
        else:
            cmd = ["lark-cli", "im", "+messages-send",
                   "--chat-id", REPLY_CHAT_ID, "--text", text, "--as", "bot"]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception as e:
        log(f"lark_say failed: {e}")


def lark_say_image(path: Path) -> None:
    """发一张图回触发的群（best-effort）。lark-cli 的 --image 只认 cwd 下的相对路径。"""
    if not REPLY_MESSAGE_ID and not REPLY_CHAT_ID:
        return
    try:
        rel = os.path.relpath(path, BOT_DIR)
        if REPLY_MESSAGE_ID:
            cmd = ["lark-cli", "im", "+messages-reply", "--message-id", REPLY_MESSAGE_ID]
        else:
            cmd = ["lark-cli", "im", "+messages-send", "--chat-id", REPLY_CHAT_ID]
        subprocess.run(cmd + ["--image", rel, "--as", "bot"], cwd=BOT_DIR,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except Exception as e:
        log(f"lark_say_image failed: {e}")


def find_xlsx() -> str:
    files = list(UPLOAD_DIR.glob("*.xlsx"))
    if len(files) != 1:
        raise RuntimeError(f"expect exactly one .xlsx in {UPLOAD_DIR}, got {len(files)}")
    return str(files[0])


def select_by_label(page, css: str, label: str) -> None:
    page.wait_for_selector(css, state="attached", timeout=15000)
    page.select_option(css, label=label)


def select_by_label_any(page, css: str, labels) -> str:
    """Pick the first <option> matching any candidate label; return what actually matched.

    Falls back to substring matching, then raises with the full option list so an
    unknown label is diagnosable from a single run instead of guess-and-retry.
    """
    page.wait_for_selector(css, state="attached", timeout=15000)
    opts = page.evaluate(
        "css => Array.from(document.querySelector(css).options)"
        ".map(o => (o.label || o.textContent || '').trim()).filter(Boolean)",
        css,
    )
    for want in labels:
        for got in opts:
            if got == want:
                page.select_option(css, label=got)
                return got
    for want in labels:
        for got in opts:
            if want and want.lower() in got.lower():
                page.select_option(css, label=got)
                return got
    raise RuntimeError(
        f"reward type not selectable: wanted one of {labels}, page offers {opts}"
    )


def click_radio(page, model: str, value: str = None, label_contains: str = None) -> str:
    """点 ng-model=model 的 radio。优先按 value 命中，其次按标签文字包含，最后取第一个。

    改版后后台把一批 select 换成了 radio，这个助手把两种定位方式合在一起，
    并在找不到时把该 model 下所有可选项打出来，避免靠猜。
    """
    page.wait_for_selector(f'input[ng-model="{model}"]', state="attached", timeout=15000)
    picked = page.evaluate(
        """(a) => {
            const rs = Array.from(document.querySelectorAll(`input[ng-model="${a.model}"]`));
            if (!rs.length) return null;
            const lab = r => ((r.closest('label') && r.closest('label').innerText)
                              || (r.parentElement && r.parentElement.innerText) || '').trim();
            let hit = null;
            if (a.value) hit = rs.find(r => r.value === a.value);
            if (!hit && a.label) hit = rs.find(r => lab(r).includes(a.label));
            if (!hit) hit = rs[0];
            hit.click();
            return {value: hit.value, label: lab(hit).slice(0, 40),
                    all: rs.map(r => r.value + '=' + lab(r).slice(0, 24))};
        }""",
        {"model": model, "value": value, "label": label_contains},
    )
    if not picked:
        raise RuntimeError(f"no radio for ng-model={model!r}")
    log(f"    radio {model} -> {picked['value']} ({picked['label']})  候选={picked['all']}")
    return picked["value"]


def _has(page, css: str) -> bool:
    return page.evaluate("css => !!document.querySelector(css)", css)


def _looks_like_login_page(page) -> bool:
    """判断当前是不是被踢回登录页。

    注意：不能用「页面里有 password 输入框 + 有含『登录』字样的按钮」这种宽松判据。
    campaign 页常驻一个隐藏的「更新密码」弹窗（userNewPassword / passwordVerify），
    而人群筛选条件里有「最后登录时间（小时级更新）」「登录次数（小时级更新）」这类
    按钮——两个条件同时满足，正常已登录的页面也会被误判成掉登录，
    于是流程里任何一步的真实报错都被盖成 SESSION_EXPIRED。
    这里改成：URL 明确是登录页，或者「可见的密码框 + 文案恰好等于登录词的可见按钮」。
    """
    try:
        return bool(page.evaluate(
            r"""() => {
                const url = (location.pathname + location.search).toLowerCase();
                if (/(^|\/)(login|signin|sign-in)(\/|$|\?)/.test(url)) return true;
                const vis = e => e.offsetParent !== null;
                const pw = Array.from(document.querySelectorAll('input[type=password]')).filter(vis);
                if (!pw.length) return false;
                const words = ['登录', '登入', 'login', 'sign in', '登 录'];
                const btn = Array.from(document.querySelectorAll('button, input[type=submit]'))
                    .filter(vis)
                    .some(b => words.includes(((b.textContent || b.value || '').trim().toLowerCase())));
                return btn;
            }"""
        ))
    except Exception:
        return False


def step_1_navigate(page) -> None:
    log("Step 1: navigate + open create form")
    # Transient race: a fresh tab can get torn down mid-goto when Chrome is busy
    # with other tab churn. Retry the navigation a couple times before giving up.
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            page.goto(CAMPAIGN_URL, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            last_err = e
            page.wait_for_timeout(800)
    else:
        raise RuntimeError(f"navigate failed after retries: {last_err}")
    # 页面 domcontentloaded 之后还会自己再导航一次（SPA 路由 / SSO 回跳），这段窗口里
    # 任何 evaluate / wait_for_function 都会以 "Execution context was destroyed" 炸掉，
    # 而且 URL 会短暂停在 /login 上——先静置让它跑完，再判断登录态，否则必然误报。
    page.wait_for_timeout(3000)
    if _looks_like_login_page(page):
        page.wait_for_timeout(3000)   # 再给一次机会，可能还在回跳
        if _looks_like_login_page(page):
            raise RuntimeError("SESSION_EXPIRED — open your dedicated Chrome (:9222) and log in to mgnt-webserver")

    # 2026-08 改版后「创建触达任务」不再是 <a>，放宽到常见可点元素。
    FIND_ENTRY = (
        "() => Array.from(document.querySelectorAll('a,button,li,span,div'))"
        ".filter(e => e.offsetParent !== null)"
        ".some(e => (e.textContent || '').trim() === '创建触达任务')"
    )
    found = False
    for attempt in range(3):
        try:
            page.wait_for_function(FIND_ENTRY, timeout=15000)
            found = True
            break
        except PWTimeout:
            break
        except Exception as e:  # 导航把执行上下文冲掉了，等一下重试
            log(f"  entry lookup retry {attempt + 1}: {str(e)[:70]}")
            page.wait_for_timeout(2000)
    if not found:
        if _looks_like_login_page(page):
            raise RuntimeError("SESSION_EXPIRED — open your dedicated Chrome (:9222) and log in to mgnt-webserver")
        raise RuntimeError(f"'创建触达任务' entry not found. URL: {page.url}")

    clicked = page.evaluate(
        "() => { const e = Array.from(document.querySelectorAll('a,button,li,span,div'))"
        ".filter(x => x.offsetParent !== null)"
        ".find(x => (x.textContent || '').trim() === '创建触达任务');"
        " if (!e) return null; e.click(); return e.tagName; }"
    )
    if not clicked:
        raise RuntimeError("'创建触达任务' entry vanished between find and click")
    log(f"  entry clicked (<{clicked.lower()}>)")
    # Wait for the create form's Angular scope to actually have newCampaign bound.
    # Just waiting for the 发布任务 button is a race — the DOM appears before Angular
    # finishes initializing scope.vm.newCampaign.
    page.wait_for_function(
        """
        () => {
            const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务');
            if (!btn) return false;
            try {
                const scope = angular.element(btn).scope();
                return !!(scope && scope.vm && scope.vm.newCampaign);
            } catch (e) {
                return false;
            }
        }
        """,
        timeout=20000,
    )


def _install_create_campaign_patch(page) -> None:
    """Monkey-patch socketService.$socket to inject totalBudget into createCampaign
    payloads. Backend rejects without it; UI doesn't propagate selectedItem.totalBonusBudget."""
    page.evaluate(
        """
        () => {
            try {
                const injector = angular.element(document.body).injector();
                const ss = injector.get('socketService');
                if (ss._touchdaMonkeyPatched) return;
                const orig = ss.$socket.bind(ss);
                ss.$socket = function(sock, evt, data) {
                    if (evt === 'createCampaign' && data && data.targetGroupConfig && Array.isArray(data.targetGroupConfig.groups)) {
                        try {
                            data.targetGroupConfig.groups.forEach(function(g) {
                                if (!g) return;
                                // Only inject when this group uses the mud (泥码) reward;
                                // credit (彩金) groups already carry a UI-supplied totalBudget
                                // on vm.newCampaign and don't need the workaround.
                                const isMud = g.rewardType === 'CAMPAIGN_MUD'
                                    || (g.mudConfig && g.mudConfig.eventId)
                                    || (Array.isArray(g.mudConfigs) && g.mudConfigs.length > 0 && g.mudConfigs[0] && g.mudConfigs[0].mudConfigId);
                                if (!isMud) return;
                                const bud = (g.mudConfig && (g.mudConfig.totalBonusBudget || g.mudConfig.totalBudget)) || 1000000000;
                                if (g.mudConfig && typeof g.mudConfig === 'object') {
                                    g.mudConfig.totalBudget = bud;
                                    g.mudConfig.disbursementLimit = bud;
                                }
                                if (Array.isArray(g.mudConfigs)) {
                                    g.mudConfigs.forEach(function(mc) {
                                        if (mc) { mc.totalBudget = bud; mc.disbursementLimit = bud; }
                                    });
                                }
                                g.totalBudget = bud;
                                g.disbursementLimit = bud;
                                if (!g.rewardConfig) g.rewardConfig = {};
                                g.rewardConfig.totalBudget = bud;
                                g.rewardConfig.disbursementLimit = bud;
                                data.totalBudget = data.totalBudget || bud;
                                data.disbursementLimit = data.disbursementLimit || bud;
                            });
                        } catch(e) {}
                    }
                    const rest = Array.prototype.slice.call(arguments, 3);
                    return orig.apply(ss, [sock, evt, data].concat(rest));
                };
                ss._touchdaMonkeyPatched = true;
            } catch(e) { /* injector or service missing — skip */ }
        }
        """
    )


def step_2_basic_info(page) -> None:
    log("Step 2: fill basic info")
    page.evaluate(
        f"""
        () => {{
            const publishBtn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务');
            const vm = angular.element(publishBtn).scope().vm;
            vm.newCampaign.name = {TASK_NAME!r};
            vm.newCampaign.remark = {TASK_REMARK!r};
            vm.newCampaign.statisticDateNumber = {TASK_STAT_DAYS};
            angular.element(publishBtn).scope().$apply();
        }}
        """
    )
    select_by_label(page, 'select[ng-model="vm.newCampaign.type"]', TASK_TYPE_LABEL)
    log(f"Step 2b: task type = {TASK_TYPE_LABEL}")
    # 改版后新增的独立字段：防干扰规则（false=遵循 / true=强制通知）
    if _has(page, 'input[ng-model="vm.newCampaign.forceNotify"]'):
        click_radio(
            page, "vm.newCampaign.forceNotify",
            value=("true" if FORCE_NOTIFY else "false"),
            label_contains=("强制通知" if FORCE_NOTIFY else "遵循"),
        )
    else:
        log("Step 2c: 无 forceNotify 字段（旧版后台）— 跳过")


def step_3_upload_xlsx(page, xlsx_path: str) -> None:
    log(f"Step 3: upload {xlsx_path}")
    inputs = page.locator('input[type=file]')
    if inputs.count() == 0:
        raise RuntimeError("no input[type=file] found on page")
    inputs.first.set_input_files(xlsx_path)
    # Wait until the upload pipeline marks itself Completed and populates
    # newCampaign.hitUserCount from the imported list.
    page.wait_for_function(
        """
        () => {
            const sub = (document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'));
            if (!sub) return false;
            const vm = angular.element(sub).scope().vm;
            return vm && vm.uploadStatus === 'Completed'
                && vm.newCampaign && vm.newCampaign.importUrl
                && vm.newCampaign.hitUserCount && Number(vm.newCampaign.hitUserCount) > 0;
        }
        """,
        timeout=30000,
    )
    log(
        "  upload completed: "
        + str(page.evaluate(
            "() => { const v = angular.element((document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'))).scope().vm;"
            "return v.newCampaign.hitUserCount + ' users from ' + (v.fileName || 'file'); }"
        ))
    )


def step_4_push_settings(page) -> None:
    """推送策略。

    新后台（2026-08 起）：radio vm.newCampaign.sendStrategyMode = ONE_TIME / RECURRING，
    选 ONE_TIME 后再出二级 radio vm.newCampaign.oneTimePushTimeMode
    （创建任务后立即推送 / 指定时间发送）。
    旧后台是两个 select，这里两种都兼容。
    """
    if _has(page, 'input[ng-model="vm.newCampaign.sendStrategyMode"]'):
        log("Step 4a: push strategy (new UI)")
        click_radio(page, "vm.newCampaign.sendStrategyMode",
                    value=PUSH_STRATEGY_MODE,
                    label_contains=("单次推送" if PUSH_STRATEGY_MODE == "ONE_TIME" else "轮询"))
        if PUSH_STRATEGY_MODE == "ONE_TIME":
            page.wait_for_selector(
                'input[ng-model="vm.newCampaign.oneTimePushTimeMode"]',
                state="attached", timeout=10000,
            )
            log(f"Step 4b: push time = {PUSH_TIME_LABEL}")
            click_radio(page, "vm.newCampaign.oneTimePushTimeMode",
                        label_contains=PUSH_TIME_LABEL)
        return

    log(f"Step 4a: push strategy (legacy UI) = {PUSH_STRATEGY_LABEL}")
    select_by_label(page, 'select[ng-model="vm.newCampaign.sendStrategy.type"]', PUSH_STRATEGY_LABEL)
    page.wait_for_selector(
        'select[ng-model="vm.newCampaign.sendStrategy.oneWayConfig.pushMode"]',
        timeout=10000,
    )
    log(f"Step 4b: push mode = {PUSH_MODE_LABEL}")
    select_by_label(
        page, 'select[ng-model="vm.newCampaign.sendStrategy.oneWayConfig.pushMode"]', PUSH_MODE_LABEL
    )


def step_5_group_all(page) -> None:
    """人群分组设置 = 纯回访策略（radio value 'ALL'）。

    2026-08 改版：radio 取值仍是 AB_TEST / ALL，但 vm.tmpSelectedGroup 和
    group.channels 都没有了（channels 被「触达内容」弹窗取代）。所以不能再拿
    tmpSelectedGroup.group.channels 当成功判据，改为「strategy 落到 ALL
    且分组行的优惠类型下拉已渲染出来」——后者正是 step_6 真正需要的前置。
    """
    log("Step 5: group strategy = ALL（纯回访策略）")
    page.evaluate(
        """
        () => {
            const anchor = document.querySelector('button[type=submit]')
                || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务');
            const scope = angular.element(anchor).scope();
            const vm = scope.vm;
            if (typeof vm.onChangeStrategy === 'function') {
                try { vm.onChangeStrategy('ALL'); } catch (e) {}
            }
            if (!vm.newCampaign.targetGroupConfig) vm.newCampaign.targetGroupConfig = {};
            try { scope.$apply(); } catch (e) {}
            const rs = Array.from(document.querySelectorAll(
                'input[ng-model="vm.newCampaign.targetGroupConfig.strategy"]'));
            const pick = rs.find(r => r.value === 'ALL')
                || rs.find(r => (((r.closest('label') || {}).innerText) || '').includes('纯回访'));
            if (!pick) throw new Error('找不到「纯回访策略」radio；候选=' +
                JSON.stringify(rs.map(r => r.value)));
            pick.click();
        }
        """
    )
    # 分组行是异步渲染的，等它把优惠类型下拉铺出来
    try:
        page.wait_for_selector('select[ng-model="group.rewardType"]', state="attached", timeout=15000)
    except PWTimeout:
        raise RuntimeError("选了纯回访策略但分组行没出现（group.rewardType 下拉未渲染）")
    strategy = page.evaluate(
        """() => {
            const anchor = document.querySelector('button[type=submit]')
                || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务');
            const vm = angular.element(anchor).scope().vm;
            return (vm.newCampaign.targetGroupConfig || {}).strategy;
        }"""
    )
    if strategy != "ALL":
        raise RuntimeError(f"group strategy 没落到 ALL，实际是 {strategy!r}")
    log("    分组行已就绪")


def step_6_reward(page) -> None:
    """Dispatch to the right reward setup based on REWARD_TYPE."""
    picked = select_by_label_any(
        page, 'select[ng-model="group.rewardType"]', REWARD_TYPE_LABELS
    )
    log(f"Step 6a: reward type = {picked}")
    if REWARD_TYPE == "credit":
        return _step_6_reward_credit(page)
    if REWARD_TYPE in ("coupon", "coupon_pct"):
        return _step_6_reward_coupon(page)
    if REWARD_TYPE == "freeplay":
        return _step_6_reward_freeplay(page)
    if REWARD_TYPE == "mud_deposit":
        return _step_6_reward_mud_deposit(page)
    if REWARD_TYPE == "fs":
        return _step_6_reward_fs(page)
    return _step_6_reward_mud(page)


def _open_reward_modal(page, btn_labels) -> str:
    """点开分组行里的「选择XXX」按钮。返回实际点到的文案。"""
    label = page.evaluate(
        """(cands) => {
            const bs = Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null);
            for (const w of cands) { const h = bs.find(b => b.textContent.trim() === w); if (h) return h.textContent.trim(); }
            const byClick = bs.find(b => (b.getAttribute('ng-click') || '').includes('openRewardSelectionModal'));
            return byClick ? byClick.textContent.trim() : null;
        }""",
        btn_labels,
    )
    if not label:
        visible = page.evaluate(
            "() => Array.from(document.querySelectorAll('button')).filter(b => b.offsetParent !== null)"
            ".map(b => b.textContent.trim()).filter(t => t.startsWith('选择'))"
        )
        raise RuntimeError(f"找不到优惠选择按钮，候选={btn_labels}；页面上的「选择*」按钮={visible}")
    log(f"Step 6b: open {label} dialog")
    page.evaluate(
        "l => Array.from(document.querySelectorAll('button'))"
        ".find(b => b.textContent.trim() === l && b.offsetParent !== null).click()", label
    )
    return label


def _pick_reward_row(page, want: str, what: str) -> str:
    """在已打开的选择弹窗里，按行文本选中包含 want 的那一行（点它的 radio）。

    2026-08 改版后弹窗是隔离作用域，拿不到 vm.currentSelectionList，
    所以改成纯 DOM：表格每行一个 radio，按行文字匹配。这样对 scope 变动免疫。
    """
    try:
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll('tr'))"
            ".some(tr => tr.offsetParent !== null && tr.querySelector('input[type=radio]'))",
            timeout=20000,
        )
    except PWTimeout:
        raise RuntimeError(f"{what} 弹窗里没有可选行（后台可能没配这类模板）")

    res = page.evaluate(
        r"""(want) => {
            const rows = Array.from(document.querySelectorAll('tr'))
                .filter(tr => tr.offsetParent !== null && tr.querySelector('input[type=radio]'));
            const norm = t => (t || '').replace(/\s+/g, ' ').trim();
            const hit = rows.find(tr => norm(tr.innerText).includes(want));
            if (!hit) return {ok: false, names: rows.slice(0, 25).map(tr => norm(tr.innerText).slice(0, 60))};
            const r = hit.querySelector('input[type=radio]');
            r.click();
            return {ok: true, row: norm(hit.innerText).slice(0, 80), total: rows.length};
        }""",
        want,
    )
    if not res.get("ok"):
        raise RuntimeError(
            f"{what} 列表里没有匹配 {want!r} 的行。列表里实际有：\n    "
            + "\n    ".join(res.get("names", []))
        )
    log(f"    选中: {res['row']}  （共 {res['total']} 行）")
    return res["row"]


def _confirm_reward_modal(page) -> None:
    """点弹窗的「确定」，并确认它真的关了。

    和弹窗配置那边同一个道理：模板列表 / 校验是异步的，盲点一下就往下走，
    连着批量跑时会撞上「点了没生效、弹窗还开着」，后面步骤全部错位。
    """
    for attempt in range(3):
        page.evaluate(
            """() => {
                const vis = x => x.offsetParent !== null;
                const b = Array.from(document.querySelectorAll('button')).filter(vis)
                    .find(x => (x.getAttribute('ng-click') || '').includes('selectReward'))
                    || Array.from(document.querySelectorAll('button')).filter(vis)
                        .find(x => x.textContent.trim() === '确定');
                if (!b) throw new Error('弹窗里找不到 确定 按钮');
                if (b.disabled) return;   // 还没就绪，等下一轮
                b.click();
            }"""
        )
        try:
            page.wait_for_function(
                "() => !Array.from(document.querySelectorAll('button'))"
                ".filter(x => x.offsetParent !== null)"
                ".some(x => (x.getAttribute('ng-click') || '').includes('selectReward'))",
                timeout=8000,
            )
            page.wait_for_timeout(600)
            return
        except PWTimeout:
            log(f"    优惠选择「确定」未生效（第 {attempt + 1} 次），等 3s 重试")
            page.wait_for_timeout(3000)
    raise RuntimeError("优惠选择弹窗没有关闭 — 「确定」连点 3 次都被拦住")


def _fill_total_budget(page, per_user: int = 1000) -> None:
    """优惠额度派发上限。不填的话发布按钮是灰的。"""
    override = os.environ.get("TOUCHDA_TOTAL_BUDGET", "").strip()
    try:
        page.wait_for_selector('input[ng-model="vm.newCampaign.totalBudget"]', timeout=6000)
    except PWTimeout:
        log("    无 totalBudget 输入框 — 跳过")
        return
    if override:
        value = override
    else:
        n = page.evaluate(
            "() => { const a = document.querySelector('button[type=submit]')"
            " || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务');"
            " const vm = angular.element(a).scope().vm;"
            " return Number((vm.newCampaign || {}).hitUserCount || 0); }"
        )
        value = str(max(1, n) * per_user)
    page.locator('input[ng-model="vm.newCampaign.totalBudget"]').first.fill(value)
    log(f"Step 6c: 优惠额度派发上限 = {value}")


def _step_6_reward_credit(page) -> None:
    """免费彩金：不走弹窗，直接填金额与有效期。"""
    page.wait_for_selector('input[ng-model="group.freeBonusConfig.amount"]', timeout=10000)
    page.locator('input[ng-model="group.freeBonusConfig.amount"]').first.fill(str(CREDIT_AMOUNT))
    page.locator('input[ng-model="group.freeBonusConfig.expiredTtlInHours"]').first.fill(
        str(CREDIT_VALIDITY_HOURS)
    )
    log(f"Step 6b: 免费彩金 {CREDIT_AMOUNT} / 有效期 {CREDIT_VALIDITY_HOURS}h")
    _fill_total_budget(page, per_user=CREDIT_AMOUNT)


def _step_6_reward_coupon(page) -> None:
    if not COUPON_TEMPLATE_NAME:
        raise RuntimeError("优惠代码模板名为空（coupon 看 TOUCHDA_COUPON_TEMPLATE，"
                           "coupon_pct 看 TOUCHDA_COUPON_PCT_TEMPLATE）")
    _open_reward_modal(page, ["选择优惠代码"])
    _pick_reward_row(page, COUPON_TEMPLATE_NAME, "优惠代码")
    _confirm_reward_modal(page)
    _fill_total_budget(page)


def _step_6_reward_mud_deposit(page) -> None:
    if not MUD_DEPOSIT_TEMPLATE_NAME:
        raise RuntimeError("TOUCHDA_MUD_DEPOSIT_TEMPLATE 为空")
    _open_reward_modal(page, ["选择泥码存送券", "选择泥码存送"])
    _pick_reward_row(page, MUD_DEPOSIT_TEMPLATE_NAME, "泥码存送券")
    _confirm_reward_modal(page)
    _fill_total_budget(page)


def _step_6_reward_fs(page) -> None:
    if not FS_TEMPLATE_NAME:
        raise RuntimeError("TOUCHDA_FS_TEMPLATE 为空")
    _open_reward_modal(page, ["选择FreeSpin", "选择Free Spin"])
    _pick_reward_row(page, FS_TEMPLATE_NAME, "FreeSpin")
    _confirm_reward_modal(page)
    _fill_total_budget(page)


def _step_6_reward_freeplay(page) -> None:
    """Freeplay 彩金（2026-08 需求）。正式站的优惠类型下拉里目前还没有这一项。"""
    if not FREEPLAY_TEMPLATE_NAME:
        raise RuntimeError("TOUCHDA_FREEPLAY_TEMPLATE 为空")
    _open_reward_modal(page, ["选择Freeplay", "选择FreePlay", "选择Free Play", "选择免费游戏"])
    _pick_reward_row(page, FREEPLAY_TEMPLATE_NAME, "Freeplay")
    _confirm_reward_modal(page)
    _fill_total_budget(page)


def _step_6_reward_mud(page) -> None:
    """泥码彩金：选红利行 + 填【玩家端泥码 Reward 显示名称】。"""
    if not MUD_EVENT_KEYWORD:
        raise RuntimeError("TOUCHDA_MUD_EVENT_KEYWORD 为空（在 config.json 填 mud_event_keyword）")
    _open_reward_modal(page, ["选择泥码"])
    _pick_reward_row(page, MUD_EVENT_KEYWORD, "泥码")
    # 弹窗底部有「玩家端泥码Reward显示名称」，必填，pattern 只收 [A-Za-z0-9 ]。
    # DOM 里同名 input 有多个（模板里还留着隐藏的），Playwright 的 .first 会抓到不可见的那个，
    # 所以这里自己挑可见的并派发 Angular 需要的事件。
    ok = page.evaluate(
        """(val) => {
            const els = Array.from(document.querySelectorAll(
                'input[ng-model="vm.mudConfig.mudDisplayName"]')).filter(e => e.offsetParent !== null);
            if (!els.length) return 'none';
            const el = els[0];
            el.focus();
            el.value = val;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            try { angular.element(el).scope().$apply(); } catch (e) {}
            return el.value;
        }""",
        MUD_DISPLAY_NAME,
    )
    if ok == 'none':
        log("    没有可见的「玩家端泥码 Reward 显示名称」输入框 — 跳过")
    else:
        log(f"    玩家端泥码 Reward 显示名称 = {ok!r}")
    _confirm_reward_modal(page)
    _fill_total_budget(page)


def step_7_popup(page) -> None:
    log("Step 7a: open popup selection modal")
    page.wait_for_selector('button[ng-click*="openPopupSelectionModal"]', timeout=15000)
    # Modal occasionally takes a beat to attach after step 6 closed its own modal.
    page.wait_for_timeout(800)
    page.evaluate('document.querySelector(\'button[ng-click*="openPopupSelectionModal"]\').click()')
    page.wait_for_function(
        """
        () => Array.from(document.querySelectorAll('input[type=radio]'))
            .some(r => r.value === 'general' && r.offsetParent !== null)
        """,
        timeout=20000,
    )
    log(f"Step 7b: pick 弹窗形态 = {POPUP_TYPE}")
    page.evaluate(
        """(want) => {
            const radio = Array.from(document.querySelectorAll('input[ng-model="vm.popupConfigType"]'))
                .find(r => r.value === want && r.offsetParent !== null);
            if (!radio) throw new Error('no popup radio for ' + want);
            radio.click();
        }""",
        POPUP_TYPE,
    )
    if POPUP_TYPE == "configured":
        # 选「配置弹窗（纯图片）」后会多出一张素材表，需要再点一行
        page.wait_for_timeout(1200)
        picked = page.evaluate(
            """(want) => {
                const rs = Array.from(document.querySelectorAll(
                    'input[ng-model="vm.selectedConfiguredPopupItem"]')).filter(r => r.offsetParent !== null);
                if (!rs.length) return null;
                const rowText = r => (r.closest('tr') ? r.closest('tr').innerText : '').trim();
                const hit = (want ? rs.find(r => rowText(r).includes(want)) : null) || rs[0];
                hit.click();
                return {name: rowText(hit).slice(0, 40), total: rs.length};
            }""",
            POPUP_ITEM_NAME,
        )
        if not picked:
            raise RuntimeError("配置弹窗：素材列表为空，CCMS 里可能没配对应类型的弹窗")
        log(f"    素材 = {picked['name']!r}（共 {picked['total']} 个可选）")
    # Radio click sets popupConfigType. Mud path also fetches popupConfigList async;
    # credit path auto-binds the only available popup (TYTC_MFCJ) without listing,
    # so popupConfigList may stay empty — gate only on popupConfigType and a brief
    # settle window for any async list fetch.
    page.wait_for_function(
        """
        () => {
            const sub = (document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'));
            if (!sub) return false;
            const vm = angular.element(sub).scope().vm;
            return vm && vm.popupConfigType === PT;
        }
        """.replace('PT', repr(POPUP_TYPE)),
        timeout=15000,
    )
    # Brief settle for any async popup list/config fetch.
    page.wait_for_timeout(1500)
    # 通用弹窗的配置是异步拉的（_popupFetchInFlight）。没拉完就点「确定」，
    # getGeneralPopupConfigForGroup 会抛错、弹窗关不掉（2026-11-06 Freeplay 首跑踩到）。
    try:
        page.wait_for_function(
            """() => {
                const r = document.querySelector('input[ng-model="vm.popupConfigType"]');
                const vm = r && angular.element(r).scope() && angular.element(r).scope().vm;
                if (!vm) return true;
                if (vm._popupFetchInFlight) return false;
                return vm.popupConfigType !== 'general' || !!vm.popupGeneralConfig;
            }""",
            timeout=15000,
        )
    except PWTimeout:
        log("    弹窗配置 15s 还没拉完，照样点确定试试")
    log("Step 7c: confirm")
    # 泥码这类要异步拉 popupConfigList，列表没就绪就点「确定」会被校验拦住而弹窗不关。
    # 单跑时通常赶得上，连着批量跑就会翻车 —— 所以点完检查弹窗有没有真的关，没关就重点。
    closed = False
    for attempt in range(3):
        page.evaluate(
            """
            () => {
                const btn = Array.from(document.querySelectorAll('button'))
                    .filter(b => b.textContent.trim() === '确定' && b.offsetParent !== null)[0];
                if (!btn) throw new Error('no visible 确定 button');
                if (btn.disabled) return;      // 还没就绪，这轮不点，交给下一次重试
                btn.click();
            }
            """
        )
        try:
            page.wait_for_function(
                "() => !Array.from(document.querySelectorAll('input[ng-model=\"vm.popupConfigType\"]'))"
                ".some(r => r.offsetParent !== null)",
                timeout=8000,
            )
            closed = True
            break
        except PWTimeout:
            log(f"    确定 未生效（第 {attempt + 1} 次），重选弹窗形态后等 3s 重试")
            # 重点一次 radio 会重新触发 onPopupConfigTypeChange → 重新拉配置
            page.evaluate(
                """(want) => {
                    const r = Array.from(document.querySelectorAll('input[ng-model="vm.popupConfigType"]'))
                        .find(x => x.value === want && x.offsetParent !== null);
                    if (r) r.click();
                }""",
                POPUP_TYPE,
            )
            page.wait_for_timeout(3000)
    if not closed:
        raise RuntimeError("弹窗配置没有关闭 — 「确定」连点 3 次都被校验拦住")
    # 旧版这里等 vm.tmpSelectedGroup.group.popupConfig 落值；2026-08 改版后
    # tmpSelectedGroup 已经不存在了，等它必然超时。改成等弹窗真正关闭 ——
    # 那才是「确定」被接受的可观察信号。
    page.wait_for_timeout(800)
    cell = page.evaluate(
        r"""() => {
            const b = Array.from(document.querySelectorAll('button')).filter(x => x.offsetParent !== null)
                .find(x => (x.getAttribute('ng-click') || '').includes('openPopupSelectionModal'));
            const td = b && b.closest('td');
            return td ? (td.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60) : '(未找到弹窗配置单元格)';
        }"""
    )
    log(f"    弹窗配置 = {cell}")


def step_7c_content(page) -> None:
    """配置触达内容（短信 / 站内信 / 邮件）。TOUCHDA_CONTENT_CHANNELS 为空则整步跳过。

    模态：vm.openOneTimeContentModal(group) → 勾渠道后才渲染对应文案框 → vm.saveOneTimeContentModal()
    字段：draft.smsEnabled/inAppEnabled/emailEnabled，
          draft.sms（短信，160 字上限）、draft.inAppTitle + draft.inApp、draft.emailTitle + draft.email
    """
    if not CONTENT_CHANNELS:
        log("Step 7d: 未指定触达内容渠道 — 保持「未配置」")
        return
    alias = {"sms": "smsEnabled", "短信": "smsEnabled",
             "inapp": "inAppEnabled", "inbox": "inAppEnabled", "站内信": "inAppEnabled",
             "email": "emailEnabled", "mail": "emailEnabled", "邮件": "emailEnabled"}
    want = []
    for c in CONTENT_CHANNELS:
        k = alias.get(c)
        if not k:
            raise RuntimeError(f"未知触达渠道 {c!r}；可选 sms / inapp / email")
        if k not in want:
            want.append(k)

    log(f"Step 7d: 配置触达内容 {want}")
    page.wait_for_selector('button[ng-click*="openOneTimeContentModal"]', timeout=15000)
    page.evaluate(
        "() => Array.from(document.querySelectorAll('button'))"
        ".find(b => (b.getAttribute('ng-click')||'').includes('openOneTimeContentModal')"
        " && b.offsetParent !== null).click()"
    )
    page.wait_for_selector(
        'input[ng-model="vm.oneTimeContentModal.draft.smsEnabled"]', timeout=15000
    )
    page.evaluate(
        """(keys) => keys.forEach(k => {
            const c = document.querySelector(`input[ng-model="vm.oneTimeContentModal.draft.${k}"]`);
            if (c && !c.checked) c.click();
        })""",
        want,
    )
    page.wait_for_timeout(1500)

    fills = []
    if "smsEnabled" in want:
        fills.append(("sms", CONTENT_SMS[:160]))
    if "inAppEnabled" in want:
        fills += [("inAppTitle", CONTENT_INAPP_TITLE), ("inApp", CONTENT_INAPP)]
    if "emailEnabled" in want:
        fills += [("emailTitle", CONTENT_EMAIL_TITLE), ("email", CONTENT_EMAIL)]
    missing = page.evaluate(
        """(pairs) => {
            const miss = [];
            for (const [k, v] of pairs) {
                const el = document.querySelector(`[ng-model="vm.oneTimeContentModal.draft.${k}"]`);
                if (!el) { miss.push(k); continue; }
                el.value = v;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                try { angular.element(el).scope().$apply(); } catch (e) {}
            }
            return miss;
        }""",
        fills,
    )
    if missing:
        log(f"    WARN: 这些字段没找到，已跳过：{missing}")

    page.evaluate(
        "() => Array.from(document.querySelectorAll('button'))"
        ".find(b => (b.getAttribute('ng-click')||'').includes('saveOneTimeContentModal')"
        " && b.offsetParent !== null).click()"
    )
    try:
        page.wait_for_function(
            "() => !document.querySelector('input[ng-model=\"vm.oneTimeContentModal.draft.smsEnabled\"]')"
            " || document.querySelector('input[ng-model=\"vm.oneTimeContentModal.draft.smsEnabled\"]')"
            ".offsetParent === null",
            timeout=15000,
        )
        log("    触达内容已保存")
    except PWTimeout:
        raise RuntimeError("触达内容弹窗没关闭 — 可能有必填项未通过校验")


def ensure_hit_user_count(page) -> None:
    """新后台加了门槛：没点过「获取」拿到命中用户数，发布任务按钮一直是灰的。"""
    if not page.evaluate(
        "() => Array.from(document.querySelectorAll('button'))"
        ".some(b => b.textContent.trim() === '获取' && b.offsetParent !== null)"
    ):
        return
    log("Step 7b: 点「获取」取命中用户数")
    page.evaluate(
        "() => Array.from(document.querySelectorAll('button'))"
        ".find(b => b.textContent.trim() === '获取' && b.offsetParent !== null).click()"
    )
    try:
        page.wait_for_function(
            "() => { const b = (document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'));"
            " if (!b) return false; const vm = angular.element(b).scope().vm;"
            " return Number(vm.newCampaign && vm.newCampaign.hitUserCount || 0) > 0; }",
            timeout=20000,
        )
        n = page.evaluate(
            "() => angular.element((document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务')))"
            ".scope().vm.newCampaign.hitUserCount"
        )
        log(f"    命中用户数 = {n}")
    except PWTimeout:
        log("    WARN: 命中用户数仍为 0，发布可能被拦住")


def step_9_verify_status(page) -> dict | None:
    """发布后回列表核对刚建的任务，输出一行 STATUS:<json> 给 bot / GUI 用。

    2026-08 改版后 vm.campaignList 这条路走不通了（scope 锚点和字段都变了），
    所以改成纯 DOM：用任务名称筛选 → 查询 → 按表头定位那一行的各列。
    尽力而为，任何失败只记日志、不影响派发结果。
    """
    log("Step 9: query list + read just-published task status")
    try:
        # 发布后会 reload，可能停在别的子页；防御性点一下「触达任务管理」
        try:
            page.evaluate(
                """() => Array.from(document.querySelectorAll('a, span, button, li'))
                    .filter(el => el.textContent.trim() === '触达任务管理' && el.offsetParent !== null)
                    .slice(0, 1).forEach(el => el.click())"""
            )
        except Exception:
            pass
        page.wait_for_selector('input[ng-model="vm.campaignsQuery.name"]', timeout=60000)   # 后台慢时 20s 不够

        # 按任务名称过滤 → 查询。每轮都重新填+点：发布后页面会 reload，第一下「查询」
        # 常落在 reload 途中被吞掉，只查一次的话后面怎么读表格都是空的（2026-11-06 8 次漏 6 次）。
        requery = """(name) => {
                const el = document.querySelector('input[ng-model="vm.campaignsQuery.name"]');
                if (!el) return false;
                el.value = name;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                try { angular.element(el).scope().$apply(); } catch (e) {}
                const q = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.trim() === '查询' && b.offsetParent !== null);
                if (q) q.click();
                return !!q;
            }"""
        read_row = r"""(name) => {
                    const tbl = Array.from(document.querySelectorAll('table'))
                        .filter(t => t.offsetParent !== null)
                        .find(t => /任务名称/.test(t.innerText));
                    if (!tbl) return null;
                    const heads = Array.from(tbl.querySelectorAll('th'))
                        .map(th => (th.innerText || '').replace(/\s+/g, '').trim());
                    const tr = Array.from(tbl.querySelectorAll('tbody tr'))
                        .find(r => (r.innerText || '').includes(name));
                    if (!tr) return null;
                    const tds = Array.from(tr.querySelectorAll('td'))
                        .map(td => (td.innerText || '').replace(/\s+/g, ' ').trim());
                    const pick = key => {
                        const i = heads.findIndex(h => h.includes(key));
                        return i >= 0 && i < tds.length ? tds[i] : null;
                    };
                    return {
                        taskId: pick('任务ID'), name: pick('任务名称'),
                        type: pick('任务类型'), anti: pick('防干扰'),
                        status: pick('任务状态'), executed: pick('已执行次数'),
                        remark: pick('备注'), createdAt: pick('任务创建时间'),
                        publisher: pick('发布人'), _heads: heads,
                    };
                }"""

        row = None
        tries = 0
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                page.evaluate(requery, TASK_NAME)
                tries += 1
                page.wait_for_timeout(2500)
                row = page.evaluate(read_row, TASK_NAME)
            except Exception:
                row = None     # reload 途中 evaluate 会抛（context destroyed），下一轮再来
                page.wait_for_timeout(1500)
            if row and row.get("status"):
                break

        if not row:
            log(f"  (查了 {tries} 次列表里都没找到刚发布的任务 — 可能还在异步落库)")
            return None
        row.pop("_heads", None)
        log("  任务状态: " + json.dumps(row, ensure_ascii=False))
        print("STATUS:" + json.dumps(row, ensure_ascii=False), flush=True)
        return row
    except Exception as e:
        log(f"  (verify status failed: {e})")
        return None


def step_8_publish(page) -> bool:
    # The upload (step 3) already populates vm.newCampaign.hitUserCount with the
    # imported list size. Calling getHitUserCount() here would *replace* that
    # with the global ALL-strategy count, sending the campaign to every user
    # instead of the uploaded list. Only fetch if upload didn't set the count.
    needs_fetch = page.evaluate(
        """
        () => {
            const sub = (document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'));
            const vm = angular.element(sub).scope().vm;
            const n = vm && vm.newCampaign && vm.newCampaign.hitUserCount;
            return n === null || n === undefined || Number(n) <= 0;
        }
        """
    )
    if needs_fetch:
        log("Step 8 pre: upload didn't set hitUserCount — calling getHitUserCount()")
        page.evaluate(
            """
            () => {
                const sub = (document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'));
                const scope = angular.element(sub).scope();
                if (typeof scope.vm.getHitUserCount === 'function') {
                    scope.vm.getHitUserCount();
                    scope.$apply();
                }
            }
            """
        )
        page.wait_for_function(
            """
            () => {
                const sub = (document.querySelector('button[type=submit]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === '发布任务'));
                if (!sub) return false;
                const vm = angular.element(sub).scope().vm;
                return vm && vm.isGettingHitCount === false
                    && vm.newCampaign && (vm.newCampaign.hitUserCount !== null && vm.newCampaign.hitUserCount !== undefined);
            }
            """,
            timeout=20000,
        )
    else:
        log("Step 8 pre: hitUserCount already set from upload — skipping getHitUserCount()")

    log("Step 8: publish")
    # The success handler calls window.location.reload() — the load event is
    # our reliable success signal. The post-reload list view is filtered &
    # empty by default, so don't try to verify the task name there.
    # 后台慢的时候发布成功后的 reload 30s 内等不到 load（白屏），以前直接判失败，
    # 其实任务已经发出去了（2026-11-06 Freeplay T004495）。超时就返回 False，交给调用方回列表核实。
    try:
        with page.expect_event("load", timeout=30000):
            page.evaluate(
                "() => { const b = document.querySelector('button[type=submit]')"
                " || Array.from(document.querySelectorAll('button'))"
                ".find(x => x.textContent.trim() === '发布任务');"
                " if (!b) throw new Error('发布任务 按钮找不到');"
                " if (b.disabled) throw new Error('发布任务 按钮是禁用的（多半是没点获取拿用户数）');"
                " b.click(); }"
                )
    except PWTimeout:
        log("    发布后 30s 没等到页面 reload — 回列表核实是否已发布")
        return False
    return True


def run() -> None:
    try:
        xlsx_path = find_xlsx()
    except Exception as e:
        print(f"ERR: {e}", file=sys.stderr)
        sys.exit(3)
    log(f"xlsx: {xlsx_path}")

    with sync_playwright() as p:
        # We always drive the user's dedicated Chrome via CDP. The user keeps
        # their single FPMS login in that window; we attach as a sibling tab.
        try:
            browser = connect_cdp(p, CDP_URL, timeout=30000, allow_restart=True, log=log)
        except Exception as e:
            print(
                f"ERR: cannot connect to Chrome via CDP at {CDP_URL} ({e}). "
                f"Start the dedicated Chrome via touchda_chrome.sh, then retry.",
                file=sys.stderr,
            )
            sys.exit(2)
        log(f"Attached to existing Chrome via CDP: {CDP_URL}")
        close_headless = None
        if HEADLESS:
            try:
                context, close_headless = headless_context(p, browser, host_of(CAMPAIGN_URL), log=log)
            except Exception as e:
                # 无头起不来（本机 Chrome 缺失 / 借登录态失败）→ 退回可见标签页，派发本身别断
                log(f"[headless] 起不来（{type(e).__name__}: {e}），改在专用 Chrome 里开标签")
                context = None
        if not HEADLESS or close_headless is None:
            contexts = browser.contexts
            context = contexts[0] if contexts else browser.new_context()
        page = context.new_page()

        try:
            step_1_navigate(page)
            _install_create_campaign_patch(page)
            step_2_basic_info(page)
            step_3_upload_xlsx(page, xlsx_path)
            step_4_push_settings(page)
            step_5_group_all(page)
            step_6_reward(page)
            step_7_popup(page)
            step_7c_content(page)
            ensure_hit_user_count(page)
            if AUTO_PUBLISH:
                # 带类型+时间：连派几种时以前同一个文件名互相覆盖，只剩最后一张
                shot = BOT_DIR / ".snapshots" / f"touchda_prepublish_{REWARD_TYPE}{time.strftime('_%m%d-%H%M%S')}.png"
                try:
                    shot.parent.mkdir(exist_ok=True)
                    for old in sorted(shot.parent.glob("touchda_prepublish_*.png"),
                                     key=lambda f: f.stat().st_mtime)[:-30]:
                        old.unlink(missing_ok=True)     # 只留最近 30 张
                    page.screenshot(path=str(shot), full_page=True)
                    log(f"发布前表单截图：{shot}")
                except Exception as e:
                    log(f"发布前截图失败（不影响发布）：{e}")
                    shot = None
                if os.environ.get("TOUCHDA_STOP_BEFORE_PUBLISH") == "1":
                    # 验证用：无头填完 1-7 步 + 截图，就是不点发布
                    print(f"OK:DRY_RUN:{TASK_NAME}", flush=True)
                    return
                if not step_8_publish(page):
                    try:
                        page.goto(CAMPAIGN_URL, wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        log(f"    重新打开列表也超时了：{e}")
                    if not step_9_verify_status(page):
                        raise RuntimeError("点了发布但页面没刷新，列表里也查不到任务 — 去后台确认是否已发布，别直接重派")
                    if shot:
                        lark_say_image(shot)
                    print(f"OK:{TASK_NAME}", flush=True)
                    return
                if shot:
                    lark_say_image(shot)
                print(f"OK:{TASK_NAME}", flush=True)
                step_9_verify_status(page)
            else:
                # Manual mode: hand off to the user. We hold the tab open and
                # watch for the published task to appear in the list (= user
                # clicked 发布任务 and server accepted). On success we post back
                # to lark ourselves; bot already replied "click publish".
                print(f"OK:READY_FOR_PUBLISH:{TASK_NAME}", flush=True)
                log(f"holding tab up to {BRIDGE_HOLD_SECS}s for manual 发布任务 click...")
                try:
                    page.wait_for_function(
                        f"""
                        () => Array.from(document.querySelectorAll('td, tr, .task-name'))
                            .some(el => el.textContent && el.textContent.includes({TASK_NAME!r}))
                        """,
                        timeout=BRIDGE_HOLD_SECS * 1000,
                    )
                    log("task appeared in list — publish succeeded")
                    lark_say(f"🎉 触达任务已发布：{TASK_NAME}")
                    print(f"OK:{TASK_NAME}", flush=True)
                except PWTimeout:
                    log("manual publish watch timed out")
                    lark_say(f"⚠️ 等了 {BRIDGE_HOLD_SECS}s 没看到你点发布，超时收工。")
                except Exception as e:
                    log(f"manual publish watch ended: {e}")
        except Exception as e:
            if _looks_like_login_page(page):
                # 一定要把原始异常带出来。曾经这里只打 SESSION_EXPIRED，
                # 结果登录判定一误报就把真实报错全盖住，排查时完全瞎。
                print(
                    "ERR: SESSION_EXPIRED mid-flow — open your dedicated Chrome (:9222) "
                    f"and log in to mgnt-webserver, then retry.  [底层异常] {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                try:
                    print(f"     当前 URL: {page.url}", file=sys.stderr)
                except Exception:
                    pass
                import traceback
                traceback.print_exc()
                sys.exit(1)
            shot = BOT_DIR / "touchda_last_error.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                print(f"ERR: {e} (screenshot: {shot})", file=sys.stderr)
            except Exception:
                print(f"ERR: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            # Don't close the user's Chrome — just close our tab (best-effort).
            try:
                page.close()
            except Exception:
                pass
            if close_headless:
                try:
                    close_headless()
                except Exception:
                    pass


if __name__ == "__main__":
    run()
