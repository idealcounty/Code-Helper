from greeting import greet


def test_greet_uses_product_format() -> None:
    assert greet("Ada") == "Hello, Ada!"
