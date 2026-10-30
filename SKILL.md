---
name: baidu-footprint-export
description: Export Baidu Maps personal footprint history using verified local UUID CSV caches and month-by-month emulator ADB collection, convert BD-09MC tracks to WGS84 GeoJSON, GPX or KML for QGIS, and optionally build Android StepLife-compatible GPX with verified trip starts and compatibility-only later timestamps. Use for 我的足迹/历史轨迹 export and interrupted collection recovery.
---

# 百度地图足迹导出

复用已经实际跑通的“App 打开历史记录 → App 生成 CSV → 电脑只读拉取 → 转换”流程。脚本运行在电脑，手机或模拟器只接受普通 UI 操作和文件读取；uiautomator 会在共享存储写入一个临时 UI XML，不是修改系统。不得使用 Root、`su`、`run-as`、抓取凭证或替代消费者足迹接口的鹰眼 API。

## 先判定任务状态

- 用户已经要求停止或排除某月时，保留其范围；不要为测试 Skill 重启真实采集。
- 读取现有 `status.json`、`events.jsonl`、`collector.sqlite3` 和原始 CSV；用 `plan` 查看断点。现有目录与设备 ID 从用户环境发现，不从本次案例硬编码。
- 原始 CSV 已齐全时直接离线 `export`。用户明确要求导入安卓版“一生足迹”时，读 [格式与验证中的兼容输出](references/formats-validation.md#可选的一生足迹兼容输出)，再用独立的 `steplife` 子命令。需要更多历史时，先读 [采集与恢复](references/collection.md)。转换与验收读 [格式与验证](references/formats-validation.md)。需要判断某方法为何保留/放弃时读 [实际工作证据](references/evidence.md)。
- 如果存在 `references/local-session.md`，这是自用版的本机断点说明，只在继续该数据集时读取。

## 入口和参数

使用本 Skill 目录中的 `scripts/footprint_export.py`，Python 3.10+。离线导出只需标准库；实时采集还需要 Windows、可用的 ADB、Pillow、简体中文 Windows OCR，以及已由用户登录百度地图的设备。已验证 UI 配置是雷电 Android 9、720×1280；其他布局必须重新观察校准，不能靠猜测点击。

复制 `config.example.json` 为 `config.local.json`，配置工作目录、原始/状态/输出目录、ADB、设备和已验证的缓存目录。配置文件相对路径以配置所在目录为基准，raw/state/output 的相对路径再相对于 workspace。命令行同名参数覆盖配置。缓存目录默认留空，必须先只读发现并确认；不同安装的十六进制名称不能假定相同。

```powershell
python scripts/footprint_export.py export --config config.local.json --formats geojson,gpx,kml --prefix my_tracks
python scripts/footprint_export.py steplife --config config.local.json --prefix my_tracks --time-mode matched-start-order
python scripts/footprint_export.py plan --config config.local.json --start 2024-09 --end 2023-01
```

采集月份使用 `--start YYYY-MM` 和 `--end YYYY-MM`，均包含，按月倒序；`--exclude-month YYYY-MM` 可重复。新设备先执行只读 `pull-cache` 保存已有 CSV，再用 `collect ... --max-records 5` 跑基准。验收后去掉上限续跑。精确命令见采集参考，不要对已完成数据集执行基准。

## 必须保留的可靠性条件

1. 年月滚轮必须 OCR 回读，且筛选面板的实际年月完全匹配目标才应用；固定滑动次数不可靠。
2. 去重键包含年份：`年份|月日时间|距离`。UUID 是文件合并依据。地址可能被屏幕截断，不可作为唯一去重依据；无年份旧库需要备份并明确提供 `--legacy-year`，不能猜。
3. 完整底部卡片必须采集后再判断月末。已验证边界为 `screen_bottom - 5`，旧 `-30` 会漏。无移动只是月末推断，需检查 `month_end_inferred`、截图及末张卡片，不能宣称绝对全量。
4. 文件至少连续两次大小不变、非空后拉取；同名不同内容停止，保留原文件。已有 CSV 与 gzip 以 UUID 合并且检查内容冲突。
   raw/state/output 三个目录必须互不嵌套；临时下载使用随机独立文件。再次导出遇到同名成果时自动另存到 output/revisions，不覆盖旧成果。采集写现有库前自动保存完整备份，合并旧记录前写入 record_history。
5. 未出现新 CSV 标记 `server_only_or_slow`，这只表示本次未拿到文件，不能断言服务器独有。只对用户范围内未解决记录执行一次定向重试；禁止自动无限重扫。
6. 所有采集实例必须使用同一个状态目录以共享锁，避免同时操作同一设备。验证码/登录立即交给用户；脚本不会替用户完成验证。

## 交付与测试

交付输出绝对路径、唯一 UUID 数、文件数、坐标点数、相对基线新增数、未解决记录数和实际日期范围。不要拿 sequence 最大值或 App 宣称总数作完成阈值。GeoJSON 使用 WGS84 经纬度，QGIS 为 EPSG:4326；不把 BD-09MC 误标成 EPSG:3857，不凭缓存 mtime 编造行程或逐点时间。

目标是“一生足迹”或其他轨迹 App 时，先读 [格式与验证中的导入兼容问题](references/formats-validation.md#一生足迹导入兼容问题)。QGIS 分析 CSV 不符合一生足迹的固定表头；普通 GPX 有坐标但无逐点时间时，导入器可能仍报“未检测到坐标”。只有用户明确选择 `steplife --time-mode matched-start-order` 后，才生成兼容时间：

- 从只读 SQLite `records` 的 `new_file` 与 `fingerprint` 匹配真实行程起点。无法解析、冲突或缺失的记录不猜时间，放入 `review_required`。
- 每条轨迹第一点保留真实起点；后续点只分配单调递增的兼容序号秒，确保轨迹内部不同。`speed_raw` 单位未验证，不用距离或速度推算时间。
- 不同轨迹的真实起点相同时保留该真实值；只承诺每条轨迹内部唯一，不为了全局唯一而改动真实起点。
- 输出只写到本机 `output_dir/steplife/<prefix>`；不覆盖已有结果，重跑进入 revisions。交付目录含分批 `import_ready`、无时间 `review_required`、manifest、中文说明和导入顺序。
- GPX 与 manifest 不写源 UUID、地点指纹、地址或源路径。不要把输出目录、ZIP、数据库或运行日志提交到本仓库。
- 必须明确说明只有行程起点真实，后续时间是仅用于导入兼容的序号。轨迹跨当地午夜时如实报告，不能宣称逐点采样时间恢复成功。

复用测试：`python -m unittest discover -s tests -v`。测试使用临时合成数据，覆盖换目录/前缀/格式/时区、5/7 列与 gzip 合并、跨年去重、底部卡片、月份断点、定向重试、StepLife 单轨迹时间唯一性、重复真实起点保留、无时间复核和重跑不覆盖。参见 `references/validation-report.md` 了解本次测试范围；离线通过不代表新的 App 版本已经通过实时采集验证。

发布前核对 [数据资产保护](references/asset-safety.md)，运行 `python scripts/audit_public_tree.py .`，只发布开源目录/ZIP；个人配置、轨迹和现场日志不在开源包内。
