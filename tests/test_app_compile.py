import py_compile


def test_app_py_compiles():
    py_compile.compile('app.py', doraise=True)
