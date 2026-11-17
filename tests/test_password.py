from app.security.password import hash_password, verify_password


def test_password_hashing():
    password = "TestPassword123!"

    hashed = hash_password(password)

    assert hashed != password
    assert hashed.startswith("$argon2id$")
    assert verify_password(password, hashed)
    assert not verify_password("WrongPassword", hashed)


def test_same_password_produces_different_hashes():
    password = "TestPassword123!"

    assert hash_password(password) != hash_password(password)
    