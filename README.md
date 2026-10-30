# baidu-footprint-export

百度地图个人足迹导出 Skill。通过已登录模拟器逐月打开记录，让百度地图正常生成本地 UUID CSV，再由电脑只读拉取，转换为 WGS84 GeoJSON、GPX、KML。没有消费者云端接口或 Cookie 抓取。

## 使用

将整个文件夹安装到 `~/.codex/skills/baidu-footprint-export`，然后调用 `$baidu-footprint-export`。也可直接用命令行：

```powershell
python scripts/footprint_export.py export --raw-dir ./raw --output-dir ./qgis --formats geojson,gpx,kml --prefix my_tracks
python -m unittest discover -s tests -v
```

离线转换需要 Python 3.10+。实时采集需要 Windows、ADB、Pillow、简体中文 Windows OCR、已由用户登录的百度地图。仅验证过雷电 Android 9、720×1280 的页面布局；App 版本变动需先验证，不自动修改系统。实时采集安装依赖 `python -m pip install -r requirements.txt`。

复制 config.example.json 为 config.local.json，发现并填写自己设备的缓存目录。具体流程、5 条基准、断点续跑和失败恢复从 [SKILL.md](SKILL.md) 进入。日期必须显式给出，命令行支持排除月份和只重试未解决项。

现场数据曾成功导出 2752 个唯一 UUID、2404667 点；不是对任何账户数量、App 版本或全量云端恢复的保证。CSV 无逐点时间，缓存修改时间不能充当行程时间。验证范围见 [测试报告](references/validation-report.md)。

## 隐私与发布内容

本仓库只含 Skill、脚本、示例配置、合成数据测试和汇总证据，不含用户 CSV、数据库、日志、截图、账号、Cookie 或令牌。config.local.json 被忽略，自己的导出结果不要提交。本地配置、测试真实轨迹样本和运行输出与仓库分开保存。

MIT 许可见 LICENSE；坐标公式与社区背景说明见 NOTICE。
