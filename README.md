# Baidu Maps Footprint Export / 百度地图个人足迹与历史轨迹导出

An AI Agent Skill for exporting personal footprint and historical location records from the Baidu Maps Android app using a user-owned, logged-in device or emulator. The workflow reads locally generated UUID CSV caches, then converts BD-09MC tracks to WGS84 GeoJSON, GPX, and KML for QGIS.

百度地图个人足迹与历史轨迹导出 Skill。通过已登录的 Android 设备或模拟器逐月打开“我的足迹”，让百度地图正常生成本地 UUID CSV，再由电脑只读拉取并转换。它不是百度官方消费者导出接口，不抓取 Cookie，也不保证任意账号或 App 版本都能恢复全部云端历史。

**Search terms / 检索词：** Baidu Maps footprint export, Baidu Map track export, location history backup, 百度地图足迹导出, 百度地图历史轨迹导出, GPS track converter, AI Agent Skill, Codex Skill, Claude Skill, GPX, GeoJSON, KML, QGIS.

QGIS 格式之外，可显式选择生成安卓版“一生足迹”兼容的本地导入包。该输出只把可核对的百度行程起点记为真实时间；后续点为避免 App 合并同一时间的点，仅追加单调递增的兼容序号，不声称是原始采样时间或速度推算结果。

## 使用

将整个文件夹安装到 `~/.codex/skills/baidu-maps-footprint-export`，然后调用 `$baidu-maps-footprint-export`。也可直接用命令行：

```powershell
python scripts/footprint_export.py export --raw-dir ./raw --output-dir ./qgis --formats geojson,gpx,kml --prefix my_tracks
python scripts/footprint_export.py steplife --raw-dir ./raw --state-dir ./state --output-dir ./qgis --prefix my_tracks --time-mode matched-start-order
python -m unittest discover -s tests -v
python scripts/audit_public_tree.py .
```

离线转换需要 Python 3.10+。实时采集需要 Windows、ADB、Pillow、简体中文 Windows OCR、已由用户登录的百度地图。仅验证过雷电 Android 9、720×1280 的页面布局；App 版本变动需先验证，不自动修改系统。实时采集安装依赖 `python -m pip install -r requirements.txt`。

复制 config.example.json 为 config.local.json，发现并填写自己设备的缓存目录。具体流程、5 条基准、断点续跑和失败恢复从 [SKILL.md](SKILL.md) 进入。日期必须显式给出，命令行支持排除月份和只重试未解决项。

真实设备会话中曾成功转换足迹数据；精确轨迹数、点数、活动月份和遗留数量属于个人活动元数据，因此不在公开文档中披露。这也不构成对任何账户数量、App 版本或全量云端恢复的保证。CSV 无逐点时间，缓存修改时间不能充当行程时间。验证范围见 [测试报告](references/validation-report.md)。

`steplife` 是独立、非默认的输出。它要求 `--time-mode matched-start-order` 明确确认；普通 `export` 仍不生成任何逐点时间。原始 CSV 的 `speed_raw` 单位未验证，因此兼容输出不用它推算时间。没有可靠匹配到行程起点的足迹会单独写入 `review_required` 且不带时间，不猜日期。导入目录、复核文件、manifest 和 ZIP 都只留在本机，不提交 Git。

## 隐私与发布内容

本仓库只含 Skill、脚本、示例配置、合成数据测试和汇总证据，不含用户 CSV、数据库、日志、截图、账号、Cookie 或令牌。config.local.json 被忽略，自己的导出结果不要提交。本地配置、测试真实轨迹样本和运行输出与仓库分开保存。

`scripts/audit_public_tree.py` 会在发布前拒绝 CSV/GPX/KML/数据库/日志/截图等数据文件、私有配置和明显凭据。`.gitignore` 同时忽略原始目录、QGIS/StepLife 输出、revision 和常见数据扩展名。导出时 GPX 与 manifest 默认不写源 UUID、地点指纹、地址、源路径或绝对本机路径；StepLife manifest 只使用相对于输出目录的交付目录和文件名。原始 CSV 前后做 SHA-256 核对。

MIT 许可见 LICENSE；坐标公式与社区背景说明见 NOTICE。
