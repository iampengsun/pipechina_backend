from app.shared.media.names import safe_object_filename


def test_storage_filename_is_reduced_to_safe_leaf() -> None:
    assert safe_object_filename("../../站场/valve image.jpg") == "valve_image.jpg"
    assert safe_object_filename(r"..\..\secret.txt") == "secret.txt"
    assert safe_object_filename("..") == "upload.bin"
