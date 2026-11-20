# 示波器波形采集平台

实验室数据工程师向频谱分析交付示波器波形的采集服务：FastAPI + SQLite 采集网页，
附本地 SCPI/TCP 示波器模拟器。每次采集冻结捕获单通道 4096 个小端 16 位有符号样本，
读取采样间隔 / 电压比例 / 偏移，波形以 IEEE 488.2 二进制块传输，完成后可下载
电压-时序 CSV。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

* 网页：<http://localhost:8000> （选通道 → 开始采集 → 完成后下载 CSV）
* 模拟器 SCPI 端口：`localhost:5025`（可用 `nc` 或自写脚本直接发 SCPI 命令）

## 本地开发启动（不用 Docker）

```bash
pip install fastapi 'uvicorn[standard]'
python3 simulator/scpi_simulator.py &                 # 监听 5025
cd web && SCOPE_PORT=5025 python3 -m uvicorn app.main:app --port 8000
```

## 组成

```
simulator/scpi_simulator.py   SCPI/TCP 示波器模拟器（纯标准库）
web/app/main.py               FastAPI 路由（采集/取消/状态/CSV/探头校验分析）
web/app/manager.py            采集队列 worker：仪器串行访问、取消语义
web/app/instrument.py         仪器客户端：单轮单连接、5s 超时、IEEE488.2 块解析
web/app/probe.py              探头校验区间分析（半平面可行域，纯标准库、可单测）
web/app/db.py                 SQLite 持久化
web/app/static/index.html     操作网页（中文，含探头校验分析工具）
tests/selftest.py             端到端自测（启动两端真实进程）
tests/test_probe.py           探头分析纯算法单测（无需服务器/第三方包）
docker-compose.yml            一键启动两端
```

## 关键设计（对应需求）

* **参数与样本同次捕获**：每轮采集先 `:STOP` 冻结，再依次读取
  `:WAVeform:XINcr?/YMUlt?/YOFf?/POINts?` 与 `:WAVeform:DATA?`（同一连接内完成），
  参数与样本用**一条 SQL UPDATE** 原子写入，绝不同次错位。
* **整块收齐才供下载**：样本 BLOB 只在完整收齐 8192 字节后落库；
  未完成时 `GET /api/acquisitions/{id}/csv` 返回 409。
* **同仪器串行**：单个 asyncio worker 消费队列，仪器会话严格串行。
* **排队取消不发命令**：排队中的任务取消只改状态，worker 出队时跳过
  （自测用模拟器侧连接数 / DATA? 计数验证：被取消任务 0 命令）。
* **采集中取消**：取消请求立即受理（状态 `cancelling`），中止当前通信轮并关闭
  本轮连接后，才上报 `cancelled`。
* **单轮 5 秒超时**：每个 SCPI 命令-应答往返（含整个二进制块传输）限时 5 秒
  （`SCOPE_TIMEOUT` 可配）。
* **迟到字节不串单 / 旧收尾不误关新连接**：每轮采集使用**全新 TCP 连接**，
  结束（成功/超时/取消/断连）即关闭丢弃；迟到字节只会进入已死 socket；
  清理只触碰本轮自己的 writer，结构上不可能误关新连接。后续采集无需重启。
* **慢响应不挡网页**：仪器 I/O 全部在后台 worker，HTTP 接口只读写 SQLite。
* **二进制安全**：样本含 `0x0A/0x0D` 字节，客户端严格按块长度读取，绝不按行解析。

## 模拟器故障注入（SCPI 扩展命令，运行时生效，无需重启）

```
:SIM:WAVE SINE|SQUARE|RAMP|NOISE   切换波形
:SIM:SEG <字节>                    波形分段的段大小
:SIM:SEGD <秒>                     段间延迟（分段发送）
:SIM:TAILD <秒>                    末段额外延迟（延迟尾部 -> 触发超时）
:SIM:DROP <字节>                   发送 N 字节块数据后断连（中途断连，0=关）
:SIM:RESPD <秒>                    每次应答延迟（慢响应）
:SIM:RESET                         恢复环境变量默认值
:SIM:STATS? / :SIM:CONFIG?         统计 / 当前配置
```

也可用环境变量预设（见 `docker-compose.yml` 注释）。

## 探头校验区间分析（波形页工具）

探头校验存在误差时，用本工具估计待测电压可能落入的范围。在波形页
「探头校验区间分析」卡片中：

1. 选择同一参考脉冲的**标准记录**与**探头校验记录**（二者采样间隔须相同），
   再选择同一探头、同一通道的**待测记录**；三条记录都必须是已完成状态。
2. 分别填写标准/校验记录的起始样本号（从 0 开始），按顺序配对 2～64 点。
3. 三条记录各自沿用自身原标定（`raw × volt_scale + volt_offset`）换算电压。
4. 输入正增益闭区间 `[g_min, g_max]`（端点均 > 0）、零偏闭区间
   `[b_min, b_max]` 与非负电压容差 `tol`。整段待测波形共用一组校正参数
   `v校正 = g × 探头读数 + b`，且每个配对点都须满足
   `|g·p_i + b − s_i| ≤ tol`。
5. 可行参数集是 (g, b) 平面上由半平面约束交出的凸多边形（`web/app/probe.py`，
   枚举约束边界两两交点求顶点）。对每个待测样本，线性式 `g·v + b` 在凸多边形
   上的最小/最大值在顶点取得，据此给出校正电压下界与上界；仅有一组可行参数
   （可行域退化为点）时上下界相等，照常出结果。
6. 待测样本的探头读数若超出**配对段校验记录实际覆盖的探头电压范围**
   `[min p_i, max p_i]`，标为 `out_of_range`（超范围），上下界留空；
   边界点视为在范围内。
7. 约束互相冲突（无可行参数）时接口返回 `feasible=false` 与冲突说明，
   网页清空旧结果并提示，CSV 接口返回 409。
8. 结果可下载 CSV，列：`sample_index,time_s,voltage_min_v,voltage_max_v,status`；
   样本时间用待测记录自身的采样间隔计算。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 操作网页 |
| GET | `/api/health` | 仪器地址与超时配置 |
| POST | `/api/acquisitions` `{channel: 1\|2}` | 提交采集（排队） |
| GET | `/api/acquisitions` | 最近采集列表 |
| GET | `/api/acquisitions/{id}` | 单个采集状态 |
| POST | `/api/acquisitions/{id}/cancel` | 取消（排队/采集中均可） |
| GET | `/api/acquisitions/{id}/csv` | 下载电压-时序 CSV（仅完成后） |
| POST | `/api/probe-analysis` | 探头校验区间分析（JSON，冲突时 200 + `feasible:false`） |
| POST | `/api/probe-analysis/csv` | 同一分析的 CSV 下载（冲突时 409） |

`/api/probe-analysis` 请求体：

```json
{
  "standard_id": 1, "calibration_id": 2, "test_id": 3,
  "standard_start": 0, "calibration_start": 0, "pair_count": 8,
  "gain_min": 0.9, "gain_max": 1.1,
  "offset_min": -0.5, "offset_max": 0.5, "tolerance": 0.05
}
```

校验规则（违例返回 400/404/409/422）：三条记录须存在且 `done`、互不相同；
校验与待测记录通道一致；标准与校验记录采样间隔一致；起始号 + 配对点数不得
超出记录长度；增益端点为正、区间端点有序、容差非负、配对点数 2～64。

状态机：`queued → running → done`，取消路径 `queued → cancelled`、
`running → cancelling → cancelled`，失败为 `error`（含错误信息）。

## 自测

```bash
python3 tests/selftest.py     # 端到端：模拟器 + Web 真实进程（含探头分析 API）
python3 tests/test_probe.py   # 探头分析纯算法单测（无需启动服务、无第三方依赖）
```

端到端脚本拉起真实模拟器与 Web 进程，覆盖：正常采集与 CSV 数值校验（含每通道参数）、
样本含换行字节、排队取消零命令、采集中取消的受理/上报时序、5 秒超时、
中途断连后恢复、断连后新采集拿到当前波形（无陈旧字节）、慢响应下网页 API 延迟，
以及探头校验分析的可行包络、单点可行域、约束冲突、CSV 内容与全部参数校验错误。

## 已验证 / 未完成

见交付说明（对话总结）与 `tests/selftest.py` 输出；Docker 镜像构建在本环境
未执行（无 docker 守护进程），Compose 配置已做静态校验。
