# P0 开发与验收记录

日期：2026-10-25。基于上游源码提交 `eb0fb3caadf57e54b34a56afbd4ec61d25b1acec`。

P0 已建立图片回归基线、独立 Conda 开发环境、视频引擎清单和媒体探测模块。视频转换执行器、任务队列与视频界面分别属于 P1/P2，当前 GUI 仍提供原有图片功能。

## 开发环境

按用户要求使用 **Anaconda / Conda**，环境名 `imadeo-p0`，Python **3.10.22**。本机解释器为 `C:\Users\KLucen\anaconda3\envs\imadeo-p0\python.exe`。本次临时创建的 uv `.venv` 及 uv Python 3.10 已删除；原有其他环境保留。

通常安装：在仓库根目录执行以下命令。已有本机 `imadeo-p0` 时直接激活即可，无需重复创建。

```powershell
conda env create -f environment.yml
conda activate imadeo-p0
python -m pip check
python formatix.py
```

`environment.yml` 固定 Python 版本；`requirements.txt` 固定运行库；`requirements-dev.txt` 固定运行库及测试、打包工具。输入清单 `.in` 用于记录允许的依赖范围，安装使用 `.txt`。升级依赖时同时核对两份锁定清单，并重新执行基线测试。

如需复现本次 Windows x64 的具体 Conda 二进制依赖：

```powershell
conda create -n imadeo-p0 --file conda-win-64.lock.txt
conda activate imadeo-p0
python -m pip install -r requirements-dev.txt
```

锁定文件包含官方 Conda 包地址与 SHA-256。上述两种创建方法任选其一。项目不修改全局 Conda/pip 源或系统 PATH。本次安装使用单次命令的网络代理参数，仓库文件不依赖该本机代理。

## 引擎选择与安装

选用 GyanD/codexffmpeg 发布的 **FFmpeg 9.0.2 essentials Windows x64 静态构建**，同时使用包内 FFmpeg 和 FFprobe。

- [固定发布页](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2)
- 清单：`tools/ffmpeg-manifest.json`。
- ZIP SHA-256：`60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`。
- 安装位置：`tools/ffmpeg/`，不进入 Git。
- 安装回执：`tools/ffmpeg/installation.json`，记录来源、配置和两个可执行文件的 SHA-256。

```powershell
conda activate imadeo-p0
python scripts/setup_ffmpeg.py
python scripts/probe_media.py --require-presets
python scripts/probe_media.py "D:\videos\示例.mp4"
```

安装脚本仅供开发/构建阶段使用：下载至缓存、校验 ZIP、暂存解包、检查版本和所需编码器/复用器，通过后提交安装目录。已有引擎时拒绝覆盖；检查它可直接运行探测命令。离线安装可传入 `--archive "完整ZIP路径"`。解包后保留原包中的许可证、README 和文档，排除不需要的 ffplay。

本机原 Conda FFmpeg 8.1.2 有 MP4/MKV/WebM 容器支持，但缺少 `libx264` 和 `libvpx-vp9`。新能力检查能够区分“存在 FFmpeg”与“可执行目标预设”。已安装构建实测支持以下组合：

| 预设 | 视频编码器 | 音频编码器 | 复用器 |
| --- | --- | --- | --- |
| MP4 | libx264 | aac | mp4 |
| MKV | libx264 | aac | matroska |
| WebM | libvpx-vp9 | libopus | webm |

引擎发现顺序为显式工具目录、项目/打包资源内引擎、开发环境 PATH。显式配置错误或捆绑目录不完整时直接报错；冻结后的应用默认不回退到 PATH。版本与构建配置必须一致。此检查可以发现混用版本，但不能替代安装包哈希校验或对全部解码器/硬件能力的测试。

当前只是开发依赖准备。正式分发时仍需按实际构建配置核对完整许可与对应源码获取材料，并验证 P3 便携产物。

## 新增模块

- `media_models.py`：不可变媒体/流模型与首版预设定义；保存精确分数帧率、像素宽高比、旋转、色彩、音轨和字幕信息；主视频排除封面图，优先默认轨道。
- `ffmpeg_service.py`：引擎发现、构建匹配、编码器/复用器枚举、FFprobe JSON 探测、超时与取消、错误分类。
- `scripts/setup_ffmpeg.py`：固定版本的校验安装。
- `scripts/probe_media.py`：命令行诊断与 JSON 输出；`--require-presets` 在必要编码能力缺失时返回非零退出码。

模块只依赖 Python 标准库，无 Tkinter/Pillow 导入。导入时不访问网络、不启动进程、不修改配置。探测接口同步运行，后续 GUI 必须从工作线程调用；通过 `threading.Event` 取消。超时或取消后终止并回收子进程，标准输出和错误同时消费，避免管道阻塞。

`MediaInfo.duration` 等无法确定的数值使用 `None`；`Fraction` 保存帧率，CLI 将其写成字符串。主视频/音轨包含真实流索引，P1 可据此显式映射。HDR/透明信息作为可识别标记提供，P0 没有进行颜色转换，也没有声称这些媒体已获转码支持。

## 设置与发布身份的决定

P0 明确迁移约定，实际接入与 GUI 参数一起在 P2 实施：

1. 产品名称暂定 ImadeoCovert，版本、仓库和发布地址集中到后续 `app_metadata.py`，仓库为 `KLucen/ImadeoCovert`。
2. 新配置使用 `~/.imadeocovert_settings.json`，包含 `schema_version: 1`、`general`、`image`、`video`。
3. 只有新配置不存在时尝试从 `.formatix_image_converter_settings.json` 一次迁移；保留旧文件。语言、主题和输出/图片参数映射到对应分区，未知字段保留在迁移备份或忽略并记录。
4. 新配置通过同目录临时文件替换保存；读取失败不覆盖原文件；“记住设置”开关对图片/视频一致生效。
5. GUI 名称、更新 API、版本链接、README 下载地址和产物命名在 P2/P3 统一切换，保留上游署名与许可证；无 fork Release 时平稳降级。

本阶段没有提前替换用户已有设置文件，也没有发布新版本。原有构建工作流加入依赖锁定与单元/图片基线测试；新增独立测试工作流，避免把 P0 当成视频版产品发布。

## 测试与结果

本机 Anaconda 环境中执行：

```powershell
conda run -n imadeo-p0 python -m pip check
$env:IMADEO_REQUIRE_FFMPEG = "1"
conda run --no-capture-output -n imadeo-p0 python -m pytest -q
conda run -n imadeo-p0 python scripts/probe_media.py --require-presets
```

结果：**62 passed**，无跳过；依赖检查通过；三个目标预设均可用。

| 验证项 | 结果 |
| --- | --- |
| PNG/JPEG/WebP/BMP/TIFF/ICO/AVIF/HEIC 输出 | 可读，源文件不变；透明 PNG 与 JPEG 背景行为符合基线 |
| 比例缩放、自定义尺寸、裁剪、SVG、命名冲突、缓存、目标大小 | 通过 |
| 原图片 GUI 创建、控件初始化、语言键检查 | 通过，使用临时设置且禁用更新检查 |
| MP4/MKV/WebM，各含音轨与无音轨 | 真实生成并探测，编码、尺寸、帧率、时长与大小符合预期 |
| 旋转元数据及两条音轨 | 真实样本通过，选中默认音轨 |
| 封面图排除、分数帧率、缺失/无效字段、HDR/Dolby Vision 标记 | 解析测试通过 |
| 损坏视频、纯音频输入 | 返回明确错误 |
| 取消、超时、双管道输出、构建不匹配、安装包哈希错误 | 通过，超时/取消后进程已退出 |
| 独立诊断 CLI | 正确找到捆绑引擎并报告三个可用预设 |

样本均由 Pillow 或 FFmpeg 在测试临时目录生成，无需下载媒体，也没有使用用户私人视频。GUI 测试是启动与控件冒烟测试，不代表完整交互、主题视觉或拖放操作已人工验收。

新增 GitHub Actions 测试矩阵为 Windows + Python 3.10/3.12。**本地已验证 3.10.22；尚未推送或执行远端 CI，3.12 矩阵结果待 CI 确认。** P3 的完整打包与干净系统验收尚未执行。

现有图片写入替换、缓存指纹、任务取消等限制仍按原开发方案记录；本阶段建立基线，不将这些限制标记为已修复。下一阶段 P1 在上述模型和引擎服务上实现转码执行器、输出保护、进度和任务生命周期。
