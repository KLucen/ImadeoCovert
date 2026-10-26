# P3 便携构建、产物验收与发行准备

日期：2026-10-26。P1、P2 已快进合入并推送到 `main`，远端提交为
`644fbe33e74255526242c438c9b68acefd23a2b4`。该提交的
[GitHub CI](https://github.com/KLucen/ImadeoCovert/actions/runs/40292506270)
已通过 Windows / Python 3.10、3.12 两组测试。

本批在 `codex/portable-release` 开发便携目录/ZIP、完整产物自检、离线 Sandbox
入口和受校验的草稿发布流程。当前版本仍为 `1.18.0-dev`，没有创建公开 Release。

## 本地构建

使用既有 Anaconda 环境，不创建 uv 环境，不新增 Python 依赖：

```powershell
conda activate imadeo-p0
python -m pip check
# 已安装 tools/ffmpeg 时不必重复执行；全新检出需要先安装。
python scripts/setup_ffmpeg.py
python scripts/build_portable.py --output-dir dist/my-build
python scripts/smoke_portable.py dist/my-build/ImadeoCovert-1.18.0-dev-windows-x64.zip
```

`build_portable.py` 仅支持 Windows x64 的 64 位 Python。每次尝试都建立独立的
`build/portable/build-*` 暂存目录，保留 PyInstaller 日志；不递归清空旧构建。
输出 ZIP 已存在时直接拒绝，需选择新的输出目录。新源码文件应先 `git add`，
这样源代码快照不会漏掉新增文件；允许构建已暂存的开发改动，并在构建信息中
明确记录工作区是否含未提交改动。

引擎校验以固定 ZIP 的 SHA-256 为依据，再比对 ZIP 中 FFmpeg / FFprobe 与
本地已安装文件的哈希，重新检查版本、编码器和容器能力；不能仅凭本地收据
宣称引擎可信。构建本身不下载依赖，不修改系统 PATH 或注册表。

## 产物结构

```text
ImadeoCovert-1.18.0-dev-windows-x64/
  ImadeoCovert.exe
  _internal/                    Python、Tk、图片库和原生依赖
  tools/ffmpeg/
    ffmpeg.exe
    ffprobe.exe
    LICENSE / README.txt / installation.json
  tools/ffmpeg-manifest.json
  licenses/                     安装包附带的 Python / GUI / 图片库许可文本
  source/ImadeoCovert-source.zip 当前受 Git 跟踪的应用源码快照
  LICENSE / THIRD_PARTY_NOTICES.md
  使用说明.txt
  build-info.json
  package-manifest.json
  release-policy.json
```

完整解压后运行 EXE，不能只复制主程序。冻结程序从 EXE 所在目录寻找
`tools/ffmpeg`，不使用当前工作目录，也不回退到系统 PATH。图标、Tk 和其他
资源仍由 PyInstaller 管理在 `_internal` 中。设置使用用户目录，程序目录本身
无需可写。

外层 ZIP 附带 `.sha256` 和 `build-summary.json`。包内清单记录所有其他文件的
SHA-256 和大小；自检启动前检查 ZIP 哈希、目录边界、完整文件清单和逐文件哈希。
输出 ZIP 先在目标卷暂存，再以不覆盖已有文件的方式提交。

## 产物自检

`smoke_portable.py` 把 ZIP 解压到包含中文、空格和括号的新路径，从无关工作目录
启动 EXE，并清除 Conda/Python/Tcl 环境变量，将 PATH 限制为 Windows 系统目录。
EXE 必须报告 `sys.frozen=True`、使用自己的捆绑引擎、退出码为 0。

EXE 的 `--self-test NEW_DIRECTORY` 模式创建独立目录，禁用 Python socket 连接
和更新检查，不修改用户设置；生成测试样本后检查：

- Tk、TkDND、图标和图片原生编码库能否加载。
- GUI 实际转换 JPEG / WebP / AVIF / HEIC，检查输出尺寸；打开图片比较窗口；渲染 SVG。
- 含音轨与无音轨的文件分别经 GUI 导出 MP4 / MKV / WebM，探测音轨并完整解码。
- 原始文件哈希不变，取消任务后进程已退出、没有最终输出或残留临时文件。
- 没有 Tk 回调异常；报告写入新目录的 `report.json`。

驱动器有 150 秒超时，并只终止自己启动的测试进程树。报告和解压文件保留在
`test-results/portable/`，便于复核。

## 干净 Windows 的离线入口

PATH 隔离和 Python socket 禁用是在本机做的模拟，不能替代一台未安装 Python 的
干净 Windows。当前主机没有 `WindowsSandbox.exe`，本批没有擅自启用系统组件。
已提供可复用的离线 Sandbox 配置生成器：

```powershell
python scripts/create_sandbox_check.py "D:\extracted\ImadeoCovert-1.18.0-dev-windows-x64" test-results/sandbox-new
```

在已支持 Windows Sandbox 的机器上打开生成的 `.wsb`：网络被禁用，程序目录只读
映射，测试结果单独映射回主机。客体登录后直接运行 EXE 自检；结果保存在
`sandbox-new/results/self-test/report.json`。重复验收应使用新的输出目录。
正式发行前仍需完成这项干净系统验收，并人工确认实际视频播放的音画同步。

## CI 与发布边界

新 `Portable Windows build` 工作流先执行全部回归，再构建 ZIP、验证解压后的
EXE，最后上传开发产物和诊断报告。手动运行仅生成 Actions artifacts，不创建
GitHub Release。构建任务只有 `contents: read`。

仅 `v*` 标签会进入独立的草稿发行任务，该任务有 `contents: write`。发布前检查：

1. 标签与 `app_metadata.VERSION` 完全一致，仅允许正式版本或 `-rc.N`，拒绝 `-dev`。
2. `tools/release-policy.json` 已完成第三方许可/对应源码核对，并覆盖所有必需组件。
3. 从已登记的 HTTPS 来源下载对应源码压缩包，校验 SHA-256，再与二进制一同附到
   **draft Release**。不会自动将草稿改为公开发行。

`source_archives` 的条目格式为：

```json
{"file": "component-source.tar.xz", "url": "https://provider.example/exact-source.tar.xz", "sha256": "64位十六进制哈希", "components": ["ffmpeg-and-external-libraries"]}
```

以上仅为格式示例，不是实际下载来源。当前 `release-policy.json` 为 `pending`：
已经收集 FFmpeg 原始许可、构建配置和版本链接，以及 Python/图片库提供的许可；
还没有核实固定 FFmpeg 所有外部库及图片原生库的完整对应源码与构建材料。
FFmpeg 的单个源码提交链接不等于全部外部库材料，pillow-heif 的源码许可也不能
代替其二进制轮子的许可说明。详见 [第三方说明](../THIRD_PARTY_NOTICES.md)。

因此，本批交付可本地验收的便携开发版及发布流水线，不宣称已完成公开发行。
签名、干净 Windows 验收和第三方对应源码材料应在实际发行前落实。

## 验证记录

测试环境：Windows、Anaconda `imadeo-p0` / Python 3.10.22、PyInstaller 6.22.3、
FFmpeg 9.0.2 essentials。执行命令：

```powershell
$env:IMADEO_REQUIRE_FFMPEG='1'
python -m pytest -q --junitxml=test-results/p3-tests.xml
python -m pip check
```

除原有图片、视频、GUI 回归外，新增冻结引擎定位、原始引擎 ZIP 与二进制一致性、
归档路径校验、逐文件校验、版本/发布来源门禁和离线 Sandbox 配置测试。最终本机
报告位于 `test-results/p3-tests.xml` 与 `test-results/p3-portable-smoke.json`。

本机结果：**134 passed，0 skipped，33.36 秒**，`pip check` 通过；便携 EXE
**29 项自检通过**。ZIP 约 100 MiB，实际大小和 SHA-256 以产物旁的
`build-summary.json`、`.sha256` 为准。最终打包基于本批提交后的干净工作区，
`build-info.json` 记录确切提交；没有将本机 PATH 隔离测试表述为干净 Windows 实测。
