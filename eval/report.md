# 评测报告 baseline-v1

- 模型：deepseek-chat
- 题数：20
- **执行准确率（严格）：90.0%**（18/20）
- **执行准确率（宽松，容忍多给列）：95.0%**（19/20）
- 多给列：1　答错：1　出错：0
- 总耗时：25.0s

| 题号 | 类型 | 结果 | 耗时 | 问题 |
|:--|:--|:--|--:|:--|
| q01 | 单值 | PASS | 1.4s | 张三的4月病假扣款是多少钱？ |
| q02 | 单值 | PASS | 1.72s | 刘一4月的实发工资是多少？ |
| q03 | 单值 | PASS | 1.09s | 陈二4月的基本工资是多少？ |
| q04 | 聚合 | PASS | 1.23s | 8个人4月的病假扣款合计是多少？ |
| q05 | 聚合 | PASS | 1.22s | 4月所有人的实发工资加起来是多少？ |
| q06 | 聚合 | PASS | 1.11s | 4月的平均基本工资是多少？ |
| q07 | 排序 | PASS | 1.31s | 谁4月病假扣款最多？叫什么名字？ |
| q08 | 排序 | PASS | 1.17s | 扣款最多的前三名是谁？ |
| q09 | 排序 | PASS | 1.25s | 按扣款从低到高列出所有人的姓名 |
| q10 | 过滤 | PASS | 1.28s | 4月实发工资低于5000的有哪些人？ |
| q11 | 过滤 | PASS | 1.16s | 4月病假天数超过20天的有谁？ |
| q12 | 过滤 | PASS | 0.85s | 4月累计病假超过50天的员工有哪几个？ |
| q13 | 考勤 | FAIL | 1.13s | 1月份的应出勤小时数是多少？ |
| q14 | 考勤 | PASS | 1.19s | 3月份每个人应出勤多少小时？ |
| q15 | 多表 | PASS | 1.39s | 工号10000005是谁？他4月扣了多少钱？ |
| q16 | 参数 | PASS | 1.11s | 最低工资标准是多少？ |
| q17 | 参数 | PASS_LOOSE | 1.35s | 病假扣款用了哪几档比例？ |
| q18 | 多表 | PASS | 1.18s | 把每个人的姓名和4月病假天数列出来 |
| q19 | 排序 | PASS | 1.18s | 4月扣款最少的是谁？扣了多少？ |
| q20 | 边界 | PASS | 1.6s | 4月有没有人扣款是0？ |

## 未通过明细
### q13 1月份的应出勤小时数是多少？（FAIL）
- 模型 SQL：`SELECT schedule_hours FROM attendance WHERE period='2026-01'`
- gold：`SELECT DISTINCT schedule_hours FROM attendance WHERE period='2026-01'`
- gold 结果：[[168.0]]
- 模型结果：[[168.0], [168.0], [168.0], [168.0], [168.0], [168.0], [168.0], [168.0]]

### q17 病假扣款用了哪几档比例？（PASS_LOOSE）
- 模型 SQL：`SELECT name, value, unit, note FROM policy_params WHERE name IN ('rate_low','rate_high')`
- gold：`SELECT name, value FROM policy_params WHERE name IN ('rate_low','rate_high') ORDER BY name`
- gold 结果：[['rate_high', 0.5], ['rate_low', 0.25]]
- 模型结果：[['rate_high', 0.5, '比例', '超长病假扣款比例'], ['rate_low', 0.25, '比例', '前段病假扣款比例']]
- 备注：多给列：gold 2 列 / got 4 列
