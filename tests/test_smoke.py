import ar_pipeline
import stub_backend


def test_packages_import():
    assert ar_pipeline is not None
    assert stub_backend is not None
