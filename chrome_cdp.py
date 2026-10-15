"""Shared CDP-attach helper for the dedicated FPMS Chrome, with auto-recovery.

The dedicated Chrome (profile ~/.fpms-chrome, remote-debugging :9222) sometimes
hangs — its CDP endpoint answers /json/version but connect_over_cdp stalls
(too many targets / a wedged process). Recovery = force-kill it, clear the
session-restore files (so it comes back clean; cookies/logins are kept), relaunch
via touchda_chrome.sh, and reconnect.

- Step 1 (newusertask): safe to restart — it navigates to /new fresh anyway.
- Step 2 (growth sub-tasks): must NOT restart (would lose the page Step 1 opened),
  so it only does a plain reconnect retry.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

_CHROME_SCRIPT = Path(__file__).resolve().parent / "touchda_chrome.sh"
_FPMS_DEFAULT = Path.home() / ".fpms-chrome" / "Default"

# 专用 Chrome 只跑自动化，但常年累积浏览残留页。connect_over_cdp 初始化会枚举并
# 附着到每个 target，页太多就会超时，所以连接前会清一次残留页。
#
# 名单分两级 —— 因为"关掉一个 Figma 页"和"关掉一篇正在写的飞书文档"代价完全不同：
#   _SAFE_PRUNE_HOSTS  纯粹的浏览残留，关了没损失，随时可重开 → 预检就清
#   _WORK_HOSTS        承载真实工作内容（飞书文档/表格/wiki）→ 预检绝不动它，
#                      只有在 connect 真的失败、必须降 target 数时才作为最后手段清
# 不在任何名单里的一律保留，绝不误关自动化页面。
_SAFE_PRUNE_HOSTS = ("figma.com", "netlify.app", "jenkins", "devtools://")
_WORK_HOSTS = ("larksuite.com", "feishu.cn")
# 超过这个页数就在连接前先清一次浏览残留页。
# 阈值定太低会导致「每次跑脚本都触发清理」——实际使用中常年开 20+ 个标签。
_PRUNE_THRESHOLD = int(os.environ.get("CDP_PRUNE_THRESHOLD", "25"))
# **默认不清页**（2026-09-15 用户拍板：别关我的标签）。这个 Chrome 当初假设"只跑
# 自动化"，实际用户在里面开着飞书文档、Figma、后台报表在干正事，关掉代价很大 ——
# 实测关掉过用户正在用的报表页两次。要恢复旧行为显式设 CDP_PRUNE=1。
_PRUNE_ENABLED = os.environ.get("CDP_PRUNE", "0") == "1"
_PRUNE_WORK = os.environ.get("CDP_PRUNE_WORK") == "1"
_TABS_SNAPSHOT = Path("/tmp/fpms-chrome-tabs.txt")


def _http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def snapshot_tabs(cdp_url: str = "http://localhost:9222", log=print) -> int:
    """把当前所有 page 的 URL 存到 /tmp/fpms-chrome-tabs.txt，方便被关掉后找回来。
    在任何会关页/重启 Chrome 的动作之前调用。返回记录条数。"""
    try:
        urls = [t.get("url") or "" for t in _http_json(f"{cdp_url.rstrip('/')}/json/list")
                if t.get("type") == "page"]
    except Exception:
        return 0
    if not urls:
        return 0
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with _TABS_SNAPSHOT.open("a", encoding="utf-8") as f:
            f.write(f"\n# {stamp}  ({len(urls)} tabs)\n")
            for u in urls:
                f.write(u + "\n")
        log(f"[cdp-tabs] 已把 {len(urls)} 个 tab 的 URL 记到 {_TABS_SNAPSHOT}")
    except Exception:
        pass
    return len(urls)


def prune_tabs(cdp_url: str = "http://localhost:9222", log=print, aggressive: bool = False) -> int:
    """关闭专用 Chrome 里的浏览残留页，保留自动化页。

    aggressive=False（预检默认）：只关 _SAFE_PRUNE_HOSTS —— 关了没损失的页。
    aggressive=True（连接失败后的最后手段）：连 _WORK_HOSTS（飞书文档等）一起关，
    因为此时不降 target 数就连不上；关之前会先把 URL 快照存盘并逐条打印。

    返回关闭数量。任何异常都吞掉——清理是尽力而为，不该阻断主流程。"""
    if not _PRUNE_ENABLED:
        log("[cdp-prune] CDP_PRUNE=0 → 跳过清页")
        return 0
    hosts = list(_SAFE_PRUNE_HOSTS)
    if aggressive or _PRUNE_WORK:
        hosts += list(_WORK_HOSTS)
    base = cdp_url.rstrip("/")
    try:
        targets = _http_json(f"{base}/json/list")
    except Exception as e:
        log(f"[cdp-prune] 读取 targets 失败（跳过清理）: {e}")
        return 0
    victims = [t for t in targets if t.get("type") == "page"
               and any(h in (t.get("url") or "").lower() for h in hosts)]
    if not victims:
        return 0
    if any(h in (t.get("url") or "").lower() for t in victims for h in _WORK_HOSTS):
        snapshot_tabs(cdp_url, log)
    closed = 0
    for t in victims:
        try:
            urllib.request.urlopen(f"{base}/json/close/{t['id']}", timeout=5).read()
            closed += 1
            log(f"[cdp-prune] 关闭：{(t.get('url') or '')[:110]}")
        except Exception:
            pass
    if closed:
        log(f"[cdp-prune] 共关闭 {closed} 个浏览残留页"
            + ("（含飞书文档，URL 见上面快照）" if aggressive or _PRUNE_WORK else ""))
    return closed


def _page_count(cdp_url: str) -> int:
    try:
        return sum(1 for t in _http_json(f"{cdp_url.rstrip('/')}/json/list") if t.get("type") == "page")
    except Exception:
        return 0


def _restart_chrome(log) -> None:
    log("[cdp-recover] 专用 Chrome 无响应 → 杀进程 + 清 session-restore + 重开")
    # 重启会清掉 session-restore，"恢复上次打开的标签页"救不回来 → 先把 URL 存盘。
    snapshot_tabs(log=log)
    subprocess.run(["pkill", "-9", "-f", r"\.fpms-chrome"], check=False)
    time.sleep(3)
    # Remove only session/tab restore files (Cookies / Login Data untouched).
    for name in ("Sessions", "Current Session", "Current Tabs", "Last Session", "Last Tabs"):
        target = _FPMS_DEFAULT / name
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except Exception:
            pass
    subprocess.run(["/bin/zsh", str(_CHROME_SCRIPT)], check=False)
    time.sleep(3)


# **默认不重启 Chrome**（同上）。_restart_chrome 会 pkill -9 并删掉 session-restore，
# 用户当前所有标签页当场消失且"恢复上次打开的标签页"也救不回来 —— 代价远大于
# 一次连接失败。要恢复旧行为显式设 CDP_ALLOW_RESTART=1，或调用时传 allow_restart=True。
_ALLOW_RESTART_DEFAULT = os.environ.get("CDP_ALLOW_RESTART") == "1"


def connect_cdp(p, cdp_url: str, timeout: int = 30000, allow_restart: bool = None, log=print):
    """Attach to the dedicated Chrome over CDP. On failure, either restart Chrome
    and reconnect (allow_restart=True) or prune browsing tabs + retry once
    (allow_restart=False). Raises the final exception if recovery also fails."""
    if allow_restart is None:
        allow_restart = _ALLOW_RESTART_DEFAULT
    # 办公室 Wi-Fi 约每 10 分钟掉一次（见文件末尾「断网重试」一节）。在这里装补丁，
    # 45 个用 chrome_cdp 的脚本不用改一行就都扛得住；要关掉设 CDP_NET_RETRY=0。
    install_page_retry(log=log)
    # Pre-flight：页太多是 connect_over_cdp 超时的头号原因，连接前先清一次浏览残留页。
    # 默认已关闭（_PRUNE_ENABLED），prune_tabs 内部会直接返回。
    if _page_count(cdp_url) > _PRUNE_THRESHOLD:
        log(f"[cdp-prune] 页数 > {_PRUNE_THRESHOLD}，连接前先清理（不动飞书文档）")
        prune_tabs(cdp_url, log, aggressive=False)
    # 先重试几次再考虑重启。connect_over_cdp 偶发失败（例如
    # "Browser context management is not supported"）多半是瞬时的，
    # 隔几秒就好；而 _restart_chrome 会 pkill -9 并清 session-restore，
    # 等于把用户当前所有标签页都干掉 —— 代价太大，不能当第一手段。
    last_exc = None
    for attempt in range(3):
        try:
            return p.chromium.connect_over_cdp(cdp_url, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                log(f"[cdp-recover] 连接失败（第 {attempt + 1}/3 次），2s 后重试: "
                    f"{str(exc).splitlines()[0][:90]}")
                time.sleep(2)
    try:
        raise last_exc
    except Exception as e:
        log(f"[cdp-recover] 三次都连不上: {e}")
        if allow_restart:
            log("[cdp-recover] 即将重启专用 Chrome —— 当前所有标签页会被关闭（URL 已快照）")
            _restart_chrome(log)
        else:
            # 不能重启（会丢失当前页）：清掉浏览残留页把 target 数降下来再重连一次，
            # 这正是"ws 连上但初始化超时"的解法，比单纯 sleep 有效。
            if _PRUNE_ENABLED:
                log("[cdp-recover] 不重启（会丢失当前页）→ 清理浏览残留页后重连一次")
                prune_tabs(cdp_url, log, aggressive=True)
            else:
                log("[cdp-recover] 不重启也不清页（CDP_PRUNE=0）→ 等 5 秒直接重连一次")
                log("[cdp-recover] 还是连不上的话：标签开太多就手动关几个，"
                    "或本次显式 CDP_ALLOW_RESTART=1 允许重启")
                time.sleep(3)
            time.sleep(2)
        return p.chromium.connect_over_cdp(cdp_url, timeout=timeout)


def host_of(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower()
    except Exception:
        return ""


def pick_page(browser, must_include, prefer_host=None, log=print, what="页面"):
    """在专用 Chrome 已开的 tab 里挑一个匹配页，返回 (page, host_ok)。

    为什么需要它：各后台不同环境（platform99 / platform88 / nwrplatform99）长得
    一模一样，只按 URL 子串（"/player"、"/marketing"）挑 tab，同时开着两个环境时
    命中哪个是任意的 —— 选错不会报错，只会静默地在错的平台上查数/改数据。
    所以这里统一做三件事：打印实际用了哪个页、多候选时告警、能按目标 host 优先。

    must_include  URL 必须同时包含的子串（str 或 list）
    prefer_host   期望 host（通常取自目标 URL）；命中它的候选优先
    返回          (page, host_ok)；没有候选时 (None, False)
    """
    if isinstance(must_include, str):
        must_include = [must_include]
    pages = [pg for pg in browser.contexts[0].pages
             if all(m in (pg.url or "") for m in must_include)]
    if not pages:
        return None, False
    host_ok = False
    if prefer_host:
        # prefer_host 可以是完整 URL（按 host 精确比对），也可以是裸子串
        # （如 REPORT_HOST="platform88"，按子串匹配）—— 两种写法项目里都在用。
        want_host = host_of(prefer_host)
        if want_host:
            on_host = [pg for pg in pages if host_of(pg.url) == want_host]
            want = want_host
        else:
            want = prefer_host.strip().lower()
            on_host = [pg for pg in pages if want in (pg.url or "").lower()]
        if on_host:
            if len(pages) > len(on_host):
                log(f"[tab] 有 {len(pages)} 个{what}候选，按目标 host「{want}」选中")
            pages, host_ok = on_host, True
        else:
            log(f"⚠ 已开的{what} host 都不是目标「{want}」，候选："
                + " | ".join(pg.url for pg in pages))
    if len(pages) > 1:
        log(f"⚠ 同时开着 {len(pages)} 个{what}，可能是不同环境 —— 候选："
            + " | ".join(pg.url for pg in pages))
    log(f"[tab] 使用{what}：{pages[0].url}")
    return pages[0], host_ok


# ---------------------------------------------------------------------------
# 断网重试
#
# 办公室 Wi-Fi 约每 10 分钟掉一次、每次 6–30 秒（2026-09-29 实测：AP 在 ch36，
# 被 6 个 160MHz 邻居盖满，信标丢失一度 72%）。这个修不了——macOS 不支持按
# BSSID 锁 AP，弹网卡只会落回同一个烂 AP——所以只能让脚本扛过去。
#
# 关键是**只对网络类错误重试**。选择器超时、元素找不到这些是真 bug，重试一遍
# 只会把 30 秒的失败拖成 5 分钟的失败，还把真正的错因埋掉。
# ---------------------------------------------------------------------------

# Chromium 在断网/切换网络时抛的错码。只列真正「等一会儿就好」的那些：
# ERR_ABORTED、ERR_BLOCKED_BY_CLIENT 这类重试没意义，不收。
_NET_ERR_MARKERS = (
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_CHANGED",
    "ERR_NAME_NOT_RESOLVED",       # DNS 挂了——SASE 隧道重建期间必现
    "ERR_NAME_RESOLUTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_FAILED",
    "ERR_CONNECTION_TIMED_OUT",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_EMPTY_RESPONSE",
)

# 断网一次通常 6–30 秒。固定 1 秒重试会在几秒内把机会烧光，所以用递增退避：
# 3 → 6 → 12 → 24（封顶 30），5 次机会累计等待约 45 秒，够盖住一次掉线。
_NET_RETRY_ATTEMPTS = int(os.environ.get("CDP_NET_RETRIES", "5"))
_NET_RETRY_BASE = float(os.environ.get("CDP_NET_RETRY_BASE", "3"))
_NET_RETRY_CAP = float(os.environ.get("CDP_NET_RETRY_CAP", "30"))


def is_network_error(exc: BaseException) -> bool:
    """这个异常是不是「网断了，等等再来」类型。"""
    msg = str(exc) or ""
    return any(m in msg for m in _NET_ERR_MARKERS)


def _net_backoff(i: int) -> float:
    """第 i 次重试前等多久（i 从 0 起）。"""
    return min(_NET_RETRY_BASE * (2 ** i), _NET_RETRY_CAP)


def goto_resilient(page, url: str, attempts: int = None, log=print, **kw):
    """page.goto 的抗断网版本。

    只有 `is_network_error` 认定的错误才重试；其余立刻原样抛出，
    不掩盖真正的失败。重试耗尽后抛最后一次的异常。
    """
    # 页面已被 install_page_retry 接管时，page.goto 自己就会重试 —— 这里再包一层
    # 就成了 5×5=25 次、一次断网空转十几分钟，所以直接透传。
    if getattr(getattr(page, "goto", None), "_netretry", False):
        return page.goto(url, **kw)
    attempts = attempts or _NET_RETRY_ATTEMPTS
    last = None
    for i in range(attempts):
        try:
            return page.goto(url, **kw)
        except Exception as e:                      # noqa: BLE001 - 要按错误内容分流
            if not is_network_error(e):
                raise
            last = e
            if i == attempts - 1:
                break
            wait = _net_backoff(i)
            log(f"[断网] {url} 第 {i + 1}/{attempts} 次失败（{type(e).__name__}），"
                f"{wait:.0f}s 后重试")
            time.sleep(wait)
    log(f"[断网] {url} 重试 {attempts} 次仍失败，放弃")
    raise last


def retry_on_netdrop(fn, *args, attempts: int = None, log=print, what="操作", **kw):
    """把任意会走网络的调用包成抗断网版本（点击后等跳转、等接口返回等）。

    用法：retry_on_netdrop(page.wait_for_url, "**/dashboard", what="等跳转")
    """
    attempts = attempts or _NET_RETRY_ATTEMPTS
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kw)
        except Exception as e:                      # noqa: BLE001
            if not is_network_error(e):
                raise
            last = e
            if i == attempts - 1:
                break
            wait = _net_backoff(i)
            log(f"[断网] {what} 第 {i + 1}/{attempts} 次失败，{wait:.0f}s 后重试")
            time.sleep(wait)
    log(f"[断网] {what} 重试 {attempts} 次仍失败，放弃")
    raise last


def install_page_retry(page_cls=None, log=print):
    """给 Playwright 的 Page.goto 装上断网重试。

    为什么用打补丁而不是改调用点：全仓库 36 个 goto 散在 25 个文件里，接收者
    变量名有 page / pg / p2 / own 四种，逐个改既容易漏又容易改错。补丁只有一个
    入口、覆盖全部，而且装两次也只生效一次。

    不想要就设 CDP_NET_RETRY=0（比如排查问题时希望断网立刻暴露）。
    """
    if os.environ.get("CDP_NET_RETRY") == "0":
        return False
    if page_cls is None:
        try:
            from playwright.sync_api import Page as page_cls  # noqa: N813
        except ImportError:
            return False
    if getattr(page_cls.goto, "_netretry", False):
        return False                      # 已经装过，别叠加

    # goto 和 reload 都是导航，撞断网的失败方式完全一样，一起包。
    # 非导航的网络等待（wait_for_url / expect_response）用 retry_on_netdrop 显式包，
    # 因为它们的超时语义各不相同，统一重试反而会掩盖真的等错了东西。
    installed = False
    for _name in ("goto", "reload"):
        _orig = getattr(page_cls, _name, None)
        if _orig is None or getattr(_orig, "_netretry", False):
            continue
        setattr(page_cls, _name, _wrap_nav(_orig, _name, log))
        installed = True
    return installed


def _wrap_nav(orig, name, log):
    """把一个导航方法包成断网会重试的版本。"""

    def nav(self, *a, **kw):
        where = a[0] if a else name        # goto 有 url，reload 没有
        last = None
        for i in range(_NET_RETRY_ATTEMPTS):
            try:
                return orig(self, *a, **kw)
            except Exception as e:        # noqa: BLE001 - 要按错误内容分流
                if not is_network_error(e):
                    raise
                last = e
                if i == _NET_RETRY_ATTEMPTS - 1:
                    break
                wait = _net_backoff(i)
                log(f"[断网] {where} 第 {i + 1}/{_NET_RETRY_ATTEMPTS} 次失败，"
                    f"{wait:.0f}s 后重试")
                time.sleep(wait)
        log(f"[断网] {where} 重试 {_NET_RETRY_ATTEMPTS} 次仍失败，放弃")
        raise last

    nav._netretry = True
    return nav


def is_online(host: str = "8.8.8.8", timeout: float = 2.0) -> bool:
    """网通不通。用 TCP 连一下 DNS 端口 —— 不做域名解析，因为 SASE 隧道重建
    期间 DNS 本身就是坏的（见 2026-09-29 诊断），拿解析结果判断会误判成断网。"""
    import socket
    try:
        with socket.create_connection((host, 53), timeout=timeout):
            return True
    except OSError:
        return False


def retry_if_offline(fn, *args, attempts: int = None, log=print, what="操作", **kw):
    """给「超时类」失败用的重试：expect_response / wait_for_url 撞上断网抛的是
    Timeout，不带 net::ERR_*，`is_network_error` 认不出来。

    但**不能见超时就重试** —— 页面改版、选择器写错也是超时，盲目重试会把 20 秒
    的失败拖成两分钟，还把真正的错因埋掉。所以这里只在「出错那一刻网确实断了」
    时才重试；网通着就原样抛出。
    """
    attempts = attempts or _NET_RETRY_ATTEMPTS
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kw)
        except Exception as e:                      # noqa: BLE001 - 要按当时网况分流
            last = e
            if is_online():
                raise                               # 网通着还失败 = 真 bug
            if i == attempts - 1:
                break
            wait = _net_backoff(i)
            log(f"[断网] {what} 失败且当前离线（第 {i + 1}/{attempts} 次），"
                f"{wait:.0f}s 后重试")
            time.sleep(wait)
    log(f"[断网] {what} 重试 {attempts} 次仍失败，放弃")
    raise last


def wait_online(timeout: float = 120.0, interval: float = 3.0, log=print) -> bool:
    """阻塞等到网恢复，最多等 timeout 秒。回来返回 True，等不回来返回 False。

    等不回来时**不抛异常** —— 调用方通常还有自己的重试预算，该放弃还是再试一次
    由它决定，这里替它决定只会把行为埋进两层逻辑里。
    """
    if is_online():
        return True
    deadline = time.monotonic() + timeout
    log(f"[断网] 等待网络恢复（最多 {timeout:.0f}s）…")
    while time.monotonic() < deadline:
        time.sleep(interval)
        if is_online():
            log("[断网] 网络已恢复")
            return True
    log(f"[断网] 等了 {timeout:.0f}s 网络仍未恢复")
    return False


def headless_context(p, browser, host: str, log=print):
    """借专用 Chrome 的登录态，另起一个无头 Chrome，返回 (context, 收尾函数)。

    FPMS 后台的登录态分两处：cookie 里的 `sinonet-management-token` 管鉴权，
    **localStorage 的 `ls.sinonet-management-role`（~100KB 权限表）管菜单渲染**。
    `context.storage_state()` 在 CDP 连上的 context 里**拿不到 localStorage**（origins 是空的），
    只带 cookie 能进页面，但菜单/按钮出不来。所以 localStorage 从一个开在 `host` 上的页面 evaluate 出来；
    专用 Chrome 里没开这个 host 就临时开一个读完再关。

    Playwright 自带的 headless shell 没装，用 `channel="chrome"` 跑本机 Google Chrome 的无头模式。
    """
    main_ctx = browser.contexts[0]
    st = main_ctx.storage_state()
    src = next((x for x in main_ctx.pages if host_of(x.url) == host), None)
    tmp = None
    if src is None:
        tmp = src = main_ctx.new_page()
        src.goto(f"https://{host}/", wait_until="domcontentloaded", timeout=60000)
        src.wait_for_timeout(3000)
    ls = src.evaluate("() => Object.keys(localStorage).map(k => ({name: k, value: localStorage[k]}))")
    if tmp:
        tmp.close()
    if not any(x["name"] == "ls.sinonet-management-role" for x in ls):
        log(f"  ⚠️ 专用 Chrome 里没读到 {host} 的权限表（没登录？），无头模式大概率打不开页面")
    st["origins"] = [{"origin": f"https://{host}", "localStorage": ls}]
    hb = p.chromium.launch(headless=True, channel="chrome")
    ctx = hb.new_context(storage_state=st, viewport={"width": 1600, "height": 1000})
    log(f"[headless] 已借专用 Chrome 的登录态（{host}），在无头 Chrome 里跑")
    return ctx, hb.close


def headless_enabled() -> bool:
    """各脚本共用的无头开关：HEADLESS=0 关掉，默认开。账号管理 tab 由复选框传 HEADLESS（默认 0，2026-10-01 用户要求该 tab 不走无头）。"""
    return os.environ.get("HEADLESS", "1") != "0"


_KILL_PW_MODAL_JS = r"""() => {
  try { $('#modalUpdatePassword').modal('hide'); } catch (e) {}
  document.querySelectorAll('.modal.in,.modal-backdrop').forEach((e) => e.remove());
  document.body.classList.remove('modal-open');
}"""


def clone_page_headless(p, browser, src_page, log=print, ready_selector: str = None):
    """把专用 Chrome 里某个已登录的后台页，在无头 Chrome 里原样开一份，返回新页面。

    用法是「先照老办法挑出对的那个页（环境靠它把关），再调这个」—— 选环境的逻辑一点不动，
    只是后面的点点填填挪到无头里做，用户的标签页不会被切走、不会被改搜索条件。

    新开的 FPMS 页有两个坑要在这里一次处理掉：
      - 每月强制「更新密码」弹窗（#modalUpdatePassword）会挡住所有点击
      - 顶部面板（报表/网站业务/运营…）不点开，左侧菜单根本不在 DOM 里
        （见 halloween_report_dump 坑 14），所以按 URL 第一段点一下对应的 openHeaderPanel
    无头浏览器随 `with sync_playwright()` 退出一起关掉，不用另外收尾。
    """
    host = host_of(src_page.url)
    ctx, _close = headless_context(p, browser, host, log=log)
    pg = ctx.new_page()
    pg.goto(src_page.url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(5000)
    pg.evaluate(_KILL_PW_MODAL_JS)
    panel = (urlparse(src_page.url).path.strip("/").split("/") or [""])[0]

    def open_panel() -> None:
        if not panel:
            return
        clicked = pg.evaluate(
            """(name) => {
                const e = [...document.querySelectorAll('[ng-click]')]
                  .find(x => (x.getAttribute('ng-click') || '').includes("openHeaderPanel('" + name + "')"));
                if (!e) return false;
                e.click();
                return true;
            }""", panel)
        if clicked:
            pg.wait_for_timeout(2500)
            pg.evaluate(_KILL_PW_MODAL_JS)

    open_panel()
    if ready_selector:
        # 冷启动的无头 Chrome 有时 5s 内 Angular 还没起来，那一下 openHeaderPanel 白点 →
        # 输入框在 DOM 里但一直 hidden（2026-10-01 设VIP 卡 30s 超时）。没出来就再点一次面板。
        loc = pg.locator(ready_selector).first
        for attempt in range(4):
            try:
                loc.wait_for(timeout=8000)
                break
            except Exception:
                if attempt == 3:
                    raise
                log(f"  [headless] 页面还没就绪，重新打开顶部面板（第 {attempt + 1} 次）")
                pg.evaluate(_KILL_PW_MODAL_JS)
                open_panel()
    return pg
