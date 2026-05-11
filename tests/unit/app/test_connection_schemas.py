import pytest
from pydantic import ValidationError

from tvbf.app.schemas import ConnectionRequestCreate


def test_connection_request_create_accepts_valid_uuid():
    body = ConnectionRequestCreate.model_validate(
        {"addressee_id": "00000000-0000-0000-0000-000000000001"}
    )
    assert str(body.addressee_id) == "00000000-0000-0000-0000-000000000001"


def test_connection_request_create_rejects_non_uuid():
    with pytest.raises(ValidationError):
        ConnectionRequestCreate.model_validate({"addressee_id": "not-a-uuid"})
