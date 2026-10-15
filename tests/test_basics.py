import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import touchda_config
from touchda_gui import campaign_url, extract_player_ids


def test_ids_prefer_a_prefixed_and_ignore_serials_and_phones():
    raw = "1 a1002201147 — 639022700670\n2 a1002201148"
    assert extract_player_ids(raw) == ["a1002201147", "a1002201148"]


def test_ids_fall_back_to_digits_and_dedupe():
    assert extract_player_ids("123, 456 123") == ["123", "456"]


def test_env_urls():
    assert campaign_url("uat", "", "") == "https://mgnt-webserver.platform99.me/campaign"
    assert campaign_url("qat", "x", "") == "https://mgnt-webserver.platform88.me/campaign"
    assert campaign_url("manual", "User-Hotfix", "platform88.me") == \
        "https://user-hotfix-mgnt-web.platform88.me/campaign"
    assert campaign_url("manual", "", "") == ""
    assert campaign_url("manual", "https://a-mgnt-web.platform99.me/", "") == \
        "https://a-mgnt-web.platform99.me/campaign"


def test_config_fills_env_but_explicit_env_wins(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"coupon_template": "FROM_CFG", "fs_template": "FS_CFG"}))
    monkeypatch.setattr(touchda_config, "CONFIG_PATH", cfg)
    monkeypatch.delenv("TOUCHDA_COUPON_TEMPLATE", raising=False)
    monkeypatch.setenv("TOUCHDA_FS_TEMPLATE", "FS_ENV")
    touchda_config.apply_env_defaults()
    assert os.environ["TOUCHDA_COUPON_TEMPLATE"] == "FROM_CFG"
    assert os.environ["TOUCHDA_FS_TEMPLATE"] == "FS_ENV"


def test_upload_dir_defaults_to_repo_data(tmp_path, monkeypatch):
    monkeypatch.setattr(touchda_config, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.delenv("TOUCHDA_UPLOAD_DIR", raising=False)
    assert touchda_config.upload_dir() == touchda_config.HERE / "data"


def test_no_personal_data_in_code():
    """代码里不许出现任何人的个人模版名 / 本机路径（全放 config.json）。"""
    root = Path(__file__).resolve().parent.parent
    banned = ("/Users/", "yhtest", "JHautoB", "kxtestsz", "Lucky Deals 0319")
    for f in list(root.glob("*.py")) + list(root.glob("*.sh")):
        text = f.read_text(encoding="utf-8")
        for b in banned:
            assert b not in text, f"{f.name} 里有个人数据 {b!r}"


def test_matrix_defaults_to_not_publishing():
    src = (Path(__file__).resolve().parent.parent / "touchda_matrix_run.py").read_text(encoding="utf-8")
    assert 'action="store_true", default=False' in src
