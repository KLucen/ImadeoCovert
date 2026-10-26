# P1 视频转换核心与验收

日期：2026-10-26。基线为已推送的 P0 提交 `0b8d849418490faeee476d3efa6db452682186f3`。

P1 已实现独立于 GUI 的视频转换核心、串行任务队列和命令行入口，沿用 Anaconda `imadeo-p0` / Python 3.10.22 及项目内固定版本 FFmpeg。没有新增第三方 Python 依赖。现有 Tkinter 窗口仍为图片转换界面，视频控件和设置接入属于 P2。

## 使用方法

在仓库根目录，激活已创建的 Conda 环境：

```powershell
conda activate imadeo-p0
python scripts/convert_video.py "D:\videos\input.mov" --output-dir "D:\converted"
python scripts/convert_video.py "D:\videos\a.mp4" "D:\videos\b.mkv" --output-dir "D:\converted" --format webm --speed fast --height 720
```

参数：

| 参数 | 选项与默认值 |
| --- | --- |
| `--output-dir` | 必填，输出文件夹；不存在时创建 |
| `--format` | `mp4`（默认）、`mkv`、`webm` |
| `--quality` | `high`、`balanced`（默认）、`compact` |
| `--speed` | `fast`、`balanced`（默认）、`slow` |
| `--height` | `720`、`1080`；省略则保持显示尺寸；只缩小，不放大 |
| `--mute` | 不输出音轨；默认保留选中的默认/第一音轨 |
| `--overwrite` | 允许替换同名输出，仍禁止覆盖本批次任何源文件 |
| `--timeout` | 每个文件的编码超时秒数；省略不设编码总时限，探测单独有超时 |
| `--engine-dir` | 显式工具目录；默认使用项目内引擎 |

每行输出一个 JSON 任务事件，包含 `batch_id`、`task_id`、状态及进度或结果。按 Ctrl+C 取消当前编码并停止余下队列。退出码为成功 `0`、存在失败 `1`、取消 `130`。

默认自动避开磁盘和批次内重名，依次使用 `_1`、`_2` 后缀；启用 `--overwrite` 也不会替换当前批次的源视频，包括同路径、大小写别名和硬链接。失败后可以再次传入失败的输入文件重试，已经完成的输出默认获得新名称。

## 编码策略

| 格式 | 视频/音频 | 画质 high / balanced / compact | 速度 fast / balanced / slow |
| --- | --- | --- | --- |
| MP4 / MKV | libx264 / AAC 192 kbps | CRF 18 / 23 / 28 | veryfast / medium / slow |
| WebM | libvpx-vp9 / Opus 192 kbps | CRF 24 / 32 / 40，`b:v=0` | cpu-used 5 / 2 / 0，deadline good |

输出为 8-bit `yuv420p`、方形像素；MP4 开启 faststart。宽高比基于显示尺寸计算，包含旋转和像素宽高比；为编码器将尺寸向下调整为偶数，可能产生不足两个像素的取整差异。正常竖屏旋转应用于实际画面，输出不保留重复旋转。

显式映射主视频与默认/第一音轨，跳过封面图。无音轨输入正常转换，静音不要求音频编码器可用。字幕、封面、附件、章节及额外音视频流不保留，涉及轨道取舍的说明在结果 `warnings` 中返回。全局 metadata 不复制。

使用 `fps_mode=passthrough`，不强制 30fps。画质和速度独立控制。当前主动拒绝探测到的 HDR/Dolby Vision、透明视频及非 90° 倍数旋转；不提供无损换封装、硬件编码、HDR 转换、多轨选择或目标文件大小模式。

## 模块接口

- `media_models.py`：新增不可变 `VideoOptions`、`MediaTask`、`ConversionProgress`、`ConversionResult`、`TaskEvent`。
- `video_converter.py`：参数验证、显式 FFmpeg 参数列表、转码进程、进度解析、输出探测校验、单任务状态和结果。
- `file_utils.py`：源文件保护、批次重名分配、输出临时文件和原子提交。
- `task_runner.py`：批次快照、唯一任务 ID、串行运行、错误隔离、每批独立取消事件及重入保护。
- `scripts/convert_video.py`：命令行适配器及 Ctrl+C 处理。

P2 可在后台线程运行下列接口，在 Tk 主线程定时消费队列：

```python
from pathlib import Path
from queue import Queue
from threading import Event
from ffmpeg_service import discover_engine, inspect_engine
from media_models import VideoOptions
from task_runner import TaskRunner, make_tasks

engine = inspect_engine(discover_engine())
tasks = make_tasks([Path("input.mov")], Path("output"), VideoOptions(preset="mp4"))
events, cancel = Queue(), Event()
results = TaskRunner(engine).run(tasks, events=events, cancel_event=cancel)
```

此调用同步执行，不应放在 Tk 主线程。每次运行创建新的 `Event`，取消调用 `cancel.set()`；事件中一直携带批次/任务 ID，UI 应据此过滤过期消息和定位行，不能使用列表下标关联结果。

任务状态依次为 `queued → probing → running → validating → succeeded`，失败为 `failed`，取消收尾为 `cancelling → cancelled`。未开始的任务在批次取消后也得到取消终态。每个任务只有一个结果事件；单个文件失败继续处理下一文件。重复运行同一个 runner 时不会沿用上次取消标志；拒绝同时执行两个批次。

## 进度、进程和文件保证

1. 标准输出解析 `-progress pipe:1`；标准错误由独立线程持续读取，只保留尾部 64 KiB，避免管道死锁和无限日志积累。编码进度约每 0.2 秒更新，内部合并积压进度。
2. 进度包含已处理时间、估算百分比、速度倍数和估算剩余时间。未知时长时百分比和 ETA 为 `None`；已知时长的编码百分比保持单调，最高 99%。只有提交输出成功后才发出 100%。
3. 取消/编码超时终止本任务的 FFmpeg，等待至多一秒后必要时强制结束，再回收进程、读管道线程和临时文件。即使 FFmpeg 报告 `progress=end`，非零退出码仍判为失败；解码错误使用 `-xerror` 停止处理。
4. 结果先写到输出目录中的唯一 `.imadeo-*.part.<容器>` 文件。退出码正常后，用 FFprobe 校验实际编码、轨道数量、尺寸、像素格式、旋转和基本时长，之后才允许提交。
5. 覆盖模式直接替换已存在输出，不预删旧文件。默认无覆盖模式在 Windows 使用不会覆盖既有目标的 rename，提交时再次检查冲突，避免其他程序在编码期间创建目标而被覆盖。
6. 失败和取消只清理该任务的临时文件；已有结果保持完整。磁盘/权限/目标占用等错误有独立 `file_error`，其余错误带稳定代码和诊断详情。
7. 取消在最后一次提交检查前生效；若正式输出已经提交，任务成功，不再删除该成功结果。操作系统强制结束本应用/断电不属于正常取消，可能留下 `.part` 文件；P1 不实现启动恢复或自动清理历史文件。

当前输出验证是结构和基本时长检查，不在每次运行时完整解码全部输出；测试样本另外进行了实际解码。源文件在转换期间被外部程序修改、特殊多节目流及未测试的色彩元数据仍需后续扩充验证。

## 验收结果

执行：

```powershell
$env:IMADEO_REQUIRE_FFMPEG = "1"
conda run --no-capture-output -n imadeo-p0 python -m pytest -q --junitxml=test-results/p1-tests.xml
conda run -n imadeo-p0 python -m pip check
```

结果：**103 passed，无失败或跳过**；依赖完整性检查通过。其中新增 P1 测试 41 项，其余为 P0 图片、界面、引擎和探测回归。测试报告位于本地 `test-results/p1-tests.xml`（不入 Git）。

已验证：

- MP4/MKV/WebM 转码，每种覆盖有声音、无声音和静音；输出另行完整解码，原文件哈希保持不变。
- 编码画质/速度映射、实际流索引、尺寸缩小、偶数尺寸、静音引擎能力检查。
- 真正旋转的视频、非方形像素、奇数尺寸；可变帧率 MP4 的逐包时间戳保持在 0.01 秒容差内。
- 运行中取消后 3 秒内返回，无存活子进程，旧输出保持，排队任务取消；同一 runner 随后能启动新批次。
- 损坏输入、编码失败、验证失败、替换失败及模拟磁盘空间错误；后续正常文件继续处理。
- 磁盘/批次重名、其他程序抢先创建输出、同源路径/硬链接、本批次后续输入保护。
- `progress=end` 后错误退出、64 KiB 错误日志上限、编码超时、预先取消、单终态和 runner 并发保护。
- 从非仓库工作目录运行 CLI，实际完成 WebM 转换并输出成功 JSON 事件。

P0 已推送到 `main`，其 [GitHub Actions](https://github.com/KLucen/ImadeoCovert/actions/runs/40191565406) 已通过。P1 当前在本地 `feat/video-conversion-core` 分支完成验证；P1 远端 CI、GUI 交互和可分发视频版尚未验收。
