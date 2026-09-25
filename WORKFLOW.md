# 工作流程（Material Bakery 项目）

> 这份文档是给**接手这个项目的下一个会话**看的：照它做，就跟我做的一样。
> 项目是什么、改过什么在 `MATERIAL_BAKERY_DESIGN.md`（设计档案）和
> `MaterialBakery/README.md`（用户手册）里；这份只讲**怎么干活**。

---

## 0. 三条铁律（比技术细节重要）

1. **只有用户明确说「开始」才动手改代码/文件。** 在那之前只做三件事：回答、给方案、提问。
   （用户为这条发过火，一次。别试探。）
2. **做完要打包并明确说「做完了」+ 给出 zip 路径。** 用户的规矩：他要能一眼知道该装什么去测，
   而不是从大段文字里找。他还特别要求过"别把我说的话当成你测完的反馈"。
3. **破坏性/不确定的操作：先说清代价，或者「决定 + 披露 + 给一条回退命令」。**
   例：删文件前先搜引用；动许可证前说明改了什么、怎么换回；重写 git 历史前说明会 force push。

---

## 1. 项目地图：哪份是"真源"

| 路径 | 是什么 | 能不能直接改 |
|---|---|---|
| `material_bakery/` | **工作树（真源）**。改代码改这里 | ✅ 唯一该动的地方 |
| `MaterialBakery/material_bakery/` | 成品副本，由 `sync_package.ps1` 镜像过去 | ❌ 别手改，会被覆盖 |
| `MaterialBakery/*_install.zip` | 打包产物 | ❌ 由 `build_zips.ps1` 生成 |
| `tests/` | 20 套回归（私有版；公开版 17 套） | ✅ 改行为必须同步改断言 |
| `tools/` | 打包/同步/测量/发布脚本 | ✅（`.ps1` 必须纯 ASCII） |
| `_probe/` | 临时草稿（gitignored，随便造） | ✅ |
| `MATERIAL_BAKERY_DESIGN.md` | 设计档案：**每一轮为什么这么改**，含实测数字 | ✅ 每轮追加一节 |
| `MaterialBakery/README.md` | 用户手册（行为、边界、已知限制） | ✅ 行为变了就要改 |
| `test.blend` | 真机 Cycles 烘焙套件的夹具 | ❌ 别删！测试要用 |

**"一个集合 → 一套贴图 + 一个材质"** 是全工程的核心假设。任何改动都要先问：
它会不会破坏这条（例如"按材质拆贴图"这种需求，就得先想清楚 group 的粒度）。
`engine/backend.py:_install_targets()` 把该组的图挂到目标物体的**每一个材质槽**上 ——
这是"材质级拆分做不到"的原因，动这个功能前先读它。

---

## 2. 每次改动的标准循环

```
① 先量 → ② 改工作树 → ③ sync + build + 跑全量 → ④ 补测试 → ⑤ 改三处文档
→ ⑥ 版本号 + build 时间戳 → ⑦ 打包 → ⑧ 告诉用户装哪个
```

### ① 先量再改（不要猜）
- 有现成量尺：`tools/probe_run_prefs.py`、`probe_undo_toggle.py`、`measure_bake_memory.py`、
  `measure_bake_startup.py`、`measure_pack_cost.py`、`qr_probe_state.py`。
- 第八轮新增的四把（2026-09-25）：
  - `probe_restore_freeze.py` —— 逐条重放 `_restore_scene` 的每个还原赋值，谁卡谁自己报数
  - `probe_gui_bake.py` —— **在 GUI 里**跑完整烘焙链路（timer 驱动），复现"收尾卡死"。
    后台模式**复现不了**这个 bug，必须 GUI
  - `probe_view_layer_debug.py` —— 打印 layer collection 树的 `exclude` 状态与视图层物体
  - `probe_real_bake_prop.py` —— 在 `Prop Creation.blend` 上真机烘一轮（验证计划修正）
  > ⚠ 拿工作树代码做无头验证**必须加 `--factory-startup`**：否则 Blender 先加载
  > `%APPDATA%` 那份已安装插件，同名模块遮蔽 `sys.path`，探针 import 到的不是工作树。
- 新问题就写一个新的探针放进 `tools/`（例：`probe_run_prefs.py` 量出
  `foreach_get` 比 Python 循环只快 2 倍 —— 于是"优化逐面索引"这条从计划里降级，
  **实测数字写进注释和设计文档**）。
- 结论必须带数字和来源，写进代码注释（`⚠` 开头那段）与设计文档对应章节。

### ②③ 改完立刻验证（一轮 30 秒，别省）
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_package.ps1   # 工作树 → 成品目录
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_zips.ps1     # 打 zip
powershell -NoProfile -ExecutionPolicy Bypass -File tools\run_all_tests.ps1  # 全量（约 30 秒）
```
> 产物固定两份，名字由 `build_zips.ps1` 里的 `Zip` 字段决定：
> `MaterialBakery\material_bakery_install.zip` 与 `MaterialBakery\quadremesher_install.zip`
> （**小写** —— 测试和 Release 附件都按这个名字找；脚本以前按包名推，产出过
> `QuadRemesher_install.zip`，第八轮被 `mb_test_zip_install.py` 抓出来）。
- 运行器逐套打 **PASS / SKIP / FAIL + 耗时**，汇总给总耗时；跑绿时自动清 `_probe\install|zipinstall`
 （约 108 MB 临时副本），跑挂则保留现场。
- 两个套件需要"启动前准备"（`mb_test_install.py` / `mb_test_zip_install.py`），运行器自己会做；
  zip 缺失时它还会先跑一遍 `build_zips.ps1`。
- **当前读数**（2026-09-25 实测）：私有 **20 套 / 1303 项 / 约 31 秒**；
  公开版 **17 套 / 1186 项 / 约 26 秒**。
  改动后数量会变 —— **文档里的数字要用实测值更新**（写死数字是这个项目反复踩的坑）。

### ④ 测试的写法（这项目里最贵的教训都在这）
- **看到 `[FAIL]` 先怀疑断言**：历史上多次是"代码对、断言过期"。
- **不要写死数量/版本/路径**：套件数、operator 数、`.py` 数、版本号，都要从数据源推。
- **不要写恒真的断言**（`check("...", True)` / `or True`）—— 有一个曾经让整轮绿得毫无意义。
- **"返回 CANCELLED"是弱断言**：Blender 会把 operator 里的异常也变成 CANCELLED，
  所以它能掩盖真实崩溃（`ui/ops_presets.py` 的 `_error` 未定义就是这么藏了很久的）。
  测"被拒绝"时要顺带断言**日志里真的有一行红字**。
- 行为变了就更新受影响的套件；新增功能的断言要能**证伪**（例：非正方形贴图那条断言
  "图像尺寸真的是 (512, 256)"、"文件名是 …-512x256" —— 只改 UI 不改引擎它会挂）。

### ⑤ 三处文档（缺一处就算没做完）
1. `MaterialBakery/README.md`：用户会看到的最终行为（含"装完必须完全重启 Blender"）。
2. `MATERIAL_BAKERY_DESIGN.md`：**追加一节**，格式照抄第 29/30/31 节 ——
   用户原话（引号原文）→ 改了什么（表）→ 为什么 → 被测试抓出的 bug → 验证读数 → 教训。
3. `tests/README.md`：套件表 + 当前读数 + 两个"启动前准备"套件的说明。

### ⑥ 版本与"我装的是哪一版"
```python
# material_bakery/__init__.py
bl_info = {"version": (1, 1, 0), ...}
__build__ = "2026-09-23 02:25"     # 每次打包都改
```
第 1 页顶部显示 `v1.1.0 · build …`。这不是装饰：**装 zip 不会替换内存里已加载的模块**，
用户不重启就会以为"改了没用"—— 这个坑浪费过一整轮排查。

### ⑦⑧ 交付
- 私有完整版：`material_bakery_install.zip`（38 模块）+ `quadremesher_install.zip`
- 公开版：`material_bakery_install.zip`（36 模块，无 Remesh 页）
- 告诉用户：装哪个文件、**装完完全退出 Blender 再启动**、第 1 页应显示哪个版本号。

---

## 3. Git 与发布

### 提交（本地）
```powershell
git add -A
git commit -F _probe\msg.txt      # 主题短、证据放正文
```
- **主题要短**（GitHub 文件列表里每行都显示"最后改动它的提交主题"；曾经因为主题里带版本号+测试
  数量，用户看到"每个文件都被盖了版本号"）。
- 证据（测试读数、做了什么）放**正文**，正文不出现在文件列表里。
- 提交信息用**文件**（`-F`），别用 `-m` 拼长文本；PowerShell here-string 写文件会坑你。

### 公开版（从私有版自动剥离，别手工改两份）
`tools/make_public_variant/README.md` 是完整流程，核心：
```powershell
git checkout --orphan public        # 全新历史（公开库历史里不该有 QR 代码）
git add -A                          # 先放进 index：剥离出错可 git checkout -- . 还原
python tools\make_public_variant\strip_qr.py
python tools\make_public_variant\public_docs.py
python tools\make_public_variant\public_docs2.py
Remove-Item -Recurse -Force tools\make_public_variant   # 公开库不带这套生成脚本
powershell -File tools\sync_package.ps1 ; powershell -File tools\build_zips.ps1
powershell -File tools\run_all_tests.ps1                # 公开版应为 17 套
git add -A ; git commit -F _probe\public_msg.txt
git checkout main                                       # 回到完整版
```
- 三个脚本**每条编辑都断言锚点**，对不上就停（不会留半成品）；它们住在 `tools/make_public_variant/`，
  所以**只在私有库里存在**。
- 剥离后必须核：`.count` 文件数 = 私有 128 / 公开 111，且 **QR 文件 0**。

### 推送与 Release
- **凭据**：`DeepSeek Harness Git Token.txt`（长期、限额，在工作区根目录，已被 `.gitignore` 挡住）。
  用它推：token 走 header，**不落盘、不出现在输出里**
  ```powershell
  $t = (Get-Content "DeepSeek Harness Git Token.txt" -Raw).Trim()
  $b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$t"))
  git -c http.extraHeader="AUTHORIZATION: basic $b64" push --force origin main
  ```
- ⚠ **GitHub 的密钥保护会拒绝含 PAT 的推送**（见过一次），所以推之前先
  `git ls-files | Select-String Token` 自查；`.gitignore` 里 `*Token*.txt` / `*token*.txt` 必须在。
- **Release**：两种走法，任选
  - `_probe/gh_release.js`（`tools/make_public_variant/gh_release.js` 是同源，**不在版本库里**）
    ```powershell
    node _probe\gh_release.js "<owner/repo>" "v1.1" "<标题>" "说明.md" "本地.zip=附件名" ["另一个.zip=名"]
    ```
  - `tools/gh_upload_assets.py`（**在版本库里**，已提交到两个库）：幂等 —— 同名附件先
    `DELETE` 再 `POST`，支持 `--dry-run` 先列现有附件
    ```powershell
    & "C:\Program Files\Blender Foundation\Blender 4.5\4.5\python\bin\python.exe" tools\gh_upload_assets.py --dry-run
    ```
    ⚠ 必须用 Python/urllib 或 node（OpenSSL）；`curl.exe` 是 schannel，会
    `SEC_E_NO_CREDENTIALS`。要改上传目标就编辑脚本顶部的 `WANTED` 表。
  - 说明文件：`tools/make_public_variant/release_private.md` / `release_public.md`（每次更新成新版本）。
- `push_to_github.ps1` 是给**用户自己**用的推送助手（校验 URL、预检可达性）；我这边不需要它。

---

## 4. 环境与沙箱（都是实测结论，别重复试）

| 事项 | 结论 |
|---|---|
| Blender | `C:\Program Files\Blender Foundation\Blender 4.5\blender.exe`（另有 5.2）。测试都用 4.5.13 LTS |
| 跑脚本的 Python | `C:\Users\Admin\AppData\Local\Programs\Python\Python311\python.exe`（也在用 Blender 自带的那份：`C:\Program Files\Blender Foundation\Blender 4.5\4.5\python\bin\python.exe`） |
| 打 GitHub API | 用 **node**（自带 OpenSSL，`api.github.com` 通）或 **Python + urllib**（同样 OpenSSL）；`tools/gh_upload_assets.py` 走的就是后者。PowerShell 的 .NET 与系统 curl 会 `SEC_E_NO_CREDENTIALS` |
| git 网络 | `git` 本身要 `http.sslBackend=openssl`（仓库级已设）；默认 schannel 连不上 |
| ❌ `git filter-branch` | 要调 MSYS2 的 `sh`，而 sh 建信号管道被禁（`Win32 error 5`）→ 重写历史用「合成一个干净提交」或 git plumbing |
| ❌ 凭据助手 / GCM / `gh` | 同上原因不可用（所以推送必须显式带 token） |
| ❌ `python -c "…"` | 经 PowerShell 传参会吞引号（踩过 3 次）→ **一律把脚本写成文件再跑** |
| ⚠ 脚本锚点 | 不要猜前导空格（同一注释在模块级/函数里缩进不同）→ 用"忽略缩进、保留文件缩进"的匹配 |
| ⚠ `.ps1` | 必须**纯 ASCII**（PS 5.1 按 ANSI 读，中文注释会毁掉解析） |
| ⚠ 验证忽略规则 | 必须 `git check-ignore -v <路径>`（看行为）；**grep 文件内容是假阳性**（同一条错了 3 次） |
| ⚠ 写文本文件 | 用文件工具或 Python，别用 PowerShell here-string 重写（丢过换行、变过乱码） |

---

## 5. 当前交付状态（2026-09-25 更新）

| | 私有 | 公开 |
|---|---|---|
| 仓库 | `AliasAliases/Material-Bakery-QuadRemesher`（private） | `AliasAliases/Material-Bakery`（public） |
| 版本 | v1.1（tag + Release） | v1.1（tag + Release） |
| 历史 | 初始提交 + 本轮修复提交 | 初始提交 + 本轮修复提交 |
| 文件 | 132（0 厂商文件 / 0 zip / 0 密钥） | 115（同上） |
| Release 附件 | `MaterialBakery-QuadRemesher-v1.1.zip`（别名）、`quadremesher_install.zip`、`material_bakery_install.zip` | `material_bakery_install.zip` |
| 测试 | 20 套 / 1303 项 / ~31 秒 | 17 套 / 1186 项 / ~26 秒 |

本地：`main` = 完整版（工作树就是它），`public` 分支 = 公开版来源；`v1.0` 的 Release 保留。
**两个库都要维护、都要提交**（用户 2026-09-25 明确），推远程要等他明确说推。
许可证：GPL v2+，`MaterialBakery/LICENSE.txt` 是 **GPLv2 正文**（v3 原文备份在
`_probe/LICENSE.gplv3.txt`；要换回去一条命令）。

---

## 6. 没做完的事（新会话优先问这个）

**需求**：用户有一个 4 材质的物体，想 2 个材质烘到贴图 A、另 2 个烘到贴图 B，**不拆物体**，
UV 他已经分好。

- **现状做不到**：工作单位是"集合 → 一套贴图"，`_install_targets()` 把图挂到物体的每个材质槽。
- **路 A（今天可用，代价 2 倍体积）**：物体同时属于两个集合 → 两套图（每张图都含全部 4 个材质
  的内容），再手工把 mat1/2 指向 A、mat3/4 指向 B，并**关掉 Auto Deliver**。
- **路 B（待实现）**：新增 Materials 目标模式 + plan/backend 支持材质级 targets +
  交付"接到现有材质"；测试断言**两张图互不含对方的区域**（唯一能证伪的判据）。
- 已问用户三件事，等他答：① 两张图是为了更高纹素密度，还是为了材质/着色器上分开？
  ② 两组名字用什么（默认取各组第一个材质名）？③ 烘完要不要自动把图接到他原有的 4 个材质上？

---

## 7. 新会话开场建议

1. 读这份文档 → 读 `MATERIAL_BAKERY_DESIGN.md` **最后几节**（第 29/31/32 节是最近三轮的
   来龙去脉；第 32 节就是那次"收尾卡死"的正解与我的违规记录）。
2. 跑一次 `tools\run_all_tests.ps1` 看基线（30 秒，别省）。
3. 问用户：这次要改什么、**是否要同步公开版与发 Release**（这是每次都要确认的分叉点）。
4. 记住第 0 节：没说「开始」就只回答/提问；做完要打包 + 说「做完了」+ 给路径。
5. 悬着的一件事：**`%APPDATA%` 那份已安装插件现在是私有版**（2026-09-25 被我镜像覆盖，
   比原公开版多一个 Remesh 页；原版备份在 `material_bakery.backup-20260925`）。
   用户还没决定是重装 `material_bakery_install.zip` 还是保持现状 —— 新会话可以顺便问一句。
