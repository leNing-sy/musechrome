# MuseChrome · Muse 账号管理器

本项目是基于 [xiaoyaodev/musechrome](https://github.com/xiaoyaodev/musechrome) 的独立二开版，由 [leNing-sy/musechrome](https://github.com/leNing-sy/musechrome) 维护。v1.9.0 提供 Chrome / Edge 多账号独立配置与通用邮箱辅助，程序调用本机浏览器，不内置浏览器。

> **升级与数据保护：**替换程序前，请退出 MuseChrome、关闭它打开的所有浏览器窗口，并另外保存完整的 `MuseChromeData` 副本。完整备份包含浏览器登录凭据，不能随安装包转发。Windows 或浏览器可能将凭据绑定到原用户及设备，复制数据或导出 ZIP 均不保证换机后仍保持登录；请保留重新登录所需的邮箱与验证方式。

## 下载与构建状态

从 [Windows Build](https://github.com/leNing-sy/musechrome/actions/workflows/windows-build.yml) 中对应提交的成功运行下载 artifact ZIP。请核对运行状态和提交号；旧的 v1.8.1 或旧提交产物不含本版全部改动。ZIP 内包含 `MuseChrome.exe` 和 `SHA256SUMS.txt`，可用 `Get-FileHash -Algorithm SHA256 .\MuseChrome.exe` 与清单核对。

发布包是 PyInstaller 单文件 EXE，没有安装向导；运行时无需另装 Python、PyQt6 或 Playwright。本机需已有 Chrome 或 Edge，两种浏览器均提供邮箱辅助。程序没有软件内更新功能，更换版本需手动下载。构建文件未做代码签名，Windows 可能提示发布者未知，请核实来源与校验信息。

## 保留的功能

- **独立浏览器配置：**每个账号拥有独立 profile，Chrome 与 Edge 也分别存储。网站登录状态能否持续有效由网站、浏览器和本机环境决定。
- **基本账号管理：**添加账号、修改名称、删除账号，以及按账号打开或关闭浏览器。管理器跟踪到该账号浏览器仍运行时，禁止切换浏览器；关闭失败会保留运行状态。
- **代理设置：**全局可选本地 HTTP/SOCKS5、跟随系统代理或直连；单个账号可覆盖全局代理。代理不会替用户启动，使用前需自行配置并运行代理工具。
- **连接检查：**仅检查当前配置指定的代理连接；不扫描其他本地端口、不查询出口 IP、不自动更改代理设置。
- **通用邮箱辅助：**Chrome 与 Edge 共用同一流程。从 CSV 选择邮箱并复制，可选打开固定收件网址或 `{email}` 地址模板，也可只复制邮箱。收件页可交给当前账号浏览器或系统默认浏览器打开。程序不会自动登录邮箱、填表、读信或提交验证码。
- **基础设置：**主页、浏览器路径、英文语言参数和 WebRTC 非代理 UDP 限制，以及浅色/深色外观和关于页。启动参数只能控制相关浏览器行为，不等于网站登录或网络隐私的保证。
- **备份导出：**保留轻量和完整 ZIP 导出；软件内导入恢复入口暂时移除。

本分支已移除软件内更新、公告与公告图片、插件助手、打开全部、卡片动画、账号备注与颜色编辑、出口 IP 查询、端口扫描和指纹测试快捷入口。旧账号记录已有的 `note`、`color` 字段继续保留往返存储，但不再显示或编辑；不迁移或删除原有 profile。扩展可在各账号的浏览器中手动安装。

## 使用

1. 将对应构建的 ZIP 解压到一个固定、可写的目录，再运行 `MuseChrome.exe`。不要直接在 ZIP 内运行。
2. 添加账号并选择 Chrome 或 Edge；按需在设置或账号卡片配置代理。
3. 点击该账号的“打开”，在其独立窗口完成 Muse 登录。之后仍通过这张账号卡片打开；网站要求重新验证时按页面提示处理。
4. 切换 Chrome / Edge 前先关闭该账号浏览器；修改代理或语言等启动设置后也需关闭并重开才能生效。

Chrome 和 Edge 使用不同 profile；同一个账号分别使用两个浏览器时，需要各自登录。浏览器账号（Google / Microsoft）与 Muse 等网站账号属于不同登录层。本版为两种浏览器传入禁用浏览器登录、同步的启动参数，Edge 另外保留隐式登录限制；这些参数不影响通过网页进行网站登录的设计目标，但不能替代对具体浏览器版本和系统策略的真实验证，不保证所有版本均能阻断隐式登录或同步。

## 邮箱辅助设置

在设置页选择 CSV，并直接填写收件地址和打开位置，无需另外准备邮件服务 `config.json`。

| 收件地址 | 行为 |
| --- | --- |
| 留空 | 只复制所选邮箱，不打开网页 |
| 固定网址，例如 `https://outlook.live.com/mail/` | 复制邮箱并打开该网址 |
| 模板，例如 `https://mail.example.com/{email}` | 将 `{email}` 替换为经过 URL 编码的邮箱，再打开收件页 |

支持 HTTP / HTTPS；模板占位符只能使用 `{email}`，放在路径、查询参数或片段中，不能用于域名。设置页提供原取件服务、Outlook 和 Gmail 预设。选择预设后仍可编辑地址，普通邮箱网址不会自动登录 CSV 中选中的邮箱。

新安装默认收件地址为空、打开位置为“当前账号浏览器”。若填写地址：

- **当前账号浏览器：**用所点账号的 Chrome / Edge 独立 profile 打开收件页，沿用该账号的代理等启动设置。若浏览器尚未运行，会启动它；收件页代替主页打开。
- **系统默认浏览器：**交给 Windows 默认浏览器打开；这可能是个人浏览器环境，显示的是那个环境中已经登录的邮箱。

升级时，已经选择过 CSV 的旧配置会继续使用原取件服务或旧自定义配置，打开位置继续为系统默认浏览器。显式保存新的收件地址后以新地址为准；清空地址表示只复制，不会重新启用旧地址。旧自定义配置文件缺失或无效时会提示修正，不会偷偷切换邮件服务。用户 CSV 和 profile 不作迁移。

## 邮箱 CSV

CSV 只需 `邮箱` 一列，每行一个邮箱，使用 UTF-8 编码。例如：

```csv
邮箱
first@example.test
second@example.test
```

在“设置 → 邮箱 CSV → 浏览”选择文件，验证通过后路径自动保存。CSV 可放在任意固定目录，不必放在软件目录；程序保存的是文件路径，移动 CSV 后需要重新选择。密码、验证码、售出标记均不需要提供。请自行使用已筛选好的邮箱清单，程序不会根据售出说明、文件顺序或注册状态替你挑账号。

混用不同邮件服务时，可增加可选列 `收件地址`。该行非空时优先于设置页的地址，空白则使用设置页的地址。收件地址既可写固定网址，也可写含 `{email}` 的模板：

```csv
邮箱,收件地址
first@example.test,https://mail.example.com/{email}
second@example.test,https://outlook.live.com/mail/
third@example.test,
```

程序仅读取 `邮箱` 和可选的 `收件地址`，忽略其他列；不把 CSV 的全部邮箱导入为软件账号。设置页地址和所选行地址均为空时，只复制邮箱。

操作顺序：

1. 在设置中选好 CSV，按需填写收件地址、选择打开位置并保存。
2. 打开需要登录的 Chrome 或 Edge 账号，在对应账号卡片使用邮箱辅助。
3. 在列表中确认本次使用的邮箱；邮箱复制到剪贴板，有收件地址时按设置打开网页。
4. 将邮箱粘贴到目标账号的登录页，自行查收并填写验证码。普通邮箱未登录时，仍需先完成邮箱网站本身的登录。

这个入口不判断哪个邮箱已经登录，也不保存“下一个邮箱”标记；每次均由用户明确选择。MuseChrome 账号列表和邮箱 CSV 保持分开。

## 数据与备份

数据保存在 `MuseChrome.exe` 同目录的 `MuseChromeData`：

```text
MuseChromeData/
  config.json            软件自动管理的全局设置
  accounts.json          账号列表
  chrome-profiles/<id>/  账号的 Chrome 配置
  edge-profiles/<id>/    账号的 Edge 配置
```

这里的软件设置 `config.json` 会由程序管理，不是邮箱辅助登录要求你另外准备的文件。邮箱 CSV 留在原位置，备份导出不包含该外部文件。

导出前关闭所有由本软件打开的浏览器，避免复制正在变化或被锁定的 profile。轻量备份只包含软件配置和账号列表；完整备份还包含浏览器 profile，可能含 Cookie、登录凭据、扩展及浏览数据，必须按敏感凭据保管。不要把完整备份、`MuseChromeData` 或个人 CSV 放进公开仓库和试用分发包。

软件内导入恢复暂时不可用。已有备份文件不作修改；需要恢复时，先另行保留现有数据，并在隔离副本核对备份的完整性和账号归属，再安排恢复。跨电脑或 Windows 用户迁移后的登录态未经保证，可能需要重新登录。删除账号时请仔细确认是否同时删除该账号的浏览器数据。

## 从源码验证与构建

Windows 构建使用 Python 3.11、PyQt6 和 PyInstaller；云端步骤见 [windows-build.yml](.github/workflows/windows-build.yml)。本节供开发者使用，运行已打包 EXE 不需要安装这些依赖。

在已有依赖的开发环境中执行：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
python tests/test_store.py
python tests/test_auto_login.py
$env:QT_QPA_PLATFORM = "offscreen"
python tests/test_mail_assist_ui.py
python tests/test_browser_parity.py
python tests/test_app_pruning.py
python tests/test_update.py
python -m py_compile musechrome/app.py musechrome/store.py musechrome/auto_login.py
pyinstaller --noconfirm MuseChrome.spec
```

产物为 `dist/MuseChrome.exe`。CI 通过 `tests/ci_runner.py` 分别运行上述测试，失败详情写入检查注释，任何一项失败都阻止打包；构建后生成 `dist/SHA256SUMS.txt` 并和 EXE 一起上传。测试使用隔离数据或替身；单元及界面回归通过不等于完成了真实邮箱登录、所有 Chrome / Edge 版本或跨机恢复验证。

```text
main.py               程序入口
musechrome/app.py     界面与浏览器操作
musechrome/store.py   配置、账号与浏览器启动参数
musechrome/auto_login.py  邮箱 CSV 和取件链接
tests/                存储、邮箱与界面回归
assets/               程序图标
MuseChrome.spec       PyInstaller 打包配置
```

## 隐私与限制

程序本身不再自动拉取公告或查询更新。用户点击连接检查时，只测试当前代理设置。使用邮箱辅助时，所选邮箱会写入剪贴板；若收件地址包含 `{email}`，替换后的邮箱会作为链接交给浏览器，目标服务可能收到该邮箱，浏览器也可能记录访问历史。仅复制模式不会打开网页。Chrome、Edge、Muse 网站和邮件服务各自的数据处理规则由其提供方决定。

本程序是独立第三方工具，与 Google、Microsoft、Muse 没有隶属或授权关系。请遵守目标平台的服务条款，自行保管账号和备份，并核对下载来源。软件按 MIT License 的现状条款提供，不承诺账号可用性、持续免登录、同步阻断或跨机登录态迁移。

## 许可与来源

基于 xiaoyaodev/musechrome 修改，许可证为 [MIT License](LICENSE)。二开构建的来源、提交和校验信息应与实际分发文件一起确认。
