def test_package_imports() -> None:
    import agent  # noqa: F401
    import core  # noqa: F401
    import storage  # noqa: F401
    import tools  # noqa: F401

    assert True
