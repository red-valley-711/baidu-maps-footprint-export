# 采集、续跑和恢复

## 环境与发现

电脑端运行 Python，模拟器不安装采集脚本。已验证的百度地图版本为 20.16.30；基准 JSON 记录 Android 9，UI 脚本以 720×1280 为已知布局。不同版本/分辨率先观察，不自动改变分辨率、系统设置或应用权限。

用户完成登录并打开“我的足迹记录”。已观察到的路径为百度地图“我的 → 历史行程”，部分页面先到足迹总览，再点“查看全部历史轨迹”。入口文字变动时用可用的 Computer Use 工具看截图，重新找真实入口；脚本不会调用私有 API。

安装实时采集依赖：`python -m pip install -r requirements.txt`。确保 `adb devices -l` 中目标已授权。只读查看：

```powershell
adb -s emulator-5554 shell ls -la /sdcard/Android/data/com.baidu.BaiduMap
adb -s emulator-5554 shell ls -la /sdcard/Android/data/com.baidu.BaiduMap/实际发现的目录
```

寻找十六进制目录内 UUID 命名的 `.csv` / `.csv.gz`。拉取一个样本，确认是无表头的 5/7 列数字，前两列为 BD-09MC x/y；不能因为目录名像十六进制就盲信。在配置中填写发现的绝对 Android 路径。如果访问被拒绝，停止此路线；不使用 Root 或绕过应用沙箱。`pull-cache` 不自动枚举/猜测目录。

缓存文件的读取是只读；点击记录本身会让 App 正常更新缓存。uiautomator 使用共享存储临时 XML，截图通过 `exec-out screencap -p` 读取。

## 首次采集

以下示例必须换成用户实际授权的年月，勿把示例日期当断点：

```powershell
python scripts/footprint_export.py pull-cache --config config.local.json
python scripts/footprint_export.py collect --config config.local.json --start 2024-09 --end 2024-01 --max-records 5 --file-timeout 20 --ui-timeout 180 --no-qgis
```

核对五次动作：实际点击的年月与卡片一致；新增文件大小稳定、可解析；events 与 SQLite 一致；返回列表成功；已采卡片再次出现时不再点击。已有缓存可能导致没有“新文件”，不能保证每次点击都新增。

基准通过后运行同一命令并移除 `--max-records 5 --no-qgis`。默认每 500 个动作及结束时重建输出。`--max-months N` 可限制月份试跑。输出格式由 `--formats` 选择，包含逐点 CSV 时文件会很大。脚本没有固定记录数阈值，也没有连续空月自动跳过剩余年份：以显式最老年月为边界。

## 状态与恢复

- `collector.sqlite3`：`records` 保存含年指纹与文件结果，`collection_months` 保存逐月状态。`plan` 只读打开数据库；运行月也必须位于调用方明确给出的范围内。
- 采集每次打开现有 records 库准备写入前，先用 SQLite backup 保存 state/backups/collector-before-write-随机名.sqlite3 并做完整性检查；失败即停止。去重或修正记录前，原行完整 JSON 保存到 record_history，避免合并后无法追溯。
- SQLite 的只读打开不会修改数据行，但 WAL 模式可能创建 `-wal/-shm` 辅助文件；若检查环境要求目录也完全不变，先对关闭的数据库制作私有副本检查。不要在活动库上使用 immutable 模式忽略 WAL。
- `collector.lock`：Windows 文件锁；锁文件存在不等于锁被持有。不要删除锁来启动第二个实例。不同状态目录无法互锁，同一设备只运行一个采集器。
- `status.json`：原子更新状态，含当前和最后节点、状态计数、进程号。换月的 OCR 操作可能一段时间没有写状态；不能仅以 PID 不可见或单次时间差判死。
- `events.jsonl`：逐条动作与 `month_end_inferred` 等事件。`max_records_reached`、`max_months_reached` 表示主动试跑上限，不表示范围采完。

异常时先确认页面。卡在详情就返回；总览点“查看全部历史轨迹”；退出 App 则从图标重新进入。检查没有别的实例持锁，再用同一配置和年月范围重启，完成月份与已登记卡片会跳过。不要重新初始化数据库或更换状态目录“解决”中断。

年月 OCR 不匹配会最多修正 40 次，筛选面板二次校验也有上限，失败停下。截图裁剪会写到 state/picker_year.png 和 picker_month.png，检查选中行、字体和裁剪；`references/ldplayer-720x1280.json` 只允许在视觉验证后修改。Windows OCR 缺中文语言时先报告缺失，由用户决定系统变更。

无新 CSV 不证明没有轨迹：可能已有缓存、加载慢、网络问题、返回页错误或年份误标。一次定向重试：

```powershell
python scripts/footprint_export.py collect --config config.local.json --start 2024-09 --end 2023-01 --retry-unresolved --file-timeout 60 --ui-timeout 180
```

只排入范围内包含未解决记录的月份，扫描列表时只点击目标指纹。元数据保留为 `retry_pending`，完成后回到 pulled/duplicate 或 server_only_or_slow；中断后也保留原序号。仍未解决就单列，不循环重试。日期疑似误标时先保留证据，不能仅按同月日/距离跨年合并。

底部漏采复核需明确执行 `--audit-months`，它仅将指定范围的月状态改回 pending，仍跳过已有记录；不要在常规巡检中自动全库重置。

需要恢复元数据时，先停止采集，保留当前库及 WAL/SHM。将完整备份复制到一个新的私有 state 目录，再用 plan 核验后由用户决定采用哪个断点，不直接覆盖唯一状态库。原始 CSV 与各轮导出版本独立保留，不随数据库回退删除。

## 可选巡检

只有用户要求时才创建/更新自动任务。建议每 30 分钟读取状态、日志、月份与末节点；健康且页面正常保持安静。进程会话存在、状态近期更新、日志有进展均是证据。持续停滞先观察截图，再判断是否恢复，不能因看不到后台进程重复启动。

自动任务保存实际配置路径和用户给出的范围，使用新的 `collect --config ... --start ... --end ...` 入口；排除月份同步保存。不得复制本案例旧路径、旧年份或已停止的任务。完成、失败、验证需接管才通知。范围完成且用户不要继续时删除巡检。未得到后续授权，不自动增加新月份或安排复扫。
