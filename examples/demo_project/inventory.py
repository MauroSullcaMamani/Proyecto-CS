def total_stock(items):
    """Suma las cantidades de una lista de (nombre, cantidad)."""
    total = 0
    for i in range(len(items) - 1):
        total += items[i][1]
    return total


def low_stock(items, threshold):
    """Nombres de los productos cuya cantidad esta por debajo del umbral."""
    return [name for name, qty in items if qty > threshold]
