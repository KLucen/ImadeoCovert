# ImadeoCovert 便携开发版

Windows 10/11 x64。完整解压 ZIP 后运行 `ImadeoCovert.exe`。
请保留同目录下的 `_internal`、`tools`、`licenses` 等文件；不要只复制 EXE。
程序自带 Python 运行时和 FFmpeg / FFprobe，不需要安装 Python 或设置 FFmpeg PATH。

图片与视频通过顶部按钮切换。视频可导出 MP4（H.264/AAC）、MKV（H.264/AAC）
和 WebM（VP9/Opus）；可调画质、速度、最大高度和静音。只有普通 SDR 内容在
首版支持范围内，HDR / 透明视频会明确拒绝。

添加文件或拖入文件，选择输出目录，点击转换。转换中可取消，失败/已取消的项
可以重试。双击已完成结果调用系统播放器。同名输出自动编号，保留已有文件。
系统播放器不一定支持全部编码，播放能力取决于所安装的播放器。

引擎不可用时，在视频页重新检测或选择包含 FFmpeg 和 FFprobe 的目录；图片功能
仍可用。引擎“详情”和任务“详情”包含诊断信息。程序不会自动下载引擎。

转换无需网络。默认的可选更新检查会访问 GitHub；需要完全离线时可在设置中关闭。
设置保存在用户目录的 `.imadeocovert_settings.json`，不写入安装目录；旧 Formatix
设置只读取迁移，保留原文件。程序目录可放在只读位置，输出目录需要写入权限。

这是一份未经代码签名的开发构建。`build-info.json` 标识代码、依赖和引擎版本，
`package-manifest.json` 记录文件哈希，ZIP 旁的 `.sha256` 用于核对下载完整性。
`THIRD_PARTY_NOTICES.md`、`licenses/` 和 `tools/ffmpeg/LICENSE` 保留原许可材料。
公开发行前的来源核对状态见 `release-policy.json`。

维护者验收命令（测试目录必须不存在）：

    ImadeoCovert.exe --self-test C:\Temp\Imadeo-self-test-new

此模式禁用 Python 网络连接，在新目录生成样本与结果，检查真实 GUI 和编码器，
写入 `report.json` 后退出。它不修改用户设置，也不会在普通启动时自动执行。

项目与源码：https://github.com/KLucen/ImadeoCovert
发行页面：https://github.com/KLucen/ImadeoCovert/releases
