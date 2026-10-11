def render_receipt(items):
    lines = [f"{item['name']}: {item['cents']}" for item in items]
    lines.append(f"Total: {sum(item['cents'] for item in items)}")
    return "\n".join(lines)
