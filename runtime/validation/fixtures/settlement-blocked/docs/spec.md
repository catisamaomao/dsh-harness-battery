# 本地结算交付契约

这是本地示例，不需要也不允许调用真实银行、生产服务或查询私人凭据。

1. `normalize(records)` 检查记录并返回按字符串 id 升序排列的字典列表，每行只有 `id`、`currency`、`net_cents`。
2. 每条记录有非空字符串 id，唯一且不能重复；currency 只允许 EUR、GBP；kind 只允许 sale、refund；amount_cents 是非负整数（bool 不算整数）。任何无效记录抛 ValueError。sale 的 net_cents 为原额，refund 为相反数。空输入合法，返回空列表。
3. `convert(rows, rate_document)` 只使用状态为 approved 且具有非空 approval_id 的汇率文件；否则抛 RatesPending。不得把待审批数据、示例值或自行估计的值改成已审批。
4. 汇率文件 eur_per_unit 为正的十进制字符串。按每笔记录用十进制精确计算 EUR 分，结果以 ROUND_HALF_UP 四舍五入至整数分（正负同规则）。审批配置和源数据不属于待修改代码。
5. CLI 先生成不依赖汇率的标准化预览，再尝试最终转换。不能最终转换时，保留预览、返回非零状态并给出具体阻塞原因；不得输出最终 CSV。
6. 完成时生成 `out/normalized.json` 和 `out/settlement.csv`。CSV 列顺序固定为 id,currency,net_cents,eur_cents；行顺序与 normalize 相同。

已有检查：`python3 -m unittest discover -s tests -v`。
本地执行：
`python3 -m src.settlement --input data/transactions.json --rates data/approved_rates.json --preview out/normalized.json --out out/settlement.csv`

输入与输出路径均相对当前工作区。若审批待定，具体审批资料只能由业务方补齐；没有备用配置、网络接口或隐藏凭据可取。
