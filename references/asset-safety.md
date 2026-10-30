# 数据资产保护检查

本次安全复核区分“公开分享内容”与“运行脚本的本地写入”。不能给出任何软件在断电、磁盘损坏、恶意改包或错误设备上都绝不会损失的保证。

## 发布内容

开源目录和 ZIP 不含用户原始轨迹、坐标样本、地点名、真实 UUID 列表、状态库、日志、截图、账号、Cookie、令牌或本机配置。tests 只使用合成 UUID 和合成坐标。历史复盘只保留汇总计数、错误原因和算法说明，算法代码本身是公开分享内容。

包中示例的缓存路径留空，不能据此定位任何账户。历史证据里的数字和年份不构成用户的逐条轨迹。不要把整个原任务 outputs 目录或个人版 ZIP 上传 GitHub。

发布前运行：

```powershell
python scripts/audit_public_tree.py .
python -m unittest discover -s tests -v
```

审计会拒绝 CSV、CSV.GZ、GPX、KML、GeoJSON、SQLite/DB、JSONL、日志、图片、电子表格、压缩包、HTTP 归档、`config.local.json`、`*.local.json`、`.env`、私钥/凭据文件和明显密钥。它只覆盖本仓库目录，不能证明外部备份或 GitHub 历史没有旧泄漏；发现旧提交含数据时仍需单独清理远端历史。

## StepLife 可选输出

`steplife` 子命令只读原始 CSV 和状态库，默认把结果写到 `output_dir/steplife/<prefix>`。`.gitignore` 已忽略 `steplife/`、`revisions/` 与常见数据扩展名，但用户仍应在提交前运行审计。

为了减少导出本身的敏感信息：

- GPX track name 使用序号，不包含源 UUID、地址或地点指纹。
- manifest 只含数量、文件哈希、时间策略和汇总分配统计，不含源路径、UUID、fingerprint 或地址。
- `review_required` 中的未匹配轨迹也不含时间，不能误当真实日期导入。
- 原始 CSV 导出前后逐文件 SHA-256 一致；状态库以 SQLite read-only URI 打开。

输出文件仍含个人坐标和时间，只是去掉了直接标识字段；它仍是敏感的个人数据资产。不得上传、粘贴到 issue、附在 Skill ZIP 或提交到公开仓库。

## 本次发现并修复的实际风险

1. 旧版允许 raw=state/pulls，可能导致 adb pull 与原始文件同路径，后续 duplicate 清理误删原文件。现在配置入口拒绝 raw/state/output 任意嵌套，核心拉取函数也独立检查 raw/pulls 不重叠，临时文件采用随机名，最终文件用独占创建模式。
2. 旧版导出用 replace 覆盖同名成果。现在遇到任一同名输出/manifest，整轮另存到 revisions 子目录，最终落盘也拒绝覆盖，临时文件名唯一。写失败时保留新临时/部分文件用于恢复，不删除旧成果。
3. 旧版指纹去重会删除重复登记行，完整结构的数据库未必先备份。现在每次准备写现有库前保存完整 SQLite 备份并检查 integrity_check；合并或修正前还把原行完整归档到 record_history。

测试覆盖目录重叠拒绝、同名原始文件冲突后哈希不变、重复导出旧文件与 manifest 不变、去重前备份含全部原行且可读取，以及 history 可找回被合并的登记。

## 仍有的正常写入和边界

collect 会更新本地状态与数据库、追加事件、生成缓存副本；App 打开历史记录也会正常更新其缓存。这不是纯读取整台设备。手机端没有删除轨迹、清缓存、退出账户、卸载或改系统的命令。

plan/export 只读打开原始 CSV 和状态库；SQLite WAL 读取可能产生辅助文件。多进程应使用同一 state 锁，不可同时操作同一设备。备份/成果版本会占用磁盘，不自动删除历史版本来腾空间。

此检查不等价安全认证。保留原始 CSV 与独立备份；只读导出成功也不证明云端记录已全部获取。
