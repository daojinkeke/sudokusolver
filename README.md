数独 OCR 求解器（拍照/截图 → 识别 → 求解 → 自动回填）
`sudoku_ocr.py` 是一个完整的数独自动化流水线：把屏幕截图或图片交给 Tesseract OCR 识别成 81 字符题目，调用 Tdoku（C++ SIMD 求解器）得出答案，然后用 Windows `SendInput` 把答案拟人点击回填入数独 APP，支持循环刷题直到全部通关。
```
 ┌────────────┐   ┌─────────────┐   ┌───────────┐   ┌────────────┐   ┌────────────────┐
 │ 图片/截屏  │→│ 网格 + 按钮检测│→│ 并发 OCR  │→│ tdoku 求解 │→│ 拟人点击回填     │
 │            │   │ (带坐标缓存)  │   │           │   │            │   │ (循环刷题)       │
 └────────────┘   └─────────────┘   └───────────┘   └────────────┘   └────────────────┘
```
目录结构
```
Sudoku/
├── sudoku_ocr.py            # 本项目主脚本
├── README.md                # 本文档
├── tdoku-master/            # C++ 高性能数独求解器（BSD 2-Clause）
│   └── solve.exe            # 已编译好的 CLI 求解器（Windows x64）
├── tesseract-main/          # Tesseract OCR 源码（Apache 2.0），运行时二进制需单独安装
├── inputsimulator-master/   # 拟人输入的参考实现（MIT），脚本本身未直接依赖
└── puzzle/                  # 运行时产物（首次运行自动创建）
    ├── puzzle.png           # 当前截图
    ├── puzzle_backup/       # 上一张截图（仅保留 1 张）
    ├── grid_cache.json      # 网格 / 按钮坐标缓存（分辨率自适应 + 抽样校验）
    └── result_*.txt         # 带格子编号的识别与求解结果
```
依赖
依赖	作用	安装
Python ≥ 3.6	运行环境	仅用标准库 + Pillow
Pillow	图像读写、裁剪、二值化、截屏	`pip install pillow`
Tesseract OCR	单格数字识别，需含 `eng.traineddata`	Windows：`winget install UB-Mannheim.TesseractOCR`
Tdoku	核心求解器，默认路径 `./tdoku-master/solve.exe`（仓库已附带编译产物）	重新构建见下节
重新编译 Tdoku（可选）
仓库已附带可直接运行的 `tdoku-master/solve.exe`，一般无需编译。如需自行构建：
WSL / Linux / macOS（官方方式）：
```bash
  cd tdoku-master
  ./BUILD.sh
  ```
Windows + MinGW-w64：官方示例 `example/solve.c` 使用了 POSIX `getline`，MinGW 缺少该函数无法直接链接。核心求解器只需两个源文件，写一个用 `fgets` 替代 `getline` 的精简 C 驱动后编译即可：
```powershell
  g++ -O3 -march=native -Iinclude -o solve.exe solve_win.c `
      src/solver_dpll_triad_simd.cc src/util.cc
  ```
驱动逻辑：从 stdin 用 `fgets` 逐行读题，调用 `tdoku.h` 的 `SolveSudoku(puzzle, 1, 0, solution, &guesses)`，输出 `原题:解数量:答案`。
确认 Tesseract 可用
```powershell
tesseract --version
# 或设置环境变量：
# $env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```
脚本会按以下顺序自动定位：`--tesseract` 参数 → `TESSERACT_CMD` 环境变量 → PATH → 常见安装目录。
快速开始
1. 识别并求解已有图片
```powershell
python sudoku_ocr.py puzzle.jpg
```
2. 截屏模式（倒计时 3 秒 → 自动截屏 → 识别 → 求解 → 写结果 txt）
```powershell
python sudoku_ocr.py --delay 5
```
3. 只识别不求解
```powershell
python sudoku_ocr.py puzzle.png --no-solve
```
4. 只输出 81 字符题目（方便管道）
```powershell
python sudoku_ocr.py puzzle.png --raw
```
5. 指定 Tesseract 路径
```powershell
python sudoku_ocr.py puzzle.png --tesseract "C:\Program Files\Tesseract-OCR\tesseract.exe"
```
高级：拟人点击回填（仅 Windows）
脚本内置 `HumanClicker` 类，用纯 `ctypes` 封装 Win32 `SendInput`（设计思路与 `inputsimulator-master` 一致，但不依赖它）：
截图坐标自动换算到 Windows 虚拟桌面归一化坐标，支持多显示器
自动 DPI-aware，避免高分辨率屏幕上截屏坐标与点击坐标错位
贝塞尔弧形轨迹 + 高斯抖动 + 15% 概率过冲回拉 + 随机按压/停顿
任意时刻按住 ESC 可安全中止
6. 求解后自动回填当前这一题
```powershell
python sudoku_ocr.py --fill --delay 5
# 倒计时截屏 → 识别 → 求解 → 切到数独窗口 → 再倒计时后开始回填
```
7. 循环刷题模式（回填后自动点“下一题”，直到出错或按 ESC）
```powershell
python sudoku_ocr.py --loop
python sudoku_ocr.py --loop --loop-delay 4 --next-wait 10
```
参数	默认	含义
`--loop-delay`	3.0	点击“下一题”后等待新题加载的秒数
`--next-wait`	8.0	等待“恭喜过关”弹窗出现的最长秒数
`--fill-delay`	3	首轮回填前切换窗口的倒计时秒数
命令行参数一览
参数	默认	说明
`image`	无	数独题目图片；不提供则自动全屏截屏存入 `puzzle/puzzle.png`
`--delay`	3	截屏模式倒计时秒数
`--tesseract`	自动探测	`tesseract.exe` 完整路径
`--solver`	`tdoku-master/solve.exe`	求解器路径，兼容 `puzzle:count:solution` 输出格式
`--no-solve`	false	只识别，不调用求解器
`--raw`	false	只输出 81 字符串，方便管道使用
`--txt`	false	文件模式下也输出带编号的结果 txt（截屏模式默认输出）
`--debug-dir`	无	保存切出的格子图和网格线覆盖图，便于排查误识别
`--recache`	false	忽略坐标缓存，强制重新检测网格与按钮
`--fill`	false	求解后拟人点击回填（仅 Windows，需截到下方粉色数字按钮行）
`--fill-delay`	3	`--fill` 开始前切换窗口的倒计时秒数
`--loop`	false	循环刷题（仅截屏模式 + 仅 Windows + 需要求解）
`--loop-delay`	3.0	点击下一题后等待新题加载的秒数
`--next-wait`	8.0	等待“恭喜过关”弹窗的最长秒数
核心流程详解
1. 图像预处理（`load_and_preprocess`）
宽度小于 1000 像素的图自动放大 2 倍以上，保证 OCR 精度
同时保留 RGB 原图（彩色网格线检测）与灰度图（Otsu 二值化 / OCR）
2. 网格检测（彩色 / 深色双线兜底）
彩色线路径（`detect_grid_colored`）：提取高饱和、不太暗的像素（青蓝网格线、粉色选中框），做行/列投影 → 聚合成候选线 → `fit_lattice` 估计等距线距并枚举锚点，拟合出完整 10 条格网线，允许少数线被选中框遮挡
深色线路径（`detect_grid_dark`）：全局 Otsu 二值化后对深色像素投影，同一套分组拟合逻辑，作为黑白照片的兜底
横纵线距比在 0.72～1.38 之间才接受（正方形格子校验）
3. 数字按钮行检测（`detect_keypad`）
在网格底边线下方 0.35～2.3 格距范围内，按粉色判定（高 R、中低 G/B、明显偏红）做行列投影
一维闭运算桥接按钮内部数字笔画造成的短低谷，保留按钮间真间隙
必须恰好分出 9 个按钮、间距变异系数 < 0.08、总跨度与网格宽匹配才通过
4. 坐标缓存（`puzzle/grid_cache.json`）
首次检测到的网格线、9 个数字按钮、“下一题”按钮坐标连同截图分辨率一起写入 JSON
后续运行先按分辨率差异等比缩放，再抽样校验网格线上是否仍有彩色线条；校验失败或读取异常（无解 / 已知数少于 17）时自动重检一轮
`--recache` 可强制绕过缓存
5. 并发 OCR（`recognize` / `ocr_one`）
按网格裁出 81 张单格图，空格跳过
`ThreadPoolExecutor`（6 线程）并发调用 tesseract，白名单限定 `123456789`
单格按 PSM 8 → 13 → 6 → 10 依次兜底，识别到数字即返回
6. 求解（`run_solver`）
向 `solve.exe` 传入参数 `1`（只求一个解），stdin 写入 81 字符题目
解析输出末行 `原题:解数量:答案`，取答案段；也兼容直接输出 81 字符的求解器
求解失败时触发两轮重试：强制重新检测网格坐标后再 OCR 一次
7. 拟人点击回填（`HumanClicker.fill_sudoku`）
先瞬移归位到第一个空格，避免长距离贝塞尔轨迹途经任务栏
每个空格：点击格子 → 随机停顿 → 点击下方对应数字按钮（适配点按钮输入的数独 APP）
移动轨迹为带随机控制点的二次贝塞尔曲线，配合高斯抖动、近目标衰减、偶发过冲回拉
点击前后的反应/按压/间隔均为截断高斯随机时长；每 0.1 秒轮询 ESC，按住即中止
8. 循环刷题（`main_loop`）
首轮倒计时截屏，之后零延迟截屏，循环执行：识别求解 → 拟人回填 → 轮询截屏等待“恭喜过关”弹窗（实时检测“下一题”按钮并写缓存，超时回退缓存坐标）→ 点击下一题 → 等待新题加载。截屏失败、求解失败、按钮缺失等任一环节异常都会安全结束循环。
输出示例
终端：
```
识别到的题目 (坐标来源: 缓存坐标):
7 . . | 9 . . | . 8 .
. 6 3 | . . . | 4 . .
...

求解结果 (解的数量: 1):
7 5 4 | 9 6 1 | 2 8 3
2 6 3 | 5 8 7 | 4 1 9
...
```
`puzzle/result_YYYYmmdd_HHMMSS.txt`：包含带 01-81 编号的题目与答案、81 字符字符串、1-9 数字按钮的屏幕坐标。
常见问题
现象	可能原因	解决
报错“未找到 tesseract”	未安装或不在 PATH	`winget install UB-Mannheim.TesseractOCR`，或用 `--tesseract` 指定路径
数字识别错误（如空格识别成 8）	单格图有干扰线/背景	加 `--debug-dir` 检查切格图；截图时让网格占满画面
提示“改用等分方式”	网格线太淡/分辨率过低	放大窗口或截图后重试，确保 10 条网格线清晰
求解失败	OCR 错误导致题目无解	用 `--recache` 强制重检；对照终端打印的题目手动核对
`--fill` 报找不到数字按钮	粉色按钮行没截全或界面变色	重新截屏确保按钮行完整露出；必要时 `--recache`
回填点击位置偏移	改过显示缩放比例/分辨率	重新运行让缓存自动更新，或加 `--recache`
找不到“下一题”按钮	未过关或弹窗样式变化	手动过关后继续，或调大 `--next-wait`
许可证
组件	许可证	版权
`sudoku_ocr.py`（本项目脚本）	建议 MIT	见根目录 LICENSE（如添加）
`tdoku-master/`	BSD 2-Clause	Copyright (c) 2019 Tom Dillon
`tesseract-main/`	Apache 2.0	Apache Software Foundation
`inputsimulator-master/`	MIT	Copyright (c) 2019 Michael Noonan
三个均为宽松型许可证，互相兼容，允许商业使用、修改与再分发。分发时请保留各子目录中的原始 LICENSE 与版权声明；若修改过 Apache 2.0 组件的源文件，需在改动文件中注明修改内容。
关键源码速查
功能	函数 / 类	位置
图像预处理	`load_and_preprocess`	`sudoku_ocr.py:112`
等距格网拟合	`fit_lattice`	`sudoku_ocr.py:145`
彩色 / 深色网格检测	`detect_grid_colored` / `detect_grid_dark`	`sudoku_ocr.py:205 / 236`
粉色数字按钮检测	`detect_keypad`	`sudoku_ocr.py:272`
“下一题”按钮检测	`detect_next_button`	`sudoku_ocr.py:368`
缓存抽样校验 / 读写	`validate_cached_grid` / `load_cache` / `save_cache`	`sudoku_ocr.py:457 / 483 / 496`
单格 OCR / 并发识别	`ocr_one` / `recognize`	`sudoku_ocr.py:590 / 613`
调用求解器	`run_solver`	`sudoku_ocr.py:628`
截屏与截图轮换	`capture_screen` / `rotate_to_backup`	`sudoku_ocr.py:677 / 662`
拟人鼠标（SendInput）	`HumanClicker`	`sudoku_ocr.py:782`
贝塞尔移动 / 回填	`move_to` / `fill_sudoku`	`sudoku_ocr.py:861 / 917`
单轮识别（两轮重试）	`recognize_round`	`sudoku_ocr.py:1013`
循环刷题	`main_loop`	`sudoku_ocr.py:1110`
命令行入口	`main`	`sudoku_ocr.py:1187`
