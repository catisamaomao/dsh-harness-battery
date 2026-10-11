# 活动物资记录

`supplies.csv` 是本次清点最终签收的逐项记录，quantity 为物品数量。

汇总保存为 `out/summary.json`，字段为：

- `by_category`：以原 category 为键、数量合计为值的对象。
- `total_quantity`：全部物品数量合计。
- `row_count`：不含表头的数据行数。

保留原始资料，JSON 对象内的键顺序不限。
