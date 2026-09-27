# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、维护封锁与泊位重排。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：演示页面（维护封锁、候泊队列、重排结果）。
- `tests/`：完整流程、规则计算、维护调度和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 维护封锁与泊位重排

维修班登记维护单（泊位、起止时刻、作业内容），确认后锁定该泊位时间窗：

- 维护单状态机：`draft → locked → completed / cancelled`，`locked`状态下可`extend`（延期）。
- 确认封锁时，撞上窗口的`draft`/`confirmed`航次按候泊顺序（原eta升序）自动重排到维护结束后的最近可用窗口；已靠泊（`berthed`）航次作为固定障碍留档不动，已离泊（`departed`）和已取消航次不参与。
- 封锁生效期间，同泊位新建或确认航次若撞上封锁窗口会被拒绝（409）。
- 延期（`extend`）必须填写原因，系统按新结束时刻重新排候泊船，原因写入维护单与被重排航次的审计时间线。
- 提前完工（`complete`）或取消（`cancel`）即释放窗口，已重排的航次保持新窗口不再回移。
- 重排允许跨午夜顺延，调度时界为48小时；时界内排不下时整个操作失败并回滚。
- 重排结果（`last_result`）保存在维护单上，被重排航次记录原窗口与`displaced_by`来源，便于页面展示封锁原因、候泊顺序和重排结果。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/maintenance`：维护单列表，可带`state`、`berth`和`limit`参数。
- `POST /api/maintenance`：登记维护单，请求体为`{"reference":"...","data":{"berth":"B12","start_hour":8,"end_hour":14,"work_content":"吊机检修"}}`。
- `GET /api/maintenance/{id}`：维护单详情，含最近重排结果。
- `GET /api/maintenance/{id}/audit`：维护单事件时间线。
- `POST /api/maintenance/{id}/actions/{action}`：维护动作，`confirm`/`extend`/`complete`/`cancel`，请求体为`{"expected_version":1,"data":{...}}`；`extend`需`{"new_end_hour":16,"reason":"..."}`。
- `GET /api/berths/{berth}/queue`：泊位候泊视图，含生效封锁（原因）、候泊顺序和各航次重排来源。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。维护单相关角色为`maintenance_planner`与`port_controller`，航次计划角色为`port_controller`，`admin`拥有全部权限。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、维护封锁与重排、延期重排队列、窗口释放、重复引用、权限拒绝和版本冲突。

