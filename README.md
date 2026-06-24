# DIT OFFLOADER — 素材备份校验与自动改名

基于Python3的专业的数字影像技术（DIT）素材备份软件。支持 macOS / Windows。

## 功能

- **安全拷贝**：逐块 `fsync` 落盘，`shutil.copystat` 保留元数据（mtime/atime/权限/birthtime）
- **流水线并发**：拷贝文件 B 的同时对文件 A 进行 xxhash64 校验
- **源+目标双校验**：源文件与目标文件独立计算 xxhash64，比对一致才标记完成
- **重名保护**：目标存在同名文件时追加 `-1`、`-2`，绝不覆盖
- **断点续传**：XML 日志记录每一个文件的拷贝/校验状态，中断后可从断点继续
- **仅校验模式**：不拷贝，仅比目标文件数量与源文件 hash 值
- **DIT 报告**：HTML 格式报告，视频首/中/尾 3 帧截图 + 元数据表 + XXHash-64 校验值
- **电子场记导入**： 从 https://givemehanzo.github.io/sscl/ 安装运行网页app 电子场记单，拷卡时导入_SSCL.xml文件实现素材根据场号镜号和机位号自动改名
- **深色 GUI**：PySide6 三栏布局，支持拖拽，macOS/Windows 原生风格

## 从Release下载打包好的exe或者dmg

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
python cli_test.py                 # 后端核心功能测试
python gui_smoke_test.py           # GUI 模块导入与构造
python gui_integration_test.py     # GUI×后端集成测试
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

### 中断恢复

- 拷贝/校验过程中强制退出 → 目标目录留下未完成的 `*_log.xml`
- 下次选择同一目标 → 弹窗询问「继续未完成任务」或「新建任务」
- 继续 → 自动跳过已校验文件，仅处理剩余文件

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
│   └── pipeline.py          # 流水线编排（拷贝线程 + 校验线程并行）
├── gui/
│   ├── main_window.py       # 三栏主窗口（深色主题）
│   ├── workers.py           # QThread 桥接
│   └── widgets/
│       ├── drive_panel.py   # 左侧：驱动器/文件浏览器
│       ├── copy_panel.py    # 中间：Copy From/To 配置
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

