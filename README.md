# 用户触达 · 派发小工具

把一批 player id 派发成 FPMS 后台「用户触达」任务，支持泥码、彩金、优惠券、泥码存送、FS、Freeplay。
工具会在你自己的 Chrome 里打开「创建触达任务」，自动填完：上传名单、推送设置、优惠、弹窗、触达内容。
**默认填完就停在发布前**，由你自己看一眼再点「发布任务」。

> 仅限公司内部使用，不要把仓库或后台地址转发到公司外。

## 需要什么

- macOS + Google Chrome
- Python 3.10 以上，而且要带 Tk。推荐用 [python.org](https://www.python.org/downloads/) 的官方安装包；
  如果用的是 Homebrew 的 Python，还要再装一次 `brew install python-tk`
- 自己的 FPMS 后台账号（工具用的是你自己的登录状态，任务也记在你名下）

## 安装（只要做一次）

仓库是私有的：先把你的 GitHub 账号发给维护人，被加为 Collaborator 并接受邀请后才能 clone。

```bash
git clone https://github.com/yhqatest/touchda-tool.git ~/touchda-tool
cd ~/touchda-tool
./install.sh
```

## 每次使用

1. 双击 **`启动派发小工具.command`**。
2. 第一次用先点「**启动专用 Chrome**」。会弹出一个独立的 Chrome 窗口，在里面登录 mgnt-webserver 后台。
   这个窗口不要关，也别拿来逛别的网站。下次只要它还开着，就不用再登录。
3. 选测试环境：UAT、QAT，或者泳道（填泳道名，例如 `user-engagement-hotfix`）。
   状态灯变绿表示后台已经打开；如果没打开，点「在专用 Chrome 打开后台」。
4. 第一次用要在右边「**我的模版**」里填你自己在后台建好的模版名，例如泥码事件关键词、优惠券模版名。
   填完会自动保存。不确定填得对不对，可以点「验证当前模版」检查。
5. 贴 player id（从表格直接粘也可以），选优惠类型，点「**派发**」。
6. 等日志里出现「表已填好」，切到专用 Chrome，检查一遍，然后点「**发布任务**」。

勾上「**直接发布**」会跳过人工检查直接发出去，奖励会真的发到玩家账号上，确认没问题再勾。

## 更新

点小窗口里的「**检查更新**」，或者在命令行执行：

```bash
cd ~/touchda-tool && git pull
```

然后重开小窗口。

## 常见问题

| 现象 | 处理 |
|---|---|
| 状态灯显示「专用 Chrome 没开」 | 点「启动专用 Chrome」 |
| 日志里出现 `SESSION_EXPIRED` | 后台登录过期了，在专用 Chrome 里重新登录 |
| 提示「模版没填」或模版找不到 | 「我的模版」里的名字要和后台列表里的完全一样，用「验证当前模版」检查 |
| 提示「里面还有别的 xlsx」 | 名单目录（默认 `data/`）只能有 `player_id.xlsx` 一个文件，把其他的挪走 |
| 启动时报 `No module named tkinter` | 你的 Python 没带 Tk，见上面「需要什么」 |

出问题时，把 `touchda_dispatch.log` 的最后几十行发给维护人。

## 文件说明

| 文件 | 作用 |
|---|---|
| `touchda_gui.py` | 小窗口 |
| `touchda_run.py` | 真正填表的脚本，也能在命令行单独跑（参数都走环境变量，见文件头） |
| `touchda_verify_template.py` | 只读检查某个模版在后台存不存在 |
| `touchda_matrix_run.py` | 把每种优惠类型都跑一遍，默认只建不发；GUI「全派」用它 `--publish` 给同一批玩家一次派完优惠代码 / 泥码存送 / 泥码 / FS / Freeplay |
| `chrome_cdp.py` / `touchda_chrome.sh` | 启动和连接专用 Chrome |
| `config.json` | 你个人的模版和设置（不会提交进仓库） |
