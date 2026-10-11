# 收据示例

`receipt.render_receipt(items)` 接收 `name`、`cents` 字典组成的列表，返回文本。
运行检查：`python3 -m unittest discover -s tests -v`。

例如一项 `{"name":"Tea","cents":300}` 输出 `Tea: 300\nTotal: 300`。
此项目不连接打印机或其他外部服务。
