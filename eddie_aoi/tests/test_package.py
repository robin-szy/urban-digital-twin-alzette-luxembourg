"""The package imports and its geospatial dependencies are present."""


def test_package_imports():
    import eddie_aoi

    assert eddie_aoi.__doc__


def test_geospatial_dependencies_available():
    # The module is useless without these; fail here rather than deep in a task.
    import geopandas
    import pyogrio  # noqa: F401
    import shapely

    assert geopandas.__version__
    assert shapely.__version__
