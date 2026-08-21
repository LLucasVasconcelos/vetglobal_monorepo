import bcrypt

# JWT encode/decode lands here in the auth step. For now this module only holds
# password hashing, which the seed script needs.


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())
