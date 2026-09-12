# DIT OFFLOADER — 素材备份校验与自动改名

基于Python3的专业的数字影像技术（DIT）素材备份软件。支持 macOS / Windows。

## 功能

- **安全拷贝**：逐块 `fsync` 落盘，`shutil.copystat` 保留元数据（mtime/atime/权限/birthtime）
- **流水线并发**：拷贝文件 B 的同时对文件 A 进行 xxhash64 校验
- **源+目标双校验**：源文件与目标文件独立计算 xxhash64，比对一致才标记完成
- **重名保护**：目标存在同名文件时追加 `-1`、`-2`，绝不覆盖
- **断点续传**：XML 日志记录每一个文件的拷贝/校验状态，中断后可从断点继续；续传时对已完成文件重新校验目标盘，防止静默损坏
- **任务排队与并发**：任务按目标盘自动调度——同一目标盘排队串行，不同目标盘并发执行；单任务内 2 拷贝 + 2 校验线程流水线
- **微信推送**：任务完成/校验失败/中断后通过 pushplus 推送微信通知，标题区分三态（成功/失败/中断）
- **仅校验模式**：不拷贝，仅比目标文件数量与源文件 hash 值
- **DIT 报告**：HTML 格式报告，视频首/中/尾 3 帧截图 + 元数据表 + XXHash-64 校验值
- **电子场记导入**： 从 https://givemehanzo.github.io/sscl/ 安装运行网页app 电子场记单，拷卡时导入_SSCL.xml文件实现素材根据场号镜号和机位号自动改名
- **深色 GUI**：PySide6 三栏布局，支持拖拽，macOS/Windows 原生风格

## 从Release下载打包好的exe或者dmg

最新版本：[**Beta 1.0.4**](https://github.com/GiveMeHanzo/DIT-OFFLOADER/releases/tag/v1.0.4)（macOS DMG 已上传；Windows 版即将发布）

┌──────────────────────────────────────────────────────────────────────────────
│  Windows 安装与启动
└──────────────────────────────────────────────────────────────────────────────

  直接双击 DIT Offload.exe 即可运行，无需安装。

  如弹出 "Windows 已保护你的电脑" 提示，点击「更多信息」→「仍要运行」。


┌──────────────────────────────────────────────────────────────────────────────
│  macOS 安装与启动
└──────────────────────────────────────────────────────────────────────────────

  ■ 从 DMG 安装（推荐）

    1. 双击 DIT Offload.dmg 挂载磁盘映像
    2. 将 DIT OFFLOADER.app 拖入 Applications 文件夹
    3. 弹出 DMG（桌面右键 → 推出）
    4. 从启动台或 Applications 文件夹双击运行

  ■ 首次启动提示"无法验证开发者"

    由于未经过 Apple 公证，首次启动时 macOS 会阻止运行。
    按以下步骤绕过（只需操作一次）：

    │ 方法 A（推荐）：
    │   右键（或 Ctrl+点击）DIT OFFLOADER.app →「打开」
    │   在弹出的对话框点击「打开」
    │
    │ 方法 B：
    │   系统设置 → 隐私与安全性 → 向下滚动到"安全性"
    │   → 找到"已阻止 DIT OFFLOADER.app" → 点击「仍要打开」

    之后再次启动无需重复此操作。


## 非打包运行

### 1. Python 依赖

```bash
python -m pip install -r requirements.txt
```

### 2. FFmpeg（可选，报告功能需要）

- **macOS**: `brew install ffmpeg`
- **Windows**: `winget install Gyan.FFmpeg` 或从 [ffmpeg.org](https://ffmpeg.org) 下载
- 程序会自动搜索常见安装路径，即使 PATH 未刷新也能检测到

### 3. 验证

```bash
python verify_fixes_test.py         # 核心回归测试（127 项断言）
python pushplus_test.py             # 微信推送单元测试（离线）
python gui_smoke_test.py            # GUI 模块导入与构造
python gui_integration_test.py      # GUI×后端集成测试
```

## 使用

```bash
python main.py                     # 启动 GUI
```

### 工作流程

1. **左侧栏**：选择驱动器/文件夹，双击加入源池
2. **中间栏**：拖入源文件/文件夹，设置目标路径，选择 Job 名称
3. 点击 **「开始拷贝」** 执行完整拷贝+校验
4. 或点击 **「仅校验」** 只比对目标文件与源文件的 hash
5. 勾选 **「生成 HTML 报告」**，完成后在目标目录生成报告
6. **右侧栏** 查看实时进度和结果

### 任务队列与并发

- 点击「开始拷贝」后任务先进入队列，由调度器按**目标盘**自动分发：
  - **同一目标盘**的任务自动排队串行执行，避免多个任务同时写一块盘
  - **不同目标盘**的任务并发执行——同一张卡同时拷往两块盘做双重备份，互不等待
- 单个任务内部为**流水线并发**：2 个拷贝线程 + 2 个校验线程，拷贝文件 B 的同时校验文件 A
- 任务参数在点击开始时快照：任务运行中可继续配置并提交下一个任务，互不影响
- 同名任务自动版本化：向同一目录追加拷贝时任务名自动改为 `J-1`、`J-2`，XML 日志与 HTML 报告不覆盖上一次
- 右侧队列实时显示每个任务的状态、进度、速度与预计剩余时间；可单独取消，也可一键清空已完成任务

### 微信推送（pushplus）

拷贝任务结束（成功 / 校验失败 / 中断）后，自动推送一条消息到微信：

- 标题三态：`DIT-拷贝成功` / `DIT-拷贝失败` / `DIT-拷贝中断`
- 正文包含：任务名称、拷贝文件数、视频文件数、非视频文件数、完成时间；
  失败任务附校验未通过文件数，中断任务附已验证进度

配置方法：

1. 点击主界面右上角 **「⚙ 设置」**
2. 勾选「开启微信推送」
3. 登录 [pushplus](https://www.pushplus.plus/)（微信扫码），在「一对一推送」页面复制 Token
4. 粘贴 Token → 点击「测试推送」确认微信能收到 → 「保存」

Token 保存在本机用户目录（macOS 偏好设置 / Windows 注册表），重启或升级程序无需重新输入。
未开启推送或未配置 Token 时，任务流程不受任何影响。

### 中断恢复

- 拷贝/校验过程中断（强制退出、中途拔卡）→ 目标目录留下未完成的 `*_log.xml`
- 下次选择同一目标 → 弹窗询问「继续未完成任务」或「新建任务」
- 拔卡等设备中断会被正确识别为「中断」而非「完成」，保证再次插卡后能续传
- 继续 → 对上次已完成的文件**重新校验目标盘**（防止静默损坏，损坏自动重拷修复），仅重拷剩余/失败文件
- 续传任务在全部文件校验通过后才生成 HTML 报告

## 项目结构

```
├── main.py                  # 程序入口
├── config.py                # 全局配置、状态机、策略
├── requirements.txt
├── core/
│   ├── scanner.py           # 源文件枚举
│   ├── copier.py            # 拷贝引擎（逐块 fsync + 元数据保留）
│   ├── verifier.py          # xxhash64 流式校验
│   ├── logger.py            # XML 日志读写 + 断点续传扫描
│   ├── renamer.py           # 根据电子场记单导出的_SSCL.XML 实现素材自动改名成场镜号格式 例如SC001_S001_T001_A_A005C050_240527B9.MXF
│   ├── notifier.py          # pushplus 微信推送（消息构建 + 发送）
│   └── pipeline.py          # 流水线编排（拷贝线程 + 校验线程并行）
├── gui/
│   ├── main_window.py       # 三栏主窗口（深色主题）
│   ├── workers.py           # QThread 桥接
│   └── widgets/
│       ├── drive_panel.py   # 左侧：驱动器/文件浏览器
│       ├── copy_panel.py    # 中间：Copy From/To 配置
│       ├── settings_dialog.py # 微信推送设置（开关 + Token + 测试推送）
│       └── queue_panel.py   # 右侧：任务队列
└── report/
    ├── ffprobe_utils.py     # FFprobe 视频元数据提取
    ├── frame_extractor.py   # FFmpeg 首/中/尾 3 帧截图
    └── generator.py         # HTML 报告生成（Base64 嵌入）
```

## 性能

- **拷贝**：1 GiB/s+（NVMe → NVMe，取决于磁盘 I/O）
- **校验**：3-5 GiB/s（xxhash64 在 NVMe 上可达 10+ GiB/s）
- **流水线**：拷贝与校验并行，总时间 ≈ max(拷贝, 校验) + 少量开销

## 报告截图

报告包含：
- 视频首/中/尾 3 帧横向排列
- 左列：Filename, Format, Dimensions, Codec, Duration, Size
- 右列：Framerate, Timecode, XXHash-64 Checksum, Destination, Status
- 非视频文件：简化表

## 许可

MIT License

┌──────────────────────────────────────────────────────────────────────────────
│  开源协议
└──────────────────────────────────────────────────────────────────────────────

  本软件使用了以下开源组件，依照其各自许可协议分发：

  PySide6             LGPL v3       https://pypi.org/project/PySide6/
  xxhash              BSD 2-Clause  https://pypi.org/project/xxhash/
  psutil              BSD 3-Clause  https://pypi.org/project/psutil/
  FFmpeg              LGPL v2.1+    https://ffmpeg.org/

  依据 LGPL 要求，您有权获取上述 LGPL 组件的完整源代码。
  如需获取，请联系软件分发方或访问对应项目官网链接。

  FFmpeg is a trademark of the FFmpeg project.
================================================================================

