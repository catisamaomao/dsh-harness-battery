"""月度导出模块，与收据页面分别维护。"""
def sum_sales(rows):
    total = 0
    for row in rows:
        total += row["cents"]
    return total

def sum_refunds(rows):
    total = 0
    for row in rows:
        total += row["cents"]
    return total
