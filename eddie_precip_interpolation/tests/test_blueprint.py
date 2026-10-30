from eddie_precip_interpolation import blueprint


def test_blueprint_name_matches_package():
    assert blueprint.blueprint.name == "eddie_precip_interpolation"
