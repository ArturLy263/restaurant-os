from tests.factories import auth_headers, create_restaurant


def test_create_order_occupies_table(db_client, db_session, restaurant):
    response = db_client.post(
        "/api/v1/orders",
        json={
            "table_id": str(restaurant.table.id),
            "guest_count": 2,
        },
        headers=auth_headers(restaurant.user),
    )

    assert response.status_code == 201
    assert response.json()["status"] == "OPEN"
    assert response.json()["restaurant_id"] == str(restaurant.restaurant.id)

    db_session.refresh(restaurant.table)
    assert restaurant.table.status == "OCCUPIED"


def test_order_is_not_visible_to_another_restaurant(
    db_client,
    db_session,
    restaurant,
):
    other = create_restaurant(db_session, name="Other Restaurant")

    created = db_client.post(
        "/api/v1/orders",
        json={
            "table_id": str(restaurant.table.id),
            "guest_count": 2,
        },
        headers=auth_headers(restaurant.user),
    )
    assert created.status_code == 201

    response = db_client.get(
        f"/api/v1/orders/{created.json()['id']}",
        headers=auth_headers(other.user),
    )

    assert response.status_code == 404
