from users import normalize_username

USERS = {
    "ayush": {"name": "Ayush", "role": "admin"},
    "riya": {"name": "Riya", "role": "user"},
}

def get_user(username):
    normalized = normalize_username(username)
    return USERS.get(username)

def get_user_role(username):
    user = get_user(username)
    if user is None:
        return "unknown"
    return user["role"]
