# 电商采集程序版本管理

2026-11-27 以当前云端部署建立独立 Git 基线，分支 main。
本地工作副本保存同一提交；旧本地仓库及处方项目未合并、未改动。

## 使用

- 查看修改：`git status`、`git diff`。
- 提交经检查的代码：`git add <文件>`，然后 `git commit -m "说明"`。
- 本仓库采用源码白名单；新增源码须同时将路径加入 `.gitignore` 的例外列表。
- `git show baseline-20261127:<文件>` 可查看本次基线，不要在运行目录执行全目录 reset 或 clean。
- 当前未配置 GitHub 等外部远程；本地 clone 的 origin 指向云端工作目录，仅用于同步读取。
- 修改部署配置时先核对线上服务。`deploy/systemd` 是单元文件快照，不会自动安装或启用。

## 不纳入版本库

实际账号密码、密钥、env/config、浏览器登录态、运行状态、数据库数据、上传文件、日志、
venv/node_modules、备份与候选目录均保留原位置并忽略。历史 AGENTS.md 含旧凭据，不纳入本次仓库。
不得修改原有处方目录、数据、浏览器、服务或定时器。

## 当前入口

- `direct_pipeline.py`：美团/京东采集，直接写 ec_dw。
- `tmall_direct_ingest.py`：天猫上传快照入库；`tmall_ecommerce_receiver/` 为接收服务。
- `joyspace_direct.py` 与 `run-joyspace-catalog-sync.sh`：京东公共目录同步。
- `run-store-cycle.sh` 与 `run-ecdw-feeds.sh`：周期调度。
- `deploy/schema/ec_dw.sql`：当前数据库结构快照，仅含 DDL，不含业务数据。
- `requirements-runtime.txt` 锁定当前云端采集依赖；原脚本与测试同时留存，旧兼容脚本不表示重新启用。
- `/etc/subsidy*` 和各店配置不入库；部署配置须另行安全配置。
- Windows 天猫浏览器采集端位于独立主机，本仓库当前覆盖云端接收与入库程序。
