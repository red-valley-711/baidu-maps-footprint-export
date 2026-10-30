# CSV 格式、坐标与验收

## 已验证的数据

- UUID 文件名：`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.csv`，另有 gzip 包装的 `.csv.gz`。
- 无表头数字行，5 列：`x_bd09mc,y_bd09mc,speed_raw,bearing_raw,accuracy_raw`。
- 7 列在末尾加两个语义未确定的数值；按 `extra_1/extra_2` 原样保留，不猜测时间或海拔。
- 不支持旧版 `bnav/TRAJECTORY` 二进制。本 Skill 不附旧转换器、APK、可执行文件或其样本。

新转换器保留原公式和逐点字段，检查列数、有限数值、至少两点、坐标落入现有实现的中国适用范围。非此格式/范围明确报错，不默默跳过。超出中国范围的处理未验证，不能将失败点硬截到边界。

坐标链是 `BD-09MC → BD-09LL → GCJ-02 → WGS84`。BD-09MC 与标准 Web Mercator 不同。转换结果是算法近似，不是测绘精度保证；沿用中间坐标可做百度底图对照。

没有逐点时间戳。`cache_mtime_*` 是本机文件的修改时间，ADB pull 也可能改变它；不是行程日期。月份筛选的日期元数据在 SQLite，不会伪造到逐点 `<time>`。要按真实行程日期分析，需另行核对 records 与 UUID 映射；无法可靠映射的记录不能猜。

## 一生足迹导入兼容问题

### CSV：QGIS 分析表不是目标 App 的导入模板

本 Skill 的逐点 CSV 使用 `track_id`、`lon_wgs84`、`lat_wgs84` 等分析字段。一生足迹通用导入器的模型定义采用以下表头及顺序（2026-10-30 核对）：

```text
dataTime,locType,longitude,latitude,heading,accuracy,speed,distance,isBackForeground,stepType,altitude
```

来源：[steplife-universal-importer/internal/model/steplife.go](https://github.com/hygao1024/steplife-universal-importer/blob/main/internal/model/steplife.go)。这是用户指定的第三方转换项目模型，不等于已对所有一生足迹版本完成导入实测。该文件本身也不足以确认 dataTime 的时间单位、时区以及每个字段的可空/默认值规则。

用户本次“CSV 表头错误”的原因已明确是模板不匹配。不能直接导入 QGIS CSV，不能只改扩展名，也不能仅把经纬度列重命名就宣称完全兼容。当前实现选择 GPX 兼容输出，不生成上述 CSV 模板。

尤其是 dataTime：本次源 CSV 没有逐点时间戳，缺失信息不能通过改表头恢复。保留原始数据及 QGIS 成果，不用缓存修改时间、当前时间或任意固定间隔自动伪造真实采样时间。

### GPX：“未检测到坐标”不等于文件里没有坐标

用户追加的完整解析核验结果是：本次 GPX 实际有 **2752 条轨迹、2404667 个坐标点**，经纬度范围有效，但所有点均没有时间戳。此结果记录自用户本次核验，不代表本次文档更新又重新读取了整个 GPX。

排查时区分三个事实：XML 能否解析、是否存在带有效 lat/lon 的 trkpt、是否存在有效逐点 time。不能仅根据目标 App 的提示宣称导出没坐标、轨迹损坏或需要重新采集。无时间点是否被该导入器过滤，需要检查目标解析逻辑或最小样本实测；目前不能把缺时间戳断言成唯一已证实原因。

### 可选的一生足迹兼容输出

用户实测后确认了第二个关键行为：同一条轨迹里的所有点使用相同时间时，安卓版一生足迹只留下很少的点状结果；一个 277 点、逐点时间各不相同的最小样本可以正常显示轨迹。因此兼容输出必须让轨迹内时间不同，但不能把兼容值说成真实采样时间。

使用独立命令，不混入默认 `export`：

```powershell
python scripts/footprint_export.py steplife `
  --config config.local.json `
  --prefix my_tracks `
  --time-mode matched-start-order `
  --max-points 100000
```

`--time-mode matched-start-order` 是强制显式确认，不提供把行程起点重复给全部点的快捷模式。生成规则：

1. 只读状态库 `records.new_file` 与 `records.fingerprint`，把 UUID 匹配到可解析的真实行程起点；冲突、缺失或无法解析的轨迹不猜日期。
2. 每条轨迹第一点保留真实起点。原始 CSV 的 `speed_raw` 单位未独立验证，不能当作米/秒；后续点只按 1 秒递增分配兼容序号，并跳过已占用时间或其他轨迹的真实起点。
3. 不同轨迹若有相同真实起点，保留该真实值；只要求每条轨迹内部时间唯一。分配后逐轨迹检查重复时间，并汇总重复真实起点数量。
4. 未匹配轨迹单独写入 `review_required/no_verified_start.gpx`，`time` 全部省略，不进入 import_ready。
5. 输出写到 `output_dir/steplife/<prefix>`，重跑自动进入 `output_dir/steplife/revisions/...`，不覆盖首次成果。原 CSV 前后 SHA-256 必须一致。

manifest 汇总真实起点数、兼容序号跨度、最大分配漂移、重复起点数和跨午夜轨迹数，但不写源 UUID、地点指纹、地址或源路径。只有行程起点是真实时间；其他点时间既不是采样值，也不是速度或距离估算。跨午夜属于兼容序号分配后的可观察结果，必须如实报告。

现有 277 点实机数据只确认了单条轨迹能显示。安卓版一生足迹是否会把“不同轨迹但起点相同”的点做全局合并尚未验证；输出保留这些真实起点，出现跨轨迹合并时应继续做目标 App 最小样本测试，不能静默改时间。

## 导出

```powershell
python scripts/footprint_export.py export --raw-dir D:/exports/raw --output-dir D:/exports/gis --state-dir D:/exports/state --formats geojson,kml --prefix archive --timezone-offset-hours 0
```

输出 `_manifest.json` 列出选中格式的绝对路径、SHA-256、文件数、唯一轨迹数、点数、总几何长度、坐标说明。若已有任一同名成果或 manifest，整轮新结果写入 output/revisions/时间与随机编号/，旧成果不覆盖；以本轮返回的 manifest 和绝对路径为准。目录中的非 UUID 文件忽略，不把 unrelated.csv 当轨迹。raw/state/output 必须互不包含。

`.csv` 与 `.csv.gz` 同 UUID 时优先明文，并解析比对实际点内容；内容冲突停止。不同 UUID 即使坐标相同也不擅自合并。全量数据在内存合并，数百万点和带扩展字段的 GPX 可能耗用数 GB 内存；大量数据优先 GeoJSON，磁盘空间不足不要重跑采集。

## 验收标准

1. 原始副本不变：导出前后 SHA-256 不变，手机原 CSV 不删除、不覆盖。
2. 每个 UUID 只产生一条轨迹；manifest.track_count 与 GeoJSON features、GPX trk、KML Placemark 数一致；三种格式逐点数一致。
3. 非空且可被 JSON/XML 解析；LineString 至少两点；GeoJSON 坐标顺序为经度、纬度；GPX 经纬度属性符合对应值。
4. 输出为 EPSG:4326，在 QGIS 抽查多条不同地区/年份轨迹的位置与走向。离线测试只能验证格式和公式回归，不能证明真实地理精度。
5. 新任务的 baseline_snapshot.json 在首次拉取前记录本机已有的 UUID；新增数用最终 UUID 集合减基线集合，不用 sequence 差。支持本案例的 track_ids/file_count 结构；若其他旧基线格式未被识别，added_unique_tracks 为 null，不伪报 0。
6. 轨迹文件导出完备不代表云端列表全量。逐月完成状态、末卡片、慢加载、未解决项都须核对；报告覆盖范围和剩余项，勿用 App 页面总数替代数据验收。

## 不启动真实采集的复用测试

运行 tests 中的标准库 unittest。合成 CSV 不是用户轨迹，临时目录自动隔离；用另一组日期、目录、前缀、格式、时区检验参数生效。真实原始文件抽样复算如需执行，副本保留在私有测试目录，绝不打包上传。
