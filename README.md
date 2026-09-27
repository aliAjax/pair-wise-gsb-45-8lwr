# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：靠泊计划（航次）列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（航次或维修单）。
- `GET /api/records/{id}/audit`：审计时间线（含自动重排的`rescheduled`事件）。
- `GET /api/stats`：状态统计（按`kind`分组）。
- `POST /api/records`：创建靠泊计划，请求体为`{"reference":"...","data":{...}}`。撞上限时维修窗口不报错，登记后自动排入候泊队列。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（航次：`confirm/berth/depart/cancel`；维修单：`confirm/extend/complete/cancel`），请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/maintenances`：维修班登记维护单，`data`含`berth`、`start_hour`、`end_hour`、`work`（作业内容即封锁原因）。需`maintenance_crew`或`admin`角色。
- `GET /api/maintenances`：维修单列表，可带`state`。
- `GET /api/board`：调度看板，返回`locks`（封锁泊位与原因）、`waitlist`（按原始到港排序的候泊顺序）、`reschedule_results`（最近重排结果），可带`berth`过滤。

### 维护单状态机

`draft`（登记）→ `confirmed`（确认即锁住`[start_hour, end_hour)`）→ `extended`（可多次延期）→ `completed`（完工归档）；草稿可`cancel`。

- 确认/延期：同泊位时间窗重叠的候泊航次（`draft`/`confirmed`）按 FCFS 重排到维护结束后的最近可用窗口，原始窗口保留在`original_eta_hour/original_etd_hour`。
- 延期（`extend`）必须提供`reason`与更晚的`new_end_hour`（可跨天，>24），原因写入`extend_history`并触发重排。
- 提前完工（`complete`带`actual_end_hour`）释放剩余窗口，候泊船回排到实际完工时刻；已实际占用时段仍视为硬占用。
- 已靠泊（`berthed`）、已离泊（`departed`）航次不参与重排，照旧留档；在泊船舶作为硬占用参与候泊计算。
- 冲突中的活动维修单不可再次确认封锁（409）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色：`port_controller`（调度）、`maintenance_crew`（维修班）、`admin`（全部）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
