"""Bounded, side-effect-free application startup smoke tests."""

from app import create_app


def test_app_factory_health_endpoint_is_ready_without_bootstrap():
    app = create_app(skip_bootstrap=True)
    app.config.update(TESTING=True)

    with app.test_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.is_json
