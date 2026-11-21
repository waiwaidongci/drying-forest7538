# gb-0044 新工况覆盖检查 · 测试说明

## 运行

```bash
cd backend
python3 ../test-artifacts/gb-0044/oracle_coverage.py         # 独立精确求解器对照（仅 stdlib）
python3 ../test-artifacts/gb-0044/test_coverage_http.py      # HTTP 接口与门禁（需 fastapi/httpx）
python3 ../test-artifacts/gb-0044/test_coverage_recovery.py  # 并发与中断恢复
```

三个脚本都在系统临时目录新建 SQLite，不触碰仓库或 Compose 数据卷，结束后可删除临时目录。

## oracle_coverage.py — 独立算法 oracle

不导入被测判定结果，只用 `fractions.Fraction` 高斯消元穷举至多 5 件训练试件，判断待判点是否为
凸组合（与“边界算覆盖”一致）。随机 600 例，混合内部整数组合、完全随机点、尺度 1/10/100/1000。
对每例核对被测求解器：

- 覆盖：权重非负、件数 ≤ 5、和恰为 1、加权逐坐标等于待判点；
- 分离：全部训练点 `c·x ≤ C`，待判点 `c·x > C`（严格超出）。

## test_coverage_http.py — 接口、权限、门禁、幂等

真 FastAPI + TestClient + 临时 SQLite，60 条样本、一条已完成与一条未完成任务。

1. 受理返回 `queued`；整批核对齐前 `viewable=false` 且所有 `proof` 为 `null`。
2. 同 `request_key` 同内容重放返回同一批次；同编号不同内容 409。
3. 访客写 403、匿名 401、未完成研究 409、含 `minutes` 等未知字段 422。
4. 模拟“单条证明已落库、进度未更新”的中断，新 worker 恢复后 `verified`。
5. 原样训练点得 `1/1` 权重；超界点得整数线性界限，用 `Decimal` 独立核对两侧方向。
6. 再次 `process_batch` 仍 `verified`，条目数不变。

## test_coverage_recovery.py — 并发与恢复

1. 部分证明已落库后重跑补齐并 `verified`。
2. 两线程并发处理同一批次：最终状态 `verified`、条目数不变、无重复入账（尝试次数均为 1）。
3. 新近占用不会被别人的陈旧回收抢走；租约过期后回收，旧令牌的迟到写入行数为 0。
4. `recover_running_items(owner)` 只回收本 worker 的遗留占用。
5. 证明被篡改、或受理后依据摘要变化时，整批 `failed`、不再可查看（fail-closed）。

## 规模与性能（手工）

100k 训练试件端到端：冻结快照 100k 条记录、受理 2 条工况，单条精确判定约 0.3–0.7 秒，
整批（含建库与索引）约 4 秒；不枚举组合，判定时间不随试件数组合爆炸。

## 前端

原生 ES Modules，无打包。`pages/coverage.js` 负责批次台账、逐条证明与“整批核对齐才展示”的门禁；
`api/research.js` 的 `coverageApi` 调 `/runs/{id}/coverage`。浏览器经 Nginx 进入“完整结果”，
在“新工况覆盖检查”逐行录入 `名称, 湿度, 厚度, 温度, 风量`，提交后等待整批核对完成。
录入过程中页面自动刷新会保留草稿、请求编号与光标位置。
