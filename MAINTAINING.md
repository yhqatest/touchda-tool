# 维护说明（给维护人）

## 只有这一份代码

这个仓库是触达派发脚本**唯一的一份**。`cli bot` 里的 `touchda_run.py`、`touchda_verify_template.py`、
`touchda_matrix_run.py` 都是指向这里的软链接：

- 群里 @Hei 派发、cli bot GUI 的「派发触达」tab，跑的都是这里的代码。
- **改 bug 只改这里**。改完自己那边马上生效，推上去以后同事「检查更新」就能拿到。
- 不要在 cli bot 里把软链接换成普通文件，否则两边会越改越不一样。

`chrome_cdp.py` 和 `touchda_chrome.sh` 在 cli bot 里还各有一份，因为 FPMS 查询、存款等别的脚本也要用。
这两个文件很少改动；如果改了，记得两边同步。

## 后台改版时怎么修

FPMS 用户触达后台改版（字段改名、下拉变成 radio 之类）是最常见的维护原因：

1. 在专用 Chrome 里按 `TOUCHDA_AUTO_PUBLISH=0` 跑一次，看停在第几步、报的是什么错。
   失败时会自动截图保存为 `touchda_last_error.png`。
2. 改 `touchda_run.py` 里对应的 `step_*` 函数。
3. 用「跑全部类型（只建不发）」回归一遍：每种类型都填一遍表，但不会真的发布。
4. 跑 `python3 -m pytest -q`。

## 发版

1. 修改 `VERSION`，例如 `1.0.0` → `1.0.1`。改了界面或用法就升中间那位。
2. 在 `CHANGELOG.md` 最上面加一条，写清楚同事能感知到的变化。
3. 执行 `git commit` 和 `git push`。
4. 在群里通知同事点「检查更新」。

## 规矩

- **代码里不能写任何人的个人数据**，包括模版名、路径、账号，全部放进 `config.json`（这个文件被 gitignore 了）。
  加新的个人参数时，需要改三处：`touchda_config.ENV_KEYS`、`config.example.json`、GUI 的 `TEMPLATE_FIELDS`。
- 小窗口里的子线程不能直接操作 Tk 控件，只能往 `self.q` 投消息（Tk 9.0 下直接操作会静默失效，甚至闪退）。
- 默认不发布，这一点不能改：GUI 的「直接发布」默认不勾，矩阵脚本默认只建不发。
