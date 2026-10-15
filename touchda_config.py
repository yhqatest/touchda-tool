"""个人配置：config.json（不进 git）→ 环境变量默认值。

优先级：调用方显式传的环境变量 > config.json > 代码里的空默认。
所以 GUI / bot 传进来的值永远赢，config.json 只是兜底。每个人的模版名、
xlsx 目录都不一样，全放 config.json，代码里不写任何人的个人数据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
EXAMPLE_PATH = HERE / "config.example.json"

# config.json 键 → 脚本读的环境变量
ENV_KEYS = {
    "upload_dir":            "TOUCHDA_UPLOAD_DIR",
    "campaign_url":          "TOUCHDA_CAMPAIGN_URL",
    "cdp_url":               "TOUCHDA_CDP_URL",
    "mud_event_keyword":     "TOUCHDA_MUD_EVENT_KEYWORD",
    "mud_display_name":      "TOUCHDA_MUD_DISPLAY_NAME",
    "credit_amount":         "TOUCHDA_CREDIT_AMOUNT",
    "credit_validity_hours": "TOUCHDA_CREDIT_VALIDITY_HOURS",
    "coupon_template":       "TOUCHDA_COUPON_TEMPLATE",
    "coupon_pct_template":   "TOUCHDA_COUPON_PCT_TEMPLATE",
    "mud_deposit_template":  "TOUCHDA_MUD_DEPOSIT_TEMPLATE",
    "fs_template":           "TOUCHDA_FS_TEMPLATE",
    "freeplay_template":     "TOUCHDA_FREEPLAY_TEMPLATE",
}


def load() -> dict:
    """读 config.json；没有或坏了就返回空 dict（GUI 会提示去填）。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


def apply_env_defaults() -> None:
    """把 config.json 里的值塞进环境变量——只在调用方没传时才塞。"""
    cfg = load()
    for key, env in ENV_KEYS.items():
        val = cfg.get(key)
        if val is not None and str(val).strip() and not os.environ.get(env):
            os.environ[env] = str(val).strip()


def upload_dir() -> Path:
    """player_id.xlsx 放哪：环境变量 > config.json > 仓库下的 data/。"""
    raw = os.environ.get("TOUCHDA_UPLOAD_DIR") or load().get("upload_dir") or ""
    return Path(os.path.expanduser(raw)) if str(raw).strip() else HERE / "data"
