# 冷链热暴露裁决服务

根据运输箱**不等间隔**的温度读数，裁决样本是否经历不可接受的热暴露。

- 服务按**绝对时刻**对读数排序（录入顺序不影响结论）。
- 相邻读数间隔未超过最大采样间隔时，按温度**线性变化**裁决，并**精确求解阈值交点**（全程 Decimal 运算，`8.10` 与 `8.1` 等不同十进制表示不影响结论）。
- 仅累计**严格高于**阈值的连续区间与度分钟；温度**等于**阈值不计暴露。
- 相邻读数间隔超过最大采样间隔时形成**覆盖缺口**，缺口两侧不做插值，超温区间不得跨越缺口。
- 任一覆盖缺口、单次超温时长超限或总度分钟超预算，裁决均为 `fail` 并给出对应原因。

零第三方依赖，仅使用 Python 3.11 标准库。

## 接口

`POST /api/cold-chain/exposure`

请求字段：

| 字段 | 说明 |
| --- | --- |
| `start_at` / `end_at` | 运输起止时刻，RFC 3339（必须带时区） |
| `temperature_threshold` | 温度阈值（数值） |
| `max_interval_seconds` | 最大采样间隔（秒，>0） |
| `max_exposure_seconds` | 单次超温时长上限（秒，≥0） |
| `degree_minutes_budget` | 度分钟预算（≥0） |
| `readings` | 2–500 条读数，`timestamp` 时刻唯一；排序后首条须恰为 `start_at`、末条须恰为 `end_at` |

成功响应（200）：

- `verdict`：`"pass"` / `"fail"`（另有布尔字段 `pass`）
- `exposures`：按时间排列的超温区间，每项含 `start`、`end`、`duration_seconds`、`degree_minutes`
- `total_degree_minutes`：总度分钟
- `coverage_gaps`：覆盖缺口（`start`、`end`、`duration_seconds`）
- `reasons`：失败原因，`code` 取值 `coverage_gap` / `single_exposure_exceeded` / `degree_minutes_budget_exceeded`

非法时刻、重复时刻、非有限数值（NaN/Infinity）、读数数量越界、首末读数不在边界等均返回 **422**，错误体 `errors[].loc` 可定位到具体字段，例如 `readings[3].temperature`。

健康检查：`GET /health` → `200 {"status":"ok"}`

### 示例

```bash
curl -s http://localhost:8000/api/cold-chain/exposure \
  -H 'Content-Type: application/json' \
  -d '{
    "start_at": "2026-10-05T08:00:00Z",
    "end_at": "2026-10-05T08:25:00Z",
    "temperature_threshold": 8,
    "max_interval_seconds": 600,
    "max_exposure_seconds": 60,
    "degree_minutes_budget": 100,
    "readings": [
      {"timestamp": "2026-10-05T08:00:00Z", "temperature": 10},
      {"timestamp": "2026-10-05T08:01:00Z", "temperature": 10},
      {"timestamp": "2026-10-05T08:02:00Z", "temperature": 6},
      {"timestamp": "2026-10-05T08:20:00Z", "temperature": 10},
      {"timestamp": "2026-10-05T08:25:00Z", "temperature": 6}
    ]
  }'
```

08:02→08:20 间隔 1080 秒 > 600 秒形成缺口；两侧线性段在 08:01:30、08:22:30 与阈值 8 ℃ 精确相交。

## 运行（Docker Compose）

```bash
# 启动 API；宿主机端口可通过 HOST_PORT 配置（默认 8000）
docker compose up --build
HOST_PORT=18080 docker compose up --build
```

Compose 内置健康检查；`verify` 一次性服务会等待 API 健康就绪后再执行：

```bash
# 运行代码测试 → 应用构建 → 提交同时含阈值交点与覆盖缺口的业务冒烟请求
# 全部成功退出码为 0，任一失败为非 0
docker compose run --build verify
```

## 本地运行（无需 Docker）

```bash
python3 -m app.main                     # 启动服务，PORT 环境变量可改端口
python3 -m unittest discover -s tests   # 37 个单元/接口测试
API_BASE=http://127.0.0.1:8000 python3 smoke.py
```

## 目录

```
app/exposure.py    # 校验、精确交点、缺口、度分钟与裁决核心
app/main.py        # 标准库 HTTP 服务（POST /api/cold-chain/exposure, GET /health）
tests/             # 核心算法与 HTTP 端到端测试
smoke.py           # 阈值交点 + 覆盖缺口业务冒烟请求
verify.sh          # 测试 → 构建 → 冒烟 的一次性校验流水线
Dockerfile
docker-compose.yml
```
