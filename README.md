# 泥咕咕技艺谱系服务

为浚县泥咕咕非遗传承提供的谱系服务：维护**造型、版本（工序/材料/照片摘要）、师承关系、学徒阶段、禁用图样与公开许可**，支持师傅提交版本、传承人确认与复核。

## 运行

```bash
python3 -m service.main          # 或安装后运行 `service`
# 环境变量：PORT（默认 8000）、LINEAGE_DB（事件日志路径，默认 ./data/lineage.jsonl）
python3 -m unittest discover -s tests -v   # 测试
```

## 设计原则

- **历史不可覆盖**：一切变更先写入追加式事件日志（`service/store.py`），再更新内存投影。工艺分支、禁用图样、学徒晋级、旧版纠正都只增不改；旧版纠正以新版本引用 `corrects_version_id`，旧版原样保留。
- **版本流程**：师傅提交带照片摘要与工序的版本（`pending`）→ 本作坊传承人确认（`confirmed`）。只有已确认版本可被引用、授权公开。
- **工艺分支**：两人基于同一版本并行修改时自动分叉（`main` / `fork-N`），各自独立确认，互不覆盖。
- **引用规范**：引用谱系必须同时标明**版本**与**适用作坊**（同名造型可分属不同作坊）；`GET /versions/{id}/chain` 给出完整引用链。
- **许可撤回语义**：撤回公开许可只影响今后的公开展示；授权与撤回两条最小事实保留在审计日志（仅传承人可查）。
- **幂等**：所有写操作接受 `Idempotency-Key` 头（或同名字段），重复提交返回首次结果；`POST /sync` 离线同步按批次 + 单操作两级幂等回放。
- **权限可解释**：`GET /permissions/explain?version_id=..&person_id=..` 返回允许/拒绝及全部理由（角色、学徒阶段、许可状态、禁用图样）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/workshops` `/persons` `/lineage` `/shapes` | 登记作坊 / 人员（师傅·学徒·传承人）/ 师承关系 / 造型 |
| POST | `/shapes/{id}/versions` | 提交版本（照片摘要、工序、材料、基线、纠正对象） |
| POST | `/versions/{id}/confirm` | 传承人确认版本 |
| GET | `/versions/{id}` `/versions/{id}/chain` | 版本详情 / 引用链 |
| GET | `/public/versions/{id}` | 公开展示视图（无有效许可则 403） |
| GET | `/permissions/explain` | 权限解释 |
| POST | `/patterns/forbid` | 禁用图样（传承人） |
| POST | `/persons/{id}/promote` | 学徒晋级；跨阶段须传承人批准并说明理由 |
| POST | `/licenses` `/licenses/{id}/revoke` | 授予 / 撤回公开许可 |
| POST | `/citations` | 记录谱系引用（版本 + 作坊） |
| GET | `/audit` | 审计日志（须 `X-Person-Id` 为传承人） |
| POST | `/sync` | 离线同步批次 `{device_id, batch_id, ops[]}` |

## 目录

- `service/domain.py` — 领域常量、记录类型、业务错误
- `service/store.py` — 追加式事件日志（JSONL）与内存存储
- `service/lineage.py` — 核心服务：事件、投影、全部业务规则
- `service/api.py` — HTTP 路由、错误映射、幂等键透传
- `tests/test_lineage.py` — 复核场景：并行修改分叉、跨阶段晋级、许可撤回、幂等与持久化
- `tests/test_api.py` — HTTP 全流程测试
