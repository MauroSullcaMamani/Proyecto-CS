from inventory import low_stock, total_stock

ITEMS = [("tornillo", 40), ("tuerca", 5), ("arandela", 12)]


def test_total_stock():
    assert total_stock(ITEMS) == 57


def test_total_stock_empty():
    assert total_stock([]) == 0


def test_low_stock():
    assert low_stock(ITEMS, 10) == ["tuerca"]


def test_low_stock_none():
    assert low_stock(ITEMS, 1) == []
